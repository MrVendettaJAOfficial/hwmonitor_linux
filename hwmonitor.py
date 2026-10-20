#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Linux HWMonitor - main entry point.

Modes:
  (default)      launch the GTK4 GUI (or fall back to terminal)
  --cli / -c     print a snapshot report and exit
  --json / -j    print a JSON snapshot and exit
  --watch / -w   live terminal mode
  --daemon       headless alert daemon (no GUI)
  --minimized    start minimized to tray (used by autostart)
  --version      print version
"""
from __future__ import annotations

import argparse
import os
import signal
import sys
import time

import hwmonitor_core as core


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog=core.APP_ID,
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
                      help="live terminal mode (Ctrl+C to quit)")
    mode.add_argument("-d", "--daemon", action="store_true",
                      help="run headless (no GUI) as an alert daemon")
    p.add_argument("--minimized", action="store_true",
                   help="start minimized to the tray icon")
    p.add_argument("--fahrenheit", action="store_true",
                   help="show temperatures in Fahrenheit")
    p.add_argument("--interval", type=float, default=None,
                   help="override the sampling interval in seconds")
    p.add_argument("--no-notify", action="store_true",
                   help="disable desktop notifications")
    return p


def _apply_cli_overrides(settings: core.Settings, args: argparse.Namespace) -> None:
    if args.fahrenheit:
        settings["fahrenheit"] = True
    if args.interval is not None:
        settings["interval"] = max(0.5, float(args.interval))
    if args.no_notify:
        settings["notifications"] = False


def run_cli(settings: core.Settings) -> int:
    reader = core.SensorReader()
    sensors = reader.read_all()
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
            os.system("clear" if os.environ.get("TERM") else "")
            sys.stdout.write(core.format_report(sensors, settings["fahrenheit"]))
            sys.stdout.write(f"\nRefreshing every {interval:.1f}s - Ctrl+C to quit\n")
            sys.stdout.flush()
            time.sleep(interval)
    except KeyboardInterrupt:
        return 0


def run_daemon(settings: core.Settings) -> int:
    """Headless alert daemon: sample forever, emit notifications."""
    stop = {"now": False}

    def _sig(_signum, _frame):
        stop["now"] = True

    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)

    monitor = core.Monitor(settings)
    monitor.start()
    sys.stderr.write(f"[{core.APP_ID}] daemon started "
                     f"(interval={settings['interval']}s)\n")
    while not stop["now"]:
        time.sleep(0.5)
    monitor.stop()
    sys.stderr.write(f"[{core.APP_ID}] daemon stopped\n")
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
    _apply_cli_overrides(settings, args)

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
