"""The control loop and every fail-safe path.

The rule the whole module is built around: when in doubt, the fan runs at
full speed. Noise is an annoyance; a CPU or an SSD cooking in a closet is
damage. Concretely:

* A sensor that is missing, returns garbage or reads an implausible value is
  bridged with its last good reading for ``stale_after_s``. After that, every
  fan driven by it goes to 100 percent.
* A sensor whose value has not changed for ``frozen_after_s`` (opt-in) is
  treated as dead too: some drivers keep returning the last value forever.
* An exception anywhere in a control tick drives all fans to 100 percent.
* A watchdog thread drives all fans to 100 percent if the loop stops ticking.
* On exit the chip is handed back to its own automatic mode, the one it was
  in before we took it. That mode is also written to a state file so that
  ``fancurve restore`` can hand it back after a ``kill -9``.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Config, FanSpec
from .curve import Hysteresis, interpolate, pct_to_raw, raw_to_pct
from .hwmon import Hwmon, HwmonError, Reading

log = logging.getLogger("fancurve")

FULL_SPEED_RAW = 255
MANUAL_MODE = 1
HISTORY_S = 15 * 60


@dataclass
class SensorTrack:
    value: float | None = None
    status: str = "unknown"
    detail: str = ""
    last_good: float | None = None
    last_good_at: float | None = None
    last_raw: float | None = None
    last_change_at: float | None = None

    @property
    def usable(self) -> bool:
        return self.status in ("ok", "holding")


def assess(
    reading: Reading, track: SensorTrack, now: float, stale_after: float, frozen_after: float
) -> SensorTrack:
    """Fold one reading into the sensor's track. Pure apart from ``track``."""
    if reading.ok:
        v = reading.value
        if track.last_raw is None or v != track.last_raw or track.last_change_at is None:
            track.last_raw = v
            track.last_change_at = now
        track.last_good = v
        track.last_good_at = now
        if frozen_after > 0 and now - track.last_change_at >= frozen_after:
            track.status = "frozen"
            track.value = None
            track.detail = f"value stuck at {v:.1f} C for {now - track.last_change_at:.0f}s"
        else:
            track.status = "ok"
            track.value = v
            track.detail = ""
        return track

    age = None if track.last_good_at is None else now - track.last_good_at
    if age is not None and age <= stale_after and track.status in ("ok", "holding"):
        track.status = "holding"
        track.value = track.last_good
        track.detail = f"{reading.detail}; holding last good {track.last_good:.1f} C"
    else:
        track.status = reading.status
        track.value = None
        if age is None:
            track.detail = f"{reading.detail}; no valid reading yet"
        else:
            track.detail = f"{reading.detail}; no valid reading for {age:.0f}s"
    return track


@dataclass
class FanTrack:
    hysteresis: Hysteresis
    source: str
    mode: str = "starting"
    reason: str = ""
    temp: float | None = None
    effective: float | None = None
    target_pct: float | None = None
    raw: int | None = None
    rpm: int | None = None


