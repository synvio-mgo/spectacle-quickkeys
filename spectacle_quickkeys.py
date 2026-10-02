#!/usr/bin/env python3
"""spectacle-quickkeys - single-key annotation tool shortcuts for KDE Spectacle.

Spectacle has no keyboard shortcuts for its annotation tools; you have to click
the toolbar every time. This daemon adds them on Wayland without patching
Spectacle, by combining two standard KDE/freedesktop mechanisms:

  * AT-SPI (accessibility) to see which window is active and to press the
    annotation tool buttons in Spectacle's toolbar.
  * KWin's org.freedesktop.a11y.KeyboardMonitor D-Bus interface to grab the
    bound keys - but only while Spectacle's editor window is focused.

See README.md for the details and the caveats.
"""

from __future__ import annotations

import argparse
import gettext
import logging
import os
import sys
import time
import tomllib
import warnings
from pathlib import Path

import gi

gi.require_version("Atspi", "2.0")
from gi.repository import Atspi, Gio, GLib  # noqa: E402

warnings.filterwarnings("ignore", category=DeprecationWarning)

log = logging.getLogger("spectacle-quickkeys")

A11Y_STATUS = ("org.a11y.Bus", "/org/a11y/bus", "org.a11y.Status")

KBD_NAME = "org.freedesktop.a11y.Manager"
KBD_PATH = "/org/freedesktop/a11y/Manager"
KBD_IFACE = "org.freedesktop.a11y.KeyboardMonitor"

# KWin only lets the owner of this bus name use the keyboard monitor. It is
# Orca's name; we claim it when Orca is not running. See README.md.
SCREEN_READER_NAME = "org.gnome.Orca.KeyboardMonitor"

# Shift | Control | Alt | Super. CapsLock and NumLock are deliberately ignored.
MOD_MASK = 0x01 | 0x04 | 0x08 | 0x40

# Spectacle shows the same annotation tools in two places with slightly
# different toolbars: the editor window has an extra "None" button in front of
# the row, the capture overlay does not (and puts Undo/Redo before it). Each
# tool is listed with its English label and the gettext context Spectacle
# marks it with, so the labels can be looked up in Spectacle's own message
# catalog when the UI is not in English.
TOOLS = {
    "none": ("None", "@action:intoolbar no tool, to allow dragging"),
    "crop": ("Crop", "@action:intoolbar crop image tool"),
    "select": ("Select", "@action:intoolbar select annotation tool"),
    "freehand": ("Freehand", "@action:intoolbar freehand line tool"),
    "highlighter": ("Highlighter", "@action:intoolbar highlighter line tool"),
    "line": ("Line", "@action:intoolbar straight line tool"),
    "arrow": ("Arrow", "@action:intoolbar arrow line tool"),
    "rectangle": ("Rectangle", "@action:intoolbar rectangle tool"),
    "ellipse": ("Ellipse", "@action:intoolbar ellipse tool"),
    "pixelate": ("Pixelate", "@action:intoolbar pixelate tool"),
    "blur": ("Blur", "@action:intoolbar blur tool"),
    "text": ("Text", "@action:intoolbar text tool"),
    "number": ("Number", "@action:intoolbar number stamp tool"),
}

TOOL_ORDER = list(TOOLS)

# Tool buttons the annotation toolbar must have before we believe it is one.
# Spectacle's capture settings page is also a row of check boxes, and pressing
# one of those would silently change the user's settings.
MIN_TOOL_MATCHES = 10

# Tools that expect you to type, so the grab has to be released for them.
TYPING_TOOLS = {"text"}


def desktop_languages() -> list[str]:
    """Languages Spectacle itself will be using, Plasma's setting first."""
    langs = []
    localerc = Path(GLib.get_user_config_dir()) / "plasma-localerc"
    try:
        for line in localerc.read_text().splitlines():
            if line.startswith("LANGUAGE="):
                langs += [v for v in line.split("=", 1)[1].split(":") if v]
    except OSError:
        pass
    for var in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        value = os.environ.get(var)
        if value:
            langs += [v.split(".")[0] for v in value.split(":") if v]
    langs.append("en")
    return langs


