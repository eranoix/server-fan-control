import json
import threading
import time
import urllib.request
from pathlib import Path

from fancurve import demo
from fancurve.__main__ import main
from fancurve.sim import Simulator

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "config.sim.json"


def sim_config(tmp_path):
    sim = Simulator(tmp_path / "sysfs", seed=1)
    sim.build()
    cfg = json.loads(EXAMPLE.read_text())
    cfg["sysfs_root"] = str(sim.root)
    cfg["state_file"] = str(tmp_path / "state.json")
    p = tmp_path / "config.json"
    p.write_text(json.dumps(cfg))
    return sim, p


def test_check_reports_a_healthy_tree(tmp_path, capsys):
    _, p = sim_config(tmp_path)
    assert main(["check", "-c", str(p)]) == 0
    out = capsys.readouterr().out
    assert "config OK: 4 sensors, 3 fans" in out
    assert "controlled chip 'nct6798': found" in out


def test_check_fails_when_the_chip_is_absent(tmp_path, capsys):
    _, p = sim_config(tmp_path)
    assert main(["check", "-c", str(p), "--sysfs-root", str(tmp_path / "empty")]) == 1


def test_invalid_config_exits_2(tmp_path, capsys):
    p = tmp_path / "bad.json"
    p.write_text('{"chip": "x", "sensors": {}, "fans": {}}')
    assert main(["check", "-c", str(p)]) == 2
    assert "config.sensors" in capsys.readouterr().err


def test_restore_command(tmp_path):
    sim, p = sim_config(tmp_path)
    nct = sim.chip_dir("nct6798")
    (nct / "pwm1_enable").write_text("1\n")
    assert main(["restore", "-c", str(p)]) == 0
    assert (nct / "pwm1_enable").read_text().strip() == "5"


def test_demo_runs_serves_and_hands_the_chip_back(capsys):
    port = 18000 + int(time.time()) % 1000
    result = {}
    t = threading.Thread(
        target=lambda: result.setdefault("rc", demo.main(["--port", str(port), "--duration", "4"]))
    )
    t.start()
    status = None
    for _ in range(40):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=1) as r:
                status = json.loads(r.read())
            if status["loop"]["ticks"] > 1:
                break
        except OSError:
            pass
        time.sleep(0.2)
    t.join(timeout=15)
    assert result.get("rc") == 0
    assert status is not None and status["state"] == "controlling"
    assert status["demo"]["workload"] == "cycle"
    out = capsys.readouterr().out
    assert "'pwm1_enable': '5'" in out
