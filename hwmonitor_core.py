#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Linux HWMonitor - sensor backend, alert engine and settings.

This module has NO GUI dependencies, so it can be used by the GTK front end,
by the terminal mode and by the headless ``--daemon`` mode.

Data sources
------------
* /sys/class/hwmon/*          temperatures, fans, voltages, power, currents
* /sys/class/thermal/*        thermal-zone fallback
* /proc/stat                  CPU utilisation (total + per thread)
* /sys/devices/system/cpu     CPU clocks
* /sys/class/powercap/*       Intel/AMD RAPL package power (may need root)
* /proc/meminfo               memory / swap
* /sys/class/power_supply     battery
* nvidia-smi                  NVIDIA GPUs (proprietary driver)
"""
from __future__ import annotations

import copy
import csv
import glob
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional

__version__ = "1.0.0"
APP_ID = "io.github.linux_hwmonitor"
APP_SLUG = "linux-hwmonitor"

# Tests point this at a fake sysfs/procfs tree.
SYSROOT = os.environ.get("HWMONITOR_SYSROOT", "/")


def _p(path: str) -> str:
    """Map an absolute system path below SYSROOT (only used by the tests)."""
    if SYSROOT in ("", "/"):
        return path
    return os.path.join(SYSROOT, path.lstrip("/"))


# --------------------------------------------------------------------------
# Small file helpers
# --------------------------------------------------------------------------
def _read(path: str) -> Optional[str]:
    try:
        with open(path, "r", errors="replace") as fh:
            return fh.read().strip()
    except (OSError, ValueError):
        return None


def _read_int(path: str) -> Optional[int]:
    text = _read(path)
    if text is None:
        return None
    try:
        return int(text)
    except ValueError:
        try:
            return int(float(text))
        except ValueError:
            return None


def _num(text: str) -> Optional[float]:
    try:
        return float(text.strip())
    except (ValueError, AttributeError):
        return None  # "[N/A]", "[Not Supported]", ""


def _tail_int(path: str) -> int:
    match = re.search(r"(\d+)$", path)
    return int(match.group(1)) if match else 0


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------
KIND_ORDER = ["temp", "load", "clock", "power", "current", "voltage", "fan", "data", "battery"]
KIND_TITLES = {
    "temp": "Temperatures",
    "load": "Utilization",
    "clock": "Clocks",
    "power": "Powers",
    "current": "Currents",
    "voltage": "Voltages",
    "fan": "Fans",
    "data": "Data",
    "battery": "Battery",
}


@dataclass
class Sensor:
    id: str
    group: str                      # device node, e.g. the CPU model
    name: str
    kind: str                       # one of KIND_ORDER
    unit: str                       # "°C", "RPM", "V", "W", "%", "MHz", "GiB", "A"
    value: Optional[float]
    vmin: Optional[float] = None
    vmax: Optional[float] = None
    crit: Optional[float] = None    # hardware limit reported by the driver
    category: str = "other"         # cpu | gpu | disk | other (alert thresholds)
    prio: int = 50                  # device ordering in the UI


CPU_CHIPS = {"coretemp", "k10temp", "zenpower", "k8temp", "cpu_thermal", "fam15h_power",
             "intel_pkg_temp", "x86_pkg_temp", "zenergy", "amd_energy"}
GPU_CHIPS = {"amdgpu", "radeon", "nouveau", "nvidia", "i915", "xe"}
DISK_CHIPS = {"nvme", "drivetemp"}
FRIENDLY = {
    "acpitz": "ACPI Thermal Zone",
    "nvme": "NVMe SSD",
    "drivetemp": "Drive",
    "amdgpu": "AMD GPU",
    "radeon": "AMD Radeon GPU",
    "nouveau": "NVIDIA GPU (nouveau)",
    "iwlwifi": "Intel Wi-Fi",
    "iwlwifi_1": "Intel Wi-Fi",
    "pch_skylake": "Chipset (PCH)",
    "pch_cannonlake": "Chipset (PCH)",
    "pch_cometlake": "Chipset (PCH)",
    "pch_haswell": "Chipset (PCH)",
    "thinkpad": "ThinkPad EC",
    "dell_smm": "Dell SMM",
    "asus": "ASUS EC",
    "asus_wmi_sensors": "ASUS WMI",
    "nct6775": "Super I/O (NCT677x)",
    "nct6779": "Super I/O (NCT6779)",
    "nct6798": "Super I/O (NCT6798)",
    "it8688": "Super I/O (IT8688E)",
    "it8628": "Super I/O (IT8628E)",
    "BAT0": "Battery",
    "BAT1": "Battery",
}
# (attribute prefix, value suffixes in order of preference, kind, unit, divisor, default label)
HWMON_KINDS = [
    ("temp", ("input",), "temp", "°C", 1000.0, "Temp"),
    ("fan", ("input",), "fan", "RPM", 1.0, "Fan"),
    ("in", ("input",), "voltage", "V", 1000.0, "Voltage"),
    ("power", ("input", "average"), "power", "W", 1e6, "Power"),
    ("curr", ("input",), "current", "A", 1000.0, "Current"),
    ("freq", ("input",), "clock", "MHz", 1e6, "Frequency"),
]


def _norm(name: str) -> str:
    return re.sub(r"_\d+$", "", name.replace("-", "_").lower())


def _classify(chip: str):
    """Return (category, device priority) for a hwmon chip name."""
    n = _norm(chip)
    if n in CPU_CHIPS:
        return "cpu", 10
    if n in GPU_CHIPS:
        return "gpu", 30
    if n in DISK_CHIPS:
        return "disk", 40
    return "other", 50


def _cpu_model() -> str:
    try:
        with open(_p("/proc/cpuinfo"), errors="replace") as fh:
            for line in fh:
                low = line.lower()
                if low.startswith(("model name", "hardware", "cpu model", "model\t")):
                    name = line.split(":", 1)[1].strip()
                    name = name.replace("(R)", "").replace("(TM)", "")
                    name = re.sub(r"\s+", " ", name).strip()
                    if name:
                        return name
    except OSError:
        pass
    return "CPU"


# --------------------------------------------------------------------------
# Sensor reader
# --------------------------------------------------------------------------
NV_FIELDS = ["index", "name", "temperature.gpu", "utilization.gpu", "utilization.memory",
             "memory.used", "memory.total", "fan.speed", "power.draw", "clocks.gr", "clocks.mem"]


class SensorReader:
    """Reads every available sensor. Call read_all() repeatedly."""

    def __init__(self) -> None:
        self.cpu_name = _cpu_model()
        self.rapl_denied = False        # RAPL present but unreadable (needs root)
        self.nvidia_error: Optional[str] = None
        self._prev_stat = self._read_proc_stat()
        self._rapl_prev: Dict[str, tuple] = {}
        self._nvsmi = shutil.which("nvidia-smi")
        self._nv_backoff_until = 0.0
        self._ids: set = set()
        self._hwmon_names: set = set()
        self._has_cpu_hwmon = False

    # ---- public ------------------------------------------------------
    def read_all(self) -> List[Sensor]:
        self._ids = set()
        out: List[Sensor] = []
        for section in (self._hwmon, self._thermal_zones, self._cpu_load, self._cpu_clocks,
                        self._rapl, self._memory, self._nvidia, self._battery):
            try:
                out.extend(section())
            except Exception as exc:  # one broken source must never kill the rest
                sys.stderr.write(f"[hwmonitor] {section.__name__} failed: {exc}\n")
        # Stable ordering: device priority -> device first-seen -> kind order
        group_index: Dict[str, int] = {}
        for s in out:
            group_index.setdefault(s.group, len(group_index))

        def kind_rank(kind: str) -> int:
            return KIND_ORDER.index(kind) if kind in KIND_ORDER else len(KIND_ORDER)

        out.sort(key=lambda s: (s.prio, group_index[s.group], kind_rank(s.kind)))
        return out

    # ---- helpers -----------------------------------------------------
    def _uid(self, base: str) -> str:
        uid, n = base, 2
        while uid in self._ids:
            uid = f"{base}#{n}"
            n += 1
        self._ids.add(uid)
        return uid

    # ---- hwmon -------------------------------------------------------
    def _group_name(self, directory: str, chip: str, category: str) -> str:
        if category == "cpu":
            return self.cpu_name
        if _norm(chip) in DISK_CHIPS:
            model = _read(os.path.join(directory, "device", "model"))
            if model:
                model = re.sub(r"\s+", " ", model)
                return f"{FRIENDLY.get(chip, chip)}: {model}"
        return FRIENDLY.get(chip, chip)

    def _hwmon(self) -> List[Sensor]:
        sensors: List[Sensor] = []
        self._hwmon_names = set()
        self._has_cpu_hwmon = False
        chip_count: Dict[str, int] = {}
        group_count: Dict[str, int] = {}
        dirs = sorted(glob.glob(_p("/sys/class/hwmon/hwmon*")), key=_tail_int)
        for d in dirs:
            chip = _read(os.path.join(d, "name")) or os.path.basename(d)
            category, prio = _classify(chip)
            idx = chip_count.get(chip, 0)
            chip_count[chip] = idx + 1
            group = self._group_name(d, chip, category)
            if category != "cpu":
                nth = group_count.get(group, 0)
                group_count[group] = nth + 1
                if nth:
                    group = f"{group} #{nth + 1}"
            before = len(sensors)
            for prefix, suffixes, kind, unit, div, default in HWMON_KINDS:
                found: Dict[int, str] = {}
                for sfx in suffixes:
                    for path in glob.glob(os.path.join(d, f"{prefix}[0-9]*_{sfx}")):
                        m = re.match(rf"{prefix}(\d+)_{sfx}$", os.path.basename(path))
                        if m:
                            found.setdefault(int(m.group(1)), path)
                for n in sorted(found):
                    raw = _read_int(found[n])
                    if raw is None:
                        continue
                    value = raw / div
                    if kind == "temp" and not (-50.0 <= value <= 250.0):
                        continue                    # driver reports "no sensor"
                    if kind == "fan" and raw >= 65535:
                        continue
                    label = _read(os.path.join(d, f"{prefix}{n}_label")) or f"{default} {n}"
                    crit = None
                    if kind == "temp":
                        for limit in ("crit", "max"):
                            c = _read_int(os.path.join(d, f"temp{n}_{limit}"))
                            if c is not None and 0 < c / 1000.0 <= 250:
                                crit = c / 1000.0
                                break
                    sensors.append(Sensor(
                        id=self._uid(f"hwmon:{chip}:{idx}:{prefix}{n}"),
                        group=group, name=label, kind=kind, unit=unit, value=value,
                        crit=crit, category=category, prio=prio))
            if kind_has_temp(sensors[before:]):
                self._hwmon_names.add(_norm(chip))
                if category == "cpu":
                    self._has_cpu_hwmon = True
        return sensors

    def _thermal_zones(self) -> List[Sensor]:
        sensors: List[Sensor] = []
        zones = sorted(glob.glob(_p("/sys/class/thermal/thermal_zone*")), key=_tail_int)
        for d in zones:
            typ = _read(os.path.join(d, "type")) or os.path.basename(d)
            n = _norm(typ)
            if n in self._hwmon_names or typ.startswith("INT34"):
                continue
            if n == "x86_pkg_temp" and self._has_cpu_hwmon:
                continue
            raw = _read_int(os.path.join(d, "temp"))
            if raw is None or not (0 < raw / 1000.0 <= 250):
                continue
            is_cpu = n in CPU_CHIPS
            sensors.append(Sensor(
                id=self._uid(f"thermal:{typ}:{_tail_int(d)}"),
                group=self.cpu_name if is_cpu else "Thermal Zones",
                name=typ, kind="temp", unit="°C", value=raw / 1000.0,
                category="cpu" if is_cpu else "other", prio=10 if is_cpu else 60))
        return sensors

    # ---- CPU load / clocks ------------------------------------------
    @staticmethod
    def _read_proc_stat() -> Dict[str, tuple]:
        res: Dict[str, tuple] = {}
        try:
            with open(_p("/proc/stat")) as fh:
                for line in fh:
                    if not line.startswith("cpu"):
                        break
                    parts = line.split()
                    vals = [int(x) for x in parts[1:]]
                    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
                    total = sum(vals[:8]) if len(vals) >= 8 else sum(vals)
                    res[parts[0]] = (total, idle)
        except (OSError, ValueError, IndexError):
            pass
        return res

    def _cpu_load(self) -> List[Sensor]:
        cur, prev = self._read_proc_stat(), self._prev_stat
        self._prev_stat = cur
        sensors: List[Sensor] = []
        for name, (tot, idle) in cur.items():
            old = prev.get(name)
            if old is None:
                continue
            dt, di = tot - old[0], idle - old[1]
            value = None if dt <= 0 else max(0.0, min(100.0, 100.0 * (dt - di) / dt))
            label = "Total" if name == "cpu" else f"Thread {name[3:]}"
            sensors.append(Sensor(id=f"load:{name}", group=self.cpu_name, name=label,
                                  kind="load", unit="%", value=value, category="cpu", prio=10))
        return sensors

    def _cpu_clocks(self) -> List[Sensor]:
        sensors: List[Sensor] = []
        files = glob.glob(_p("/sys/devices/system/cpu/cpu[0-9]*/cpufreq/scaling_cur_freq"))
        files.sort(key=lambda f: int(re.search(r"/cpu(\d+)/", f).group(1)))
        for f in files:
            khz = _read_int(f)
            if khz is None:
                continue
            i = re.search(r"/cpu(\d+)/", f).group(1)
            sensors.append(Sensor(id=f"clock:cpu{i}", group=self.cpu_name, name=f"Thread {i}",
                                  kind="clock", unit="MHz", value=khz / 1000.0,
                                  category="cpu", prio=10))
        if sensors:
            return sensors
        try:  # fallback: /proc/cpuinfo
            with open(_p("/proc/cpuinfo"), errors="replace") as fh:
                i = 0
                for line in fh:
                    if line.lower().startswith("cpu mhz"):
                        mhz = _num(line.split(":", 1)[1])
                        if mhz is not None:
                            sensors.append(Sensor(id=f"clock:cpu{i}", group=self.cpu_name,
                                                  name=f"Thread {i}", kind="clock", unit="MHz",
                                                  value=mhz, category="cpu", prio=10))
                        i += 1
        except OSError:
            pass
        return sensors

    # ---- RAPL power --------------------------------------------------
    def _rapl(self) -> List[Sensor]:
        sensors: List[Sensor] = []
        now = time.monotonic()
        labels = {"package-0": "CPU Package", "core": "CPU Cores",
                  "uncore": "Integrated GPU", "dram": "DRAM", "psys": "Platform (PSYS)"}
        zones = sorted(glob.glob(_p("/sys/class/powercap/intel-rapl:*")))
        for d in zones:
            key = os.path.basename(d)
            name = _read(os.path.join(d, "name")) or key
            try:
                with open(os.path.join(d, "energy_uj")) as fh:
                    energy = int(fh.read().strip())
            except PermissionError:
                self.rapl_denied = True
                continue
            except (OSError, ValueError):
                continue
            span = _read_int(os.path.join(d, "max_energy_range_uj")) or 0
            prev = self._rapl_prev.get(key)
            self._rapl_prev[key] = (now, energy)
            if prev is None:
                continue
            dt, de = now - prev[0], energy - prev[1]
            if de < 0 and span:
                de += span                      # counter wrapped
            if dt <= 0 or de < 0:
                continue
            sensors.append(Sensor(id=f"rapl:{key}", group=self.cpu_name,
                                  name=labels.get(name, name), kind="power", unit="W",
                                  value=de / dt / 1e6, category="cpu", prio=10))
        return sensors

    # ---- memory ------------------------------------------------------
    def _memory(self) -> List[Sensor]:
        info: Dict[str, int] = {}
        try:
            with open(_p("/proc/meminfo")) as fh:
                for line in fh:
                    key, _, rest = line.partition(":")
                    parts = rest.split()
                    if parts:
                        info[key] = int(parts[0])           # kB
        except (OSError, ValueError):
            return []
        total = info.get("MemTotal")
        if not total:
            return []
        avail = info.get("MemAvailable", info.get("MemFree", 0))
        used = total - avail
        gib = 1024.0 * 1024.0
        out = [
            Sensor("mem:used_pct", "Memory", "Memory Used", "load", "%", 100.0 * used / total, prio=20),
            Sensor("mem:used", "Memory", "Used", "data", "GiB", used / gib, prio=20),
            Sensor("mem:avail", "Memory", "Available", "data", "GiB", avail / gib, prio=20),
        ]
        swap_total = info.get("SwapTotal", 0)
        if swap_total:
            swap_used = swap_total - info.get("SwapFree", 0)
            out.append(Sensor("mem:swap_pct", "Memory", "Swap Used", "load", "%",
                              100.0 * swap_used / swap_total, prio=20))
        return out

    # ---- NVIDIA ------------------------------------------------------
    def _nvidia(self) -> List[Sensor]:
        if not self._nvsmi or time.monotonic() < self._nv_backoff_until:
            return []
        try:
            res = subprocess.run(
                [self._nvsmi, "--query-gpu=" + ",".join(NV_FIELDS), "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=3)
        except (OSError, subprocess.SubprocessError):
            self._nv_backoff_until = time.monotonic() + 30
            return []
        if res.returncode != 0:
            self.nvidia_error = (res.stderr or res.stdout).strip()[:200]
            self._nv_backoff_until = time.monotonic() + 30
            return []
        sensors: List[Sensor] = []
        for row in csv.reader(res.stdout.splitlines(), skipinitialspace=True):
            if len(row) < len(NV_FIELDS):
                continue
            idx, name = row[0].strip(), row[1].strip()
            v = [_num(x) for x in row[2:]]
            group = f"NVIDIA {name}" if idx == "0" else f"NVIDIA {name} #{idx}"
            gid = f"nvidia:{idx}"
            mem_used = v[3] / 1024.0 if v[3] is not None else None
            rows = [
                ("temp", "GPU Core", "temp", "°C", v[0]),
                ("util", "GPU Core", "load", "%", v[1]),
                ("memctl", "Memory Controller", "load", "%", v[2]),
                ("memused", "Memory Used", "data", "GiB", mem_used),
                ("fan", "Fan", "fan", "%", v[5]),
                ("power", "Board Power", "power", "W", v[6]),
                ("sclk", "Graphics", "clock", "MHz", v[7]),
                ("mclk", "Memory", "clock", "MHz", v[8]),
            ]
            for key, label, kind, unit, value in rows:
                sensors.append(Sensor(id=f"{gid}:{key}", group=group, name=label, kind=kind,
                                      unit=unit, value=value, category="gpu", prio=30))
        return sensors

    # ---- battery -----------------------------------------------------
    def _battery(self) -> List[Sensor]:
        sensors: List[Sensor] = []
        for d in sorted(glob.glob(_p("/sys/class/power_supply/BAT*"))):
            bat = os.path.basename(d)
            model = _read(os.path.join(d, "model_name"))
            group = f"Battery {bat}" + (f" ({model})" if model else "")
            status = _read(os.path.join(d, "status")) or ""
            cap = _read_int(os.path.join(d, "capacity"))
            if cap is not None:
                name = f"Charge ({status})" if status else "Charge"
                sensors.append(Sensor(f"bat:{bat}:charge", group, name, "battery", "%",
                                      float(cap), prio=70))
            volt = _read_int(os.path.join(d, "voltage_now"))
            if volt is not None:
                sensors.append(Sensor(f"bat:{bat}:volt", group, "Voltage", "voltage", "V",
                                      volt / 1e6, prio=70))
            power = _read_int(os.path.join(d, "power_now"))
            if power is None:
                cur = _read_int(os.path.join(d, "current_now"))
                if cur is not None and volt is not None:
                    power = int(abs(cur) * volt / 1e6)
            if power is not None:
                sensors.append(Sensor(f"bat:{bat}:power", group, "Power", "power", "W",
                                      abs(power) / 1e6, prio=70))
            for full, design in (("energy_full", "energy_full_design"),
                                 ("charge_full", "charge_full_design")):
                f, ds = _read_int(os.path.join(d, full)), _read_int(os.path.join(d, design))
                if f and ds:
                    sensors.append(Sensor(f"bat:{bat}:health", group, "Wear Level (health)",
                                          "battery", "%", 100.0 * f / ds, prio=70))
                    break
        return sensors


def kind_has_temp(sensors: List[Sensor]) -> bool:
    return any(s.kind == "temp" for s in sensors)


# --------------------------------------------------------------------------
# Formatting / helpers shared by every front end
# --------------------------------------------------------------------------
def fmt_value(unit: str, value: Optional[float], fahrenheit: bool = False) -> str:
    if value is None:
        return "–"
    if unit == "°C":
        return f"{value * 9 / 5 + 32:.1f} °F" if fahrenheit else f"{value:.1f} °C"
    prec = {"RPM": 0, "V": 3, "W": 2, "A": 2, "%": 1, "MHz": 0, "GiB": 2}.get(unit, 2)
    return f"{value:.{prec}f} {unit}"


def format_report(sensors: List[Sensor], fahrenheit: bool = False) -> str:
    lines: List[str] = []
    group = kind = None
    width = max([len(s.name) for s in sensors] + [10])
    for s in sensors:
        if s.group != group:
            group, kind = s.group, None
            lines += ["", group]
        if s.kind != kind:
            kind = s.kind
            lines.append(f"  {KIND_TITLES.get(kind, kind)}")
        lines.append(f"    {s.name:<{width}}  {fmt_value(s.unit, s.value, fahrenheit):>12}"
                     f"  min {fmt_value(s.unit, s.vmin, fahrenheit):>10}"
                     f"  max {fmt_value(s.unit, s.vmax, fahrenheit):>10}")
    return f"{APP_NAME} {__version__}".strip() + "\n" + "\n".join(lines).rstrip() + "\n"


def sensors_to_json(sensors: List[Sensor]) -> str:
    return json.dumps([{
        "id": s.id, "device": s.group, "name": s.name, "type": s.kind, "unit": s.unit,
        "value": s.value, "min": s.vmin, "max": s.vmax, "hardware_limit": s.crit,
    } for s in sensors], indent=2, ensure_ascii=False)


_PREFERRED = {
    "cpu": ("package", "tctl", "tdie", "cpu", "soc"),
    "gpu": ("edge", "gpu", "core"),
}


def primary_temp(sensors: List[Sensor], category: str) -> Optional[Sensor]:
    """The one temperature to show in the tray for a category (CPU, GPU, ...)."""
    temps = [s for s in sensors if s.kind == "temp" and s.category == category
             and s.value is not None]
    if not temps:
        return None
    for token in _PREFERRED.get(category, ()):
        for s in temps:
            if token in s.name.lower():
                return s
    return max(temps, key=lambda s: s.value)


def hottest(sensors: List[Sensor]) -> Optional[Sensor]:
    temps = [s for s in sensors if s.kind == "temp" and s.value is not None]
    return max(temps, key=lambda s: s.value) if temps else None


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------
DEFAULTS = {
    "interval": 2.0,                # seconds between samples
    "fahrenheit": False,
    "close_to_tray": True,
    "tray_temp": True,              # draw the CPU temperature into the tray icon
    "notifications": True,
    "notify_recovery": True,        # "back to normal" message
    "sustain_seconds": 5.0,         # must stay hot this long before alerting
    "repeat_minutes": 5.0,          # reminder interval while still hot (0 = never)
    "hysteresis": 3.0,              # degrees below a threshold before re-arming
    "thresholds": {                 # [warning, critical] in °C
        "cpu": [85.0, 95.0],
        "gpu": [85.0, 95.0],
        "disk": [65.0, 75.0],
        "other": [80.0, 90.0],
    },
}


def _config_home() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))


