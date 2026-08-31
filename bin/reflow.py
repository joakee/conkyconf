#!/usr/bin/env python3
"""Keep the gaps between cards constant while cards resize themselves.

Conky reads gap_y once, at parse time, so a card's y position is fixed for the
life of the process -- but its HEIGHT is not. Cards marked `auto` in
widget-common.lua size their window to their text, which is what lets the
batteries card grow a row as devices connect; the Now Playing card is 70 px
shorter when nothing is playing. Anything below such a card is then stranded:
the stack keeps a tidy 12 px gap everywhere except under whichever card has
shrunk, where it opens up to 82.

Conky does not fight a correction. It resizes its window without ever
repositioning it -- measured, not assumed: a probe card whose text height
flipped repeatedly stayed at exactly the y it had been moved to. So this needs
no cooperation from the widgets at all. Watch the cards; whenever one changes
height, re-derive every y below it from the heights actually on screen.

Stack order, the first card's offset and the gap are read from
widget-common.lua, so that file stays the single source of truth for layout
and this one has no copy of it to drift out of date.

Positions are written in ROOT coordinates, which span every monitor -- so a
card's offset is measured from the primary head's corner, not from the root's.
Without that the stack lands on whichever monitor happens to sit at the top of
the root window, undoing conky's own xinerama_head placement. bin/head.py
resolves which head that is; both this and widget-common.lua ask it, so they
cannot disagree about where the stack belongs. That origin is re-read on every
pass rather than cached at startup -- see resolve_anchor for what a stale one
does, and why BOTH axes are written even though only y ever needs correcting
for a resize.

XFWM reparents each card into a frame, so a move has to go through the window
manager: configuring the client window is redirected to the WM, which moves
the frame. Positions are therefore READ from the frame (the window actually
placed on screen) and WRITTEN to the client.

Needs python-xlib. Everything else is stdlib.
"""

import fcntl
import os
import re
import select
import sys
import time
from collections import namedtuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import head as headmod                                       # noqa: E402

try:
    from Xlib import X, display, error as xerror
except ImportError:
    sys.stderr.write("reflow: python-xlib is not installed "
                     "(pacman -S python-xlib)\n")
    sys.exit(2)

try:
    from Xlib.ext import randr
except ImportError:                                          # pragma: no cover
    randr = None

COMMON = os.path.expanduser("~/.config/conky/gruvbox-dark/widget-common.lua")
PREFIX = "conky-gruvbox-"
LOCK = os.path.join(os.environ.get("XDG_RUNTIME_DIR") or "/tmp",
                    "conky-gruvbox", "reflow.lock")

# A resize arrives as a burst of events; wait for it to stop before moving
# anything, so the stack settles in one step instead of shuffling.
DEBOUNCE = 0.08
# Safety net for anything that slips past the event mask (a card restarted by
# hand, say). Cheap: a handful of X round trips.
IDLE_POLL = 5.0
# Cards are launched together but map one by one, and the same is true when
# the stack is restarted by hand. While any card is missing, sit still rather
# than close the stack up over a gap that is about to be filled -- but only for
# so long, or a card that has genuinely died would strand the gap for good.
STARTUP_GRACE = 20.0


