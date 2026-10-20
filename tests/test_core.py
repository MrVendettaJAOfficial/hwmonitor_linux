"""Tests for hwmonitor_core. Run with: python3 -m pytest -q"""
from __future__ import annotations

import json
import os
import textwrap
from pathlib import Path

import pytest

import hwmonitor_core as core


@pytest.fixture()
def fake_root(tmp_path, monkeypatch):
    """Build a minimal fake sysfs tree and point SYSROOT at it."""
    def w(path: str, text: str) -> None:
        p = tmp_path / path.lstrip("/")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(text).lstrip())

    # CPU
    w("/proc/cpuinfo", "model name : Test CPU @ 3.00GHz\n")
    w("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq", "3000000\n")
    w("/sys/devices/system/cpu/cpu1/cpufreq/scaling_cur_freq", "2500000\n")

    # hwmon
    w("/sys/class/hwmon/hwmon0/name", "coretemp\n")
    w("/sys/class/hwmon/hwmon0/temp1_input", "45000\n")
    w("/sys/class/hwmon/hwmon0/temp1_crit", "100000\n")
    w("/sys/class/hwmon/hwmon0/temp1_label", "Package id 0\n")
    w("/sys/class/hwmon/hwmon0/temp2_input", "42000\n")
    w("/sys/class/hwmon/hwmon0/temp2_label", "Core 0\n")

    w("/sys/class/hwmon/hwmon1/name", "nvme\n")
    w("/sys/class/hwmon/hwmon1/temp1_input", "38000\n")
    w("/sys/class/hwmon/hwmon1/device/model", "Test SSD 1TB\n")

    # meminfo
    w("/proc/meminfo",
      "MemTotal:       16000000 kB\n"
      "MemAvailable:    8000000 kB\n"
      "SwapTotal:       2000000 kB\n"
      "SwapFree:        1500000 kB\n")

    # battery
    w("/sys/class/power_supply/BAT0/capacity", "75\n")
    w("/sys/class/power_supply/BAT0/voltage_now", "12000000\n")
    w("/sys/class/power_supply/BAT0/status", "Discharging\n")

    # proc/stat (two snapshots for load)
    w("/proc/stat",
      "cpu  100 0 100 800 0 0 0 0 0 0\n"
      "cpu0 50 0 50 400 0 0 0 0 0 0\n"
      "cpu1 50 0 50 400 0 0 0 0 0 0\n")

    monkeypatch.setattr(core, "SYSROOT", str(tmp_path))
    return tmp_path


def test_cpu_model(fake_root):
    assert "Test CPU" in core._cpu_model()


def test_hwmon_temps(fake_root):
    reader = core.SensorReader()
    sensors = reader.read_all()
    temps = [s for s in sensors if s.kind == "temp"]
    assert any(s.name == "Package id 0" and s.value == 45.0 for s in temps)
    assert any(s.category == "cpu" for s in temps)
    assert any("Test SSD 1TB" in s.group for s in sensors)


def test_memory(fake_root):
    sensors = core.SensorReader().read_all()
    mem = {s.name: s for s in sensors if s.group == "Memory"}
    assert mem["Memory Used"].value == pytest.approx(50.0, abs=0.1)
    assert "Swap Used" in mem


def test_battery(fake_root):
    sensors = core.SensorReader().read_all()
    bat = [s for s in sensors if s.group.startswith("Battery")]
    assert any(s.name.startswith("Charge") and s.value == 75.0 for s in bat)


def test_format_and_json(fake_root):
    sensors = core.SensorReader().read_all()
    report = core.format_report(sensors)
    assert "Temperatures" in report and "Memory" in report
    data = json.loads(core.sensors_to_json(sensors))
    assert isinstance(data, list) and data[0]["id"]


def test_alert_hysteresis_and_sustain():
    settings = core.Settings(Path("/nonexistent/config.json"))
    settings.data["sustain_seconds"] = 0.0
    settings.data["hysteresis"] = 5.0
    events = []
    mgr = core.AlertManager(settings, notifier=lambda *a, **k: events.append(a))

    s = core.Sensor("t1", "CPU", "Package", "temp", "°C", 90.0, category="cpu")
    mgr.evaluate([s], now=0.0)
    assert mgr.level_of("t1") == 1                 # crossed warn (85)

    s.value = 96.0
    mgr.evaluate([s], now=1.0)
    assert mgr.level_of("t1") == 2                 # crossed crit (95)

    s.value = 92.0                                 # inside hysteresis band
    mgr.evaluate([s], now=2.0)
    assert mgr.level_of("t1") == 2

    s.value = 89.0                                 # still inside band (crit-5)
    mgr.evaluate([s], now=3.0)
    assert mgr.level_of("t1") == 1                 # dropped to warn


def test_settings_roundtrip(tmp_path):
    cfg = tmp_path / "cfg.json"
    s = core.Settings(cfg)
    s["fahrenheit"] = True
    s["interval"] = 5.0
    s["thresholds"]["cpu"] = [70.0, 80.0]
    assert s.save()
    s2 = core.Settings(cfg)
    assert s2["fahrenheit"] is True
    assert s2["interval"] == 5.0
    assert s2["thresholds"]["cpu"] == [70.0, 80.0]


def test_settings_rejects_bad_types(tmp_path):
    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps({"interval": "not-a-number",
                               "fahrenheit": 1,
                               "thresholds": {"cpu": [1, 2, 3]}}))
    s = core.Settings(cfg)
    assert s["interval"] == core.DEFAULTS["interval"]
    assert s["fahrenheit"] is False
    assert s["thresholds"]["cpu"] == core.DEFAULTS["thresholds"]["cpu"]
