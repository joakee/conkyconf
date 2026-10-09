#!/usr/bin/env python3
"""Live countdowns for the conky countdowns card.

The countdowns themselves are owned by the Countdowns GTK app
(~/.local/bin/countdowns), which writes them to

    $XDG_DATA_HOME/countdowns/countdowns.json

with an atomic rename, so this never sees half a file. Only countdowns whose
switch is on in the app are shown here. Targets are local wall-clock times.

Emits conky markup (the card uses ${execpi 1}), so the card grows and shrinks
with the number of countdowns and bin/reflow.py keeps the gaps.

With nothing to show, the card hides altogether: this script unmaps its own
conky window, and bin/reflow.py closes the stack up over a card that has done
that. It is mapped again the moment a countdown is switched on. The X work only
happens on a change -- the last state is cached per conky pid, so a restarted
card (which comes up mapped) is caught too -- and only then is python-xlib
imported, so a normal tick stays stdlib-only and as cheap as before.
"""

import json
import math
import os
import sys
import time
from datetime import datetime

DATA = os.path.join(os.environ.get("XDG_DATA_HOME")
                    or os.path.expanduser("~/.local/share"),
                    "countdowns", "countdowns.json")

MAX_SHOWN = int(os.environ.get("COUNTDOWNS_MAX", 6))
# A countdown that has passed reads "done" for this long, then drops off.
DONE_HOLD = int(os.environ.get("COUNTDOWNS_DONE_HOLD", 6 * 3600))
LABEL_MAX = 22    # chars at size 9 that fit beside a size-11 bold time

# Row spacing, as ${voffset}s. Conky sizes the window WITHOUT the voffsets
# inside the text, so each row's net shift eats into the bottom padding and a
# full card ends up nearly touching its edge. A voffset at the very end of the
# last line IS counted, so the total is handed back there -- measured, that
# restores the bottom margin pixel for pixel whatever the row count.
ROW_GAP   = 4     # above each countdown
DATE_LIFT = -1    # tucks the date up under its label

TITLE = "conky-gruvbox-countdowns"   # own_window_title, from widget-common.lua
STATE = os.path.join(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}",
                     "conky-gruvbox", "countdowns.visible")

# What conky's own_window_hints ask for. A WM is required to strip
# _NET_WM_STATE from a window it lets go of (xfwm4 does), and a client must set
# it again before mapping, or the card comes back as an ordinary window: not
# sticky, not below, on the taskbar. The live value is saved at hide time; this
# is only the fallback if that was lost.
WM_STATE_DEFAULT = ["_NET_WM_STATE_STICKY", "_NET_WM_STATE_SKIP_PAGER",
                    "_NET_WM_STATE_SKIP_TASKBAR", "_NET_WM_STATE_BELOW"]
WM_STATE_SAVED = STATE + ".wmstate"
# Where the card stood when hidden. Conky sets no WM_NORMAL_HINTS, so a remapped
# card is smart-placed by xfwm (mid-screen) and flashes there until
# bin/reflow.py moves it; asking for this position up front with USPosition
# brings it back where it was instead.
POS_SAVED = STATE + ".pos"

FONT = "Maple Mono NF CN"
GLYPH = "\U000F051F"   # timer-sand, verified by rendering in this font


def esc(s):
    return s.replace("$", "$$")


def fmt_remaining(secs):
    s = max(0, math.ceil(secs))
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    hms = f"{h:02}:{m:02}:{s:02}"
    return f"{d}d {hms}" if d else hms


def colour(left):
    if left <= 0:
        return "${color6}"       # green: done
    if left < 3600:
        return "${color7}"       # red: under an hour
    if left < 86400:
        return "${color1}"       # orange: under a day
    return "${color4}"           # blue


# ── showing / hiding the card ─────────────────────────────────

def conky_pid():
    """The conky running this card. execpi goes through `sh -c`, so walk up
    rather than trusting the parent to be conky itself."""
    pid = os.getppid()
    for _ in range(4):
        try:
            with open(f"/proc/{pid}/stat") as f:
                stat = f.read()
        except OSError:
            return 0
        comm = stat[stat.index("(") + 1:stat.rindex(")")]
        if comm == "conky":
            return pid
        pid = int(stat[stat.rindex(")") + 2:].split()[1])
    return 0