def single_instance():
    """A second watcher would only duplicate every move. Returns the held lock,
    which the caller must keep referenced for the life of the process."""
    os.makedirs(os.path.dirname(LOCK), exist_ok=True)
    fh = open(LOCK, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return None
    return fh


# ── layout, from widget-common.lua ────────────────────────────
# top and side are the margins from the primary head's top and near edge; rows
# group half-width pairs onto one line; align says which edge "near" means.
Layout = namedtuple("Layout", "top gap side align rows")


def layout(path=COMMON):
    """The Layout described by widget-common.lua.

    Parsed rather than executed: the stack is a flat table of literals, and
    reading it with a regex keeps this script free of a lua dependency.
    """
    with open(path) as fh:
        src = fh.read()

    def scalar(key, default):
        m = re.search(r"^\s*M\.%s\s*=\s*(\d+)" % key, src, re.M)
        return int(m.group(1)) if m else default

    top, gap, side = scalar("TOP", 34), scalar("GAP", 12), scalar("SIDE", 28)

    m = re.search(r"^\s*M\.ALIGN\s*=\s*'([^']*)'", src, re.M)
    align = m.group(1) if m else "top_left"

    block = re.search(r"M\.stack\s*=\s*\{(.*?)^\}", src, re.S | re.M)
    if not block:
        raise RuntimeError("no M.stack table in " + path)

    stack = []
    for entry in re.findall(r"\{([^{}]*)\}", block.group(1)):
        # two shapes of value, quoted and bare, so collect them separately
        fields = {}
        for k, v in re.findall(r"(\w+)\s*=\s*'([^']*)'", entry):
            fields[k] = v
        for k, v in re.findall(r"(\w+)\s*=\s*(\d+)", entry):
            fields.setdefault(k, int(v))
        if "name" in fields:
            stack.append(fields)

    # Halves sit side by side and share a y; the row is as tall as the taller.
    rows, i = [], 0
    while i < len(stack):
        s = stack[i]
        if (s.get("half") == "l" and i + 1 < len(stack)
                and stack[i + 1].get("half") == "r"):
            rows.append([s, stack[i + 1]])
            i += 2
        else:
            rows.append([s])
            i += 1
    return Layout(top, gap, side, align, rows)


# ── where the stack begins ───────────────────────────────────
# The corner the stack is measured from: `top` is M.TOP already lifted onto the
# head, `left` and `width` are the head's own, for resolving the near edge.
Anchor = namedtuple("Anchor", "top left width")


def resolve_anchor(d, root, base_top, verbose=False):
    """Where on the root window the primary head's corner currently is.

    Called on every pass instead of once at startup, because the origin moves
    when the monitors are rearranged and a stale one is worse than no offset at
    all: it puts every card's target off the edge of that monitor, where xfwm
    declines to follow. The cards do not merely land in the wrong place, they
    pile up on one another at the nearest position the window manager will
    allow -- and because the watcher re-asserts that pile every pass, restarting
    conky cannot undo it. Costs a handful of X round trips, next to nothing
    beside the tree walk in discover.
    """
    h = headmod.resolve(d, root)
    if verbose:
        print("head %d at %d,%d %dx%d (%s)"
              % (h.index, h.x, h.y, h.width, h.height, h.source), flush=True)
    return Anchor(base_top + h.y, h.x, h.width)


def watch_screen_changes(d, root, verbose=False):
    """Ask to be woken when the monitor layout changes.

    Only a matter of latency: settle() re-resolves the origin either way, so a
    server without RandR just means the stack takes up to IDLE_POLL to follow a
    monitor being moved, rather than following it immediately.
    """
    if randr is None:
        return False
    try:
        root.xrandr_select_input(randr.RRScreenChangeNotifyMask)
        return True
    except Exception:
        if verbose:
            print("randr: no screen-change events", flush=True)
        return False


# ── the cards on screen ───────────────────────────────────────
def discover(d, root):
    """name -> (client, frame). frame is what is placed on screen; it is the
    client itself if the WM has not reparented it."""
    found = {}

    def walk(w, depth):
        if depth > 4:
            return
        try:
            kids = w.query_tree().children
        except xerror.XError:
            return
        for c in kids:
            try:
                name = c.get_wm_name()
            except Exception:
                name = None
            if name and name.startswith(PREFIX):
                try:
                    parent = c.query_tree().parent
                except xerror.XError:
                    continue
                found[name[len(PREFIX):]] = (
                    c, c if parent.id == root.id else parent)
                continue
            walk(c, depth + 1)

    walk(root, 0)
    return found


def plan(cards, anchor, lay):
    """[(name, client, (x, y) now, (x, y) wanted)] for every card present.

    A card that is not running reserves no space -- if a widget has died or
    been switched off, the stack should close up over it rather than leave a
    hole shaped like a card that is not there.

    x is derived here rather than left to conky. Conky reads xinerama_head at
    parse time, exactly as it does gap_y, so a card started before a monitor
    was plugged in keeps the column of a head that has since renumbered. Widths
    are measured off the frames for the same reason the heights are: a half-row
    pair then keeps its gap without this file having to know M.PAD or M.HALF_W.
    """
    out, y = [], anchor.top
    for row in lay.rows:
        here, height = [], 0
        for s in row:
            got = cards.get(s["name"])
            if not got:
                continue
            client, frame = got
            try:
                g = frame.get_geometry()
            except xerror.XError:
                continue
            here.append((s["name"], client, (g.x, g.y), g.width))
            height = max(height, g.height)
        if not here:
            continue
        # Lay the row out left to right from whichever edge it hugs, so a
        # right-aligned stack mirrors without reordering the row.
        span = sum(w for _, _, _, w in here) + lay.gap * (len(here) - 1)
        if lay.align == "top_right":
            x = anchor.left + anchor.width - lay.side - span
        else:
            x = anchor.left + lay.side
        for name, client, cur, w in here:
            out.append((name, client, cur, (x, y)))
            x += w + lay.gap
        y += height + lay.gap
    return out


def apply(d, moves, verbose=False, refused=None):
    """Move every card that is not where it should be. `refused` remembers the
    targets the window manager declined, and suppresses asking again.

    Without that, a target the WM will not honour is re-issued on every pass --
    and a declined move is itself a ConfigureNotify, which wakes the next pass.
    The two feed each other into a loop running as fast as the X server will
    answer, which pins a core and stamps on any correction made by hand within
    milliseconds. A card sitting below the bottom of the screen is the way in:
    xfwm clamps it back, forever, however often it is asked. Asking once and
    waiting is no slower when the move is going to be honoured, because the
    move that lands is itself the event that brings the next pass.

    A refusal is only ever provisional -- the caller clears it on the idle pass,
    and whenever the head moves (see watch), so a target wrongly judged refused
    costs at most IDLE_POLL.

    Both axes go in ONE request even when only one of them is wrong. Sending
    just the y is what a resize needs, but it is also a move to a point in the
    OLD column, and once the head has moved that column can be a stretch of the
    root window no monitor covers -- xfwm refuses to put a window there and the
    y never lands. Measured: with the stack stranded on a monitor that had been
    renumbered away, configure(y=1114) was declined outright, while
    configure(x=1948, y=1114) was honoured immediately.
    """
    n = 0
    for name, client, cur, want in moves:
        if cur == want:
            if refused is not None:
                refused.pop(name, None)          # it landed; that is settled
            continue
        if refused is not None and refused.get(name) == want:
            continue                          # asked for this spot; it did not take
        try:
            client.configure(x=want[0], y=want[1])
        except xerror.XError:
            continue
        if refused is not None:
            refused[name] = want
        n += 1
        if verbose:
            print("%-12s %d,%d -> %d,%d" % ((name,) + cur + want), flush=True)
    if n:
        d.sync()
    return n


# ── modes ─────────────────────────────────────────────────────
def show(d, root, anchor, lay):
    cards = discover(d, root)
    moves = plan(cards, anchor, lay)
    print("%-12s %6s %12s %12s" % ("card", "h", "at", "want"))
    for name, client, cur, want in moves:
        h = cards[name][1].get_geometry().height
        print("%-12s %6d %12s %12s   %s"
              % (name, h, "%d,%d" % cur, "%d,%d" % want,
                 "" if cur == want else "MOVE"))
    missing = [s["name"] for row in lay.rows for s in row
               if s["name"] not in cards]
    if missing:
        print("not running:", ", ".join(missing))


def watch(d, root, lay, verbose):
    root.change_attributes(event_mask=X.SubstructureNotifyMask)
    watch_screen_changes(d, root, verbose)
    anchor = resolve_anchor(d, root, lay.top, verbose)
    expected = sum(len(r) for r in lay.rows)
    subscribed = set()
    refused = {}
    grace_until = time.monotonic() + STARTUP_GRACE

    def settle(retry=False):
        nonlocal grace_until, anchor
        if retry:
            # An idle pass means the server has been quiet for IDLE_POLL, so
            # nothing is in flight and a refusal recorded mid-burst deserves
            # one more try -- the layout it was judged against may be gone.
            refused.clear()
        moved = resolve_anchor(d, root, lay.top)
        if moved != anchor:
            if verbose:
                print("head moved: top %d -> %d, left %d -> %d"
                      % (anchor.top, moved.top, anchor.left, moved.left),
                      flush=True)
            anchor = moved
            # Every refusal on record was judged against the old head, and the
            # move that replaces it is the one that gets the stack back.
            refused.clear()
        cards = discover(d, root)
        for _, (client, _) in cards.items():
            if client.id not in subscribed:
                try:
                    client.change_attributes(event_mask=X.StructureNotifyMask)
                    subscribed.add(client.id)
                except xerror.XError:
                    pass
        if len(cards) >= expected:
            grace_until = time.monotonic() + STARTUP_GRACE   # arm for next time
        elif time.monotonic() < grace_until:
            return
        apply(d, plan(cards, anchor, lay), verbose, refused)

    settle()
    while True:
        ready, _, _ = select.select([d.fileno()], [], [], IDLE_POLL)
        idle = not ready             # a timeout is the periodic safety pass
        touched = idle
        while d.pending_events():
            d.next_event()
            touched = True
        if not touched:
            continue
        time.sleep(DEBOUNCE)         # let the burst finish
        while d.pending_events():
            d.next_event()
        try:
            settle(retry=idle)
        except xerror.XError:
            pass


def main():
    verbose = "-v" in sys.argv or "--verbose" in sys.argv
    lay = layout()
    d = display.Display()
    root = d.screen().root

    # M.TOP and M.SIDE are margins from the primary monitor's corner; everything
    # below works in root coordinates, so that corner has to be found. The
    # one-shot modes resolve it here and are done with it; the watcher re-reads
    # it every pass instead, so it survives the monitors being rearranged.
    anchor = resolve_anchor(d, root, lay.top, verbose)

    if "--show" in sys.argv:
        show(d, root, anchor, lay)
        return 0
    if "--once" in sys.argv:
        moved = apply(d, plan(discover(d, root), anchor, lay), True)
        if not moved:
            print("already aligned")
        return 0

    lock = single_instance()
    if lock is None:
        sys.stderr.write("reflow: already running\n")
        return 0
    watch(d, root, lay, verbose)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        pass
