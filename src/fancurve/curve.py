"""Fan curves: piecewise linear interpolation, hysteresis and presets.

Everything here is pure. Temperatures are degrees Celsius, duty cycles are
percent (0 to 100). Conversion to the 0..255 range the kernel wants happens at
the edge, in the controller.
"""

from __future__ import annotations

from collections.abc import Sequence

Point = tuple[float, float]

# Shipped presets. Every one of them ends at 100 percent, because config
# validation refuses a curve that never reaches full speed.
PRESETS: dict[str, tuple[Point, ...]] = {
    "quiet": ((30, 30), (55, 35), (70, 55), (82, 100)),
    "balanced": ((30, 35), (50, 45), (65, 70), (78, 100)),
    "performance": ((30, 50), (45, 65), (60, 85), (70, 100)),
    "full": ((30, 85), (40, 92), (50, 100), (60, 100)),
}


def interpolate(points: Sequence[Sequence[float]], temp: float) -> float:
    """Duty cycle for ``temp`` on a curve given as ``[(temp, pct), ...]``.

    Below the first point the curve is flat at the first duty cycle, above the
    last point it is flat at the last one. Between points it is linear.
    ``points`` must be sorted by temperature with strictly increasing
    temperatures; config validation guarantees that for anything loaded from
    disk or the API.
    """
    if not points:
        raise ValueError("a curve needs at least one point")
    first_t, first_p = points[0]
    if temp <= first_t:
        return float(first_p)
    last_t, last_p = points[-1]
    if temp >= last_t:
        return float(last_p)
    for (t0, p0), (t1, p1) in zip(points, points[1:], strict=False):
        if t0 <= temp <= t1:
            return p0 + (p1 - p0) * (temp - t0) / (t1 - t0)
    # Unreachable for a sorted curve; fall back to the safe end.
    return float(last_p)


class Hysteresis:
    """Backlash (dead band) on the temperature that drives a curve.

    Rising temperatures pass through immediately, so the fan speeds up as soon
    as it is needed. Falling temperatures only pass through once they have
    dropped ``width`` degrees below the value that set the current speed. The
    result: a sensor wobbling between 64.5 and 65.5 no longer makes the fan
    hunt up and down, but a real cool down still slows it.
    """

    def __init__(self, width: float) -> None:
        if width < 0:
            raise ValueError("hysteresis width must be >= 0")
        self.width = float(width)
        self._effective: float | None = None

    @property
    def effective(self) -> float | None:
        return self._effective

    def update(self, temp: float) -> float:
        if self._effective is None or temp >= self._effective:
            self._effective = temp
        elif temp < self._effective - self.width:
            self._effective = temp + self.width
        return self._effective

    def reset(self) -> None:
        self._effective = None


def pct_to_raw(pct: float) -> int:
    """Percent duty cycle to the kernel's 0..255 PWM value."""
    return max(0, min(255, round(pct * 255 / 100)))


def raw_to_pct(raw: int) -> float:
    return round(raw * 100 / 255, 1)
