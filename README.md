# Linux HWMonitor

Hardware monitor for Linux. Reads hwmon, thermal zones, RAPL, memory,
battery and NVIDIA GPUs. GTK4/libadwaita GUI + CLI + JSON + daemon.

## Install

```bash
sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-4.0 gir1.2-adw-1 libnotify-bin

cd ~/hwmonitor_linux
python3 -m venv --system-site-packages .venv
source .venv/bin/activate
pip install -e .

./install.sh          # creates the desktop launcher
