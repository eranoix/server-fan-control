import copy
import json
from pathlib import Path

import pytest

from fancurve.config import (
    ConfigError,
    config_to_dict,
    load_config,
    parse_config,
    save_config,
    update_fan,
)
from fancurve.demo import DEMO_CONFIG

from .conftest import BASE_CONFIG

ROOT = Path(__file__).resolve().parent.parent


def errors_for(mutate) -> list[str]:
    d = copy.deepcopy(BASE_CONFIG)
    mutate(d)
    with pytest.raises(ConfigError) as exc:
        parse_config(d)
    return exc.value.errors


def test_base_config_is_valid():
    cfg = parse_config(copy.deepcopy(BASE_CONFIG))
    assert set(cfg.fans) == {"cpu_fan", "intake"}
    assert cfg.fans["cpu_fan"].pwm_attr == "pwm1"
    assert cfg.fans["intake"].rpm_attr == "fan2_input"
    assert cfg.failsafe.stale_after_s == 5


@pytest.mark.parametrize("name", ["config.example.json", "config.sim.json"])
def test_shipped_examples_are_valid(name):
    cfg = load_config(ROOT / "examples" / name)
    assert cfg.chip == "nct6798"


def test_demo_config_is_valid():
    parse_config(dict(DEMO_CONFIG, sysfs_root="/tmp/x"))


def test_defaults_are_filled_in():
    d = copy.deepcopy(BASE_CONFIG)
    del d["failsafe"], d["interval_s"], d["fans"]["intake"]["min_pwm"]
    cfg = parse_config(d)
    assert cfg.interval_s == 2.0
    assert cfg.sysfs_root == "/sys"
    assert cfg.fans["intake"].min_pwm == 20.0
    assert cfg.web.bind == "127.0.0.1"


def test_unknown_key_is_an_error_not_ignored():
    errs = errors_for(lambda d: d["fans"]["cpu_fan"].update(min_pmw=30))
    assert errs == ["fans.cpu_fan.min_pmw: unknown key"]


def test_missing_required_keys():
    def drop(d):
        del d["fans"]["cpu_fan"]["channel"]
        del d["chip"]

    errs = errors_for(drop)
    assert "config.chip: required" in errs
    assert "fans.cpu_fan.channel: required" in errs


def test_all_problems_are_reported_in_one_pass():
    def many(d):
        d["interval_s"] = 0
        d["fans"]["cpu_fan"]["source"] = "gpu"
        d["fans"]["intake"]["min_pwm"] = 140

    errs = errors_for(many)
    assert len(errs) == 3
    assert any(e.startswith("config.interval_s") for e in errs)
    assert any("'gpu' is not a configured sensor" in e for e in errs)
    assert any(e.startswith("fans.intake.min_pwm") for e in errs)


@pytest.mark.parametrize(
    ("curve", "fragment"),
    [
        ([[40, 20], [40, 50], [70, 100]], "strictly increasing"),
        ([[40, 20], [30, 50], [70, 100]], "strictly increasing"),
        ([[40, 50], [60, 30], [70, 100]], "never slow the fan"),
        ([[40, 20], [60, 40], [70, 90]], "must be 100 percent"),
        ([[40, 100]], "needs 2 to 8 points"),
        ([[t, 100] for t in range(10, 100, 10)], "needs 2 to 8 points"),
        ([[40, 20], [60], [70, 100]], "two numbers"),
        ([[40, 20], ["60", 40], [70, 100]], "two numbers"),
        ([[40, 20], [True, 40], [70, 100]], "two numbers"),
        ([[40, -5], [60, 40], [70, 100]], "outside 0..100"),
        ([[40, 20], [60, 40], [130, 100]], "outside 0..110"),
        ("hot", "must be a list"),
    ],
)
def test_curve_validation(curve, fragment):
    errs = errors_for(lambda d: d["fans"]["cpu_fan"].update(curve=curve))
    assert any(fragment in e for e in errs), errs
    assert all(e.startswith("fans.cpu_fan.curve") for e in errs)


def test_duplicate_channel_is_refused():
    errs = errors_for(lambda d: d["fans"]["intake"].update(channel=1))
    assert errs == ["fans.intake.channel: pwm1 is already driven by fan 'cpu_fan'"]