def tool_names() -> dict:
    """Accepted accessible names per tool: English, plus Spectacle's own
    translation of the label for the languages the desktop is set to."""
    names = {tool: {english} for tool, (english, _ctx) in TOOLS.items()}
    try:
        catalog = gettext.translation("spectacle", "/usr/share/locale",
                                      languages=desktop_languages())
    except OSError:
        log.debug("no spectacle message catalog, matching English labels only")
        return names
    for tool, (english, context) in TOOLS.items():
        names[tool].add(catalog.pgettext(context, english))
    return names


TOOL_NAMES = {}          # filled in by init_tool_names()
KNOWN_TOOL_NAMES = set()


def init_tool_names() -> None:
    global TOOL_NAMES, KNOWN_TOOL_NAMES
    TOOL_NAMES = tool_names()
    KNOWN_TOOL_NAMES = {n for names in TOOL_NAMES.values() for n in names}
    log.debug("tool labels: %s", sorted(KNOWN_TOOL_NAMES))


DEFAULTS = {
    "bindings": {"a": "arrow", "l": "line"},
    "options": {
        "auto_enable_annotate": True,
        "release_on_text_tool": True,
        "watch_fallback": False,
    },
}

CONFIG_PATH = Path(
    GLib.get_user_config_dir()) / "spectacle-quickkeys" / "config.toml"


# --------------------------------------------------------------------------
# configuration


def load_config(path: Path = CONFIG_PATH) -> dict:
    cfg = {"bindings": dict(DEFAULTS["bindings"]),
           "options": dict(DEFAULTS["options"])}
    if not path.exists():
        log.info("no config at %s, using defaults", path)
        return cfg
    with path.open("rb") as fh:
        raw = tomllib.load(fh)
    if "bindings" in raw:
        cfg["bindings"] = dict(raw["bindings"])
    cfg["options"].update(raw.get("options", {}))

    bindings = {}
    for key, tool in cfg["bindings"].items():
        if len(key) != 1 or not (32 < ord(key) < 127):
            log.error("ignoring binding %r: keys must be one printable "
                      "ASCII character", key)
            continue
        if tool not in TOOL_ORDER:
            log.error("ignoring binding %r: unknown tool %r (known: %s)",
                      key, tool, ", ".join(TOOL_ORDER))
            continue
        bindings[key] = tool
    cfg["bindings"] = bindings
    return cfg


# --------------------------------------------------------------------------
# AT-SPI helpers


def silence_atspi_cache_warnings() -> None:
    """Drop libatspi's warnings about snap-confined apps it may not query.

    AppArmor blocks org.a11y.atspi.Cache calls into confined applications
    (Chromium, Vivaldi, ...). We never look at those, but libatspi warns about
    every one of them, which floods the journal.
    """
    GLib.log_set_handler(
        "dbind",
        GLib.LogLevelFlags.LEVEL_WARNING | GLib.LogLevelFlags.LEVEL_MESSAGE,
        lambda *_args: None, None)


def app_name_of(node) -> str:
    try:
        app = node.get_application()
        return app.get_name() if app else ""
    except Exception:
        return ""


def is_checked(node) -> bool:
    try:
        return node.get_state_set().contains(Atspi.StateType.CHECKED)
    except Exception:
        return False


def do_named_action(node, action: str) -> bool:
    """Invoke `action` on an AT-SPI node, falling back to its first action."""
    try:
        iface = node.get_action_iface()
        if iface is None:
            return False
        names = [Atspi.Action.get_action_name(iface, i)
                 for i in range(Atspi.Action.get_n_actions(iface))]
        index = names.index(action) if action in names else 0
        return bool(Atspi.Action.do_action(iface, index))
    except Exception as exc:
        log.debug("action %r failed: %s", action, exc)
        return False


