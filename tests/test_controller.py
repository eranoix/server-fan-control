"""Normal operation: taking the chip, following curves, floors, sources."""

import json
from pathlib import Path

from fancurve.config import update_fan
from fancurve.curve import pct_to_raw


def test_take_records_modes_and_switches_to_manual(make_controller, sysfs, raw_config):
    ctl = make_controller()
    ctl.tick()
    assert ctl.taken
    assert ctl.original_modes == {1: 5, 2: 5}
    assert sysfs.mode(1) == 1 and sysfs.mode(2) == 1
    state = json.loads(Path(raw_config["state_file"]).read_text())
    assert state == {"chip": "nct6798", "modes": {"1": 5, "2": 5}}


def test_curve_is_followed(make_controller, sysfs):
    ctl = make_controller()
    sysfs.set_temp("cpu", 65)  # halfway between (60, 40) and (70, 70)
    ctl.tick()
    assert ctl.fans["cpu_fan"].target_pct == 55
    assert sysfs.pwm(1) == pct_to_raw(55)
    assert ctl.fans["cpu_fan"].mode == "curve"


def test_floor_wins_over_a_lower_curve(make_controller, sysfs):
    ctl = make_controller()
    sysfs.set_temp("cpu", 20)  # curve says 20 percent, floor is 30
    ctl.tick()
    assert ctl.fans["cpu_fan"].target_pct == 30
    assert sysfs.pwm(1) == pct_to_raw(30)


def test_top_of_curve_writes_exactly_255(make_controller, sysfs):
    ctl = make_controller()
    sysfs.set_temp("cpu", 95)
    ctl.tick()
    assert sysfs.pwm(1) == 255


def test_each_fan_follows_its_own_source(make_controller, sysfs):
    ctl = make_controller()
    sysfs.set_temp("cpu", 80)
    sysfs.set_temp("nvme", 30)
    ctl.tick()
    assert ctl.fans["cpu_fan"].target_pct == 100
    assert ctl.fans["intake"].target_pct == 30


def test_hysteresis_is_applied_in_the_loop(make_controller, sysfs):
    ctl = make_controller()
    ctl.set_config(update_fan(ctl.config, "cpu_fan", {"hysteresis_c": 4}))
    sysfs.set_temp("cpu", 70)
    ctl.tick()
    assert ctl.fans["cpu_fan"].target_pct == 70
    sysfs.set_temp("cpu", 67)  # inside the band: keep the speed
    ctl.tick()
    assert ctl.fans["cpu_fan"].target_pct == 70
    assert ctl.fans["cpu_fan"].effective == 70
    sysfs.set_temp("cpu", 62)  # beyond it: follow, 4 degrees behind
    ctl.tick()
    assert ctl.fans["cpu_fan"].effective == 66
    assert ctl.fans["cpu_fan"].target_pct == 58


def test_manual_mode_is_reasserted_if_something_flips_it(make_controller, sysfs):
    ctl = make_controller()
    ctl.tick()
    sysfs.write(sysfs.nct / "pwm1_enable", 5)  # firmware after resume, or another tool
    ctl.tick()
    assert sysfs.mode(1) == 1


def test_changing_source_resets_hysteresis(make_controller, sysfs):
    ctl = make_controller()
    ctl.set_config(update_fan(ctl.config, "cpu_fan", {"hysteresis_c": 5}))
    sysfs.set_temp("cpu", 75)
    ctl.tick()
    ctl.set_config(update_fan(ctl.config, "cpu_fan", {"source": "nvme"}))
    sysfs.set_temp("nvme", 50)
    ctl.tick()
    assert ctl.fans["cpu_fan"].effective == 50  # not held at 75 by the old sensor


def test_snapshot_and_history_shape(make_controller, sysfs):
    ctl = make_controller()
    ctl.tick()
    snap = ctl.snapshot()
    assert snap["state"] == "controlling"
    assert snap["sensors"]["cpu"]["status"] == "ok"
    assert snap["fans"]["cpu_fan"]["pwm_raw"] == sysfs.pwm(1)
    assert snap["fans"]["cpu_fan"]["rpm"] == 900
    assert len(ctl.history) == 1
    assert ctl.history[0]["failsafe"] == []
    assert json.dumps(snap)  # must be JSON serialisable as is


def test_history_holds_fifteen_minutes_at_any_interval(make_controller):
    assert make_controller().history.maxlen >= 15 * 60  # interval_s is 1
    assert make_controller(interval_s=2).history.maxlen >= 15 * 60 / 2
