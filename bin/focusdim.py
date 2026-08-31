#!/usr/bin/env python3
"""Fade the widget cards back while a real window has the focus.

The cards are meant to be glanceable, not competing with whatever you are
actually working in. So: as soon as a normal window takes focus they drop to
DIM, and the moment focus returns to the desktop (click the backdrop, close
the last window, switch to an empty workspace) they come back to full.

HOW THE FADE HAPPENS. This process only writes a number --
_NET_WM_WINDOW_OPACITY, a 32-bit cardinal where 0xFFFFFFFF is opaque -- onto
each card's client window. picom does the rest; the `class_g = 'conky'` rule
in ~/.config/picom/picom.conf animates the change instead of stepping it.
Nothing is redrawn by conky and no widget knows this exists.

Opacity rather than picom's `dim`, which is what you would reach for first:
the cards are only 45% opaque to begin with (own_window_argb_value in
widget-common.lua), so most of a "card" is wallpaper showing through it and
dim -- which only touches the window's own pixels -- has almost nothing to
work on. Measured here, dim = 1.0 moved the card area's mean brightness by 6%
and a subtle 0.38 was indistinguishable from no dim at all.

WHAT COUNTS AS FOCUSED is read from the root's _NET_ACTIVE_WINDOW. Three cases
mean "nothing is focused" and leave the cards lit:
  * the property is absent or 0 -- no window has focus at all;
  * the focused window is part of the desktop rather than something running on
    it (xfdesktop's backdrop, the panel, the cards themselves, the wallpaper
    sheet -- see DESKTOP below);
  * the focused window is not viewable. xfwm4 unmaps windows on other
    workspaces, and _NET_ACTIVE_WINDOW can still name one for a moment after a
    switch, so this is what makes switching to an empty workspace light the
    cards up rather than leave them dimmed by a window you can no longer see.

THE SLIDE INTERLOCK is the one piece of coupling. Workspace switches animate
the sticky cards by nudging _NET_WM_WINDOW_OPACITY (see the sticky rules in
picom.conf and ~/.local/bin/picom-slide-switch), so this process and that one
write the same property. They are kept apart by the tag the switch script
already sets: while any card's _PICOM_SLIDE_STICKY is 'l' or 'r' a slide is in
flight and nothing is written here. The tag going back to 'n' is a property
change on a window we are subscribed to, so the deferred write happens as soon
as the slide is over, without polling for it.

The level being targeted is also published to $XDG_RUNTIME_DIR/conky-gruvbox/
dim-level, because picom-slide-switch has to nudge AROUND it: nudging to its
own fixed near-1.0 values would light the cards up for the length of every
slide.

Needs python-xlib. Everything else is stdlib.
"""

import fcntl
import os
import select
import signal
import sys
import time

try:
    from Xlib import X, Xatom, display, error as xerror
except ImportError:
    sys.stderr.write("focusdim: python-xlib is not installed "
                     "(pacman -S python-xlib)\n")
    sys.exit(2)

PREFIX = "conky-gruvbox-"
OPAQUE = 0xFFFFFFFF

# How far back the cards go when a window has focus, 0.0 - 1.0, multiplying
# against the alpha they already have -- the cards are 45% opaque to start
# with (own_window_argb_value in widget-common.lua), so 0.4 leaves them at
# roughly 18%: clearly there, clearly behind whatever is in front of them.
#
# Lower means MORE transparent, and transparency is the whole effect -- there
# is no separate darkening knob, see the note at the top of the file. Tune it
# live without restarting anything:
#     CONKY_DIM_LEVEL=0.3 ~/.config/conky/gruvbox-dark/bin/focusdim.py --once
# then put the value you settle on here and restart the daemon:
#     pkill -f focusdim.py; ~/.config/conky/gruvbox-dark/bin/focusdim.py &
LEVEL = float(os.environ.get("CONKY_DIM_LEVEL", "0.65"))

# Windows that are the desktop rather than something on it. Matched against the
# class half of WM_CLASS, lowercased.
DESKTOP = {"xfdesktop", "conky", "wallpaper-sheet", "xfce4-panel"}

