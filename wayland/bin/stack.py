#!/usr/bin/env python3
"""Run the gruvbox-dark conky stack under Wayland (Wayfire), and look after it.

What bin/reflow.py and bin/focusdim.py do for the stack on X11, this does on
Wayland, talking to Wayfire's IPC socket instead of to an X server -- plus
starting the cards, which start.sh does on X11:

  * One conky per widgets/*.conf, each run through wayland/card.lua. A card
    that exits is started again: conky quits when its surface is closed,
    which is what Wayfire does to the surfaces on a monitor that goes away.

  * The stack stays on M.OUTPUT. Each card's namespace names that output and
    the layer-output Wayfire plugin opens it there; one that is elsewhere --
    started while that monitor was unplugged -- is reloaded once it is back.

  * Positions are re-derived from the heights actually on screen, as
    bin/reflow.py does, wrapping into a new column rather than running into
    the panel. Conky sets a layer surface's margins when it creates it, so
    every move is what reflow.py calls a pin: <name>.place is written and the
    card reloaded (SIGUSR1), which makes it a new surface where
    widget-common.lua now puts it. The surface goes away for the moment that
    takes, so nothing is moved while any card's size is still changing.

  * A card that hides itself (bin/cardvis.py; on Wayland its stand-in
    wayland/bin/cardvis.py) is parked off screen, and the stack closes up
    over it as it does on X11.

  * The cards fade back while a window has the focus, to bin/focusdim.py's
    LEVEL. What is drawn on a card fades; its background does not, so the
    blur behind it stays as strong (see Stack.dim).

Layout -- the stack table, M.TOP, M.GAP, M.SIDE, M.ALIGN -- is read from
widget-common.lua by bin/reflow.py's own parser, so there is still one place
to edit it.

  stack.py         start the cards and watch them (wayland/start.sh runs this)
  stack.py --show  print where each card is and where it should be
  stack.py -v      watch, saying what is done and why
"""

import fcntl
import json
import os
import re
import select
import signal
import socket
import struct
import subprocess
import sys
import time

HOME = os.path.expanduser("~")
DIR = os.path.join(HOME, ".config/conky/gruvbox-dark")
CARD = os.path.join(DIR, "wayland", "card.lua")
WIDGETS = os.path.join(DIR, "widgets")
COMMON = os.path.join(DIR, "widget-common.lua")
RUNDIR = os.path.join(os.environ.get("XDG_RUNTIME_DIR") or "/tmp",
                      "conky-gruvbox")
LOCK = os.path.join(RUNDIR, "stack.lock")
LEVEL_FILE = os.path.join(RUNDIR, "wl-level")     # read by lua/compat.lua
PREFIX = "conky-gruvbox-"

sys.path.insert(0, os.path.join(DIR, "bin"))
from reflow import layout                                    # noqa: E402
from focusdim import LEVEL                                   # noqa: E402

# conky 1.25.2 as packaged aborts on startup under Wayland (set_font(0) before
# any font is loaded; fixed upstream in f7f573f0, released in 1.25.4). A build
# of a fixed version in ~/.local/opt/conky is used in preference, and
# $CONKY_BIN overrides both.
LOCAL_CONKY = os.path.join(HOME, ".local/opt/conky/bin/conky")

# Where a hidden card is parked: above the top of the output by far more than
# any monitor arrangement reaches, so it is on none of them.
HIDDEN_TOP = -20000
# A card's size has to hold this long before the stack is re-planned around
# it, so a card that changes size twice in quick succession (Now Playing as
# the track changes) costs one round of reloads, not two.
SIZE_SETTLE = 1.0
# How long a reload is given to show up -- conky rewrites <name>.placed when
# it reads its config, and maps a new surface -- before it is sent again.
RELOAD_WAIT = 6.0
# Cards are started together but map one by one. Until each has been seen,
# its nominal height from the stack table stands in for it, and nothing is
# pinned for this long, so the first plan is made from real heights.
STARTUP_GRACE = 8.0
# See Stack.pass_: reloads per card to get it onto M.OUTPUT.
OUTPUT_TRIES = 2
# A card that keeps exiting is not restarted more often than this many times
# a minute; it is tried again once the minute is up.
MAX_RESTARTS = 5
# The fade, as in picom.conf's rule for the cards.
FADE = 0.18
# Each step redraws every card, so not more of them than it takes to look
# smooth at this length.
FADE_STEP = 1 / 30
# Nothing here has to happen fast except the fade; this bounds how long a
# change that brings no IPC event (a card hiding itself) waits to be seen.
POLL = 1.0

