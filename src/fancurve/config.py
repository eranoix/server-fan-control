"""Configuration: schema, validation and atomic persistence.

The config is JSON so that the dashboard can write it back with the standard
library alone. Validation is strict and collects every problem in one pass,
each one with the path of the offending field, so a broken file produces a
single readable report instead of a fix-one-rerun loop. Unknown keys are
errors: a misspelled ``min_pwm`` silently ignored is a fan that stalls.
"""

from __future__ import annotations

import contextlib
import copy
import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MIN_POINTS = 2
MAX_POINTS = 8
TEMP_RANGE = (0.0, 110.0)
MAX_CHANNEL = 7


class ConfigError(ValueError):
    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("invalid config:\n  " + "\n  ".join(errors))


@dataclass(frozen=True)
class SensorSpec:
    id: str
    chip: str
    input: str
    label: str


@dataclass(frozen=True)
class FanSpec:
    id: str
    label: str
    channel: int
    source: str
    curve: tuple[tuple[float, float], ...]
    min_pwm: float
    hysteresis_c: float

    @property
    def pwm_attr(self) -> str:
        return f"pwm{self.channel}"

    @property
    def enable_attr(self) -> str:
        return f"pwm{self.channel}_enable"

    @property
    def rpm_attr(self) -> str:
        return f"fan{self.channel}_input"


@dataclass(frozen=True)
class FailsafeSpec:
    stale_after_s: float = 6.0
    frozen_after_s: float = 0.0
    min_valid_c: float = 0.0
    max_valid_c: float = 120.0
    watchdog_s: float = 10.0


@dataclass(frozen=True)
class WebSpec:
    bind: str = "127.0.0.1"
    port: int = 8790
    token_file: str | None = None


@dataclass(frozen=True)
class Config:
    sysfs_root: str
    chip: str
    interval_s: float
    auto_mode: int
    state_file: str | None
    sensors: dict[str, SensorSpec]
    fans: dict[str, FanSpec]
    failsafe: FailsafeSpec = field(default_factory=FailsafeSpec)
    web: WebSpec = field(default_factory=WebSpec)


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _check_keys(obj: dict, allowed: set[str], required: set[str], where: str, errs: list[str]):
    for k in sorted(set(obj) - allowed):
        errs.append(f"{where}.{k}: unknown key")
    for k in sorted(required - set(obj)):
        errs.append(f"{where}.{k}: required")


def _num(obj: dict, key: str, where: str, errs: list[str], lo: float, hi: float, default=None):
    if key not in obj:
        return default
    v = obj[key]
    if not _is_num(v):
        errs.append(f"{where}.{key}: must be a number, got {type(v).__name__}")
        return default
    if not (lo <= v <= hi):
        errs.append(f"{where}.{key}: {v} is outside {lo:g}..{hi:g}")
        return default
    return float(v)


def _str(obj: dict, key: str, where: str, errs: list[str], default=None):
    if key not in obj:
        return default
    v = obj[key]
    if not isinstance(v, str) or not v.strip():
        errs.append(f"{where}.{key}: must be a non-empty string")
        return default
    return v


def validate_curve(points: Any, where: str) -> list[str]:
    """Problems with a curve, empty when it is usable."""
    errs: list[str] = []
    if not isinstance(points, list):
        return [f"{where}: must be a list of [temp_c, pwm_pct] points"]
    if not (MIN_POINTS <= len(points) <= MAX_POINTS):
        errs.append(f"{where}: needs {MIN_POINTS} to {MAX_POINTS} points, got {len(points)}")
    good: list[tuple[float, float]] = []
    for i, p in enumerate(points):
        w = f"{where}[{i}]"
        if not (isinstance(p, list) and len(p) == 2 and all(_is_num(x) for x in p)):
            errs.append(f"{w}: must be [temp_c, pwm_pct] with two numbers")
            continue
        t, pct = float(p[0]), float(p[1])
        if not (TEMP_RANGE[0] <= t <= TEMP_RANGE[1]):
            errs.append(f"{w}: temperature {t:g} is outside {TEMP_RANGE[0]:g}..{TEMP_RANGE[1]:g}")
        if not (0 <= pct <= 100):
            errs.append(f"{w}: duty cycle {pct:g} is outside 0..100")
        good.append((t, pct))
    if errs:
        return errs
    for i in range(1, len(good)):
        if good[i][0] <= good[i - 1][0]:
            errs.append(f"{where}[{i}]: temperatures must be strictly increasing")
        if good[i][1] < good[i - 1][1]:
            errs.append(
                f"{where}[{i}]: duty cycle drops from {good[i - 1][1]:g} to {good[i][1]:g}; "
                "a curve must never slow the fan as it gets hotter"
            )
    if good and good[-1][1] != 100:
        errs.append(f"{where}: the last point must be 100 percent; a curve has to reach full speed")
    return errs