def config_file() -> Path:
    return _config_home() / APP_SLUG / "config.json"

def autostart_file() -> Path:
    return _config_home() / "autostart" / f"{APP_SLUG}.desktop"


class Settings:
    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path else config_file()
        self.data = copy.deepcopy(DEFAULTS)
        self.load()

    def __getitem__(self, key: str):
        return self.data[key]

    def __setitem__(self, key: str, value) -> None:
        self.data[key] = value

    def load(self) -> None:
        try:
            raw = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return
        if not isinstance(raw, dict):
            return
        for key, value in raw.items():
            default = DEFAULTS.get(key)
            if key == "thresholds" and isinstance(value, dict):
                for cat, pair in value.items():
                    if (cat in self.data["thresholds"] and isinstance(pair, (list, tuple))
                            and len(pair) == 2 and all(isinstance(x, (int, float)) for x in pair)):
                        self.data["thresholds"][cat] = [float(pair[0]), float(pair[1])]
            elif isinstance(default, bool):
                if isinstance(value, bool):
                    self.data[key] = value
            elif isinstance(default, float):
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    self.data[key] = float(value)

    def save(self) -> bool:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=2))
            os.replace(tmp, self.path)
            return True
        except OSError as exc:
            sys.stderr.write(f"[hwmonitor] could not save settings: {exc}\n")
            return False


