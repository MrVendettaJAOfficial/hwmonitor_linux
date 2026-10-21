#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Linux HWMonitor - main entry point."""
from __future__ import annotations

import argparse
import os
import signal
import sys
import time

import hwmonitor_core as core


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog=core.APP_SLUG,
        description=f"{core.APP_NAME} - hardware temperature and sensor monitor",
    )
    p.add_argument("--version", action="version",
                   version=f"{core.APP_NAME} {core.__version__}")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("-c", "--cli", action="store_true",
                      help="print one sensor snapshot and exit")
    mode.add_argument("-j", "--json", action="store_true",
                      help="print a JSON snapshot and exit")
    mode.add_argument("-w", "--watch", action="store_true",
                      help="live terminal mode")
    mode.add_argument("-d", "--daemon", action="store_true",
                      help="run headless alert daemon")
    p.add_argument("--minimized", action="store_true")
    p.add_argument("--fahrenheit", action="store_true")
    p.add_argument("--interval", type=float, default=None)
    p.add_argument("--no-notify", action="store_true")
    return p


def _apply(settings: core.Settings, args) -> None:
    if args.fahrenheit:
        settings["fahrenheit"] = True
    if args.interval is not None:
        settings["interval"] = max(0.5, float(args.interval))
    if args.no_notify:
        settings["notifications"] = False


def run_cli(settings: core.Settings) -> int:
    sensors = core.SensorReader().read_all()
    if not sensors:
        print("No sensors found.", file=sys.stderr)
        return 1
    sys.stdout.write(core.format_report(sensors, settings["fahrenheit"]))
    return 0


def run_json(settings: core.Settings) -> int:
    sensors = core.SensorReader().read_all()
    print(core.sensors_to_json(sensors))
    return 0


def run_watch(settings: core.Settings) -> int:
    monitor = core.Monitor(settings)
    interval = float(settings["interval"])
    try:
        while True:
            sensors = monitor.poll_once()
            os.system("clear")
            sys.stdout.write(core.format_report(sensors, settings["fahrenheit"]))
            sys.stdout.write(f"\nRefreshing every {interval:.1f}s - Ctrl+C to quit\n")
            sys.stdout.flush()
            time.sleep(interval)
    except KeyboardInterrupt:
        return 0


def run_daemon(settings: core.Settings) -> int:
    stop = {"now": False}

    def _sig(_s, _f):
        stop["now"] = True

    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)

    monitor = core.Monitor(settings)
    monitor.start()
    sys.stderr.write(f"[{core.APP_SLUG}] daemon started "
                     f"(interval={settings['interval']}s)\n")
    while not stop["now"]:
        time.sleep(0.5)
    monitor.stop()
    sys.stderr.write(f"[{core.APP_SLUG}] daemon stopped\n")
    return 0


def run_gui(settings: core.Settings, minimized: bool) -> int:
    try:
        import hwmonitor_gui
    except ImportError as exc:
        sys.stderr.write(f"GUI unavailable ({exc}); falling back to --watch.\n")
        return run_watch(settings)
    return hwmonitor_gui.main(settings, start_minimized=minimized)


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    settings = core.Settings()
    _apply(settings, args)
    if args.cli:
        return run_cli(settings)
    if args.json:
        return run_json(settings)
    if args.watch:
        return run_watch(settings)
    if args.daemon:
        return run_daemon(settings)
    return run_gui(settings, minimized=args.minimized)


if __name__ == "__main__":
    sys.exit(main())
