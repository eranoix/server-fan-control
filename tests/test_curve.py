import pytest

from fancurve.config import validate_curve
from fancurve.curve import PRESETS, Hysteresis, interpolate, pct_to_raw, raw_to_pct

CURVE = [(40, 20), (60, 40), (70, 70), (80, 100)]


@pytest.mark.parametrize(
    ("temp", "expected"),
    [
        (0, 20),
        (40, 20),
        (50, 30),
        (60, 40),
        (65, 55),
        (75, 85),
        (80, 100),
        (120, 100),
    ],
)
def test_interpolate(temp, expected):
    assert interpolate(CURVE, temp) == pytest.approx(expected)


def test_interpolate_two_points_and_fractional_temps():
    assert interpolate([(30, 0), (70, 100)], 45.5) == pytest.approx(38.75)


def test_interpolate_is_monotonic_for_a_valid_curve():
    values = [interpolate(CURVE, t / 10) for t in range(300, 900)]
    assert values == sorted(values)


def test_interpolate_rejects_empty_curve():
    with pytest.raises(ValueError):
        interpolate([], 50)


@pytest.mark.parametrize("name", sorted(PRESETS))
def test_every_preset_passes_validation(name):
    assert validate_curve([list(p) for p in PRESETS[name]], name) == []


def test_pct_raw_conversions():
    assert pct_to_raw(0) == 0
    assert pct_to_raw(100) == 255
    assert pct_to_raw(30) == 76
    assert pct_to_raw(150) == 255
    assert pct_to_raw(-5) == 0
    assert raw_to_pct(255) == 100.0


class TestHysteresis:
    def test_rising_passes_through_immediately(self):
        h = Hysteresis(3)
        assert h.update(50) == 50
        assert h.update(55) == 55
        assert h.update(55.4) == 55.4

    def test_small_drop_is_ignored(self):
        h = Hysteresis(3)
        h.update(60)
        assert h.update(58) == 60
        assert h.update(57.1) == 60

    def test_drop_beyond_width_follows_with_offset(self):
        h = Hysteresis(3)
        h.update(60)
        assert h.update(56) == 59
        assert h.update(55) == 58
        assert h.update(57) == 58

    def test_wobbling_sensor_does_not_move_the_output(self):
        h = Hysteresis(2)
        h.update(65.5)
        outputs = {h.update(t) for t in [64.5, 65.0, 64.6, 65.5, 64.8] * 5}
        assert outputs == {65.5}

    def test_zero_width_is_a_passthrough(self):
        h = Hysteresis(0)
        for t in [50, 49, 51, 40]:
            assert h.update(t) == t

    def test_reset_forgets_the_reference(self):
        h = Hysteresis(5)
        h.update(80)
        h.reset()
        assert h.effective is None
        assert h.update(40) == 40

    def test_negative_width_is_refused(self):
        with pytest.raises(ValueError):
            Hysteresis(-1)
