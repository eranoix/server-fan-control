"""Every path that must end with the fan at full speed, or back in firmware hands."""

import json
import os
import pathlib
import threading

import pytest

from fancurve.config import parse_config
from fancurve.controller import Controller, SensorTrack, assess, restore_from_state
from fancurve.hwmon import Hwmon, HwmonError, Reading

FULL = 255


def run(ctl, clock, seconds, step=1.0):
    for _ in range(int(seconds / step)):
        clock.advance(step)
        ctl.tick()


# -- sensor faults ------------------------------------------------------------


def test_sensor_missing_from_the_start_means_full_speed_immediately(make_controller, sysfs):
    sysfs.temp_path("cpu").unlink()
    ctl = make_controller()
    ctl.tick()
    assert sysfs.pwm(1) == FULL
    assert ctl.fans["cpu_fan"].mode == "failsafe"
    assert "no valid reading yet" in ctl.sensors["cpu"].detail


@pytest.mark.parametrize(
    "break_it",
    [
        pytest.param(lambda s: s.temp_path("cpu").unlink(), id="missing"),
        pytest.param(lambda s: s.temp_path("cpu").write_text("N/A\n"), id="garbage"),
        pytest.param(lambda s: s.write(s.temp_path("cpu"), -55000), id="out_of_range_low"),
        pytest.param(lambda s: s.write(s.temp_path("cpu"), 250000), id="out_of_range_high"),
        pytest.param(
            lambda s: (s.temp_path("cpu").unlink(), s.temp_path("cpu").mkdir()), id="io_error"
        ),
    ],
)
def test_bad_reading_is_bridged_then_goes_full(make_controller, sysfs, clock, break_it):
    ctl = make_controller()
    sysfs.set_temp("cpu", 50)
    ctl.tick()
    before = sysfs.pwm(1)
    assert before < FULL

    break_it(sysfs)
    run(ctl, clock, 5)  # stale_after_s is 5: still bridging
    assert ctl.sensors["cpu"].status == "holding"
    assert ctl.fans["cpu_fan"].mode == "curve"
    assert sysfs.pwm(1) == before

    run(ctl, clock, 1)
    assert ctl.fans["cpu_fan"].mode == "failsafe"
    assert sysfs.pwm(1) == FULL
    assert ctl.overall_state() == "degraded"  # the other fan is still fine


def test_a_bad_sensor_only_affects_the_fans_it_drives(make_controller, sysfs, clock):
    ctl = make_controller()
    ctl.tick()
    sysfs.temp_path("nvme").write_text("garbage")
    run(ctl, clock, 10)
    assert ctl.fans["intake"].mode == "failsafe"
    assert sysfs.pwm(2) == FULL
    assert ctl.fans["cpu_fan"].mode == "curve"
    assert sysfs.pwm(1) < FULL


def test_all_fans_failing_reports_failsafe(make_controller, sysfs, clock):
    ctl = make_controller()
    ctl.tick()
    sysfs.temp_path("cpu").unlink()
    sysfs.temp_path("nvme").unlink()
    run(ctl, clock, 10)
    assert ctl.overall_state() == "failsafe"


def test_recovery_goes_back_to_the_curve(make_controller, sysfs, clock):
    ctl = make_controller()
    ctl.tick()
    sysfs.temp_path("cpu").unlink()
    run(ctl, clock, 10)
    assert sysfs.pwm(1) == FULL
    sysfs.set_temp("cpu", 60)
    run(ctl, clock, 1)
    assert ctl.fans["cpu_fan"].mode == "curve"
    assert sysfs.pwm(1) < FULL
    messages = [e["message"] for e in ctl.events]
    assert any("FAIL-SAFE" in m for m in messages)
    assert any("back on its curve" in m for m in messages)


def test_frozen_sensor_is_caught_when_enabled(make_controller, sysfs, clock):
    ctl = make_controller(failsafe={"frozen_after_s": 30})
    sysfs.set_temp("cpu", 55)
    ctl.tick()
    run(ctl, clock, 29)
    assert ctl.fans["cpu_fan"].mode == "curve"
    run(ctl, clock, 2)
    assert ctl.sensors["cpu"].status == "frozen"
    assert sysfs.pwm(1) == FULL
    sysfs.set_temp("cpu", 55.5)  # it moves again
    run(ctl, clock, 1)
    assert ctl.fans["cpu_fan"].mode == "curve"


