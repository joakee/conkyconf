#!/usr/bin/env python3
"""Keep the gaps between cards constant while cards resize themselves.

Conky reads gap_y once, at parse time, so a card's y position is fixed for the
life of the process -- but its HEIGHT is not. Cards marked `auto` in
widget-common.lua size their window to their text, which is what lets the
batteries card grow a row as devices connect; the Now Playing card is 70 px
shorter when nothing is playing. Anything below such a card is then stranded:
the stack keeps a tidy 12 px gap everywhere except under whichever card has
shrunk, where it opens up to 82.

Watch the cards; whenever one changes height, re-derive every y below it from
the heights actually on screen, and move the cards there.

Moving them is not enough on its own any more. Conky 1.24 left a card where it
had been moved, but 1.25 puts the window back at its configured gap_x/gap_y on
every update, so a moved card was dragged back within one update_interval and
moved again by the next pass here: it bobbed between the two, every 5 s for the
batteries card. gap_y can not be changed from inside a running card, but a
SIGUSR1 reload re-reads the config while keeping the same window, so each move
is also PINNED (see Pins): the spot is written where widget-common.lua's
M.card will read it, and the card is reloaded, after which conky's idea of
where the card belongs is this one's. The direct move still happens first, so
the stack closes up at once rather than after the reload's one-second pause.

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

The stack also wraps. The space kept clear under the last card is never less
than M.TOP, the space kept above the first -- measured from the top of any
panel reserving the bottom of that monitor (its _NET_WM_STRUT_PARTIAL), not
from the glass. A row that would cut into it starts a new column instead, at
the top, one card-width plus M.GAP further from the aligned edge. It is
re-derived every pass like everything else, so a card that grows (Now Playing
when a track starts) pushes whatever no longer fits across, and it comes back
when there is room again.

Needs python-xlib. Everything else is stdlib.
"""

import fcntl
import os
import re
import select
import signal
import subprocess
import sys
import time
from collections import namedtuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import head as headmod                                       # noqa: E402

try:
    from Xlib import X, Xatom, display, error as xerror
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
RUNDIR = os.path.join(os.environ.get("XDG_RUNTIME_DIR") or "/tmp",
                      "conky-gruvbox")
LOCK = os.path.join(RUNDIR, "reflow.lock")

# A resize arrives as a burst of events; wait for it to stop before moving
# anything, so the stack settles in one step instead of shuffling.
DEBOUNCE = 0.08
# Safety net for anything that slips past the event mask (a card restarted by
# hand, say). Cheap: a handful of X round trips.
IDLE_POLL = 5.0
# Finding the cards means walking the whole window tree, ~250 windows and two
# round trips each with a browser open: most of the cost of a pass, and once
# it ran every IDLE_POLL it was a CPU spike every 5 s. So the cards found are
# kept and only re-checked, and the tree is walked again only when one of them
# has gone, fewer than the stack's cards are known, or this long has passed.
REDISCOVER = 60.0
# Cards are launched together but map one by one, and the same is true when
# the stack is restarted by hand. While any card is missing, sit still rather
# than close the stack up over a gap that is about to be filled -- but only for
# so long, or a card that has genuinely died would strand the gap for good.
STARTUP_GRACE = 20.0
# A reload takes conky a second (it sleeps for one on purpose). Until its
# <name>.placed shows the new spot, do not signal it again for the same one.
PIN_WAIT = 3.0
# A card whose OWN size has just changed is left where it is until the size
# has held for this long, and only then moved and pinned. Reloading conky while
# its window is mid-resize can wedge it: the reload takes the window's size
# from the server, a resize still in flight then arrives against conky's new
# idea of it, and conky reads the mismatch as the user resizing the window --
# it sets fixed_size and never sizes the window to its text again, which a
# reload does not undo. Measured: a reload sent ~0.7 s after the text shrank
# froze the card at its old height. Cards that only move because something
# above them changed keep their size, so they are not held up.
SIZE_SETTLE = 2.0
# Holding back the card's own resizes is not enough: its text can change at
# any moment, and it changing around a reload wedges conky just the same
# (measured: text changed 0.2 s before the signal, frozen in 2 runs of 3 --
# conky is asleep in the reload by then, so the resize lands right after it).
# So a card whose size changed between RESCUE_BEFORE before the signal and
# RESCUE_AFTER after the reload finished is restarted, which is the only thing
# that clears fixed_size. That costs a blink of about half a second, which is
# why it is not simply done instead of every reload. A resize later than that
# is an ordinary one.
RESCUE_BEFORE = 1.0
RESCUE_AFTER = 0.6
RELOAD_POLL = 0.25
# Conky places some cards a pixel off their configured gap_x/gap_y; Pins
# measures that and pins around it. Anything bigger is a card in transit.
BIAS_MAX = 3


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
# `limit` is the lowest y a card may reach: the top of whatever panel reserves
# the bottom of this head (or the head's own bottom), less M.TOP again, so the
# margin under the stack matches the margin above it.
Anchor = namedtuple("Anchor", "top left width limit")