# --------------------------------------------------------------------------
# Notifications
# --------------------------------------------------------------------------
def send_notification(title: str, body: str, urgency: str = "normal", key: str = "",
                      icon: str = "dialog-warning") -> bool:
    """Desktop notification through notify-send (package libnotify-bin)."""
    exe = shutil.which("notify-send")
    if not exe:
        return False
    cmd = [exe, "-a", APP_NAME, "-u", urgency, "-i", icon, "-c", "device"]
    if urgency != "critical":
        cmd += ["-t", "10000"]
    if key:  # same key -> the new bubble replaces the old one (GNOME/KDE)
        cmd += ["-h", f"string:x-canonical-private-synchronous:{APP_ID}-{key}"]
    cmd += [title, body]
    try:
        return subprocess.run(cmd, capture_output=True, timeout=5).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


# --------------------------------------------------------------------------
# Alert engine
# --------------------------------------------------------------------------
@dataclass
class Alert:
    sensor: Sensor
    level: int          # 0 = recovered, 1 = warning, 2 = critical
    threshold: float
    kind: str           # "raised" | "reminder" | "recovered"


_CAT_LABEL = {"cpu": "CPU", "gpu": "GPU", "disk": "Disk", "other": ""}


class AlertManager:
    """Threshold alerts with debounce (sustain), hysteresis and reminders."""

    def __init__(self, settings: Settings, notifier: Optional[Callable] = None) -> None:
        self.settings = settings
        self.notifier = notifier or send_notification
        self.state: Dict[str, dict] = {}

    def level_of(self, sensor_id: str) -> int:
        return self.state.get(sensor_id, {}).get("level", 0)

    def worst_level(self) -> int:
        return max((st["level"] for st in self.state.values()), default=0)

    def thresholds_for(self, category: str):
        table = self.settings["thresholds"]
        pair = table.get(category) or table["other"]
        warn = float(pair[0])
        return warn, max(float(pair[1]), warn)

    def evaluate(self, sensors: List[Sensor], now: Optional[float] = None) -> List[Alert]:
        now = time.monotonic() if now is None else now
        cfg = self.settings
        sustain = max(0.0, float(cfg["sustain_seconds"]))
        repeat = max(0.0, float(cfg["repeat_minutes"])) * 60.0
        hyst = max(0.0, float(cfg["hysteresis"]))
        events: List[Alert] = []
        for sen in sensors:
            if sen.kind != "temp" or sen.value is None:
                continue
            warn, crit = self.thresholds_for(sen.category)
            thr = {1: warn, 2: crit}
            st = self.state.setdefault(sen.id, {"level": 0, "rise_since": None, "last": 0.0})
            v, level = sen.value, 0
            level = st["level"]
            raw = 2 if v >= crit else (1 if v >= warn else 0)
            if raw > level:                                   # getting hotter
                if st["rise_since"] is None:
                    st["rise_since"] = now
                if now - st["rise_since"] >= sustain:
                    st.update(level=raw, rise_since=None, last=now)
                    events.append(Alert(sen, raw, thr[raw], "raised"))
                continue
            st["rise_since"] = None
            if raw < level and v > thr[level] - hyst:         # inside the hysteresis band
                raw = level
            if raw < level:                                   # really cooled down
                st["level"] = raw
                if raw == 0:
                    events.append(Alert(sen, 0, warn, "recovered"))
                else:
                    st["last"] = now
            elif level > 0 and repeat > 0 and now - st["last"] >= repeat:
                st["last"] = now
                events.append(Alert(sen, level, thr[level], "reminder"))
        if cfg["notifications"]:
            for ev in events:
                self._notify(ev)
        return events

    def _notify(self, ev: Alert) -> None:
        fahr = bool(self.settings["fahrenheit"])
        s = ev.sensor
        label = _CAT_LABEL.get(s.category, "")
        name = s.name if (not label or s.name.lower().startswith(label.lower())) else f"{label} {s.name}"
        now_txt = fmt_value("°C", s.value, fahr)
        limit = fmt_value("°C", ev.threshold, fahr)
        key = re.sub(r"[^A-Za-z0-9_.-]", "_", s.id)
        if ev.kind == "recovered":
            if not self.settings["notify_recovery"]:
                return
            self.notifier(f"{name} is back to normal", f"{s.group}\nNow {now_txt}.",
                          "low", key, "dialog-information")
        elif ev.level == 1:
            again = " (still hot)" if ev.kind == "reminder" else ""
            self.notifier(f"Warning: {name} {now_txt}{again}",
                          f"{s.group}\nAbove the warning level of {limit}.",
                          "normal", key, "dialog-warning")
        else:
            again = " (still critical)" if ev.kind == "reminder" else ""
            self.notifier(f"CRITICAL: {name} {now_txt}{again}",
                          f"{s.group}\nAbove the critical limit of {limit}. "
                          f"Check cooling or reduce the load now.",
                          "critical", key, "dialog-error")


