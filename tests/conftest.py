"""Shared fixtures: a hand-built fake hwmon tree and a controllable clock."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from fancurve.config import parse_config
from fancurve.controller import Controller
from fancurve.hwmon import Hwmon


class FakeSysfs:
    """A minimal /sys/class/hwmon with one NCT chip and one NVMe drive."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.base = root / "class" / "hwmon"
        self.nct = self._chip("hwmon3", "nct6798")
        self.nvme = self._chip("hwmon1", "nvme")
        for ch in (1, 2):
            self.write(self.nct / f"pwm{ch}_enable", 5)
            self.write(self.nct / f"pwm{ch}", 128)
            self.write(self.nct / f"fan{ch}_input", 900)
        self.set_temp("cpu", 40.0)
        self.set_temp("nvme", 35.0)

    def _chip(self, d: str, name: str) -> Path:
        p = self.base / d
        p.mkdir(parents=True)
        (p / "name").write_text(name + "\n")
        return p

    @staticmethod
    def write(path: Path, value) -> None:
        path.write_text(f"{value}\n")

    def temp_path(self, sensor: str) -> Path:
        return self.nct / "temp13_input" if sensor == "cpu" else self.nvme / "temp1_input"

    def set_temp(self, sensor: str, celsius: float) -> None:
        self.write(self.temp_path(sensor), round(celsius * 1000))

    def pwm(self, ch: int) -> int:
        return int((self.nct / f"pwm{ch}").read_text())

    def mode(self, ch: int) -> int:
        return int((self.nct / f"pwm{ch}_enable").read_text())


BASE_CONFIG = {
    "chip": "nct6798",
    "interval_s": 1,
    "auto_mode": 5,
    "sensors": {
        "cpu": {"chip": "nct6798", "input": "temp13_input", "label": "CPU"},
        "nvme": {"chip": "nvme", "input": "temp1_input", "label": "NVMe"},
    },
    "fans": {
        "cpu_fan": {
            "label": "CPU fan",
            "channel": 1,
            "source": "cpu",
            "curve": [[40, 20], [60, 40], [70, 70], [80, 100]],
            "min_pwm": 30,
            "hysteresis_c": 0,
        },
        "intake": {
            "label": "Intake",
            "channel": 2,
            "source": "nvme",
            "curve": [[30, 30], [50, 50], [60, 80], [70, 100]],
            "min_pwm": 25,
            "hysteresis_c": 0,
        },
    },
    "failsafe": {"stale_after_s": 5, "frozen_after_s": 0, "watchdog_s": 4},
}


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, s: float) -> None:
        self.now += s


@pytest.fixture
def sysfs(tmp_path: Path) -> FakeSysfs:
    return FakeSysfs(tmp_path / "sys")


@pytest.fixture
def raw_config(sysfs: FakeSysfs, tmp_path: Path) -> dict:
    d = copy.deepcopy(BASE_CONFIG)
    d["sysfs_root"] = str(sysfs.root)
    d["state_file"] = str(tmp_path / "run" / "state.json")
    return d


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def make_controller(raw_config: dict, sysfs: FakeSysfs, clock: Clock):
    def _make(**overrides) -> Controller:
        d = copy.deepcopy(raw_config)
        for k, v in overrides.items():
            if isinstance(v, dict) and isinstance(d.get(k), dict):
                d[k].update(v)
            else:
                d[k] = v
        cfg = parse_config(d)
        return Controller(cfg, Hwmon(sysfs.root), clock=clock, wall=clock)

    return _make