def test_frozen_detection_is_off_by_default(make_controller, sysfs, clock):
    ctl = make_controller()
    sysfs.set_temp("cpu", 55)
    run(ctl, clock, 600, step=5)
    assert ctl.fans["cpu_fan"].mode == "curve"


def test_holding_is_not_granted_to_a_sensor_that_was_already_failed():
    track = SensorTrack()
    assess(Reading(None, "garbage", "x"), track, 0, 5, 0)
    assert track.status == "garbage"
    assess(Reading(None, "garbage", "x"), track, 1, 5, 0)
    assert not track.usable


# -- controller faults --------------------------------------------------------


def test_exception_in_a_tick_drives_every_fan_to_full(make_controller, sysfs):
    ctl = make_controller()
    ctl.tick()
    assert sysfs.pwm(1) < FULL and sysfs.pwm(2) < FULL
    ctl.crash_next_tick = "boom"
    ctl.tick()  # must not raise
    assert sysfs.pwm(1) == FULL and sysfs.pwm(2) == FULL
    assert ctl.errors == 1 and "boom" in ctl.last_error
    ctl.tick()  # next tick is healthy again
    assert sysfs.pwm(1) < FULL
    assert ctl.errors == 0


def test_a_failing_pwm_write_forces_the_other_channels_to_full(make_controller, sysfs, monkeypatch):
    ctl = make_controller()
    ctl.tick()
    real = Hwmon.write_int

    def flaky(self, chip, attr, value):
        if attr == "pwm2" and value != FULL:
            raise HwmonError("EIO")
        return real(self, chip, attr, value)

    monkeypatch.setattr(Hwmon, "write_int", flaky)
    ctl.tick()
    assert sysfs.pwm(1) == FULL
    assert sysfs.pwm(2) == FULL


def test_full_speed_write_failing_on_one_channel_still_covers_the_rest(
    make_controller, sysfs, monkeypatch
):
    ctl = make_controller()
    ctl.tick()
    real = Hwmon.write_int

    def dead_pwm1(self, chip, attr, value):
        if attr.startswith("pwm1"):
            raise HwmonError("EIO")
        return real(self, chip, attr, value)

    monkeypatch.setattr(Hwmon, "write_int", dead_pwm1)
    ctl.force_full_speed("test")
    assert sysfs.pwm(2) == FULL


def test_watchdog_forces_full_when_the_loop_stalls(make_controller, sysfs, clock):
    ctl = make_controller()
    ctl.tick()
    clock.advance(3)
    assert ctl.watchdog_check() is False
    assert sysfs.pwm(1) < FULL
    clock.advance(2)  # watchdog_s is 4
    assert ctl.watchdog_check() is True
    assert sysfs.pwm(1) == FULL and sysfs.pwm(2) == FULL
    assert ctl.overall_state() == "failsafe"
    ctl.tick()  # the loop comes back
    assert not ctl.watchdog_tripped


def test_watchdog_fires_even_if_the_first_tick_never_happens(make_controller, sysfs, clock):
    ctl = make_controller()
    clock.advance(10)
    assert ctl.watchdog_check() is True


def test_watchdog_does_not_need_the_loop_lock(make_controller, sysfs, clock):
    ctl = make_controller()
    ctl.tick()
    done = threading.Event()

    def hang():  # a tick that hangs while holding the lock
        with ctl.lock:
            done.wait(5)

    t = threading.Thread(target=hang)
    t.start()
    try:
        clock.advance(10)
        assert ctl.watchdog_check() is True
        assert sysfs.pwm(1) == FULL
    finally:
        done.set()
        t.join()