def find(node, predicate, depth: int = 0, max_depth: int = 12):
    if depth > max_depth:
        return None
    try:
        if predicate(node):
            return node
        count = node.get_child_count()
    except Exception:
        return None
    for i in range(count):
        try:
            child = node.get_child_at_index(i)
        except Exception:
            continue
        if child is None:
            continue
        found = find(child, predicate, depth + 1, max_depth)
        if found is not None:
            return found
    return None


def find_all(node, predicate, depth: int = 0, max_depth: int = 12,
             out: list | None = None) -> list:
    out = [] if out is None else out
    if depth > max_depth:
        return out
    try:
        if predicate(node):
            out.append(node)
            return out
        count = node.get_child_count()
    except Exception:
        return out
    for i in range(count):
        try:
            child = node.get_child_at_index(i)
        except Exception:
            continue
        if child is not None:
            find_all(child, predicate, depth + 1, max_depth, out)
    return out


def is_showing(node) -> bool:
    try:
        return node.get_state_set().contains(Atspi.StateType.SHOWING)
    except Exception:
        return False


def tool_boxes(node) -> list:
    """The check boxes directly inside a container, in toolbar order."""
    boxes = []
    try:
        for i in range(node.get_child_count()):
            child = node.get_child_at_index(i)
            if child is not None and child.get_role_name() == "check box":
                boxes.append(child)
    except Exception:
        return []
    return boxes


def looks_like_tool_panel(node) -> bool:
    """Recognise the annotation toolbar by its tool labels.

    Counting check boxes is not enough: Spectacle's capture settings page is
    also a row of check boxes, and pressing one of those would change a
    setting instead of picking a tool.
    """
    matches = sum(1 for box in tool_boxes(node)
                  if name_of(box) in KNOWN_TOOL_NAMES)
    return matches >= MIN_TOOL_MATCHES


def name_of(node) -> str:
    try:
        return node.get_name()
    except Exception:
        return ""


def tool_node(panel, tool: str):
    """Find one tool button, by label, else by position from the end of the row."""
    boxes = tool_boxes(panel)
    names = TOOL_NAMES.get(tool, set())
    for box in boxes:
        if name_of(box) in names:
            return box
    offset = TOOL_ORDER.index(tool) - len(TOOL_ORDER)
    return boxes[offset] if -offset <= len(boxes) else None


# --------------------------------------------------------------------------


