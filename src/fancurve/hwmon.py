"""Access to Linux hwmon through sysfs, rooted anywhere.

The kernel exposes every monitoring chip as ``/sys/class/hwmon/hwmonN`` with a
``name`` file and a flat set of attribute files:

* ``tempN_input``  temperature in millidegrees Celsius
* ``fanN_input``   tachometer reading in RPM
* ``pwmN``         duty cycle, 0..255
* ``pwmN_enable``  control mode (for the nct6775 family: 0 = full speed,
                   1 = manual, 2..5 = the chip's own automatic modes)

The ``hwmonN`` numbers are assigned at probe time and can change between
boots, so chips are always located by ``name``. The root is configurable,
which is what lets the controller, the tests and the simulator all run
against a fake tree in a temporary directory.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


class HwmonError(OSError):
    """A read or write against a hwmon attribute failed."""


@dataclass(frozen=True)
class Reading:
    """One temperature read, with the reason when it is not usable."""

    value: float | None
    status: str
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "ok"


class Hwmon:
    def __init__(self, sysfs_root: str | Path = "/sys") -> None:
        self.sysfs_root = Path(sysfs_root)
        self.base = self.sysfs_root / "class" / "hwmon"
        self._cache: dict[str, Path] = {}

    def chips(self) -> dict[str, str]:
        """Map of ``hwmonN`` directory to chip name, for diagnostics."""
        out: dict[str, str] = {}
        for d in sorted(self.base.glob("hwmon*")):
            try:
                out[d.name] = (d / "name").read_text().strip()
            except OSError:
                continue
        return out

    def find(self, chip: str) -> Path | None:
        """Directory of the chip called ``chip``, or None when absent.

        The cached path is re-validated on every call: after a driver reload
        the same ``hwmonN`` can belong to a different chip.
        """
        cached = self._cache.get(chip)
        if cached is not None and _name_of(cached) == chip:
            return cached
        self._cache.pop(chip, None)
        for d in sorted(self.base.glob("hwmon*")):
            if _name_of(d) == chip:
                self._cache[chip] = d
                return d
        return None

    def read_temp(
        self, chip: str, attr: str, min_valid: float = -273.0, max_valid: float = 1000.0
    ) -> Reading:
        d = self.find(chip)
        if d is None:
            return Reading(None, "missing", f"chip {chip!r} not found")
        path = d / attr
        try:
            raw = path.read_text()
        except FileNotFoundError:
            return Reading(None, "missing", f"{chip}/{attr} does not exist")
        except OSError as exc:
            return Reading(None, "io_error", f"{chip}/{attr}: {exc.strerror or exc}")
        text = raw.strip()
        try:
            milli = int(text)
        except ValueError:
            shown = text[:20] if text else "<empty>"
            return Reading(None, "garbage", f"{chip}/{attr} returned {shown!r}")
        value = milli / 1000.0
        if not (min_valid <= value <= max_valid):
            return Reading(
                value,
                "out_of_range",
                f"{chip}/{attr} reads {value:.1f} C, outside {min_valid:g}..{max_valid:g}",
            )
        return Reading(value, "ok")

    def read_int(self, chip: str, attr: str) -> int:
        d = self.find(chip)
        if d is None:
            raise HwmonError(f"chip {chip!r} not found")
        try:
            return int((d / attr).read_text().strip())
        except (OSError, ValueError) as exc:
            raise HwmonError(f"{chip}/{attr}: {exc}") from exc

    def read_int_or_none(self, chip: str, attr: str) -> int | None:
        try:
            return self.read_int(chip, attr)
        except HwmonError:
            return None

    def write_int(self, chip: str, attr: str, value: int) -> None:
        d = self.find(chip)
        if d is None:
            raise HwmonError(f"chip {chip!r} not found")
        path = d / attr
        if not path.exists():
            raise HwmonError(f"{chip}/{attr} does not exist")
        try:
            with open(path, "w") as fh:
                fh.write(f"{int(value)}\n")
        except OSError as exc:
            raise HwmonError(f"{chip}/{attr}: {exc}") from exc


def _name_of(d: Path) -> str | None:
    try:
        return (d / "name").read_text().strip()
    except OSError:
        return None