def test_chip_missing_waits_then_takes_control(make_controller, sysfs, clock, tmp_path):
    hidden = sysfs.nct.rename(tmp_path / "parked")
    ctl = make_controller()
    ctl.tick()
    assert ctl.overall_state() == "waiting"
    assert not ctl.taken
    hidden.rename(sysfs.nct)  # driver loads late
    clock.advance(1)
    ctl.tick()
    assert ctl.taken and sysfs.mode(1) == 1


# -- handing back ---------------------------------------------------------------


def test_release_restores_the_original_modes(make_controller, sysfs, raw_config):
    sysfs.write(sysfs.nct / "pwm2_enable", 2)
    ctl = make_controller()
    ctl.tick()
    ctl.release()
    assert sysfs.mode(1) == 5 and sysfs.mode(2) == 2
    assert ctl.fans["cpu_fan"].mode == "released"
    assert ctl.overall_state() == "released"
    assert not os.path.exists(raw_config["state_file"])
    ctl.release()  # idempotent
    ctl.tick()  # and a late tick does not take the chip back
    assert sysfs.mode(1) == 5


def test_release_before_take_writes_nothing(make_controller, sysfs):
    ctl = make_controller()
    ctl.release()
    assert sysfs.mode(1) == 5


def test_if_restore_fails_the_channel_is_left_at_full_not_at_our_last_duty(
    make_controller, sysfs, monkeypatch
):
    ctl = make_controller()
    sysfs.set_temp("cpu", 20)
    ctl.tick()
    real = Hwmon.write_int

    def enable_refuses_auto(self, chip, attr, value):
        if attr == "pwm1_enable" and value != 1:
            raise HwmonError("EINVAL")
        return real(self, chip, attr, value)

    monkeypatch.setattr(Hwmon, "write_int", enable_refuses_auto)
    ctl.release()
    assert sysfs.mode(1) == 1 and sysfs.pwm(1) == FULL
    assert sysfs.mode(2) == 5


def test_chip_left_in_manual_by_a_crash_is_restored_to_auto_mode(make_controller, sysfs):
    for ch in (1, 2):
        sysfs.write(sysfs.nct / f"pwm{ch}_enable", 1)
    ctl = make_controller()  # no state file: the true original is unknown
    ctl.tick()
    assert ctl.original_modes == {1: 5, 2: 5}  # configured auto_mode


def test_state_file_survives_a_kill_and_restore_uses_it(make_controller, sysfs, raw_config):
    sysfs.write(sysfs.nct / "pwm1_enable", 3)
    first = make_controller()
    first.tick()  # ...and then the process is killed: no release()
    assert sysfs.mode(1) == 1

    second = make_controller()  # the service restarts
    second.tick()
    assert second.original_modes[1] == 3  # from the state file, not the current manual 1

    # or, instead of a restart, ExecStopPost runs `fancurve restore`
    lines = restore_from_state(second.config, Hwmon(sysfs.root))
    assert sysfs.mode(1) == 3 and sysfs.mode(2) == 5
    assert "pwm1 -> mode 3" in lines
    assert not os.path.exists(raw_config["state_file"])


def test_restore_without_a_state_file_uses_auto_mode(make_controller, sysfs, raw_config):
    ctl = make_controller()
    ctl.tick()
    os.unlink(raw_config["state_file"])
    restore_from_state(ctl.config, Hwmon(sysfs.root))
    assert sysfs.mode(1) == 5


def test_state_file_for_another_chip_is_ignored(make_controller, sysfs, raw_config):
    p = pathlib.Path(raw_config["state_file"])
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"chip": "it8686", "modes": {"1": 2}}))
    ctl = make_controller()
    ctl.tick()
    assert ctl.original_modes[1] == 5


def test_run_loop_stops_and_can_be_released(make_controller, sysfs):
    ctl = make_controller()
    stop = threading.Event()
    t = threading.Thread(target=ctl.run, args=(stop,))
    t.start()
    stop.set()
    t.join(timeout=3)
    assert not t.is_alive()
    ctl.release()
    assert sysfs.mode(1) == 5


def test_controller_uses_the_configured_sysfs_root(raw_config, sysfs):
    ctl = Controller(parse_config(raw_config))
    assert ctl.hwmon.sysfs_root == sysfs.root
