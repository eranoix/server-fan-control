import json
import threading
import urllib.error
import urllib.request

import pytest

from fancurve.config import load_config, parse_config, save_config
from fancurve.controller import Controller
from fancurve.hwmon import Hwmon
from fancurve.server import App, DemoHooks, make_server


@pytest.fixture
def served(raw_config, sysfs, tmp_path):
    started = []

    def _serve(token=None, demo=None):
        cfg = parse_config(raw_config)
        path = tmp_path / "config.json"
        save_config(cfg, path)
        ctl = Controller(cfg, Hwmon(sysfs.root))
        ctl.tick()
        app = App(ctl, config_path=path, token=token, demo=demo)
        srv = make_server(app, "127.0.0.1", 0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        started.append(srv)
        return f"http://127.0.0.1:{srv.server_address[1]}", app, path

    yield _serve
    for srv in started:
        srv.shutdown()
        srv.server_close()


def call(url, method="GET", body=None, headers=None, raw=None):
    data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
    h = {"Content-Type": "application/json"} if data is not None else {}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read() or b"null"), r.headers
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null"), e.headers


def test_status_config_history(served):
    base, _, _ = served()
    code, st, _ = call(base + "/api/status")
    assert code == 200 and st["state"] == "controlling"
    assert set(st["fans"]) == {"cpu_fan", "intake"}
    assert st["demo"] is None
    code, cfg, _ = call(base + "/api/config")
    assert code == 200 and "quiet" in cfg["presets"] and cfg["demo"] is False
    code, hist, _ = call(base + "/api/history?since=0")
    assert code == 200 and len(hist["samples"]) == 1
    code, hist, _ = call(base + "/api/history?since=9999999999")
    assert hist["samples"] == []
    assert call(base + "/api/history?since=abc")[0] == 400


def test_dashboard_is_served_with_a_strict_csp(served):
    base, _, _ = served()
    with urllib.request.urlopen(base + "/") as r:
        body = r.read().decode()
        assert "<title>server-fan-control</title>" in body
        assert "default-src 'self'" in r.headers["Content-Security-Policy"]
    with urllib.request.urlopen(base + "/app.js") as r:
        assert "javascript" in r.headers["Content-Type"]
    assert call(base + "/healthz")[1]["ok"] is True
    assert call(base + "/nope")[0] == 404


def test_put_curve_applies_and_persists(served, sysfs):
    base, app, path = served()
    curve = [[30, 40], [50, 60], [60, 80], [70, 100]]
    code, res, _ = call(base + "/api/fans/cpu_fan", "PUT", {"curve": curve, "min_pwm": 35})
    assert code == 200 and res["fan"]["curve"] == curve
    assert load_config(path).fans["cpu_fan"].min_pwm == 35
    assert app.controller.config.fans["cpu_fan"].curve[0] == (30, 40)
    # applied immediately: 40 C on the new curve is 50 percent
    assert app.controller.fans["cpu_fan"].target_pct == 50


def test_invalid_curve_is_rejected_and_nothing_changes(served):
    base, app, path = served()
    before = path.read_text()
    code, res, _ = call(base + "/api/fans/cpu_fan", "PUT", {"curve": [[30, 90], [60, 40]]})
    assert code == 400
    assert any("never slow the fan" in e for e in res["errors"])
    assert any("full speed" in e for e in res["errors"])
    assert path.read_text() == before
    assert app.controller.config.fans["cpu_fan"].curve[0] == (40, 20)


@pytest.mark.parametrize(
    ("path", "body", "code"),
    [
        ("/api/fans/ghost", {"min_pwm": 30}, 400),
        ("/api/fans/cpu_fan", {"channel": 3}, 400),
        ("/api/fans/cpu_fan", {"source": "gpu"}, 400),
        ("/api/fans/cpu_fan", [1, 2], 400),
        ("/api/other", {}, 404),
    ],
)
def test_put_errors(served, path, body, code):
    base, _, _ = served()
    assert call(base + path, "PUT", body)[0] == code


def test_body_must_be_json_and_small(served):
    base, _, _ = served()
    assert call(base + "/api/fans/cpu_fan", "PUT", raw=b"{not json")[0] == 400
    code, res, _ = call(
        base + "/api/fans/cpu_fan", "PUT", raw=b"{}", headers={"Content-Type": "text/plain"}
    )
    assert code == 400 and "Content-Type" in res["error"]
    code, res, _ = call(base + "/api/fans/cpu_fan", "PUT", raw=b" " * (70 * 1024))
    assert code == 400 and "too large" in res["error"]


def test_preset(served):
    base, _, _ = served()
    code, res, _ = call(base + "/api/fans/intake/preset", "POST", {"preset": "performance"})
    assert code == 200 and res["fan"]["curve"][0] == [30, 50]
    assert call(base + "/api/fans/intake/preset", "POST", {"preset": "turbo"})[0] == 400


def test_token_is_required_when_configured(served):
    base, _, _ = served(token="s3cret-token-for-tests")
    assert call(base + "/api/status")[0] == 401
    assert call(base + "/api/status", headers={"Authorization": "Bearer wrong"})[0] == 401
    ok = {"Authorization": "Bearer s3cret-token-for-tests"}
    assert call(base + "/api/status", headers=ok)[0] == 200
    assert call(base + "/api/fans/cpu_fan", "PUT", {"min_pwm": 40})[0] == 401
    assert call(base + "/healthz")[0] == 200  # liveness stays open
    with urllib.request.urlopen(base + "/") as r:  # the page itself is static
        assert r.status == 200


def test_demo_routes_only_exist_in_demo_mode(served):
    base, _, _ = served()
    assert call(base + "/api/demo/crash", "POST", {})[0] == 404


def test_demo_routes(served, sysfs):
    calls = []
    hooks = DemoHooks(
        set_workload=lambda w: calls.append(("w", w)),
        set_fault=lambda s, f: calls.append(("f", s, f)),
        summary=lambda: {"phase": "idle"},
    )
    base, app, _ = served(demo=hooks)
    assert call(base + "/api/demo/workload", "POST", {"workload": "stress"})[0] == 200
    assert call(base + "/api/demo/fault", "POST", {"sensor": "nvme", "fault": "missing"})[0] == 200
    assert call(base + "/api/demo/fault", "POST", {"sensor": "nvme", "fault": None})[0] == 200
    assert calls == [("w", "stress"), ("f", "nvme", "missing"), ("f", "nvme", None)]
    assert call(base + "/api/demo/crash", "POST", {})[0] == 200
    app.controller.tick()
    assert sysfs.pwm(1) == 255
    assert call(base + "/api/status")[1]["demo"] == {"phase": "idle"}