class Controller:
    def __init__(
        self,
        config: Config,
        hwmon: Hwmon | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
    ) -> None:
        self._cfg = config
        self.hwmon = hwmon or Hwmon(config.sysfs_root)
        self.clock = clock
        self.wall = wall
        self.lock = threading.RLock()
        self.sensors: dict[str, SensorTrack] = {sid: SensorTrack() for sid in config.sensors}
        self.fans: dict[str, FanTrack] = {
            f.id: FanTrack(Hysteresis(f.hysteresis_c), f.source) for f in config.fans.values()
        }
        self.original_modes: dict[int, int] = {}
        self.taken = False
        self.released = False
        self.ticks = 0
        self.errors = 0
        self.last_error = ""
        self.last_tick_at: float | None = None
        self.started_at = clock()
        self.watchdog_tripped = False
        self.history: deque[dict[str, Any]] = deque(maxlen=int(HISTORY_S / config.interval_s) + 5)
        self.events: deque[dict[str, Any]] = deque(maxlen=100)
        self.crash_next_tick: str | None = None

    @property
    def config(self) -> Config:
        return self._cfg

    def set_config(self, new: Config) -> None:
        """Swap the config between ticks. Fan state survives unless its source changed."""
        with self.lock:
            for f in new.fans.values():
                track = self.fans.get(f.id)
                if track is None:
                    self.fans[f.id] = FanTrack(Hysteresis(f.hysteresis_c), f.source)
                    continue
                if track.source != f.source:
                    track.hysteresis = Hysteresis(f.hysteresis_c)
                    track.source = f.source
                else:
                    track.hysteresis.width = f.hysteresis_c
            for sid in new.sensors:
                self.sensors.setdefault(sid, SensorTrack())
            self._cfg = new

    def event(self, level: str, message: str) -> None:
        self.events.append({"t": self.wall(), "level": level, "message": message})
        getattr(log, "warning" if level == "warn" else level, log.info)(message)

    def take(self) -> bool:
        """Record the chip's current modes and switch our channels to manual.

        Returns False when the chip is not there yet; the loop retries.
        """
        cfg = self._cfg
        if self.hwmon.find(cfg.chip) is None:
            return False
        saved = self._read_state_file()
        for f in cfg.fans.values():
            mode = saved.get(f.channel)
            if mode is None:
                mode = self.hwmon.read_int_or_none(cfg.chip, f.enable_attr)
            if mode is None or mode == MANUAL_MODE:
                mode = cfg.auto_mode
            self.original_modes[f.channel] = mode
        self._write_state_file()
        self.taken = True
        self.released = False
        self.event(
            "info",
            f"took control of {cfg.chip}: "
            + ", ".join(f"pwm{c} (was mode {m})" for c, m in sorted(self.original_modes.items())),
        )
        return True

    def release(self) -> None:
        """Hand every channel back to automatic mode. Idempotent and never raises."""
        with self.lock:
            if not self.taken or self.released:
                return
            cfg = self._cfg
            restored = []
            for f in cfg.fans.values():
                mode = self.original_modes.get(f.channel, cfg.auto_mode)
                try:
                    self.hwmon.write_int(cfg.chip, f.enable_attr, mode)
                    restored.append(f"pwm{f.channel}={mode}")
                except HwmonError as exc:
                    self.event("error", f"restore of pwm{f.channel} failed ({exc}); forcing full")
                    self._write_full(f)
                self.fans[f.id].mode = "released"
            self.released = True
            self._remove_state_file()
            self.event("info", "released chip to automatic mode: " + ", ".join(restored))

    def tick(self) -> None:
        """One control step. Never raises: any failure becomes full speed."""
        with self.lock:
            if self.released:
                return
            try:
                if self.crash_next_tick:
                    msg, self.crash_next_tick = self.crash_next_tick, None
                    raise RuntimeError(msg)
                self._tick()
            except Exception as exc:  # noqa: BLE001 - this is the last line of defence
                self.errors += 1
                self.last_error = f"{type(exc).__name__}: {exc}"
                self.event("error", f"control tick failed ({self.last_error}); all fans to full")
                self.force_full_speed(f"controller error: {self.last_error}")
            else:
                if self.errors:
                    self.event("info", "control tick healthy again")
                self.errors = 0
                self.last_error = ""
            finally:
                self.ticks += 1
                self.last_tick_at = self.clock()
                if self.watchdog_tripped:
                    self.watchdog_tripped = False
                    self.event("info", "control loop is ticking again; watchdog cleared")
                self._record_history()

    def _tick(self) -> None:
        cfg = self._cfg
        now = self.clock()
        if not self.taken and not self.take():
            for fid in self.fans:
                self._set_mode(fid, "failsafe", f"chip {cfg.chip!r} not found")
            self._read_sensors(cfg, now)
            return

        self._read_sensors(cfg, now)
        for f in cfg.fans.values():
            track = self.fans[f.id]
            sensor = self.sensors[f.source]
            track.rpm = self.hwmon.read_int_or_none(cfg.chip, f.rpm_attr)
            if not sensor.usable:
                track.temp = None
                track.effective = None
                track.hysteresis.reset()
                self._set_mode(f.id, "failsafe", f"sensor {f.source}: {sensor.status}")
                self._apply(f, track, 100.0)
                continue
            track.temp = sensor.value
            track.effective = track.hysteresis.update(sensor.value)
            pct = max(f.min_pwm, interpolate(f.curve, track.effective))
            self._set_mode(f.id, "curve", "")
            self._apply(f, track, pct)

    def _read_sensors(self, cfg: Config, now: float) -> None:
        fs = cfg.failsafe
        for sid, spec in cfg.sensors.items():
            reading = self.hwmon.read_temp(spec.chip, spec.input, fs.min_valid_c, fs.max_valid_c)
            track = self.sensors[sid]
            before = track.status
            assess(reading, track, now, fs.stale_after_s, fs.frozen_after_s)
            if track.status != before and not (before == "unknown" and track.status == "ok"):
                level = "info" if track.status == "ok" else "warn"
                text = f"sensor {sid}: {before} -> {track.status}"
                if track.detail:
                    text += f" ({track.detail})"
                self.event(level, text)

    def _set_mode(self, fan_id: str, mode: str, reason: str) -> None:
        track = self.fans[fan_id]
        if track.mode != mode:
            if mode == "failsafe":
                self.event("warn", f"fan {fan_id}: FAIL-SAFE full speed ({reason})")
            elif track.mode == "failsafe":
                self.event("info", f"fan {fan_id}: back on its curve")
        track.mode = mode
        track.reason = reason

    def _apply(self, f: FanSpec, track: FanTrack, pct: float) -> None:
        raw = FULL_SPEED_RAW if pct >= 100 else pct_to_raw(pct)
        cfg = self._cfg
        if self.hwmon.read_int_or_none(cfg.chip, f.enable_attr) != MANUAL_MODE:
            self.hwmon.write_int(cfg.chip, f.enable_attr, MANUAL_MODE)
        self.hwmon.write_int(cfg.chip, f.pwm_attr, raw)
        track.target_pct = round(pct, 1)
        track.raw = raw

    def _write_full(self, f: FanSpec) -> bool:
        cfg = self._cfg
        try:
            self.hwmon.write_int(cfg.chip, f.enable_attr, MANUAL_MODE)
            self.hwmon.write_int(cfg.chip, f.pwm_attr, FULL_SPEED_RAW)
            return True
        except HwmonError as exc:
            log.error("could not force pwm%d to full speed: %s", f.channel, exc)
            return False

    def force_full_speed(self, reason: str) -> None:
        """Best effort, per channel: one failing channel does not stop the others."""
        with self.lock:
            for f in self._cfg.fans.values():
                track = self.fans[f.id]
                self._set_mode(f.id, "failsafe", reason)
                if self._write_full(f):
                    track.target_pct = 100.0
                    track.raw = FULL_SPEED_RAW

    def watchdog_check(self) -> bool:
        """Called from the watchdog thread. True when it had to intervene."""
        limit = self._cfg.failsafe.watchdog_s
        last = self.last_tick_at if self.last_tick_at is not None else self.started_at
        if self.clock() - last <= limit or self.released:
            return False
        if not self.watchdog_tripped:
            self.watchdog_tripped = True
            self.event("error", f"control loop silent for over {limit:g}s; watchdog forcing full")
        for f in self._cfg.fans.values():
            self._write_full(f)
            track = self.fans[f.id]
            track.mode, track.reason = "failsafe", "watchdog: control loop stalled"
        return True

    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            started = self.clock()
            self.tick()
            elapsed = self.clock() - started
            stop.wait(max(0.05, self._cfg.interval_s - elapsed))

    def run_watchdog(self, stop: threading.Event) -> None:
        while not stop.wait(1.0):
            try:
                self.watchdog_check()
            except Exception:  # noqa: BLE001
                log.exception("watchdog check failed")

    def _read_state_file(self) -> dict[int, int]:
        path = self._cfg.state_file
        if not path:
            return {}
        try:
            data = json.loads(Path(path).read_text())
            if data.get("chip") != self._cfg.chip:
                return {}
            return {int(k): int(v) for k, v in data.get("modes", {}).items()}
        except (OSError, ValueError, AttributeError):
            return {}

    def _write_state_file(self) -> None:
        path = self._cfg.state_file
        if not path:
            return
        p = Path(path)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(p.suffix + ".tmp")
            tmp.write_text(json.dumps({"chip": self._cfg.chip, "modes": self.original_modes}))
            os.replace(tmp, p)
        except OSError as exc:
            self.event("warn", f"could not write state file {p}: {exc}")

    def _remove_state_file(self) -> None:
        if self._cfg.state_file:
            with contextlib.suppress(OSError):
                Path(self._cfg.state_file).unlink()

    def _record_history(self) -> None:
        cfg = self._cfg
        self.history.append(
            {
                "t": round(self.wall(), 1),
                "temps": {
                    sid: round(t.value, 2) if t.usable else None
                    for sid, t in self.sensors.items()
                    if sid in cfg.sensors
                },
                "pwm": {fid: t.target_pct for fid, t in self.fans.items() if fid in cfg.fans},
                "rpm": {fid: t.rpm for fid, t in self.fans.items() if fid in cfg.fans},
                "failsafe": [fid for fid, t in self.fans.items() if t.mode == "failsafe"],
            }
        )

    def overall_state(self) -> str:
        if self.released:
            return "released"
        if self.watchdog_tripped:
            return "failsafe"
        if not self.taken:
            return "waiting"
        modes = [t.mode for fid, t in self.fans.items() if fid in self._cfg.fans]
        if modes and all(m == "failsafe" for m in modes):
            return "failsafe"
        if any(m == "failsafe" for m in modes):
            return "degraded"
        if any(s.status == "holding" for s in self.sensors.values()):
            return "degraded"
        return "controlling"

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            cfg = self._cfg
            now = self.clock()
            sensors = {}
            for sid, spec in cfg.sensors.items():
                t = self.sensors[sid]
                sensors[sid] = {
                    "label": spec.label,
                    "value": None if t.value is None else round(t.value, 2),
                    "status": t.status,
                    "detail": t.detail,
                    "age_s": None if t.last_good_at is None else round(now - t.last_good_at, 1),
                }
            fans = {}
            for fid, spec in cfg.fans.items():
                t = self.fans[fid]
                fans[fid] = {
                    "label": spec.label,
                    "channel": spec.channel,
                    "source": spec.source,
                    "mode": t.mode,
                    "reason": t.reason,
                    "temp": t.temp,
                    "effective_temp": None if t.effective is None else round(t.effective, 2),
                    "target_pct": t.target_pct,
                    "pwm_raw": t.raw,
                    "pwm_pct": None if t.raw is None else raw_to_pct(t.raw),
                    "rpm": t.rpm,
                    "min_pwm": spec.min_pwm,
                }
            return {
                "time": self.wall(),
                "state": self.overall_state(),
                "chip": cfg.chip,
                "sensors": sensors,
                "fans": fans,
                "loop": {
                    "interval_s": cfg.interval_s,
                    "ticks": self.ticks,
                    "consecutive_errors": self.errors,
                    "last_error": self.last_error,
                    "last_tick_age_s": None
                    if self.last_tick_at is None
                    else round(now - self.last_tick_at, 1),
                    "watchdog_tripped": self.watchdog_tripped,
                },
                "events": list(self.events)[-40:],
            }


def restore_from_state(config: Config, hwmon: Hwmon | None = None) -> list[str]:
    """Hand channels back to automatic mode without a running controller.

    This is what ``ExecStopPost=`` runs, so it also covers a ``kill -9``.
    """
    hw = hwmon or Hwmon(config.sysfs_root)
    ctl = Controller(config, hw)
    saved = ctl._read_state_file()
    done = []
    for f in config.fans.values():
        mode = saved.get(f.channel, config.auto_mode)
        if mode == MANUAL_MODE:
            mode = config.auto_mode
        hw.write_int(config.chip, f.enable_attr, mode)
        done.append(f"pwm{f.channel} -> mode {mode}")
    ctl._remove_state_file()
    return done