RUNDIR = os.path.join(os.environ.get("XDG_RUNTIME_DIR") or "/tmp",
                      "conky-gruvbox")
LOCK = os.path.join(RUNDIR, "focusdim.lock")
LEVEL_FILE = os.path.join(RUNDIR, "dim-level")

# Focus can change several times in one gesture (alt-tab held down, a window
# closing and handing focus on). Coalesce the burst rather than write through
# every intermediate state.
DEBOUNCE = 0.05
# Safety net for anything that slips past the event mask.
IDLE_POLL = 5.0
# The poll used instead while a write is being held back by the slide
# interlock. A slide is over in well under a second, so this only ever runs a
# handful of times, and it bounds how long the cards can stay at the wrong
# level if the tag's property change is missed.
RETRY = 0.15


def single_instance():
    """Two of these would fight over the same property. Returns the held lock,
    which the caller must keep referenced for the life of the process."""
    os.makedirs(RUNDIR, exist_ok=True)
    fh = open(LOCK, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return None
    return fh


class Dimmer:
    def __init__(self, d, level=LEVEL):
        self.d = d
        self.root = d.screen().root
        self.level = max(0.0, min(1.0, level))
        self.dim_value = int(round(self.level * OPAQUE))
        self.a_opacity = d.intern_atom("_NET_WM_WINDOW_OPACITY")
        self.a_active = d.intern_atom("_NET_ACTIVE_WINDOW")
        self.a_sticky = d.intern_atom("_PICOM_SLIDE_STICKY")
        self.cards = {}          # name -> client window
        self.subscribed = set()
        self.want = None         # the value last decided on, dim or opaque
        self.deferred = False    # a write held back by the slide interlock

    # ── the cards ─────────────────────────────────────────────
    def discover(self):
        """name -> client window, found by WM_NAME the same way reflow.py does.

        The CLIENT is what is wanted here, not xfwm4's frame: picom is run with
        detect-client-opacity, and picom-slide-switch writes to the client too
        (wmctrl reports client ids), so both writers land on the same window.
        """
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
                    found[name[len(PREFIX):]] = c
                    continue
                walk(c, depth + 1)

        walk(self.root, 0)
        self.cards = found
        for w in found.values():
            if w.id in self.subscribed:
                continue
            try:
                w.change_attributes(event_mask=X.PropertyChangeMask)
                self.subscribed.add(w.id)
            except xerror.XError:
                pass
        return found

    # ── the decision ──────────────────────────────────────────
    def focused(self):
        """The window with focus, or None if that is the desktop / nothing."""
        try:
            p = self.root.get_full_property(self.a_active, Xatom.WINDOW)
        except xerror.XError:
            return None
        if not p or not p.value or not p.value[0]:
            return None
        w = self.d.create_resource_object("window", p.value[0])
        try:
            if w.get_attributes().map_state != X.IsViewable:
                return None          # on another workspace, or minimised
            cls = w.get_wm_class()
        except xerror.XError:
            return None
        if cls and cls[-1].lower() in DESKTOP:
            return None
        return w

    def sliding(self):
        """True while a workspace slide is animating the cards.

        picom-slide-switch tags every card before it nudges their opacity and
        resets the tag to 'n' once the slide is done, so the tag is both the
        interlock and -- because we are subscribed to these windows -- the
        wake-up that releases it.
        """
        for w in self.cards.values():
            try:
                p = w.get_full_property(self.a_sticky, X.AnyPropertyType)
            except xerror.XError:
                continue
            if not p or not p.value:
                continue
            tag = p.value
            if isinstance(tag, bytes):
                tag = tag.decode("latin-1", "replace")
            if tag.strip("\x00") in ("l", "r"):
                return True
        return False

    # ── the write ─────────────────────────────────────────────
    def publish(self, value):
        """Tell picom-slide-switch what to nudge around. Written before the
        cards themselves so a switch racing this one never nudges to the level
        the cards have just left."""
        try:
            tmp = LEVEL_FILE + ".tmp"
            with open(tmp, "w") as fh:
                fh.write("%d\n" % value)
            os.replace(tmp, LEVEL_FILE)
        except OSError:
            pass

    def write(self, value, force=False):
        """A card already holding the target is skipped, so a settle() that
        changes nothing cannot fire a spurious fade."""
        n = 0
        for w in self.cards.values():
            if not force:
                try:
                    p = w.get_full_property(self.a_opacity, Xatom.CARDINAL)
                except xerror.XError:
                    continue
                if p and p.value and p.value[0] == value:
                    continue
            try:
                w.change_property(self.a_opacity, Xatom.CARDINAL, 32, [value])
                n += 1
            except xerror.XError:
                continue
        if n:
            self.d.sync()
        return n

    def settle(self):
        self.discover()
        value = self.dim_value if self.focused() else OPAQUE
        if value != self.want:
            self.publish(value)
            self.want = value
        if self.sliding():
            # Retried when the tag goes back to 'n'. That is a property change
            # on a window we are subscribed to, so it is normally an event --
            # but see RETRY: this flag makes the wait short enough that the
            # relight is never noticeable even if the event is not seen.
            self.deferred = True
            return 0
        self.deferred = False
        return self.write(value)

    def restore(self):
        """Leave the cards lit. A dimmer that has been stopped should not go on
        dimming them for the rest of the session."""
        self.discover()
        self.publish(OPAQUE)
        self.write(OPAQUE, force=True)


def watch(dim):
    dim.root.change_attributes(
        event_mask=X.PropertyChangeMask | X.SubstructureNotifyMask)

    def bye(*_):
        dim.restore()
        sys.exit(0)

    signal.signal(signal.SIGTERM, bye)
    signal.signal(signal.SIGINT, bye)

    dim.settle()
    while True:
        # ASK THE QUEUE BEFORE THE SOCKET. python-xlib keeps its own event
        # queue, and every X request settle() makes reads from the connection
        # to get its reply -- which pulls any events that have arrived off the
        # socket and into that queue as a side effect. Those events leave the
        # fd unreadable, so a select() on the fd alone will happily block for
        # the whole IDLE_POLL with work already waiting.
        #
        # This is what used to leave the cards dimmed for ~5s after sliding to
        # an empty workspace: the write is deferred while the slide is in
        # flight and released by _PICOM_SLIDE_STICKY going back to 'n', and
        # that release lands ~0.7s later -- often during a settle(), where it
        # was swallowed into the queue and not looked at again until the poll.
        work = bool(dim.d.pending_events())
        if not work:
            ready, _, _ = select.select([dim.d.fileno()], [], [],
                                        RETRY if dim.deferred else IDLE_POLL)
            # A timed-out select settles anyway: that is the poll doing its job.
            work = not ready
        while dim.d.pending_events():
            dim.d.next_event()
            work = True
        if not work:
            continue             # readable, but nothing we care about came out
        time.sleep(DEBOUNCE)
        while dim.d.pending_events():
            dim.d.next_event()
        try:
            dim.settle()
        except xerror.XError:
            pass


def main():
    d = display.Display()
    dim = Dimmer(d)

    if "--show" in sys.argv:
        dim.discover()
        w = dim.focused()
        cls = "-"
        if w is not None:
            try:
                c = w.get_wm_class()
                cls = c[-1] if c else "?"
            except xerror.XError:
                pass
        print("level      %.2f  (%d)" % (dim.level, dim.dim_value))
        print("focused    %s" % cls)
        print("sliding    %s" % dim.sliding())
        print("cards      %s" % (", ".join(sorted(dim.cards)) or "none"))
        for name in sorted(dim.cards):
            w = dim.cards[name]
            p = w.get_full_property(dim.a_opacity, Xatom.CARDINAL)
            cur = p.value[0] if p and p.value else OPAQUE
            print("  %-12s %10d  %.2f" % (name, cur, cur / OPAQUE))
        return 0
    if "--once" in sys.argv:
        dim.settle()
        return 0
    if "--restore" in sys.argv:
        dim.restore()
        return 0

    lock = single_instance()
    if lock is None:
        sys.stderr.write("focusdim: already running\n")
        return 0
    watch(dim)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        pass