TOP_KEYS = {
    "sysfs_root",
    "chip",
    "interval_s",
    "auto_mode",
    "state_file",
    "sensors",
    "fans",
    "failsafe",
    "web",
}
SENSOR_KEYS = {"chip", "input", "label"}
FAN_KEYS = {"label", "channel", "source", "curve", "min_pwm", "hysteresis_c"}
FAILSAFE_KEYS = {"stale_after_s", "frozen_after_s", "min_valid_c", "max_valid_c", "watchdog_s"}
WEB_KEYS = {"bind", "port", "token_file"}


def parse_config(data: Any) -> Config:
    """Validate a decoded JSON document and build a :class:`Config`.

    Raises :class:`ConfigError` listing every problem found.
    """
    errs: list[str] = []
    if not isinstance(data, dict):
        raise ConfigError(["config: top level must be an object"])
    _check_keys(data, TOP_KEYS, {"chip", "sensors", "fans"}, "config", errs)

    sysfs_root = _str(data, "sysfs_root", "config", errs, "/sys")
    chip = _str(data, "chip", "config", errs, "")
    interval = _num(data, "interval_s", "config", errs, 0.5, 30.0, 2.0)
    auto_mode = 5
    if "auto_mode" in data:
        if _is_int(data["auto_mode"]) and 0 <= data["auto_mode"] <= 5 and data["auto_mode"] != 1:
            auto_mode = data["auto_mode"]
        else:
            errs.append("config.auto_mode: must be an integer 0 or 2..5 (1 is manual mode)")
    state_file = data.get("state_file")
    if state_file is not None and (not isinstance(state_file, str) or not state_file):
        errs.append("config.state_file: must be a path or null")
        state_file = None

    sensors: dict[str, SensorSpec] = {}
    raw_sensors = data.get("sensors", {})
    if not isinstance(raw_sensors, dict) or not raw_sensors:
        errs.append("config.sensors: must be a non-empty object")
        raw_sensors = {}
    for sid, s in raw_sensors.items():
        w = f"sensors.{sid}"
        if not isinstance(s, dict):
            errs.append(f"{w}: must be an object")
            continue
        _check_keys(s, SENSOR_KEYS, {"chip", "input"}, w, errs)
        s_chip = _str(s, "chip", w, errs)
        s_input = _str(s, "input", w, errs)
        if s_input and not (s_input.startswith("temp") and s_input.endswith("_input")):
            errs.append(f"{w}.input: expected a tempN_input attribute, got {s_input!r}")
        label = _str(s, "label", w, errs, sid)
        if s_chip and s_input:
            sensors[sid] = SensorSpec(sid, s_chip, s_input, label)

    fans: dict[str, FanSpec] = {}
    raw_fans = data.get("fans", {})
    if not isinstance(raw_fans, dict) or not raw_fans:
        errs.append("config.fans: must be a non-empty object")
        raw_fans = {}
    channels: dict[int, str] = {}
    for fid, f in raw_fans.items():
        w = f"fans.{fid}"
        if not isinstance(f, dict):
            errs.append(f"{w}: must be an object")
            continue
        n_before = len(errs)
        _check_keys(f, FAN_KEYS, {"channel", "source", "curve"}, w, errs)
        ch = f.get("channel")
        if "channel" in f:
            if not (_is_int(ch) and 1 <= ch <= MAX_CHANNEL):
                errs.append(f"{w}.channel: must be an integer 1..{MAX_CHANNEL}")
            elif ch in channels:
                errs.append(f"{w}.channel: pwm{ch} is already driven by fan {channels[ch]!r}")
            else:
                channels[ch] = fid
        src = f.get("source")
        if "source" in f and src not in raw_sensors:
            known = ", ".join(sorted(raw_sensors)) or "none"
            errs.append(f"{w}.source: {src!r} is not a configured sensor (known: {known})")
        if "curve" in f:
            errs.extend(validate_curve(f["curve"], f"{w}.curve"))
        min_pwm = _num(f, "min_pwm", w, errs, 0, 100, 20.0)
        hyst = _num(f, "hysteresis_c", w, errs, 0, 15, 2.0)
        label = _str(f, "label", w, errs, fid)
        if len(errs) == n_before:
            fans[fid] = FanSpec(
                id=fid,
                label=label,
                channel=ch,
                source=src,
                curve=tuple((float(t), float(p)) for t, p in f["curve"]),
                min_pwm=min_pwm,
                hysteresis_c=hyst,
            )

    fs_raw = data.get("failsafe", {})
    failsafe = FailsafeSpec()
    if not isinstance(fs_raw, dict):
        errs.append("config.failsafe: must be an object")
    else:
        _check_keys(fs_raw, FAILSAFE_KEYS, set(), "failsafe", errs)
        d = FailsafeSpec()
        failsafe = FailsafeSpec(
            stale_after_s=_num(fs_raw, "stale_after_s", "failsafe", errs, 0, 300, d.stale_after_s),
            frozen_after_s=_num(
                fs_raw, "frozen_after_s", "failsafe", errs, 0, 3600, d.frozen_after_s
            ),
            min_valid_c=_num(fs_raw, "min_valid_c", "failsafe", errs, -60, 60, d.min_valid_c),
            max_valid_c=_num(fs_raw, "max_valid_c", "failsafe", errs, 40, 150, d.max_valid_c),
            watchdog_s=_num(fs_raw, "watchdog_s", "failsafe", errs, 1, 300, d.watchdog_s),
        )
        if failsafe.min_valid_c >= failsafe.max_valid_c:
            errs.append("failsafe: min_valid_c must be below max_valid_c")
        if interval is not None and failsafe.watchdog_s < 2 * interval:
            errs.append(
                f"failsafe.watchdog_s: {failsafe.watchdog_s:g} must be at least twice "
                f"interval_s ({interval:g}), or the watchdog trips on a healthy loop"
            )
        if interval is not None and 0 < failsafe.frozen_after_s < 3 * interval:
            errs.append("failsafe.frozen_after_s: must be 0 (off) or at least three intervals")

    web_raw = data.get("web", {})
    web = WebSpec()
    if not isinstance(web_raw, dict):
        errs.append("config.web: must be an object")
    else:
        _check_keys(web_raw, WEB_KEYS, set(), "web", errs)
        port = web_raw.get("port", web.port)
        if not (_is_int(port) and 1 <= port <= 65535):
            errs.append("web.port: must be an integer 1..65535")
            port = web.port
        token_file = web_raw.get("token_file")
        if token_file is not None and not (isinstance(token_file, str) and token_file):
            errs.append("web.token_file: must be a path or null")
            token_file = None
        web = WebSpec(
            bind=_str(web_raw, "bind", "web", errs, web.bind), port=port, token_file=token_file
        )

    if errs:
        raise ConfigError(errs)
    return Config(
        sysfs_root=sysfs_root,
        chip=chip,
        interval_s=interval,
        auto_mode=auto_mode,
        state_file=state_file,
        sensors=sensors,
        fans=fans,
        failsafe=failsafe,
        web=web,
    )