verbose = False


def say(*args):
    if verbose:
        print(*args, flush=True)


# ── Wayfire IPC ───────────────────────────────────────────────
class IPCError(Exception):
    pass


class Wayfire:
    """One connection to Wayfire's IPC socket. Requests and the event stream
    each get their own, so a reply is never mistaken for an event."""

    def __init__(self):
        path = os.environ.get("WAYFIRE_SOCKET")
        if not path:
            raise IPCError("WAYFIRE_SOCKET is not set (not running under "
                           "Wayfire, or its ipc plugin is not loaded)")
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(path)

    def send(self, method, data=None):
        msg = json.dumps({"method": method, "data": data or {}}).encode()
        self.sock.sendall(struct.pack("<I", len(msg)) + msg)

    def recv(self):
        def exact(n):
            buf = b""
            while len(buf) < n:
                chunk = self.sock.recv(n - len(buf))
                if not chunk:
                    raise EOFError("Wayfire closed the IPC socket")
                buf += chunk
            return buf
        (size,) = struct.unpack("<I", exact(4))
        return json.loads(exact(size))

    def call(self, method, **data):
        self.send(method, data)
        reply = self.recv()
        if isinstance(reply, dict) and "error" in reply:
            raise IPCError("%s: %s" % (method, reply["error"]))
        return reply

    def fileno(self):
        return self.sock.fileno()


# ── the cards ─────────────────────────────────────────────────
def setting(key, default=""):
    """A quoted scalar from widget-common.lua (M.OUTPUT, say)."""
    try:
        with open(COMMON) as fh:
            m = re.search(r"^\s*M\.%s\s*=\s*'([^']*)'" % key, fh.read(), re.M)
    except OSError:
        return default
    return m.group(1) if m else default


def card_files():
    """card name -> widget file, for every widget in widgets/."""
    out = {}
    for f in sorted(os.listdir(WIDGETS)):
        m = re.match(r"(?:\d+-)?(.+)\.conf$", f)
        if m:
            out[m.group(1)] = f
    return out


def conky_binary():
    if os.environ.get("CONKY_BIN"):
        return os.environ["CONKY_BIN"]
    if os.access(LOCAL_CONKY, os.X_OK):
        return LOCAL_CONKY
    return "/usr/bin/conky"


def running():
    """widget file -> pid, for each card conky running in THIS Wayland
    session (another one -- a nested compositor, say -- is left alone).
    With `background = true` conky forks after reading its config, so the pid
    of the process started is not the one to keep."""
    display = os.environ.get("WAYLAND_DISPLAY", "")
    out = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open("/proc/%s/comm" % entry) as fh:
                if fh.read().strip() != "conky":
                    continue
            with open("/proc/%s/environ" % entry, "rb") as fh:
                env = dict(e.split(b"=", 1) for e in fh.read().split(b"\0")
                           if b"=" in e)
        except OSError:
            continue
        card = env.get(b"CONKY_GRUVBOX_CARD")
        if card and env.get(b"WAYLAND_DISPLAY", b"").decode() == display:
            out[card.decode()] = int(entry)
    return out


