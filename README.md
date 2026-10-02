# spectacle-quickkeys

Press <kbd>a</kbd> for the arrow tool and <kbd>l</kbd> for the line tool while
you are in KDE Spectacle's editor. Spectacle itself has no keyboard shortcuts
for its annotation tools, so you normally have to go back to the toolbar for
every single annotation.

This is a small user-space daemon. It does not patch or rebuild Spectacle.

* Works on **Plasma 6 / Wayland** (tested on Plasma 6.6, Spectacle 6.6,
  Ubuntu 26.04).
* Works in both places Spectacle offers annotation: the rectangular-region
  capture overlay and the editor window.
* The keys only do anything while a Spectacle window is focused. Everywhere
  else they type normally.
* In the editor window, if annotation mode is off the first keypress turns it
  on *and* picks the tool, so one key is enough.

## Install

Dependencies (already present on a default Plasma desktop):

```bash
sudo apt install python3-gi gir1.2-atspi-2.0
```

Then:

```bash
./install.sh
```

That copies the script to `~/.local/bin/spectacle-quickkeys`, installs a
systemd user service, and drops a config at
`~/.config/spectacle-quickkeys/config.toml`.

Verify:

```bash
spectacle-quickkeys --check
```

Uninstall with `./install.sh --uninstall`.

> If Spectacle was already running before the daemon first started, restart
> Spectacle once. Qt only publishes its widget tree to the accessibility bus
> when that bus is enabled, and the daemon enables it at startup.

## Configuration

`~/.config/spectacle-quickkeys/config.toml`, see `config.example.toml`:

```toml
[bindings]
a = "arrow"
l = "line"
r = "rectangle"
```

Bindable tools: `none`, `crop`, `select`, `freehand`, `highlighter`, `line`,
`arrow`, `rectangle`, `ellipse`, `pixelate`, `blur`, `text`, `number`.

Restart after editing: `systemctl --user restart spectacle-quickkeys`.

## How it works

Two standard mechanisms, no patching:

1. **AT-SPI** (the accessibility bus) exposes Spectacle's annotation toolbar
   as a row of check boxes named `Arrow`, `Line`, and so on. The daemon
   watches `window:activate` to know when Spectacle is focused, and invokes
   the button's **`Press`** action when a bound key is pressed.

   `Press`, not `Toggle`. `Toggle` only flips the check box's visual state:
   the button lights up while Spectacle carries on drawing with the previous
   tool. `Press` is what a mouse click does. Pressing an already-active tool
   is a no-op in Spectacle, so repeats are harmless.

   The two toolbars differ - the editor has an extra `None` button in front
   and an `Edit...` toggle to switch annotation mode on, the capture overlay
   has neither and puts Undo/Redo in front instead. The row is identified by
   its tool labels rather than by counting check boxes, because Spectacle's
   capture settings page is *also* a row of check boxes and pressing one of
   those would silently change a setting. Labels for non-English desktops
   come from Spectacle's own message catalog in `/usr/share/locale`.
2. **KWin's `org.freedesktop.a11y.KeyboardMonitor`** D-Bus interface grabs the
   bound keys. This is the mechanism screen readers use on Wayland, where an
   application cannot simply grab the keyboard. The grab is installed when
   Spectacle gains focus and removed when it loses focus, so `a` and `l` keep
   working normally in every other application.

## Caveats, and they matter

**It claims Orca's D-Bus name.** KWin only accepts keyboard-monitor calls from
the owner of `org.gnome.Orca.KeyboardMonitor`. There is no other way in;
that name *is* the permission check. The daemon therefore requests that name,
and refuses to start if something else already holds it. **If you use the Orca
screen reader, do not run this daemon** — whichever starts second loses.

**It can see keystrokes while Spectacle is focused.** With
`watch_fallback = true` (the default) the daemon calls `WatchKeyboard`, which
delivers every key pressed while it is armed — and it is only armed while
Spectacle's editor has focus. Nothing is logged or stored; keys that are not
bound are dropped immediately. Set `watch_fallback = false` if you want the
daemon to receive only the keys it explicitly grabbed. It is on by default
because the explicit grab is the part that depends on KWin's exact struct
layout, and the fallback makes the shortcuts work either way.

**Typing in a text annotation.** While the Text tool is selected, or a text
field has focus, the grabs are dropped so letters reach Spectacle normally.
If you ever do lose a letter there, set `release_on_text_tool = true` (the
default) and file an issue with `--debug` output.

**Accessibility stays enabled.** The daemon sets
`org.a11y.Status.IsEnabled = true` and does not turn it back off. That is what
makes Qt applications publish their widget tree. It does *not* touch
`ScreenReaderEnabled`.

**Safety net.** Grabs are released on window change, on shutdown (`SIGTERM`,
`SIGINT`), and by a 5 second watchdog if Spectacle vanishes. KWin also drops
them when the daemon's bus name disappears, so a crash cannot leave your
keyboard missing an `a`.

## Debugging

```bash
systemctl --user stop spectacle-quickkeys
spectacle-quickkeys --debug
```

You should see `grabbing a, l` when Spectacle gets focus, `released key grabs`
when it loses focus, and `key 'a' -> tool 'arrow'` on a keypress.

## License

MIT
