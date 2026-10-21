# Linux HWMonitor

A lightweight, native hardware monitor for Linux with a modern GTK4/libadwaita front end. 

Linux HWMonitor reads temperatures, fans, voltages, power, clocks, CPU load, memory, battery status, and NVIDIA GPU stats directly from the kernel (`sysfs`, `procfs`, RAPL). It ships as a clickable desktop application, but also includes a terminal UI, JSON output, and a headless alert daemon.

## Features

- **Native GUI:** Built with GTK4 and libadwaita. Integrates perfectly with GNOME, Linux Mint, and other modern desktop environments.
- **Comprehensive Sensor Support:** Reads from `hwmon`, `thermal_zone`, Intel/AMD RAPL (power), `nvidia-smi`, and battery interfaces.
- **Smart Alert Engine:** Threshold-based desktop notifications with configurable "sustain" times (to avoid flapping) and hysteresis (to avoid notification spam).
- **Multiple Modes:** Run it as a GUI, a one-shot CLI report, a live terminal dashboard, a JSON data dump, or a background daemon.
- **Zero Pip Dependencies:** The core logic is pure Python standard library. The GUI relies on system-installed `PyGObject`.

## Installation

We use a custom build script to package the application as a standard `.deb` file. This ensures it shows up in your application menu and includes an isolated virtual environment so it won't break your system Python.

1. Install System Dependencies

You need the GTK4 bindings and notification tools installed at the system level (these cannot be installed via `pip`):

```
sudo apt update
sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-4.0 gir1.2-adw-1 libnotify-bin

```
2. Build the Package
Clone or download this repository, open a terminal in the project folder, and run:

```chmod +x build_deb.sh
./build_deb.sh
```
This will generate dist/linux-hwmonitor_1.0.0_all.deb.

3. Install the Application
Install the generated .deb file using apt so that dependencies are resolved automatically:

```
sudo apt install ./dist/linux-hwmonitor_1.0.0_all.deb
```
You're done! You can now find Linux HWMonitor by searching your system's application menu, or launch it from the terminal.


# Usage
### GUI Mode (Default)
Launch it from your desktop application menu, or run:

```linux-hwmonitor```

### CLI Mode
Print a single report of all current sensors and exit:

```
linux-hwmonitor --cli
```

### JSON Mode
Output all sensor data as structured JSON (useful for scripts or dashboards):

```
linux-hwmonitor --json
```

### Live Terminal Mode
Watch the sensors update live in your terminal (like htop but for temperatures):

```
linux-hwmonitor --watch
```

### Headless Daemon Mode
Run in the background to monitor temperatures and send desktop notifications if things get too hot:

```
linux-hwmonitor --daemon
```

# Configuration
Settings are saved in standard XDG format:
```
~/.config/linux-hwmonitor/config.json
```
You can edit this file directly, or use the Preferences menu inside the GUI.

Default thresholds (Warning / Critical):

CPU: 85 °C / 95 °C

GPU: 85 °C / 95 °C

Disk: 65 °C / 75 °C

Other: 80 °C / 90 °C

Other settings include Fahrenheit toggle, notification recovery toggle, polling interval, and "Start at login" (autostart).

# Uninstallation
### To remove the application:

```
sudo apt remove linux-hwmonitor
```

### To remove the application and the isolated Python environment created in /opt:

```
sudo apt purge linux-hwmonitor
```
(Note: Your personal configuration in ~/.config/linux-hwmonitor/ is never touched by apt and must be deleted manually if desired.)

# License
This project is licensed under the MIT License.