def config_to_dict(cfg: Config) -> dict[str, Any]:
    return {
        "sysfs_root": cfg.sysfs_root,
        "chip": cfg.chip,
        "interval_s": cfg.interval_s,
        "auto_mode": cfg.auto_mode,
        "state_file": cfg.state_file,
        "sensors": {
            s.id: {"chip": s.chip, "input": s.input, "label": s.label} for s in cfg.sensors.values()
        },
        "fans": {
            f.id: {
                "label": f.label,
                "channel": f.channel,
                "source": f.source,
                "curve": [[_tidy(t), _tidy(p)] for t, p in f.curve],
                "min_pwm": _tidy(f.min_pwm),
                "hysteresis_c": _tidy(f.hysteresis_c),
            }
            for f in cfg.fans.values()
        },
        "failsafe": {
            "stale_after_s": cfg.failsafe.stale_after_s,
            "frozen_after_s": cfg.failsafe.frozen_after_s,
            "min_valid_c": cfg.failsafe.min_valid_c,
            "max_valid_c": cfg.failsafe.max_valid_c,
            "watchdog_s": cfg.failsafe.watchdog_s,
        },
        "web": {"bind": cfg.web.bind, "port": cfg.web.port, "token_file": cfg.web.token_file},
    }


def _tidy(x: float) -> float | int:
    return int(x) if float(x).is_integer() else x


FAN_EDITABLE = {"curve", "source", "min_pwm", "hysteresis_c", "label"}


def update_fan(cfg: Config, fan_id: str, changes: dict[str, Any]) -> Config:
    """A new config with ``changes`` applied to one fan, fully re-validated."""
    if fan_id not in cfg.fans:
        raise ConfigError([f"fans.{fan_id}: no such fan"])
    if not isinstance(changes, dict) or not changes:
        raise ConfigError(["body: expected an object with at least one field"])
    bad = sorted(set(changes) - FAN_EDITABLE)
    if bad:
        raise ConfigError([f"fans.{fan_id}.{k}: not editable here" for k in bad])
    data = copy.deepcopy(config_to_dict(cfg))
    data["fans"][fan_id].update(changes)
    return parse_config(data)


def load_config(path: str | Path) -> Config:
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError([f"{p}: cannot read ({exc.strerror or exc})"]) from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError([f"{p}: not valid JSON (line {exc.lineno}: {exc.msg})"]) from exc
    return parse_config(data)


def save_config(cfg: Config, path: str | Path) -> None:
    """Write atomically: a crash mid-write leaves the old file, never half of one."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{p.name}.", dir=p.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(config_to_dict(cfg), fh, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, p)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