@pytest.mark.parametrize("channel", [0, 8, "1", 1.0, True])
def test_bad_channel(channel):
    errs = errors_for(lambda d: d["fans"]["cpu_fan"].update(channel=channel))
    assert any("channel" in e for e in errs)


def test_auto_mode_cannot_be_manual():
    errs = errors_for(lambda d: d.update(auto_mode=1))
    assert "1 is manual mode" in errs[0]


def test_watchdog_must_outlast_two_intervals():
    errs = errors_for(lambda d: d.update(interval_s=5, failsafe={"watchdog_s": 8}))
    assert any(e.startswith("failsafe.watchdog_s") for e in errs)


def test_frozen_detection_needs_a_sane_window():
    errs = errors_for(lambda d: d["failsafe"].update(frozen_after_s=2))
    assert any(e.startswith("failsafe.frozen_after_s") for e in errs)


def test_valid_range_must_be_ordered():
    errs = errors_for(lambda d: d["failsafe"].update(min_valid_c=50, max_valid_c=45))
    assert "failsafe: min_valid_c must be below max_valid_c" in errs


def test_sensor_input_must_be_a_temperature():
    errs = errors_for(lambda d: d["sensors"]["cpu"].update(input="fan1_input"))
    assert any("expected a tempN_input" in e for e in errs)


def test_top_level_must_be_an_object():
    with pytest.raises(ConfigError):
        parse_config([1, 2])


def test_update_fan_validates_and_leaves_original_untouched():
    cfg = parse_config(copy.deepcopy(BASE_CONFIG))
    new = update_fan(cfg, "cpu_fan", {"curve": [[30, 30], [80, 100]], "hysteresis_c": 4})
    assert new.fans["cpu_fan"].curve == ((30, 30), (80, 100))
    assert new.fans["cpu_fan"].hysteresis_c == 4
    assert cfg.fans["cpu_fan"].curve[0] == (40, 20)

    with pytest.raises(ConfigError) as exc:
        update_fan(cfg, "cpu_fan", {"curve": [[30, 30], [80, 90]]})
    assert "full speed" in exc.value.errors[0]
    with pytest.raises(ConfigError):
        update_fan(cfg, "cpu_fan", {"channel": 2})
    with pytest.raises(ConfigError):
        update_fan(cfg, "nope", {"min_pwm": 30})
    with pytest.raises(ConfigError):
        update_fan(cfg, "cpu_fan", {})


def test_save_and_load_round_trip(tmp_path):
    cfg = parse_config(copy.deepcopy(BASE_CONFIG))
    path = tmp_path / "etc" / "config.json"
    save_config(cfg, path)
    assert load_config(path) == cfg
    assert json.loads(path.read_text())["fans"]["cpu_fan"]["curve"][0] == [40, 20]
    assert [p.name for p in path.parent.iterdir()] == ["config.json"]


def test_failed_save_keeps_the_old_file(tmp_path, monkeypatch):
    cfg = parse_config(copy.deepcopy(BASE_CONFIG))
    path = tmp_path / "config.json"
    save_config(cfg, path)
    before = path.read_text()

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr("fancurve.config.os.replace", boom)
    new = update_fan(cfg, "cpu_fan", {"min_pwm": 50})
    with pytest.raises(OSError):
        save_config(new, path)
    assert path.read_text() == before
    assert [p.name for p in tmp_path.iterdir()] == ["config.json"]


def test_load_reports_json_errors_with_line(tmp_path):
    p = tmp_path / "c.json"
    p.write_text('{\n "chip": "x"\n "fans": {}\n}')
    with pytest.raises(ConfigError) as exc:
        load_config(p)
    assert "line 3" in exc.value.errors[0]


def test_load_reports_unreadable_file(tmp_path):
    with pytest.raises(ConfigError) as exc:
        load_config(tmp_path / "missing.json")
    assert "cannot read" in exc.value.errors[0]


def test_to_dict_round_trips_through_parse():
    cfg = parse_config(copy.deepcopy(BASE_CONFIG))
    assert parse_config(config_to_dict(cfg)) == cfg
