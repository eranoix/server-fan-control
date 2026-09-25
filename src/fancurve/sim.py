"""A fake hwmon tree driven by a small thermal model.

Run it on any machine and point the controller at the directory it prints:

    python -m fancurve.sim --root ./sim-sysfs

It builds ``<root>/class/hwmon/hwmon{0,1,2}`` with the same attribute files a
Nuvoton NCT6798D, an NVMe drive and an AMD iGPU expose, then keeps them
moving: components heat up under a workload, fans cool them, sensors lag the
silicon, tachometers lag the PWM and stall below their start duty. When a
channel is not in manual mode the simulated chip runs its own stepped curve,
the way the firmware does, so taking and releasing control is visible.

Faults are injected through ``<root>/sim-control.json`` (or the demo's
buttons): a sensor can go missing, return garbage, freeze or read as a
disconnected thermistor.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import random
import signal
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

AMBIENT_C = 24.0

WORKLOADS: dict[str, dict[str, float]] = {
    # watts per heat source
    "idle": {"cpu": 14.0, "gpu": 4.0, "nvme": 1.6},
    "build": {"cpu": 72.0, "gpu": 6.0, "nvme": 6.5},
    "stress": {"cpu": 98.0, "gpu": 32.0, "nvme": 3.0},
}
# The default workload cycles, so a demo left open keeps changing.
CYCLE: tuple[tuple[str, float], ...] = (("idle", 45), ("build", 70), ("stress", 55), ("idle", 40))

FAULTS = ("missing", "garbage", "frozen", "disconnected")


@dataclass
class SimSensor:
    chip: str
    attr: str
    node: str  # which thermal node it reads
    offset: float = 0.0
    tau: float = 3.0  # sensor lag, seconds
    step: float = 0.5  # reported resolution, C
    reading: float = AMBIENT_C


@dataclass
class SimFan:
    channel: int
    max_rpm: float
    stall_pct: float  # below this duty the rotor stops
    tau: float = 1.6
    rpm: float = 0.0


# Sensor ids used by the example configs and the demo buttons.
SENSORS: dict[str, SimSensor] = {
    "cpu": SimSensor("nct6798", "temp13_input", "cpu", tau=1.5, step=0.125),
    "system": SimSensor("nct6798", "temp1_input", "case", offset=2.0, tau=6.0, step=0.5),
    "nvme": SimSensor("nvme", "temp1_input", "nvme", tau=4.0, step=1.0),
    "gpu": SimSensor("amdgpu", "temp1_input", "gpu", tau=2.0, step=1.0),
}
FANS: dict[int, SimFan] = {
    1: SimFan(1, max_rpm=1850, stall_pct=18),  # CPU cooler
    2: SimFan(2, max_rpm=1400, stall_pct=22),  # front intake
    3: SimFan(3, max_rpm=1500, stall_pct=20),  # rear exhaust
}
CHIPS = {"hwmon0": "nct6798", "hwmon1": "nvme", "hwmon2": "amdgpu"}

# What the simulated firmware does in automatic mode: a blunt stepped curve,
# the kind that sits at 60 percent at idle and jumps to 100 at the first peak.
FIRMWARE_STEPS = ((0, 60), (50, 75), (60, 100))


def firmware_duty(temp: float) -> float:
    duty = FIRMWARE_STEPS[0][1]
    for t, d in FIRMWARE_STEPS:
        if temp >= t:
            duty = d
    return duty


@dataclass
class ThermalState:
    cpu: float = 38.0
    gpu: float = 34.0
    nvme: float = 36.0
    case: float = 28.0


@dataclass
class Simulator:
    root: Path
    seed: int | None = None
    speed: float = 1.0
    workload: str = "cycle"
    state: ThermalState = field(default_factory=ThermalState)
    sensors: dict[str, SimSensor] = field(default_factory=dict)
    fans: dict[int, SimFan] = field(default_factory=dict)
    faults: dict[str, str] = field(default_factory=dict)
    sim_time: float = 0.0
    power: dict[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.root = Path(self.root)
        self.rng = random.Random(self.seed)
        self.sensors = {k: SimSensor(**vars(v)) for k, v in SENSORS.items()}
        self.fans = {k: SimFan(**vars(v)) for k, v in FANS.items()}
        self.lock = threading.Lock()
        self._frozen_values: dict[str, str] = {}
        self._pwm_seen: dict[int, int] = {}
        self._written: dict[Path, str] = {}

    # -- tree ------------------------------------------------------------

    @property
    def hwmon_base(self) -> Path:
        return self.root / "class" / "hwmon"

    @property
    def control_file(self) -> Path:
        return self.root / "sim-control.json"

    def chip_dir(self, chip: str) -> Path:
        for d, name in CHIPS.items():
            if name == chip:
                return self.hwmon_base / d
        raise KeyError(chip)

    def build(self) -> Path:
        for d, name in CHIPS.items():
            p = self.hwmon_base / d
            p.mkdir(parents=True, exist_ok=True)
            _atomic_write(p / "name", name)
        nct = self.chip_dir("nct6798")
        for ch, fan in self.fans.items():
            _atomic_write(nct / f"pwm{ch}_enable", "5")  # firmware in charge
            _atomic_write(nct / f"pwm{ch}", "153")
            _atomic_write(nct / f"fan{ch}_input", "0")
            fan.rpm = 0.6 * fan.max_rpm
        for sid, s in self.sensors.items():
            s.reading = getattr(self.state, s.node) + s.offset
            self._write_sensor(sid)
        # A couple of sensors nobody uses, as on the real chip.
        _atomic_write(nct / "temp2_input", "31000")
        _atomic_write(nct / "in0_input", "1016")
        return self.root

    # -- faults and workload ---------------------------------------------

    def set_fault(self, sensor: str, fault: str | None) -> None:
        if sensor not in self.sensors:
            raise KeyError(f"unknown sensor {sensor!r}")
        if fault is not None and fault not in FAULTS:
            raise ValueError(f"unknown fault {fault!r}; choose from {', '.join(FAULTS)}")
        with self.lock:
            if fault is None:
                self.faults.pop(sensor, None)
                self._frozen_values.pop(sensor, None)
            else:
                self.faults[sensor] = fault
            self._write_sensor(sensor)

    def clear_faults(self) -> None:
        for sid in list(self.faults):
            self.set_fault(sid, None)

    def set_workload(self, name: str) -> None:
        if name != "cycle" and name not in WORKLOADS:
            raise ValueError(f"unknown workload {name!r}")
        with self.lock:
            self.workload = name

    def current_phase(self) -> str:
        if self.workload != "cycle":
            return self.workload
        total = sum(d for _, d in CYCLE)
        t = self.sim_time % total
        for name, dur in CYCLE:
            if t < dur:
                return name
            t -= dur
        return "idle"

    def _poll_control_file(self) -> None:
        """Apply faults/workload written by another process (CLI use)."""
        try:
            data = json.loads(self.control_file.read_text())
        except (OSError, ValueError):
            return
        try:
            if "workload" in data and data["workload"] != self.workload:
                self.set_workload(data["workload"])
            wanted = data.get("faults", {}) or {}
            for sid in list(self.faults):
                if sid not in wanted:
                    self.set_fault(sid, None)
            for sid, fault in wanted.items():
                if self.faults.get(sid) != fault:
                    self.set_fault(sid, fault)
        except (KeyError, ValueError):
            pass

    # -- physics ---------------------------------------------------------

    def _duty(self, ch: int) -> float:
        """Duty the chip is actually driving on ``ch``, in percent."""
        nct = self.chip_dir("nct6798")
        mode = _read_int(nct / f"pwm{ch}_enable", 5)
        if mode == 0:
            return 100.0
        if mode == 1:
            raw = _read_int(nct / f"pwm{ch}", self._pwm_seen.get(ch, 153))
            self._pwm_seen[ch] = raw
            return max(0.0, min(255.0, raw)) * 100 / 255
        # Automatic: the firmware picks, from the CPU for the CPU fan and
        # from the board sensor for the case fans, and reports it in pwmN.
        src = self.sensors["cpu"].reading if ch == 1 else self.sensors["system"].reading
        duty = firmware_duty(src)
        self._put(nct / f"pwm{ch}", str(round(duty * 255 / 100)))
        return duty

    def step(self, dt: float) -> None:
        with self.lock:
            self._step(dt)

    def _step(self, dt: float) -> None:
        self.sim_time += dt
        phase = self.current_phase()
        base = WORKLOADS[phase]
        wobble = 1 + 0.06 * math.sin(self.sim_time / 7.0) + self.rng.uniform(-0.04, 0.04)
        p = {k: v * wobble for k, v in base.items()}
        s = self.state
        if s.cpu > 96:  # the CPU protects itself, as real silicon does
            p["cpu"] *= 0.55
        self.power = {k: round(v, 1) for k, v in p.items()}

        frac = {}
        for ch, fan in self.fans.items():
            duty = self._duty(ch)
            target = 0.0 if duty < fan.stall_pct else fan.max_rpm * (0.12 + 0.88 * duty / 100)
            fan.rpm += (target - fan.rpm) * min(1.0, dt / fan.tau)
            frac[ch] = fan.rpm / fan.max_rpm
        case_flow = 0.6 * frac[2] + 0.4 * frac[3]

        ambient = AMBIENT_C + 0.8 * math.sin(self.sim_time / 300.0)
        g_cpu = 0.35 + 2.3 * frac[1] ** 0.8
        g_gpu = 0.45 + 1.3 * case_flow
        g_nvme = 0.12 + 0.55 * frac[2]
        g_case = 2.0 + 14.0 * case_flow
        into_case = p["cpu"] + p["gpu"] + p["nvme"] + 18.0  # board, PSU, disks
        s.cpu += dt * (p["cpu"] - g_cpu * (s.cpu - s.case)) / 55.0
        s.gpu += dt * (p["gpu"] - g_gpu * (s.gpu - s.case)) / 40.0
        s.nvme += dt * (p["nvme"] - g_nvme * (s.nvme - s.case)) / 14.0
        s.case += dt * (into_case - g_case * (s.case - ambient)) / 420.0

        for sid, sensor in self.sensors.items():
            truth = getattr(s, sensor.node) + sensor.offset
            sensor.reading += (truth - sensor.reading) * min(1.0, dt / sensor.tau)
            self._write_sensor(sid)

        nct = self.chip_dir("nct6798")
        for ch, fan in self.fans.items():
            jitter = self.rng.uniform(-6, 6) if fan.rpm > 50 else 0
            self._put(nct / f"fan{ch}_input", str(max(0, round(fan.rpm + jitter))))

    def _write_sensor(self, sid: str) -> None:
        sensor = self.sensors[sid]
        path = self.chip_dir(sensor.chip) / sensor.attr
        fault = self.faults.get(sid)
        # Measurement noise on top of the lagged value, sized to the sensor's
        # resolution, so the reported number moves the way real ones do.
        noisy = sensor.reading + self.rng.gauss(0, 0.6 * sensor.step)
        q = round(noisy / sensor.step) * sensor.step
        value = str(round(q * 1000))
        if fault == "missing":
            self._written.pop(path, None)
            with contextlib.suppress(FileNotFoundError):
                path.unlink()
            return
        if fault == "garbage":
            self._put(path, "N/A")
        elif fault == "disconnected":
            # What an open thermistor input typically reads: far below zero.
            self._put(path, "-55000")
        elif fault == "frozen":
            self._put(path, self._frozen_values.setdefault(sid, value))
        else:
            self._put(path, value)

    def _put(self, path: Path, text: str) -> None:
        """Write only what changed; the tree lives on a real filesystem."""
        if self._written.get(path) == text and path.exists():
            return
        _atomic_write(path, text)
        self._written[path] = text

    def run(self, stop: threading.Event, period: float = 0.5) -> None:
        last = time.monotonic()
        while not stop.wait(period):
            now = time.monotonic()
            self._poll_control_file()
            self.step((now - last) * self.speed)
            last = now

    def summary(self) -> dict:
        with self.lock:
            return {
                "phase": self.current_phase(),
                "workload": self.workload,
                "power_w": dict(self.power),
                "truth_c": {k: round(v, 1) for k, v in vars(self.state).items()},
                "faults": dict(self.faults),
                "rpm": {ch: round(f.rpm) for ch, f in self.fans.items()},
            }


def _atomic_write(path: Path, text: str) -> None:
    # Readers must never see a half-written value, or the controller would
    # (correctly) report garbage that the real kernel never produces.
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(text + "\n")
    os.replace(tmp, path)


def _read_int(path: Path, default: int) -> int:
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return default


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m fancurve.sim",
        description="Run a fake NCT6798D hwmon tree with a thermal model.",
    )
    ap.add_argument("--root", default="./sim-sysfs", help="directory for the fake sysfs tree")
    ap.add_argument("--speed", type=float, default=1.0, help="simulated seconds per real second")
    ap.add_argument("--workload", default="cycle", choices=["cycle", *WORKLOADS])
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--duration", type=float, default=0, help="stop after N seconds (0 = never)")
    args = ap.parse_args(argv)

    sim = Simulator(Path(args.root), seed=args.seed, speed=args.speed, workload=args.workload)
    sim.build()
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    print(f"fake sysfs ready at {sim.root.resolve()}")
    print(f'point the controller at it with  "sysfs_root": "{sim.root.resolve()}"')
    print(f"inject faults by writing {sim.control_file}, for example:")
    print('  {"workload": "stress", "faults": {"nvme": "missing"}}')
    t = threading.Thread(target=sim.run, args=(stop,), daemon=True)
    t.start()
    started = time.monotonic()
    try:
        while not stop.wait(5):
            s = sim.summary()
            t = s["truth_c"]
            print(
                f"[{s['phase']:>6}] cpu {t['cpu']:5.1f}C  case {t['case']:5.1f}C"
                f"  nvme {t['nvme']:5.1f}C  rpm {s['rpm']}  faults {s['faults'] or '-'}",
                flush=True,
            )
            if args.duration and time.monotonic() - started >= args.duration:
                break
    except KeyboardInterrupt:
        pass
    stop.set()
    t.join(timeout=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