def find_card(root):
    """The card's client window, wherever it is: under its xfwm frame while
    shown, straight under the root once withdrawn."""
    def walk(w, depth):
        if depth > 4:
            return None
        for c in w.query_tree().children:
            try:
                if c.get_wm_name() == TITLE:
                    return c
            except Exception:
                pass
            hit = walk(c, depth + 1)
            if hit:
                return hit
        return None
    return walk(root, 0)


def set_visible(want):
    key = "%d %s" % (conky_pid(), "shown" if want else "hidden")
    try:
        with open(STATE) as f:
            if f.read() == key:
                return                 # already done, nothing to ask X
    except OSError:
        pass
    try:
        from Xlib import X, Xatom, Xutil, display, protocol
        d = display.Display()
        root = d.screen().root
        card = find_card(root)
        if card is None:
            return                     # not mapped yet at startup; next tick
        NET_WM_STATE = d.intern_atom("_NET_WM_STATE")
        if want:
            try:
                with open(WM_STATE_SAVED) as f:
                    names = f.read().split()
            except OSError:
                names = []
            atoms = [d.intern_atom(n) for n in (names or WM_STATE_DEFAULT)]
            card.change_property(NET_WM_STATE, Xatom.ATOM, 32, atoms)
            try:
                with open(POS_SAVED) as f:
                    x, y = map(int, f.read().split())
                card.set_wm_normal_hints(
                    flags=Xutil.USPosition | Xutil.PPosition, x=x, y=y)
                card.configure(x=x, y=y)
            except (OSError, ValueError):
                pass
            card.map()
        else:
            prop = card.get_full_property(NET_WM_STATE, Xatom.ATOM)
            if prop and len(prop.value):
                os.makedirs(os.path.dirname(STATE), exist_ok=True)
                with open(WM_STATE_SAVED, "w") as f:
                    f.write(" ".join(d.get_atom_name(a) for a in prop.value))
            if card.get_attributes().map_state == X.IsViewable:
                at = root.translate_coords(card, 0, 0)
                with open(POS_SAVED, "w") as f:
                    f.write("%d %d" % (at.x, at.y))
            # ICCCM withdrawal: the real unmap, plus the synthetic
            # UnmapNotify to the root that tells the WM to let go of it.
            card.unmap()
            root.send_event(
                protocol.event.UnmapNotify(window=card, event=root,
                                           from_configure=False),
                event_mask=X.SubstructureRedirectMask | X.SubstructureNotifyMask)
        d.sync()
        d.close()
    except Exception as e:             # never break the card over this
        print("countdowns: could not %s card: %s"
              % ("show" if want else "hide", e), file=sys.stderr)
        return
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    with open(STATE, "w") as f:
        f.write(key)


def load():
    with open(DATA) as f:
        items = json.load(f).get("countdowns", [])
    out = []
    for it in items:
        if not it.get("enabled", True):
            continue
        try:
            t = datetime.fromisoformat(it["target"])
        except (KeyError, ValueError, TypeError):
            continue
        out.append((t.timestamp(), t, it.get("label") or "Countdown"))
    return sorted(out)


def main():
    print("${color1}${font %s:size=10}%s${font}  "
          "${color5}${font %s:size=8}COUNTDOWNS${font}" % (FONT, GLYPH, FONT))
    try:
        items = load()
    except FileNotFoundError:
        items = []
    except (OSError, ValueError, AttributeError):
        set_visible(True)              # a broken file is worth seeing
        print("${voffset 2}${color7}countdowns.json unreadable${voffset 2}",
              end="")
        return

    now = time.time()
    rows = [r for r in items if r[0] - now > -DONE_HOLD][:MAX_SHOWN]
    set_visible(bool(rows))
    if not rows:
        # Hidden, so never seen; kept so the card is not blank if the hide
        # fails (no X, say).
        print("${voffset 2}${color5}nothing counting down${voffset 2}", end="")
        return

    lines = []
    for ts, t, label in rows:
        left = ts - now
        if len(label) > LABEL_MAX:
            label = label[:LABEL_MAX - 1] + "…"
        value = fmt_remaining(left) if left > 0 else "done"
        lines.append(
            "${voffset %d}${color0}%s${alignr}%s${font %s:bold:size=11}%s${font}\n"
            "${voffset %d}${color5}${font %s:size=8}%s${font}"
            % (ROW_GAP, esc(label), colour(left), FONT, value, DATE_LIFT,
               FONT, t.strftime("%a %-d %b %Y · %H:%M")))
    print("\n".join(lines)
          + "${voffset %d}" % ((ROW_GAP + DATE_LIFT) * len(lines)), end="")


if __name__ == "__main__":
    main()
