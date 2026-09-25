import pytest

from fancurve.config import parse_config
from fancurve.controller import Controller
from fancurve.demo import DEMO_CONFIG
from fancurve.hwmon import Hwmon
from fancurve.sim import Simulator


@pytest.fixture
def sim(tmp_path):
    s = Simulator(tmp_path / "sysfs", seed=7)
    s.build()
    return s


def test_tree_looks_like_hwmon(sim):
    hw = Hwmon(sim.root)
    assert set(hw.chips().values()) == {"nct6798", "nvme", "amdgpu"}
    assert hw.read_temp("nct6798", "temp13_input").ok
    assert hw.read_int("nct6798", "pwm1_enable") == 5


def test_firmware_mode_drives_the_fans_itself(sim):
    for _ in range(20):
        sim.step(0.5)
    hw = Hwmon(sim.root)
    assert hw.read_int("nct6798", "pwm1") > 0
    assert hw.read_int("nct6798", "fan1_input") > 500


def test_stalled_fans_under_stress_overheat(sim):
    hw = Hwmon(sim.root)
    for ch in (1, 2, 3):
        hw.write_int("nct6798", f"pwm{ch}_enable", 1)
        hw.write_int("nct6798", f"pwm{ch}", 0)
    sim.set_workload("stress")
    for _ in range(600):
        sim.step(0.5)
    assert hw.read_int("nct6798", "fan1_input") == 0  # below stall duty: rotor stops
    assert sim.state.cpu > 90


def test_controller_keeps_the_simulated_machine_cool(sim):
    cfg = parse_config(dict(DEMO_CONFIG, sysfs_root=str(sim.root)))
    t = [0.0]
    ctl = Controller(cfg, clock=lambda: t[0])
    sim.set_workload("stress")
    peak = 0.0
    for _ in range(600):  # 10 simulated minutes
        sim.step(1.0)
        t[0] += 1.0
        ctl.tick()
        peak = max(peak, sim.state.cpu)
    assert ctl.overall_state() == "controlling"
    assert peak < 85
    assert ctl.fans["cpu_fan"].target_pct > 60


@pytest.mark.parametrize(
    ("fault", "status"),
    [("missing", "missing"), ("garbage", "garbage"), ("disconnected", "out_of_range")],
)
def test_injected_faults_are_seen_by_hwmon(sim, fault, status):
    sim.set_fault("nvme", fault)
    r = Hwmon(sim.root).read_temp("nvme", "temp1_input", 0, 120)
    assert r.status == status
    sim.set_fault("nvme", None)
    assert Hwmon(sim.root).read_temp("nvme", "temp1_input").ok


def test_frozen_fault_stops_the_value(sim):
    hw = Hwmon(sim.root)
    sim.set_fault("cpu", "frozen")
    first = hw.read_temp("nct6798", "temp13_input").value
    sim.set_workload("stress")
    for _ in range(100):
        sim.step(0.5)
    assert hw.read_temp("nct6798", "temp13_input").value == first
    assert sim.state.cpu > first + 5


def test_bad_fault_and_workload_names(sim):
    with pytest.raises(ValueError):
        sim.set_fault("cpu", "melted")
    with pytest.raises(KeyError):
        sim.set_fault("psu", "missing")
    with pytest.raises(ValueError):
        sim.set_workload("bitcoin")


def test_control_file_drives_the_simulator(sim):
    sim.control_file.write_text('{"workload": "build", "faults": {"gpu": "garbage"}}')
    sim._poll_control_file()
    assert sim.workload == "build" and sim.faults == {"gpu": "garbage"}
    sim.control_file.write_text('{"faults": {}}')
    sim._poll_control_file()
    assert sim.faults == {}