def usable_bottom(d, root, h):
    """The y where the bottom panel on head `h` begins, or the head's bottom.

    Read from every client's strut (_NET_WM_STRUT_PARTIAL, falling back to the
    legacy full-width _NET_WM_STRUT). _NET_WORKAREA is no use here: it is one
    rectangle for the whole root, and with monitors of different heights it
    says nothing about this one. Walks _NET_CLIENT_LIST rather than the root's
    children because xfwm reparents the panel; ~1 ms for 26 clients.
    """
    bottom = h.y + h.height
    try:
        root_h = root.get_geometry().height
        clients = root.get_full_property(
            d.intern_atom("_NET_CLIENT_LIST"), Xatom.WINDOW)
    except xerror.XError:
        return bottom
    if not clients:
        return bottom
    partial = d.intern_atom("_NET_WM_STRUT_PARTIAL")
    legacy = d.intern_atom("_NET_WM_STRUT")
    for wid in clients.value:
        w = d.create_resource_object("window", wid)
        try:
            p = w.get_full_property(partial, Xatom.CARDINAL)
            if p and len(p.value) >= 12:
                reserve, x0, x1 = p.value[3], p.value[10], p.value[11]
            else:
                p = w.get_full_property(legacy, Xatom.CARDINAL)
                if not (p and len(p.value) >= 4):
                    continue
                reserve, x0, x1 = p.value[3], 0, root.get_geometry().width
        except xerror.XError:
            continue                    # a client that went away mid-walk
        if reserve and x0 < h.x + h.width and x1 >= h.x:
            bottom = min(bottom, root_h - reserve)
    return bottom


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
    limit = usable_bottom(d, root, h) - base_top
    if verbose:
        print("head %d at %d,%d %dx%d (%s), cards end by y=%d"
              % (h.index, h.x, h.y, h.width, h.height, h.source, limit),
              flush=True)
    return Anchor(base_top + h.y, h.x, h.width, limit)


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


class Finder:
    """discover(), remembering the cards between passes (see REDISCOVER)."""

    def __init__(self, d, root, expected):
        self.d, self.root, self.expected = d, root, expected
        self.cards, self.walked = {}, None

    def recheck(self):
        """The known cards with their frames re-read (a card hiding or showing
        itself changes its parent), or None if any is no longer that card."""
        found = {}
        for name, (client, _) in self.cards.items():
            try:
                if client.get_wm_name() != PREFIX + name:
                    return None
                parent = client.query_tree().parent
            except Exception:
                return None
            found[name] = (client, client if parent.id == self.root.id
                           else parent)
        return found

    def __call__(self):
        if (len(self.cards) >= self.expected and self.walked is not None
                and time.monotonic() - self.walked < REDISCOVER):
            found = self.recheck()
            if found is not None:
                self.cards = found
                return found
        self.cards = discover(self.d, self.root)
        self.walked = time.monotonic()
        return self.cards


