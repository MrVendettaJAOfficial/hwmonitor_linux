# Linux HWMonitor

A lightweight, dependency-light hardware monitor for Linux with a GTK4
front end, a live terminal mode, a JSON/CLI mode and a headless alert
daemon.

## Install

```bash
git clone https://github.com/MrVendettaJAOfficial/hwmonitor_linux.git
cd hwmonitor-linux
pip install --user .
# GUI extras (Debian/Ubuntu):
sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1
# Fedora:
sudo dnf install python3-gobject gtk4 libadwaita libnotify
