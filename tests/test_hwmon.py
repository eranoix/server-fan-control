import pytest

from fancurve.hwmon import Hwmon, HwmonError


def test_chips_are_found_by_name_not_by_number(sysfs):
    hw = Hwmon(sysfs.root)
    assert hw.find("nct6798") == sysfs.nct
    assert hw.find("nvme") == sysfs.nvme
    assert hw.find("k10temp") is None
    assert hw.chips() == {"hwmon1": "nvme", "hwmon3": "nct6798"}


def test_cache_is_revalidated_after_renumbering(sysfs):
    hw = Hwmon(sysfs.root)
    assert hw.find("nct6798") == sysfs.nct
    # driver reload: the chip comes back under another number
    moved = sysfs.nct.rename(sysfs.base / "hwmon7")
    assert hw.find("nct6798") == moved


def test_read_temp_ok(sysfs):
    sysfs.set_temp("cpu", 47.125)
    r = Hwmon(sysfs.root).read_temp("nct6798", "temp13_input")
    assert r.ok and r.value == pytest.approx(47.125)


def test_read_temp_missing_attribute(sysfs):
    sysfs.temp_path("cpu").unlink()
    r = Hwmon(sysfs.root).read_temp("nct6798", "temp13_input")
    assert r.status == "missing" and r.value is None


def test_read_temp_missing_chip(sysfs):
    r = Hwmon(sysfs.root).read_temp("amdgpu", "temp1_input")
    assert r.status == "missing"
    assert "not found" in r.detail


@pytest.mark.parametrize("content", ["N/A", "", "47.5", "0x2f"])
def test_read_temp_garbage(sysfs, content):
    sysfs.temp_path("cpu").write_text(content)
    r = Hwmon(sysfs.root).read_temp("nct6798", "temp13_input")
    assert r.status == "garbage"


@pytest.mark.parametrize("milli", [-55000, 127000])
def test_read_temp_out_of_range(sysfs, milli):
    sysfs.write(sysfs.temp_path("cpu"), milli)
    r = Hwmon(sysfs.root).read_temp("nct6798", "temp13_input", 0, 120)
    assert r.status == "out_of_range"
    assert r.value == milli / 1000


def test_read_temp_io_error(sysfs):
    p = sysfs.temp_path("cpu")
    p.unlink()
    p.mkdir()  # reading a directory raises an OSError that is not ENOENT
    r = Hwmon(sysfs.root).read_temp("nct6798", "temp13_input")
    assert r.status == "io_error"


def test_write_refuses_to_create_attributes(sysfs):
    hw = Hwmon(sysfs.root)
    with pytest.raises(HwmonError):
        hw.write_int("nct6798", "pwm9", 100)
    assert not (sysfs.nct / "pwm9").exists()


def test_write_and_read_int(sysfs):
    hw = Hwmon(sysfs.root)
    hw.write_int("nct6798", "pwm1", 200)
    assert hw.read_int("nct6798", "pwm1") == 200
    assert hw.read_int_or_none("nct6798", "fan9_input") is None
    with pytest.raises(HwmonError):
        hw.read_int("amdgpu", "pwm1")