def launch(conky, file):
    say("%-12s starting" % file)
    env = dict(os.environ, CONKY_GRUVBOX_CARD=file)
    subprocess.Popen([conky, "-c", CARD], env=env, cwd=HOME,
                     start_new_session=True, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def card_views(wf):
    """card name -> its surface, as Wayfire's list-views describes it."""
    out = {}
    for v in wf.call("window-rules/list-views"):
        app = v.get("app-id") or ""
        if not app.startswith(PREFIX) or not v.get("mapped"):
            continue
        name = app[len(PREFIX):].split("@")[0]
        if name not in out or v["id"] > out[name]["id"]:
            out[name] = v            # the newest, if a reload overlaps
    return out


def placed(name):
    """(top, inset, token) that card's conky last configured itself with --
    widget-common.lua writes it on every config load."""
    try:
        with open(os.path.join(RUNDIR, name + ".placed")) as fh:
            top, inset, token = fh.read().split()[:3]
        return int(top), int(inset), token
    except (OSError, ValueError):
        return None


def pin(name, where):
    """Make <name>.place say `where`, for M.card to read on the next load."""
    path = os.path.join(RUNDIR, name + ".place")
    os.makedirs(RUNDIR, exist_ok=True)
    with open(path + ".tmp", "w") as fh:
        fh.write("%d %d\n" % where)
    os.replace(path + ".tmp", path)


def shown(name):
    """False if the card has hidden itself (wayland/bin/cardvis.py)."""
    try:
        with open(os.path.join(RUNDIR, name + ".shown")) as fh:
            return fh.read().strip() != "0"
    except OSError:
        return True


# ── layout ────────────────────────────────────────────────────
def plan(lay, sizes, hidden, area):
    """name -> (top, inset), the margins each card should have, in the terms
    M.card's pinned positions use: `top` from the top of the output's usable
    area, `inset` from its aligned edge. As bin/reflow.py's plan(): rows from
    the stack table, as tall as their taller card, hidden cards leaving no gap,
    and a new column once a row would end within M.TOP of the bottom."""
    out = {}
    limit = area["height"] - lay.top
    y, offset, col_w, col_rows = lay.top, 0, 0, 0
    for row in lay.rows:
        here = [(s["name"], sizes[s["name"]]) for s in row
                if s["name"] in sizes and s["name"] not in hidden]
        if not here:
            continue
        height = max(h for _, (_, h) in here)
        span = sum(w for _, (w, _) in here) + lay.gap * (len(here) - 1)
        if col_rows and y + height > limit:
            offset += col_w + lay.gap
            y, col_w, col_rows = lay.top, 0, 0
        # Lay the row out from the edge it hugs; `inset` is measured from that
        # same edge, so a right-aligned row is walked from its right end.
        inset = lay.side + offset
        for name, (w, _) in (reversed(here) if lay.align == "top_right"
                             else here):
            out[name] = (y, inset)
            inset += w + lay.gap
        y += height + lay.gap
        col_w, col_rows = max(col_w, span), col_rows + 1
    return out


# ── the watcher ───────────────────────────────────────────────
class Stack:
    def __init__(self, wf):
        self.wf = wf
        self.conky = conky_binary()
        self.lay = layout(COMMON)
        self.nominal = {s["name"]: s.get("h", 100)
                        for row in self.lay.rows for s in row}
        self.output = os.environ.get("CONKY_HEAD_OUTPUT") or setting("OUTPUT")
        self.files = card_files()
        self.started = time.monotonic()
        self.deaths = {}       # name -> recent start times
        self.sizes = {}        # name -> ((w, h), when it last changed)
        self.reloading = {}    # name -> (token before, give up at)
        self.arrangement = ()  # the outputs connected, by name
        self.tries = {}        # name -> reloads sent to move it there
        # dimming
        self.level = 1.0       # where the cards are now
        self.fade = None       # (from, to, started) while fading
        self.pids = {}         # widget file -> pid, as of the last pass

    # -- starting and restarting --------------------------------
    def supervise(self, pids, now):
        for name, file in self.files.items():
            if file in pids:
                continue
            recent = [t for t in self.deaths.get(name, []) if now - t < 60]
            if len(recent) >= MAX_RESTARTS:
                continue
            self.deaths[name] = recent + [now]
            self.reloading.pop(name, None)
            launch(self.conky, file)

    def reload(self, name, pids, why):
        pid = pids.get(self.files.get(name))
        have = placed(name)
        if pid is None or have is None:
            return
        say("%-12s reloading: %s" % (name, why))
        try:
            os.kill(pid, signal.SIGUSR1)
        except OSError:
            return
        self.reloading[name] = (have[2], time.monotonic() + RELOAD_WAIT)

    def settled_reloads(self, views, now):
        """Forget reloads that have landed (a new config load, and a surface
        for it) or that have been waited on long enough."""
        for name, (token, until) in list(self.reloading.items()):
            have = placed(name)
            if (have and have[2] != token and name in views) or now > until:
                del self.reloading[name]

    # -- placement ----------------------------------------------
    def pass_(self):
        now = time.monotonic()
        outputs = {o["name"]: o for o in self.wf.call("window-rules/list-outputs")}
        pids = self.pids = running()
        self.supervise(pids, now)
        views = card_views(self.wf)
        self.settled_reloads(views, now)

        # Sizes, and when each last changed.
        for name, v in views.items():
            size = (int(v["geometry"]["width"]), int(v["geometry"]["height"]))
            if self.sizes.get(name, (None,))[0] != size:
                self.sizes[name] = (size, now)

        # Which output the stack belongs on: M.OUTPUT, when it is connected.
        # Moving a card there is up to the layer-output plugin, so each card
        # gets OUTPUT_TRIES at it per monitor arrangement: without the plugin
        # it would come back on the same output every time, and blink forever.
        target = self.output if self.output in outputs else None
        arrangement = tuple(sorted(outputs))
        if arrangement != self.arrangement:
            self.arrangement, self.tries = arrangement, {}
        if target:
            for name, v in views.items():
                if (v.get("output-name") != target and name not in self.reloading
                        and self.tries.get(name, 0) < OUTPUT_TRIES):
                    self.tries[name] = self.tries.get(name, 0) + 1
                    self.reload(name, pids, "belongs on " + target)
        # The plan is made for where the cards are, though: the target while
        # they are on it, or wherever they ended up when it is unplugged (or
        # when they cannot be moved there).
        names = [v.get("output-name") for v in views.values()]
        if names:
            target = max(set(names), key=names.count)
        if target not in outputs:
            return
        area = self.wf.call("window-rules/output-info",
                            id=outputs[target]["id"])["workarea"]

        hidden = {n for n in self.files if not shown(n)}
        grace = now - self.started < STARTUP_GRACE
        if grace and any(n not in self.sizes for n in self.files):
            return
        if any(now - since < SIZE_SETTLE for _, since in self.sizes.values()):
            return

        sizes = {n: (self.sizes[n][0] if n in self.sizes
                     else (0, self.nominal.get(n, 100)))
                 for n in self.files}
        want = plan(self.lay, sizes, hidden, area)
        for name in self.files:
            if name in self.reloading:
                continue
            have = placed(name)
            if have is None:
                continue
            if name in hidden:
                where = (HIDDEN_TOP, have[1])
            elif name in want:
                where = want[name]
            else:
                continue
            if have[:2] != where:
                pin(name, where)
                self.reload(name, pids, "%s -> top %d, inset %d" % (
                    "hiding" if name in hidden else "moving", *where))

    # -- dimming ------------------------------------------------
    def focused_window(self):
        """Is a real window focused? Not the desktop, the panel, a card, or
        nothing at all (focusdim.py's three cases, in Wayfire's terms)."""
        info = self.wf.call("window-rules/get-focused-view").get("info")
        return bool(info and info.get("type") == "toplevel"
                    and info.get("mapped") and not info.get("minimized"))

    def dim(self):
        """Advance the fade, if any. Returns True while one is running.

        The cards do the fading themselves (lua/compat.lua): this publishes
        the level in LEVEL_FILE and has each card redraw (SIGUSR2, which
        conky takes as "refresh now") for every step of it. Fading the
        surfaces from the compositor, with the alpha plugin, would fade the
        blur behind them too."""
        now = time.monotonic()
        goal = LEVEL if self.focused_window() else 1.0
        if self.fade and self.fade[1] != goal or not self.fade and self.level != goal:
            self.fade = (self.level, goal, now)
        if not self.fade:
            return False
        a, b, t0 = self.fade
        t = min(1.0, (now - t0) / FADE)
        ease = 1 - (1 - t) ** 3                # close to cubic-bezier(.4,0,.2,1)
        self.level = a + (b - a) * ease
        if t >= 1.0:
            self.level, self.fade = b, None
        self.publish()
        return self.fade is not None

    def publish(self):
        os.makedirs(RUNDIR, exist_ok=True)
        with open(LEVEL_FILE + ".tmp", "w") as fh:
            fh.write("%.4f\n" % self.level)
        os.replace(LEVEL_FILE + ".tmp", LEVEL_FILE)
        for pid in self.pids.values():
            try:
                os.kill(pid, signal.SIGUSR2)
            except OSError:
                pass

    def undim(self):
        self.level = 1.0
        self.publish()

    # -- --show -------------------------------------------------
    def show(self):
        views = card_views(self.wf)
        outputs = {o["name"]: o for o in self.wf.call("window-rules/list-outputs")}
        target = self.output if self.output in outputs else None
        print("conky: %s   stack output: %s%s" % (
            self.conky, self.output or "(unset)",
            "" if target else " (not connected)"))
        area = None
        if target:
            area = self.wf.call("window-rules/output-info",
                                id=outputs[target]["id"])["workarea"]
        sizes = {n: (int(v["geometry"]["width"]), int(v["geometry"]["height"]))
                 for n, v in views.items()}
        hidden = {n for n in self.files if not shown(n)}
        want = plan(self.lay, sizes, hidden, area) if area else {}
        print("%-12s %-8s %16s %12s %12s" % ("card", "output", "at", "placed", "want"))
        for name in self.files:
            v = views.get(name)
            g = v["geometry"] if v else None
            have = placed(name)
            print("%-12s %-8s %16s %12s %12s%s" % (
                name, v.get("output-name", "") if v else "-",
                "%d,%d %dx%d" % (g["x"], g["y"], g["width"], g["height"]) if g else "not mapped",
                "%d,%d" % have[:2] if have else "-",
                "%d,%d" % want[name] if name in want else "-",
                "  hidden" if name in hidden else ""))


def single_instance():
    os.makedirs(RUNDIR, exist_ok=True)
    fh = open(LOCK, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return None
    return fh


def watch(stack):
    events = Wayfire()
    events.send("window-rules/events/watch", {"events": [
        "view-mapped", "view-unmapped", "view-focused", "view-minimized",
        "output-added", "output-removed", "output-layout-changed",
        "wset-workspace-changed"]})
    events.recv()                                    # the acknowledgement

    def stop(*_):
        stack.undim()
        sys.exit(0)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    # Cards started here are our children until they fork; nothing to
    # collect from them, so have the kernel reap them.
    signal.signal(signal.SIGCHLD, signal.SIG_IGN)

    stack.pass_()
    stack.publish()        # from full strength, whatever a last run left
    fading = False
    last_pass = time.monotonic()
    while True:
        timeout = FADE_STEP if fading else POLL
        ready, _, _ = select.select([events], [], [], timeout)
        if ready:
            events.recv()                   # what changed is re-read below
            while select.select([events], [], [], 0)[0]:
                events.recv()
        now = time.monotonic()
        if ready or now - last_pass >= POLL:
            stack.pass_()
            last_pass = now
        fading = stack.dim()


def main():
    global verbose
    verbose = "-v" in sys.argv or "--verbose" in sys.argv
    try:
        stack = Stack(Wayfire())
        if "--show" in sys.argv:
            stack.show()
            return 0
        lock = single_instance()
        if lock is None:
            sys.stderr.write("stack: already running\n")
            return 0
        watch(stack)
    except (IPCError, EOFError, ConnectionError) as e:
        sys.stderr.write("stack: %s\n" % e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
