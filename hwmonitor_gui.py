#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Linux HWMonitor - GTK4/libadwaita front end.

System packages (NOT pip):
  Debian/Ubuntu:  sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1
  Fedora:         sudo dnf install python3-gobject gtk4 libadwaita
"""
from __future__ import annotations

import sys
from typing import Dict, List, Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

import hwmonitor_core as core  # noqa: E402


CSS = """
.sensor-value  { font-feature-settings: "tnum"; }
.sensor-warn   { color: @warning_color; }
.sensor-crit   { color: @error_color; }
.big-temp      { font-size: 2.4em; font-weight: 800; }
"""


class SensorRow(Gtk.Box):
    def __init__(self, sensor: core.Sensor) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.sensor = sensor
        self._name = Gtk.Label(xalign=0, hexpand=True)
        self._name.add_css_class("dim-label")
        self._value = Gtk.Label(xalign=1)
        self._value.add_css_class("sensor-value")
        self._value.add_css_class("monospace")
        self.append(self._name)
        self.append(self._value)
        self.update(sensor, fahrenheit=False, level=0)

    def update(self, sensor: core.Sensor, fahrenheit: bool, level: int) -> None:
        self.sensor = sensor
        self._name.set_text(sensor.name)
        self._value.set_text(core.fmt_value(sensor.unit, sensor.value, fahrenheit))
        self._value.remove_css_class("sensor-warn")
        self._value.remove_css_class("sensor-crit")
        if level == 1:
            self._value.add_css_class("sensor-warn")
        elif level == 2:
            self._value.add_css_class("sensor-crit")


class SensorGroup(Gtk.Box):
    def __init__(self, group: str, kind: str) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.group = group
        self.kind = kind
        self._expander = Gtk.Expander(
            label=f"{core.KIND_TITLES.get(kind, kind)}", expanded=True
        )
        self._box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=2,
            margin_start=18, margin_top=4, margin_bottom=6,
        )
        self._rows: Dict[str, SensorRow] = {}
        self._expander.set_child(self._box)
        self.append(self._expander)

    def update(self, sensors: List[core.Sensor], fahrenheit: bool,
               levels: Dict[str, int]) -> None:
        seen = set()
        for s in sensors:
            seen.add(s.id)
            row = self._rows.get(s.id)
            if row is None:
                row = SensorRow(s)
                self._rows[s.id] = row
                self._box.append(row)
            row.update(s, fahrenheit, levels.get(s.id, 0))
        for sid in list(self._rows):
            if sid not in seen:
                self._box.remove(self._rows.pop(sid))
        self.set_visible(bool(self._rows))


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application, settings: core.Settings) -> None:
        super().__init__(application=app, title=core.APP_NAME,
                         default_width=520, default_height=760)
        self.settings = settings
        self.monitor = core.Monitor(settings, on_update=self._on_update)
        self.monitor.start()

        self._toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self._title = Adw.WindowTitle(title=core.APP_NAME, subtitle="Loading…")
        header.set_title_widget(self._title)

        menu_btn = Gtk.MenuButton(icon_name="open-menu-symbolic")
        menu_btn.set_popover(Gtk.PopoverMenu.new_from_model(self._build_menu()))
        header.pack_end(menu_btn)
        self._toolbar.add_top_bar(header)

        self._scroll = Gtk.ScrolledWindow(vexpand=True)
        self._content = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=10,
            margin_top=12, margin_bottom=12, margin_start=18, margin_end=18,
        )
        self._summary = Gtk.Label(xalign=0)
        self._summary.add_css_class("big-temp")
        self._summary.set_margin_bottom(6)
        self._content.append(self._summary)
        self._scroll.set_child(self._content)
        self._toolbar.set_content(self._scroll)
        self.set_content(self._toolbar)

        self._groups: Dict[tuple, SensorGroup] = {}
        GLib.timeout_add(500, self._tick)
        self.connect("close-request", self._on_close)

    def _build_menu(self) -> Gio.Menu:
        menu = Gio.Menu()
        menu.append("Refresh", "win.refresh")
        menu.append("Reset min/max", "win.reset")
        menu.append("Preferences", "win.settings")
        menu.append("Quit", "win.quit")
        return menu

    def _on_update(self) -> None:
        GLib.idle_add(self._refresh_ui)

    def _tick(self) -> bool:
        self._refresh_ui()
        return True

    def _refresh_ui(self) -> bool:
        sensors = self.monitor.snapshot()
        if not sensors:
            return False
        fahr = bool(self.settings["fahrenheit"])
        levels = {s.id: self.monitor.level(s.id) for s in sensors}

        hot = core.hottest(sensors)
        if hot and hot.value is not None:
            self._summary.set_text(f"🔥 {core.fmt_value(hot.unit, hot.value, fahr)}")
            self._summary.set_tooltip_text(f"{hot.group} · {hot.name}")
        else:
            self._summary.set_text("No temperatures")
        self._title.set_subtitle(
            f"{len(sensors)} sensors · every {self.settings['interval']:.0f}s"
        )

        buckets: Dict[tuple, List[core.Sensor]] = {}
        order: List[tuple] = []
        for s in sensors:
            key = (s.group, s.kind)
            if key not in buckets:
                buckets[key] = []
                order.append(key)
            buckets[key].append(s)

        existing = set()
        for key in order:
            g = self._groups.get(key)
            if g is None:
                g = SensorGroup(*key)
                self._groups[key] = g
                self._content.append(g)
            g.update(buckets[key], fahr, levels)
            existing.add(key)
        for key in list(self._groups):
            if key not in existing:
                self._content.remove(self._groups.pop(key))

        if self.monitor.worst_level() == 2:
            self._toolbar.add_css_class("error")
        else:
            self._toolbar.remove_css_class("error")
        return False

    def _on_close(self, *_):
        self.monitor.stop()
        return False

    def action_quit(self, *_):
        self.monitor.stop()
        self.get_application().quit()

    def action_refresh(self, *_):
        self.monitor.wake()

    def action_reset(self, *_):
        self.monitor.reset_minmax()
        self._refresh_ui()

    def action_settings(self, *_):
        PrefsWindow(self).present()


class PrefsWindow(Adw.PreferencesWindow):
    def __init__(self, parent: MainWindow) -> None:
        super().__init__(transient_for=parent, modal=True, title="Preferences")
        self.settings = parent.settings

        page = Adw.PreferencesPage()
        self.add(page)

        display = Adw.PreferencesGroup(title="Display")
        page.add(display)
        display.add(self._switch("Fahrenheit", "fahrenheit"))

        interval = Adw.SpinRow.new_with_range(1, 60, 1)
        interval.set_title("Refresh interval (s)")
        interval.set_value(float(self.settings["interval"]))
        interval.connect("changed", lambda r: self._set("interval", float(r.get_value())))
        display.add(interval)

        notif = Adw.PreferencesGroup(title="Notifications")
        page.add(notif)
        notif.add(self._switch("Desktop notifications", "notifications"))
        notif.add(self._switch("Notify on recovery", "notify_recovery"))

        startup = Adw.PreferencesGroup(title="Startup")
        page.add(startup)
        auto = Adw.SwitchRow(title="Start at login",
                             active=core.autostart_enabled())
        auto.connect("notify::active",
                     lambda r, _p: core.set_autostart(r.get_active()))
        startup.add(auto)

    def _switch(self, title: str, key: str) -> Adw.SwitchRow:
        row = Adw.SwitchRow(title=title, active=bool(self.settings[key]))
        row.connect("notify::active",
                    lambda r, _p: self._set(key, bool(r.get_active())))
        return row

    def _set(self, key: str, value) -> None:
        self.settings[key] = value
        self.settings.save()


class App(Adw.Application):
    def __init__(self, settings: core.Settings, start_minimized: bool) -> None:
        super().__init__(application_id=core.APP_ID,
                         flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.settings = settings
        self.start_minimized = start_minimized
        self.window: Optional[MainWindow] = None

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        display = Gdk.Display.get_default()
        if display is not None:
            provider = Gtk.CssProvider()
            provider.load_from_data(CSS.encode())
            Gtk.StyleContext.add_provider_for_display(
                display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )

    def do_activate(self) -> None:
        if not self.window:
            self.window = MainWindow(self, self.settings)
            self._install_actions(self.window)
        self.window.present()
        if self.start_minimized:
            self.window.minimize()

    def _install_actions(self, win: MainWindow) -> None:
        for name, cb in (
            ("quit", win.action_quit),
            ("refresh", win.action_refresh),
            ("reset", win.action_reset),
            ("settings", win.action_settings),
        ):
            act = Gio.SimpleAction.new(name, None)
            act.connect("activate", cb)
            win.add_action(act)


def main(settings: core.Settings, start_minimized: bool = False) -> int:
    app = App(settings, start_minimized=start_minimized)
    return app.run([sys.argv[0]])