def plan(cards, anchor, lay):
    """[(name, client, (x, y) now, (x, y) wanted, (width, height))] for
    every card present.

    A card that is not running reserves no space -- if a widget has died or
    been switched off, the stack should close up over it rather than leave a
    hole shaped like a card that is not there. Nor does a card that is running
    but has unmapped its own window: that is how a card hides itself while it
    has nothing to show (the countdowns card does). It still counts as present
    for STARTUP_GRACE, so hiding does not freeze the rest of the stack, and the
    Map/UnmapNotify it causes arrives on the StructureNotifyMask already held
    on every client, so the stack closes up and reopens at once.

    x is derived here rather than left to conky. Conky reads xinerama_head at
    parse time, exactly as it does gap_y, so a card started before a monitor
    was plugged in keeps the column of a head that has since renumbered. Widths
    are measured off the frames for the same reason the heights are: a half-row
    pair then keeps its gap without this file having to know M.PAD or M.HALF_W.

    The stack wraps: a row that would end below anchor.limit starts a new
    column at the top instead, offset from the previous column by that
    column's widest row plus the gap. The first row of a column always goes
    in, however tall, so an oversized card cannot push the stack off screen.
    """
    out, y = [], anchor.top
    offset, col_w, col_rows = 0, 0, 0     # this column's distance from the edge
    for row in lay.rows:
        here, height = [], 0
        for s in row:
            got = cards.get(s["name"])
            if not got:
                continue
            client, frame = got
            try:
                if client.get_attributes().map_state == X.IsUnmapped:
                    continue                  # hidden by its own card
                g = frame.get_geometry()
            except xerror.XError:
                continue
            here.append((s["name"], client, (g.x, g.y), (g.width, g.height)))
            height = max(height, g.height)
        if not here:
            continue
        span = sum(sz[0] for _, _, _, sz in here) + lay.gap * (len(here) - 1)
        if col_rows and y + height > anchor.limit:
            offset += col_w + lay.gap     # next column, back at the top
            y, col_w, col_rows = anchor.top, 0, 0
        # Lay the row out left to right from whichever edge it hugs, so a
        # right-aligned stack mirrors without reordering the row; further
        # columns grow away from that edge.
        if lay.align == "top_right":
            x = anchor.left + anchor.width - lay.side - offset - span
        else:
            x = anchor.left + lay.side + offset
        for name, client, cur, sz in here:
            out.append((name, client, cur, (x, y), sz))
            x += sz[0] + lay.gap
        y += height + lay.gap
        col_w, col_rows = max(col_w, span), col_rows + 1
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
    moved = []
    for name, client, cur, want, _ in moves:
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
        moved.append(name)
        if verbose:
            print("%-12s %d,%d -> %d,%d" % ((name,) + cur + want), flush=True)
    if moved:
        d.sync()
    return moved


# ── pinning ───────────────────────────────────────────────────
def spot(want, width, anchor, lay):
    """A root position as M.card counts it: (top, inset), `top` from the head's
    top edge and `inset` from its aligned edge to the card's near side."""
    top = want[1] - (anchor.top - lay.top)
    if lay.align == "top_right":
        inset = anchor.left + anchor.width - (want[0] + width)
    else:
        inset = want[0] - anchor.left
    return top, inset


def unspot(where, width, anchor, lay):
    """The root position M.card's (top, inset) names: spot() run backwards."""
    top, inset = where
    y = top + anchor.top - lay.top
    if lay.align == "top_right":
        return anchor.left + anchor.width - inset - width, y
    return anchor.left + inset, y


def conky_pids():
    """card name -> pid of the conky running it, read off its `-c
    widgets/NN-name.conf` argument. Neither the card nor its window can say:
    conky sets no _NET_WM_PID, and with `background = true` it forks after
    loading its config, so the pid the config sees at startup is gone a moment
    later."""
    out = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open("/proc/%s/cmdline" % entry, "rb") as fh:
                argv = fh.read().split(b"\0")
        except OSError:
            continue
        if os.path.basename(argv[0]) != b"conky":
            continue
        for flag, arg in zip(argv, argv[1:]):
            if flag in (b"-c", b"--config"):
                m = re.match(r"(?:\d+-)?(.+)\.conf$",
                             os.path.basename(arg.decode(errors="replace")))
                if m:
                    out[m.group(1)] = int(entry)
    return out