class QuickKeys:
    def __init__(self, config: dict):
        self.bindings = config["bindings"]
        self.options = config["options"]
        self.keysyms = {ord(key): tool for key, tool in self.bindings.items()}

        self.session = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self.kbd = None
        self.has_name = False
        self.name_id = 0

        self.spectacle_active = False
        self.typing_tool = False
        self.typing_focus = False
        self.grabs_on = False

        self._panel = None
        self._listeners = []
        self._last_key = (0, 0.0)
        self.loop = GLib.MainLoop()

    # -- accessibility bus ------------------------------------------------

    def enable_accessibility(self) -> None:
        """Qt only publishes its widget tree when the a11y bus is enabled."""
        name, path, iface = A11Y_STATUS
        try:
            enabled = self.session.call_sync(
                name, path, "org.freedesktop.DBus.Properties", "Get",
                GLib.Variant("(ss)", (iface, "IsEnabled")), None,
                Gio.DBusCallFlags.NONE, 3000, None)[0]
            if enabled:
                return
            self.session.call_sync(
                name, path, "org.freedesktop.DBus.Properties", "Set",
                GLib.Variant("(ssv)", (iface, "IsEnabled",
                                       GLib.Variant("b", True))),
                None, Gio.DBusCallFlags.NONE, 3000, None)
            log.info("enabled the accessibility bus (org.a11y.Status.IsEnabled)")
        except GLib.Error as exc:
            log.warning("could not enable the accessibility bus: %s", exc)

    # -- Spectacle's widget tree ------------------------------------------

    def spectacle_app(self):
        try:
            desktop = Atspi.get_desktop(0)
            for i in range(desktop.get_child_count()):
                child = desktop.get_child_at_index(i)
                if child is not None and child.get_name() == "spectacle":
                    return child
        except Exception as exc:
            log.debug("desktop scan failed: %s", exc)
        return None

    def tool_panel(self):
        """The annotation toolbar.

        There is one in the editor window and one in the capture overlay, and
        the overlay builds a window per screen, so prefer a visible one. The
        editor's toolbar is not "showing" until annotation mode is on, which
        is also exactly when the cache would be wrong, so only cache visible
        ones.
        """
        if self._panel is not None:
            if looks_like_tool_panel(self._panel) and is_showing(self._panel):
                return self._panel
            self._panel = None
        app = self.spectacle_app()
        if app is None:
            return None
        panels = find_all(app, looks_like_tool_panel)
        if not panels:
            return None
        panel = next((p for p in panels if is_showing(p)), panels[0])
        self._panel = panel if is_showing(panel) else None
        return panel

    def annotate_toggle(self):
        """Spectacle's "Edit..." button: the only check box in the main toolbar."""
        app = self.spectacle_app()
        if app is None:
            return None
        toolbar = find(app, lambda n: n.get_role_name() == "tool bar")
        if toolbar is None:
            return None
        return find(toolbar, lambda n: n.get_role_name() == "check box",
                    max_depth=3)

    def select_tool(self, tool: str) -> bool:
        """Pick an annotation tool.

        Use the "Press" action, not "Toggle": Toggle only flips the check
        box's visual state, so the button lights up while Spectacle carries on
        with the previous tool. Press is what a mouse click does. Pressing an
        already-active tool is a no-op in Spectacle, so this is safe to repeat.
        """
        panel = self.tool_panel()
        if panel is None:
            log.warning("Spectacle's annotation toolbar was not found")
            return False

        if self.options["auto_enable_annotate"]:
            self.enable_annotate_mode()

        node = tool_node(panel, tool)
        if node is None:
            log.warning("tool %r was not found in the toolbar", tool)
            return False
        return do_named_action(node, "Press")

    def enable_annotate_mode(self) -> None:
        """Switch the editor window's "Edit..." toggle on if it is off.

        The capture overlay has no such toggle; there the tools are always
        live and this does nothing.
        """
        toggle = self.annotate_toggle()
        if toggle is None or is_checked(toggle):
            return
        do_named_action(toggle, "Press")
        time.sleep(0.1)
        if not is_checked(toggle):
            do_named_action(toggle, "Toggle")
            time.sleep(0.1)

    # -- key grabs --------------------------------------------------------

    @property
    def blocked(self) -> bool:
        if not self.options["release_on_text_tool"]:
            return False
        return self.typing_tool or self.typing_focus

    def call_kbd(self, method: str, params=None) -> bool:
        if self.kbd is None:
            return False
        try:
            self.kbd.call_sync(method, params, Gio.DBusCallFlags.NONE,
                               3000, None)
            return True
        except GLib.Error as exc:
            log.error("%s failed: %s", method, exc.message)
            return False

    def sync_grabs(self) -> None:
        want = self.has_name and self.spectacle_active and not self.blocked
        if want == self.grabs_on:
            return
        if want:
            if self.options["watch_fallback"]:
                self.call_kbd("WatchKeyboard")
            strokes = [(keysym, 0) for keysym in self.keysyms]
            self.call_kbd("SetKeyGrabs",
                          GLib.Variant("(aua(uu))", ([], strokes)))
            log.debug("grabbing %s", ", ".join(sorted(self.bindings)))
        else:
            self.call_kbd("SetKeyGrabs", GLib.Variant("(aua(uu))", ([], [])))
            if self.options["watch_fallback"]:
                self.call_kbd("UnwatchKeyboard")
            log.debug("released key grabs")
        self.grabs_on = want

    def release_grabs(self) -> None:
        self.spectacle_active = False
        self.sync_grabs()

    # -- events -----------------------------------------------------------

    def on_key_event(self, _conn, _sender, _path, _iface, _signal, params):
        released, state, keysym, _unichar, _keycode = params.unpack()
        if released or state & MOD_MASK:
            return
        if not (self.spectacle_active and not self.blocked):
            return
        tool = self.keysyms.get(keysym)
        if tool is None:
            return

        now = time.monotonic()
        last_sym, last_at = self._last_key
        if keysym == last_sym and now - last_at < 0.15:
            return  # key repeat, or the same event via both grab and watch
        self._last_key = (keysym, now)

        log.debug("key %r -> tool %r", chr(keysym), tool)
        self.select_tool(tool)

    def on_window_event(self, event):
        if event.type == "window:activate":
            active = app_name_of(event.source) == "spectacle"
            if active and not self.spectacle_active:
                self._panel = None
                self.typing_focus = False
                self.typing_tool = self.detect_typing_tool()
            self.spectacle_active = active
        elif app_name_of(event.source) == "spectacle":
            self.spectacle_active = False
        self.sync_grabs()

    def on_tool_checked(self, event):
        if event.detail1 != 1 or app_name_of(event.source) != "spectacle":
            return
        try:
            parent = event.source.get_parent()
            name = event.source.get_name()
            index = event.source.get_index_in_parent()
        except Exception:
            return
        if parent is None or not looks_like_tool_panel(parent):
            return  # not the annotation toolbar, e.g. the "Edit..." button
        typing = False
        for tool in TYPING_TOOLS:
            node = tool_node(parent, tool)
            if node is None:
                continue
            try:
                if name in TOOL_NAMES[tool] or node.get_index_in_parent() == index:
                    typing = True
            except Exception:
                continue
        self.typing_tool = typing
        self.sync_grabs()

    def on_focus(self, event):
        if app_name_of(event.source) != "spectacle":
            return
        try:
            role = event.source.get_role_name()
            editable = event.source.get_state_set().contains(
                Atspi.StateType.EDITABLE)
        except Exception:
            return
        self.typing_focus = editable and role in {
            "text", "entry", "paragraph", "document text", "password text"}
        self.sync_grabs()

    def detect_typing_tool(self) -> bool:
        panel = self.tool_panel()
        if panel is None:
            return False
        return any(is_checked(node)
                   for tool in TYPING_TOOLS
                   if (node := tool_node(panel, tool)) is not None)

    def watchdog(self) -> bool:
        """Never leave keys grabbed after Spectacle has gone away."""
        if self.grabs_on and self.spectacle_app() is None:
            log.debug("Spectacle disappeared, releasing key grabs")
            self.release_grabs()
        return True

    # -- lifecycle --------------------------------------------------------

    def on_name_acquired(self, _conn, _name):
        self.has_name = True
        log.info("acquired %s", SCREEN_READER_NAME)
        try:
            self.kbd = Gio.DBusProxy.new_sync(
                self.session, Gio.DBusProxyFlags.NONE, None,
                KBD_NAME, KBD_PATH, KBD_IFACE, None)
        except GLib.Error as exc:
            log.error("KWin's keyboard monitor is unavailable: %s", exc.message)
            self.loop.quit()
            return
        self.session.signal_subscribe(
            KBD_NAME, KBD_IFACE, "KeyEvent", KBD_PATH, None,
            Gio.DBusSignalFlags.NONE, self.on_key_event)
        self.sync_grabs()

    def on_name_lost(self, _conn, _name):
        if self.has_name:
            log.error("lost %s, exiting", SCREEN_READER_NAME)
        else:
            log.error("could not claim %s - a screen reader (Orca) is "
                      "probably running. Stop it, or stop this daemon.",
                      SCREEN_READER_NAME)
        self.has_name = False
        self.loop.quit()

    def listen(self, event_type: str, handler) -> None:
        listener = Atspi.EventListener.new(handler)
        listener.register(event_type)
        self._listeners.append(listener)  # keep it alive

    def run(self) -> int:
        if not self.bindings:
            log.error("no usable bindings configured")
            return 1
        Atspi.init()
        silence_atspi_cache_warnings()
        init_tool_names()
        self.enable_accessibility()

        self.listen("window:activate", self.on_window_event)
        self.listen("window:deactivate", self.on_window_event)
        self.listen("object:state-changed:checked", self.on_tool_checked)
        self.listen("focus:", self.on_focus)
        self.listen("object:state-changed:focused", self.on_focus)

        self.name_id = Gio.bus_own_name(
            Gio.BusType.SESSION, SCREEN_READER_NAME,
            Gio.BusNameOwnerFlags.NONE, None,
            self.on_name_acquired, self.on_name_lost)

        GLib.timeout_add_seconds(5, self.watchdog)
        for sig in (1, 2, 15):  # SIGHUP, SIGINT, SIGTERM
            GLib.unix_signal_add(GLib.PRIORITY_HIGH, sig, self.quit)

        log.info("ready: %s", ", ".join(
            f"{key} -> {tool}" for key, tool in sorted(self.bindings.items())))
        self.loop.run()
        self.release_grabs()
        return 0

    def quit(self) -> bool:
        log.info("shutting down")
        self.loop.quit()
        return False


