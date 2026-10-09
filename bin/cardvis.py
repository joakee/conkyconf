"""Let a conky card hide itself while it has nothing to show.

A card's own script calls set_visible(name, bool) on every tick. The card's
conky window is withdrawn (unmapped) to hide it and mapped to bring it back;
bin/reflow.py closes the stack up over a card whose window is unmapped and
reopens the gap when it returns, so nothing else has to know.

X is only touched on a change. The last state applied is cached per conky
pid, so a restarted card -- which always comes up mapped -- is caught too, and
python-xlib is only imported then: a normal tick costs a small file read.
"""

import os
import sys

PREFIX = "conky-gruvbox-"           # + the card name = own_window_title
STATE_DIR = os.path.join(os.environ.get("XDG_RUNTIME_DIR")
                         or f"/run/user/{os.getuid()}", "conky-gruvbox")

# What conky's own_window_hints ask for. A WM is required to strip
# _NET_WM_STATE from a window it lets go of (xfwm4 does), and a client must set
# it again before mapping, or the card comes back as an ordinary window: not
# sticky, not below, on the taskbar. The live value is saved at hide time; this
# is only the fallback if that was lost.
WM_STATE_DEFAULT = ["_NET_WM_STATE_STICKY", "_NET_WM_STATE_SKIP_PAGER",
                    "_NET_WM_STATE_SKIP_TASKBAR", "_NET_WM_STATE_BELOW"]
# <name>.visible.wmstate holds that saved value; <name>.visible.pos holds
# where the card stood when hidden. Conky sets no WM_NORMAL_HINTS, so a remapped
# card is smart-placed by xfwm (mid-screen) and flashes there until
# bin/reflow.py moves it; asking for this position up front with USPosition
# brings it back where it was instead.


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


def find_card(root, title):
    """The card's client window, wherever it is: under its xfwm frame while
    shown, straight under the root once withdrawn."""
    def walk(w, depth):
        if depth > 4:
            return None
        for c in w.query_tree().children:
            try:
                if c.get_wm_name() == title:
                    return c
            except Exception:
                pass
            hit = walk(c, depth + 1)
            if hit:
                return hit
        return None
    return walk(root, 0)


def set_visible(name, want):
    """Show or hide the conky card `name` (as in M.stack). Cheap to call every
    tick: X is only touched when the wanted state differs from the last one
    applied by this same conky process."""
    state = os.path.join(STATE_DIR, name + ".visible")
    wm_state_saved, pos_saved = state + ".wmstate", state + ".pos"
    key = "%d %s" % (conky_pid(), "shown" if want else "hidden")
    try:
        with open(state) as f:
            if f.read() == key:
                return                 # already done, nothing to ask X
    except OSError:
        pass
    try:
        from Xlib import X, Xatom, Xutil, display, protocol
        d = display.Display()
        root = d.screen().root
        card = find_card(root, PREFIX + name)
        if card is None:
            return                     # not mapped yet at startup; next tick
        NET_WM_STATE = d.intern_atom("_NET_WM_STATE")
        if want:
            try:
                with open(wm_state_saved) as f:
                    names = f.read().split()
            except OSError:
                names = []
            atoms = [d.intern_atom(n) for n in (names or WM_STATE_DEFAULT)]
            card.change_property(NET_WM_STATE, Xatom.ATOM, 32, atoms)
            try:
                with open(pos_saved) as f:
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
                os.makedirs(STATE_DIR, exist_ok=True)
                with open(wm_state_saved, "w") as f:
                    f.write(" ".join(d.get_atom_name(a) for a in prop.value))
            if card.get_attributes().map_state == X.IsViewable:
                at = root.translate_coords(card, 0, 0)
                with open(pos_saved, "w") as f:
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
        print("%s: could not %s card: %s"
              % (name, "show" if want else "hide", e), file=sys.stderr)
        return
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(state, "w") as f:
        f.write(key)
