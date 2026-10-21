#!/usr/bin/env bash
# Build a self-contained .deb for linux-hwmonitor.
# Requires: dpkg-deb (part of dpkg, already installed on every Debian/Ubuntu)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PKG="linux-hwmonitor"
VER="1.0.0"
DEB_NAME="${PKG}_${VER}_all.deb"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

# ---- sanity check ----
for f in hwmonitor.py hwmonitor_core.py hwmonitor_gui.py pyproject.toml; do
    [[ -f "$HERE/$f" ]] || { echo "missing: $f" >&2; exit 1; }
done
[[ -f "$HERE/data/linux-hwmonitor.desktop" ]] || { echo "missing: data/linux-hwmonitor.desktop" >&2; exit 1; }
[[ -f "$HERE/data/linux-hwmonitor.svg"     ]] || { echo "missing: data/linux-hwmonitor.svg" >&2; exit 1; }

echo ">> staging into $STAGE"

# ---- app files -> /opt/linux-hwmonitor/app ----
APP="$STAGE/opt/linux-hwmonitor/app"
mkdir -p "$APP"
install -m 0644 "$HERE/hwmonitor_core.py" "$APP/"
install -m 0755 "$HERE/hwmonitor.py"      "$APP/"
install -m 0644 "$HERE/hwmonitor_gui.py"  "$APP/"
install -m 0644 "$HERE/pyproject.toml"    "$APP/"
[[ -f "$HERE/README.md" ]] && install -m 0644 "$HERE/README.md" "$APP/"

# ---- launcher -> /usr/bin/linux-hwmonitor ----
mkdir -p "$STAGE/usr/bin"
cat > "$STAGE/usr/bin/linux-hwmonitor" <<'LAUNCHER'
#!/bin/sh
VENV=/opt/linux-hwmonitor/venv
APP=/opt/linux-hwmonitor/app/hwmonitor.py
[ -x "$VENV/bin/python" ] || { echo "linux-hwmonitor: venv missing; reinstall" >&2; exit 1; }
exec "$VENV/bin/python" "$APP" "$@"
LAUNCHER
chmod 0755 "$STAGE/usr/bin/linux-hwmonitor"

# ---- desktop entry ----
mkdir -p "$STAGE/usr/share/applications"
install -m 0644 "$HERE/data/linux-hwmonitor.desktop" "$STAGE/usr/share/applications/"

# ---- icon ----
mkdir -p "$STAGE/usr/share/icons/hicolor/scalable/apps"
install -m 0644 "$HERE/data/linux-hwmonitor.svg" \
    "$STAGE/usr/share/icons/hicolor/scalable/apps/"

# ---- DEBIAN/ ----
CTRL="$STAGE/DEBIAN"
mkdir -p "$CTRL"

cat > "$CTRL/control" <<'EOF'
Package: linux-hwmonitor
Version: 1.0.0
Architecture: all
Maintainer: Vendettaja <vendettaja@localhost>
Section: utils
Priority: optional
Depends: python3 (>= 3.9), python3-gi, python3-gi-cairo, gir1.2-gtk-4.0, gir1.2-adw-1, libnotify-bin
Recommends: nvidia-utils
Description: Hardware temperature and sensor monitor for Linux
 Reads temperatures, fans, voltages, power, currents, clocks, memory
 and battery status from sysfs, procfs, RAPL and nvidia-smi. Ships a
 GTK4/libadwaita GUI, a live terminal mode, a JSON dump and a headless
 alert daemon with desktop notifications.
EOF

cat > "$CTRL/postinst" <<'EOF'
#!/bin/sh
set -e
if [ "$1" = "configure" ]; then
    VENV=/opt/linux-hwmonitor/venv
    [ -x "$VENV/bin/python" ] || python3 -m venv --system-site-packages "$VENV"
    "$VENV/bin/pip" install --quiet --no-deps --force-reinstall \
        /opt/linux-hwmonitor/app || true
    command -v update-desktop-database >/dev/null 2>&1 && \
        update-desktop-database -q /usr/share/applications || true
    command -v gtk-update-icon-cache  >/dev/null 2>&1 && \
        gtk-update-icon-cache -q -t -f /usr/share/icons/hicolor || true
fi
exit 0
EOF
chmod 0755 "$CTRL/postinst"

cat > "$CTRL/prerm" <<'EOF'
#!/bin/sh
set -e
command -v pkill >/dev/null 2>&1 && pkill -TERM -f '/opt/linux-hwmonitor/' || true
exit 0
EOF
chmod 0755 "$CTRL/prerm"

cat > "$CTRL/postrm" <<'EOF'
#!/bin/sh
set -e
[ "$1" = "purge" ] && rm -rf /opt/linux-hwmonitor/venv || true
command -v update-desktop-database >/dev/null 2>&1 && \
    update-desktop-database -q /usr/share/applications || true
command -v gtk-update-icon-cache  >/dev/null 2>&1 && \
    gtk-update-icon-cache -q -t -f /usr/share/icons/hicolor || true
exit 0
EOF
chmod 0755 "$CTRL/postrm"

mkdir -p "$STAGE/usr/share/doc/$PKG"
cat > "$STAGE/usr/share/doc/$PKG/copyright" <<'EOF'
Upstream-Name: linux-hwmonitor
Copyright: 2024 Vendettaja
License: MIT
 Permission is hereby granted, free of charge, to any person obtaining a copy
 of this software and associated documentation files (the "Software"), to deal
 in the Software without restriction, including without limitation the rights
 to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
 copies of the Software, and to permit persons to whom the Software is
 furnished to do so, subject to the following conditions:
 .
 The above copyright notice and this permission notice shall be included in
 all copies or substantial portions of the Software.
 .
 THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
 IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
 FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
 AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
 LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
 OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
 SOFTWARE.
EOF

cat > "$STAGE/usr/share/doc/$PKG/changelog" <<EOF
$PKG ($VER-1) unstable; urgency=medium

  * Initial release.

 -- Vendettaja <vendettaja@localhost>  $(date -R)
EOF
gzip -9 -n "$STAGE/usr/share/doc/$PKG/changelog"

( cd "$STAGE" && find opt usr -type f -print0 | xargs -0 md5sum > "$CTRL/md5sums" )

# ---- build ----
mkdir -p "$HERE/dist"
OUT="$HERE/dist/$DEB_NAME"
echo ">> building $OUT"
dpkg-deb --root-owner-group --build "$STAGE" "$OUT" >/dev/null

echo
echo ">> info:"
dpkg-deb --info "$OUT" | sed -n '1,15p'
echo
echo ">> files:"
dpkg-deb --contents "$OUT" | awk '{print "   " $6}'
echo
echo "OK -> $OUT"
echo "Install:  sudo apt install ./$OUT"
echo "Remove:   sudo apt remove $PKG"
echo "Purge:    sudo apt purge $PKG"