# --------------------------------------------------------------------------


def check(config: dict) -> int:
    """Print what the daemon can and cannot see right now."""
    ok = True
    qk = QuickKeys(config)
    Atspi.init()
    silence_atspi_cache_warnings()
    init_tool_names()
    qk.enable_accessibility()

    print("bindings:")
    for key, tool in sorted(config["bindings"].items()):
        print(f"  {key} -> {tool}")

    owner = None
    try:
        owner = qk.session.call_sync(
            "org.freedesktop.DBus", "/org/freedesktop/DBus",
            "org.freedesktop.DBus", "GetNameOwner",
            GLib.Variant("(s)", (SCREEN_READER_NAME,)), None,
            Gio.DBusCallFlags.NONE, 3000, None)[0]
    except GLib.Error:
        pass
    if owner:
        print(f"\n{SCREEN_READER_NAME}: taken by {owner}")
        print("  -> expected if the daemon is already running; otherwise a")
        print("     screen reader has it and the daemon cannot start")
    else:
        print(f"\n{SCREEN_READER_NAME}: free")

    try:
        Gio.DBusProxy.new_sync(qk.session, Gio.DBusProxyFlags.NONE, None,
                               KBD_NAME, KBD_PATH, KBD_IFACE, None)
        print(f"{KBD_IFACE}: available")
    except GLib.Error as exc:
        print(f"{KBD_IFACE}: MISSING ({exc.message})")
        print("  -> needs KWin (Plasma 6.2+) on Wayland")
        ok = False

    app = qk.spectacle_app()
    if app is None:
        print("\nSpectacle: not running, or not visible on the a11y bus.")
        print("  -> start Spectacle and run this again; if it was already")
        print("     running before accessibility got enabled, restart it.")
        return 0 if ok else 1

    panel = qk.tool_panel()
    if panel is None:
        print("\nSpectacle: running, but its annotation toolbar was not found.")
        return 1
    print("\nSpectacle annotation toolbar found:")
    for tool in config["bindings"].values():
        node = tool_node(panel, tool)
        state = "missing" if node is None else (
            "selected" if is_checked(node) else "ok")
        print(f"  {tool:12s} {state}")
    toggle = qk.annotate_toggle()
    print(f"  {'annotate':12s} "
          f"{'missing' if toggle is None else 'ok'}")
    return 0 if ok else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="spectacle-quickkeys",
        description="Single-key annotation tool shortcuts for KDE Spectacle.")
    parser.add_argument("-c", "--config", type=Path, default=CONFIG_PATH,
                        help=f"config file (default: {CONFIG_PATH})")
    parser.add_argument("--check", action="store_true",
                        help="report what is detected, then exit")
    parser.add_argument("-v", "--debug", action="store_true",
                        help="verbose logging")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(levelname)s: %(message)s")

    config = load_config(args.config)
    if args.check:
        return check(config)
    return QuickKeys(config).run()


if __name__ == "__main__":
    sys.exit(main())