class Pins:
    """Make each card's own configured position agree with the plan.

    Conky 1.25 re-applies gap_x/gap_y on every update (see the module notes),
    so a card is only really settled once its config says where it is. The
    card's config records what it was given in <name>.placed; when that is not
    the spot the plan wants, write <name>.place and SIGUSR1 the card's conky,
    whose reload reads it back through M.card.

    A reload can wedge a card that resizes around it (see SIZE_SETTLE and
    RESCUE_BEFORE), so each one is watched until RESCUE_AFTER past its
    completion, and the card restarted if its size moved in that window.

    Only cards on screen are pinned -- plan() leaves hidden ones out, which
    matters because a reload maps the window again. And only a spot the WM
    has accepted, or is being asked for in this same pass: pinning one it
    refused would have conky ask for it again on every update. A card that
    has never written .placed (started from an older widget-common.lua) is
    left alone; the direct moves still apply to it.

    Conky does not put every card exactly where its gap_x/gap_y says, either:
    the cards that load Lua land a pixel up or left of the rest (see M.SHIFT).
    Pinned at the plan's spot, such a card was dragged a pixel off it on every
    update and moved straight back by the next pass -- a 1 px jiggle, once a
    second. So each card's error is measured whenever conky has placed it (it
    sits off the plan, and no reload is in flight to make .placed stale), and
    the pin is offset to cancel it. An error larger than BIAS_MAX is not
    conky's but a card caught mid-move, and is ignored.
    """

    def __init__(self, changes, verbose=False):
        self.verbose = verbose
        self.changes = changes       # name -> recent resize times, see watch
        self.sent = {}               # name -> (pid, spot, give up waiting at)
        self.checks = {}             # name -> [pid, sent at, loaded, due]
        self.bias = {}               # name -> (dx, dy) conky lands off its pin

    @staticmethod
    def loaded(name):
        """The token <name>.placed carries for its last config load."""
        try:
            with open(os.path.join(RUNDIR, name + ".placed")) as fh:
                return fh.read().split()[-1]
        except (OSError, IndexError):
            return None

    def due(self):
        """When the next rescue check falls due, or None. A reload nothing has
        been seen of yet is looked for every RELOAD_POLL: when it lands is
        what the rescue window is measured from, and it may bring no X event
        to wake a pass."""
        times = [c[3] if c[3] is not None else time.monotonic() + RELOAD_POLL
                 for c in self.checks.values()]
        return min(times) if times else None

    def rescue(self, now):
        for name, check in list(self.checks.items()):
            pid, sent, loaded, due = check
            if due is None:
                if self.loaded(name) != loaded:
                    check[3] = now + RESCUE_AFTER   # reloaded; watch a while
                elif now > sent + 2 * PIN_WAIT:
                    del self.checks[name]           # never reloaded; let go
                continue
            if now < due:
                continue
            del self.checks[name]
            hits = [t for t in self.changes.get(name, ())
                    if sent - RESCUE_BEFORE <= t <= due]
            if hits:
                if self.verbose:
                    print("%-12s size changed %+.1f s from its reload signal"
                          % (name, hits[0] - sent), flush=True)
                self.restart(name, pid)

    def restart(self, name, pid):
        """Replace the card's conky with a fresh one, run exactly as before:
        same command line, environment and working directory."""
        try:
            with open("/proc/%d/cmdline" % pid, "rb") as fh:
                argv = [a.decode() for a in fh.read().split(b"\0") if a]
            with open("/proc/%d/environ" % pid, "rb") as fh:
                env = dict(e.decode(errors="replace").split("=", 1)
                           for e in fh.read().split(b"\0") if b"=" in e)
            cwd = os.readlink("/proc/%d/cwd" % pid)
            os.kill(pid, signal.SIGTERM)
        except (OSError, ValueError):
            return
        for _ in range(60):                  # let it take its window down
            if not os.path.exists("/proc/%d" % pid):
                break
            time.sleep(0.05)
        try:
            subprocess.Popen(argv, env=env, cwd=cwd, start_new_session=True,
                             stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        except OSError:
            return
        if self.verbose:
            print("%-12s resized around its reload; restarted" % name,
                  flush=True)

    @staticmethod
    def placed(name):
        try:
            with open(os.path.join(RUNDIR, name + ".placed")) as fh:
                top, inset = fh.read().split()[:2]
            return int(top), int(inset)
        except (OSError, ValueError):
            return None

    @staticmethod
    def write(name, where):
        """Make <name>.place say `where`, if it does not already."""
        path = os.path.join(RUNDIR, name + ".place")
        try:
            with open(path) as fh:
                if fh.read() == "%d %d\n" % where:
                    return
        except OSError:
            pass
        with open(path + ".tmp", "w") as fh:
            fh.write("%d %d\n" % where)
        os.replace(path + ".tmp", path)

    def sync(self, moves, moved, anchor, lay):
        now, pids = time.monotonic(), None
        self.rescue(now)
        for name, _, cur, want, (width, _) in moves:
            have = self.placed(name)
            # Conky reads the head's geometry when it loads its config, not
            # when the monitors change -- so a card started before the layout
            # settled (DP-1 hotplugged at login, eDP-1 pushed down to y=1080)
            # keeps placing itself against where its head USED to be, while
            # its .placed already matches the plan. Every move here is then
            # dragged back on conky's next update, and nothing below reloads
            # it because the config "is right": the card sits on the wrong
            # monitor, or bobs between the two. Landing far from where its own
            # config says is the tell; a reload re-reads the head.
            adrift = False
            if have is not None and cur != want and name not in self.checks:
                at = unspot(have, width, anchor, lay)
                dx, dy = cur[0] - at[0], cur[1] - at[1]
                if abs(dx) <= BIAS_MAX and abs(dy) <= BIAS_MAX:
                    self.bias[name] = (dx, dy)
                else:
                    adrift = True
            if cur != want and name not in moved and not adrift:
                continue                      # refused, or not ours to pin yet
            if have is None:
                continue
            dx, dy = self.bias.get(name, (0, 0))
            where = spot((want[0] - dx, want[1] - dy), width, anchor, lay)
            # Even when the card is configured right already: a reload sent
            # for an earlier plan may still be on its way (conky sleeps a
            # second before it reads the config), and must not find that
            # plan's spot when it gets there.
            try:
                self.write(name, where)
            except OSError:
                continue
            if have == where and not adrift:
                self.sent.pop(name, None)
                continue
            if pids is None:
                pids = conky_pids()       # only scan /proc when a pin is due
            pid = pids.get(name)
            if pid is None:
                continue
            last = self.sent.get(name)
            if last and last[:2] == (pid, where) and now < last[2]:
                continue                      # reload already on its way
            try:
                os.kill(pid, signal.SIGUSR1)
            except OSError:
                continue
            self.sent[name] = (pid, where, now + PIN_WAIT)
            self.checks[name] = [pid, now, self.loaded(name), None]
            if self.verbose:
                print("%-12s pinned at top %d, inset %d (was %d, %d)"
                      % ((name,) + where + have), flush=True)


# ── modes ─────────────────────────────────────────────────────
def show(d, root, anchor, lay):
    cards = discover(d, root)
    moves = plan(cards, anchor, lay)
    print("%-12s %6s %12s %12s" % ("card", "h", "at", "want"))
    for name, client, cur, want, _ in moves:
        h = cards[name][1].get_geometry().height
        print("%-12s %6d %12s %12s   %s"
              % (name, h, "%d,%d" % cur, "%d,%d" % want,
                 "" if cur == want else "MOVE"))
    placed = {m[0] for m in moves}
    hidden = [n for n in cards if n not in placed]
    if hidden:
        print("hidden:", ", ".join(hidden))
    missing = [s["name"] for row in lay.rows for s in row
               if s["name"] not in cards]
    if missing:
        print("not running:", ", ".join(missing))


def watch(d, root, lay, verbose):
    root.change_attributes(event_mask=X.SubstructureNotifyMask)
    watch_screen_changes(d, root, verbose)
    anchor = resolve_anchor(d, root, lay.top, verbose)
    expected = sum(len(r) for r in lay.rows)
    find = Finder(d, root, expected)
    subscribed = set()
    refused = {}
    sizes = {}                    # name -> (size, when it was last different)
    changes = {}                  # name -> times its size changed, last 10 s
    pins = Pins(changes, verbose)
    grace_until = time.monotonic() + STARTUP_GRACE
    # Cards restarted by Pins.restart are children of this process; there is
    # nothing to collect from them, so have the kernel reap them.
    signal.signal(signal.SIGCHLD, signal.SIG_IGN)
    wake = None                   # when the next held card is due a pass

    def settled(moves):
        """The moves that may go ahead now: those not waiting on a card's own
        resize to settle (see SIZE_SETTLE). Sets `wake` for the rest."""
        nonlocal wake
        now, ready, wake = time.monotonic(), [], None
        for m in moves:
            name, _, cur, want, size = m
            if sizes.get(name, (None,))[0] != size:
                if name in sizes:     # first sight is not a resize
                    changes[name] = [t for t in changes.get(name, ())
                                     if t > now - 10] + [now]
                sizes[name] = (size, now)
            due = sizes[name][1] + SIZE_SETTLE
            if cur == want or now >= due:
                ready.append(m)
            else:
                wake = due if wake is None else min(wake, due)
        return ready

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
                print("head moved: top %d -> %d, left %d -> %d, limit %d -> %d"
                      % (anchor.top, moved.top, anchor.left, moved.left,
                         anchor.limit, moved.limit), flush=True)
            anchor = moved
            # Every refusal on record was judged against the old head, and the
            # move that replaces it is the one that gets the stack back.
            refused.clear()
        cards = find()
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
        moves = settled(plan(cards, anchor, lay))
        pins.sync(moves, apply(d, moves, verbose, refused), anchor, lay)

    settle()
    while True:
        timeout = IDLE_POLL
        due = [t for t in (wake, pins.due()) if t is not None]
        if due:
            timeout = min(timeout, max(0.0, min(due) - time.monotonic()))
        ready, _, _ = select.select([d.fileno()], [], [], timeout)
        # A timeout is the periodic safety pass -- unless it was only cut short
        # for a card that has finished settling, or a rescue check.
        held = not ready and timeout < IDLE_POLL
        idle = not ready and not held
        touched = not ready
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