# --------------------------------------------------------------------------
# Monitor: background sampling thread
# --------------------------------------------------------------------------
class Monitor:
    def __init__(self, settings: Settings, on_update: Optional[Callable[[], None]] = None,
                 notifier: Optional[Callable] = None) -> None:
        self.settings = settings
        self.on_update = on_update
        self.reader = SensorReader()
        self.alerts = AlertManager(settings, notifier)
        self.last_update: Optional[float] = None
        self._lock = threading.Lock()
        self._sensors: List[Sensor] = []
        self._minmax: Dict[str, List[float]] = {}
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def poll_once(self) -> List[Sensor]:
        sensors = self.reader.read_all()
        with self._lock:
            for s in sensors:
                mm = self._minmax.get(s.id)
                if s.value is not None:
                    if mm is None:
                        mm = self._minmax[s.id] = [s.value, s.value]
                    else:
                        mm[0], mm[1] = min(mm[0], s.value), max(mm[1], s.value)
                if mm:
                    s.vmin, s.vmax = mm
        self.alerts.evaluate(sensors)
        with self._lock:
            self._sensors = sensors
            self.last_update = time.time()
        return sensors

    def snapshot(self) -> List[Sensor]:
        with self._lock:
            return list(self._sensors)

    def reset_minmax(self) -> None:
        with self._lock:
            self._minmax.clear()
            for s in self._sensors:
                s.vmin = s.vmax = s.value

    def level(self, sensor_id: str) -> int:
        return self.alerts.level_of(sensor_id)

    def worst_level(self) -> int:
        return self.alerts.worst_level()

    def wake(self) -> None:
        self._wake.set()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="hwmon-poll", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                self.poll_once()
                if self.on_update:
                    self.on_update()
            except Exception as exc:
                sys.stderr.write(f"[hwmonitor] poll failed: {exc}\n")
            interval = min(60.0, max(0.5, float(self.settings["interval"])))
            self._wake.wait(max(0.1, interval - (time.monotonic() - started)))
            self._wake.clear()


