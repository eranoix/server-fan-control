"""One command demo: simulator, controller and dashboard in one process.

    python -m fancurve.demo            # then open http://127.0.0.1:8790/

Nothing here touches the real /sys. The fake tree and the demo config live
in a temporary directory that is removed on exit.
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

from .__main__ import serve
from .config import parse_config, save_config
from .server import DemoHooks
from .sim import Simulator

DEMO_CONFIG = {
    "chip": "nct6798",
    "interval_s": 1.0,
    "auto_mode": 5,
    "sensors": {
        "cpu": {"chip": "nct6798", "input": "temp13_input", "label": "CPU (TSI0)"},
        "system": {"chip": "nct6798", "input": "temp1_input", "label": "Board (SYSTIN)"},
        "nvme": {"chip": "nvme", "input": "temp1_input", "label": "NVMe SSD"},
        "gpu": {"chip": "amdgpu", "input": "temp1_input", "label": "iGPU"},
    },
    "fans": {
        "cpu_fan": {
            "label": "CPU cooler",
            "channel": 1,
            "source": "cpu",
            "curve": [[40, 25], [60, 40], [72, 65], [82, 100]],
            "min_pwm": 25,
            "hysteresis_c": 3,
        },
        "intake": {
            "label": "Front intake",
            "channel": 2,
            "source": "nvme",
            "curve": [[35, 28], [50, 40], [62, 70], [72, 100]],
            "min_pwm": 28,
            "hysteresis_c": 2,
        },
        "exhaust": {
            "label": "Rear exhaust",
            "channel": 3,
            "source": "system",
            "curve": [[28, 25], [36, 40], [44, 70], [52, 100]],
            "min_pwm": 25,
            "hysteresis_c": 1.5,
        },
    },
    # The simulator adds sensor noise, so a value that stops moving for 30 s
    # is a dead sensor there. On real hardware this check is opt-in.
    "failsafe": {
        "stale_after_s": 5,
        "frozen_after_s": 30,
        "min_valid_c": 0,
        "max_valid_c": 115,
        "watchdog_s": 5,
    },
}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m fancurve.demo", description=__doc__.splitlines()[0]
    )
    ap.add_argument("--bind", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8790)
    ap.add_argument("--speed", type=float, default=1.0, help="simulated seconds per real second")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--duration", type=float, default=0, help="stop after N seconds (0 = never)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")

    work = Path(tempfile.mkdtemp(prefix="fancurve-demo-"))
    try:
        sim = Simulator(work / "sysfs", seed=args.seed, speed=args.speed)
        sim.build()
        data = dict(
            DEMO_CONFIG,
            sysfs_root=str(sim.root),
            state_file=str(work / "state.json"),
            web={"bind": args.bind, "port": args.port, "token_file": None},
        )
        cfg = parse_config(json.loads(json.dumps(data)))
        cfg_path = work / "config.json"
        save_config(cfg, cfg_path)

        stop = threading.Event()
        sim_thread = threading.Thread(target=sim.run, args=(stop,), name="sim", daemon=True)
        sim_thread.start()

        def ready(url: str) -> None:
            print(f"\n  server-fan-control demo running on {url}")
            print(f"  fake sysfs tree: {sim.root}")
            print(
                "  Ctrl-C to stop; the chip is handed back to automatic mode on exit.\n", flush=True
            )
            if args.duration:
                threading.Timer(args.duration, stop.set).start()

        hooks = DemoHooks(
            set_workload=sim.set_workload, set_fault=sim.set_fault, summary=sim.summary
        )
        started = time.monotonic()
        ctl = serve(cfg, cfg_path, demo=hooks, stop=stop, on_ready=ready)
        stop.set()
        sim_thread.join(timeout=2)

        nct = sim.chip_dir("nct6798")
        modes = {f"pwm{c}_enable": (nct / f"pwm{c}_enable").read_text().strip() for c in (1, 2, 3)}
        print(f"\n  stopped after {time.monotonic() - started:.0f}s, {ctl.ticks} control ticks")
        print(f"  chip handed back to automatic mode: {modes}")
        return 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
