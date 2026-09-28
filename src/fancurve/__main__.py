"""Command line: ``python -m fancurve {run,check,restore}``."""

from __future__ import annotations

import argparse
import atexit
import contextlib
import logging
import signal
import sys
import threading
from dataclasses import replace
from pathlib import Path

from .config import Config, ConfigError, load_config
from .controller import Controller, restore_from_state
from .hwmon import Hwmon, HwmonError
from .server import App, DemoHooks, make_server

log = logging.getLogger("fancurve")


def read_token(cfg: Config) -> str | None:
    if not cfg.web.token_file:
        return None
    try:
        token = Path(cfg.web.token_file).read_text().strip()
    except OSError as exc:
        raise SystemExit(f"cannot read token file {cfg.web.token_file}: {exc}") from exc
    if len(token) < 16:
        raise SystemExit("token file must hold at least 16 characters")
    return token


def serve(
    cfg: Config,
    config_path: Path | None,
    demo: DemoHooks | None = None,
    stop: threading.Event | None = None,
    on_ready=None,
) -> Controller:
    """Run controller, watchdog and web server until ``stop`` is set.

    Whatever ends the run (signal, Ctrl-C, an exception escaping a thread
    join) goes through ``finally``, which releases the chip. ``atexit`` is a
    second net for paths that skip ``finally``, such as ``sys.exit`` from a
    signal handler in another library.
    """
    stop = stop or threading.Event()
    ctl = Controller(cfg)
    atexit.register(ctl.release)
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        with contextlib.suppress(ValueError):
            signal.signal(sig, lambda *_: stop.set())

    app = App(ctl, config_path=config_path, token=read_token(cfg), demo=demo)
    srv = make_server(app, cfg.web.bind, cfg.web.port)
    threads = [
        threading.Thread(target=ctl.run, args=(stop,), name="control", daemon=True),
        threading.Thread(target=ctl.run_watchdog, args=(stop,), name="watchdog", daemon=True),
        threading.Thread(target=srv.serve_forever, name="http", daemon=True),
    ]
    try:
        for t in threads:
            t.start()
        host, port = srv.server_address[:2]
        log.info("dashboard on http://%s:%s/", host, port)
        if on_ready:
            on_ready(f"http://{host}:{port}/")
        while not stop.wait(0.5):
            pass
    finally:
        stop.set()
        srv.shutdown()
        srv.server_close()
        threads[0].join(timeout=cfg.interval_s + 2)
        ctl.release()
    return ctl


def cmd_run(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    if args.sysfs_root:
        cfg = replace(cfg, sysfs_root=args.sysfs_root)
    if args.bind or args.port:
        cfg = replace(
            cfg,
            web=replace(cfg.web, bind=args.bind or cfg.web.bind, port=args.port or cfg.web.port),
        )
    serve(cfg, Path(args.config))
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    """Validate the config and show what the controller would see. Writes nothing."""
    cfg = load_config(args.config)
    if args.sysfs_root:
        cfg = replace(cfg, sysfs_root=args.sysfs_root)
    hw = Hwmon(cfg.sysfs_root)
    print(f"config OK: {len(cfg.sensors)} sensors, {len(cfg.fans)} fans")
    print(f"chips under {hw.base}:")
    for d, name in hw.chips().items() or {"(none)": ""}.items():
        print(f"  {d:8} {name}")
    ok = hw.find(cfg.chip) is not None
    print(f"controlled chip {cfg.chip!r}: {'found' if ok else 'NOT FOUND'}")
    fs = cfg.failsafe
    for s in cfg.sensors.values():
        r = hw.read_temp(s.chip, s.input, fs.min_valid_c, fs.max_valid_c)
        shown = f"{r.value:.1f} C" if r.value is not None else "-"
        print(f"  sensor {s.id:8} {shown:>9}  {r.status}{'  ' + r.detail if r.detail else ''}")
    for f in cfg.fans.values():
        mode = hw.read_int_or_none(cfg.chip, f.enable_attr)
        rpm = hw.read_int_or_none(cfg.chip, f.rpm_attr)
        print(f"  fan {f.id:10} pwm{f.channel} mode={mode} rpm={rpm} source={f.source}")
    return 0 if ok else 1


def cmd_restore(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    if args.sysfs_root:
        cfg = replace(cfg, sysfs_root=args.sysfs_root)
    try:
        for line in restore_from_state(cfg):
            print(line)
    except HwmonError as exc:
        print(f"restore failed: {exc}", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="fancurve", description="Fan curve controller for hwmon.")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, helptext in (
        ("run", "take control of the fans and serve the dashboard"),
        ("check", "validate the config and print what the controller would read"),
        ("restore", "hand the fans back to automatic mode (for ExecStopPost)"),
    ):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("-c", "--config", required=True)
        p.add_argument("--sysfs-root", help="override sysfs_root, e.g. a simulator tree")
        if name == "run":
            p.add_argument("--bind")
            p.add_argument("--port", type=int)
    args = ap.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
    )
    try:
        return {"run": cmd_run, "check": cmd_check, "restore": cmd_restore}[args.cmd](args)
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