# --------------------------------------------------------------------------
# Start at login (XDG autostart)
# --------------------------------------------------------------------------
def _desktop_quote(arg: str) -> str:
    if re.search(r"[\s\"'\\><~|&;$*?#()`]", arg):
        arg = re.sub(r'(["`$\\])', r"\\\1", arg)
        return f'"{arg}"'
    return arg


def launch_command(minimized: bool = False) -> str:
    exe = shutil.which(APP_ID)
    cmd = [exe] if exe else [sys.executable, str(Path(__file__).resolve().with_name("hwmonitor.py"))]
    if minimized:
        cmd.append("--minimized")
    return " ".join(_desktop_quote(c) for c in cmd)


def autostart_enabled() -> bool:
    try:
        text = autostart_file().read_text()
    except OSError:
        return False
    return not re.search(r"^(Hidden=true|X-GNOME-Autostart-enabled=false)", text, re.M)


def set_autostart(enabled: bool) -> bool:
    path = autostart_file()
    try:
        if enabled:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                "[Desktop Entry]\n"
                "Type=Application\n"
                "Version=1.0\n"
                f"Name={APP_NAME}\n"
                "Comment=Hardware temperature and sensor monitor\n"
                f"Exec={launch_command(minimized=True)}\n"
                f"Icon={APP_ID}\n"
                "Terminal=false\n"
                "StartupNotify=false\n"
                "Categories=System;Monitor;\n"
                "X-GNOME-Autostart-enabled=true\n"
                "X-GNOME-Autostart-Delay=5\n"
                "X-KDE-autostart-after=panel\n")
        else:
            path.unlink(missing_ok=True)
        return True
    except OSError as exc:
        sys.stderr.write(f"[hwmonitor] autostart change failed: {exc}\n")
        return False
