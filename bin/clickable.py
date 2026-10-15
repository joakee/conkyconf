#!/usr/bin/env python3
"""Let a card take mouse clicks again.

    clickable.py <card name>

Conky 1.25 gives an undecorated own_window an empty X input shape -- meant to
make it click-through, like a desktop -- so a click on the card lands on the
desktop under it and the card's lua_mouse_hook never hears of it. This puts
the input shape back to the whole window, for the card titled
conky-gruvbox-<name>. lua/clickable.lua runs it once per window.

The card may not be managed yet when this starts (it runs from the card's first
draw), so it looks for a few seconds before giving up.

Needs python-xlib. Everything else is stdlib.
"""

import sys
import time

from Xlib import X, display
from Xlib.ext import shape

PREFIX = "conky-gruvbox-"
TRIES, WAIT = 20, 0.25


def cards(d, title):
    root = d.screen().root
    clients = root.get_full_property(
        d.intern_atom("_NET_CLIENT_LIST"), X.AnyPropertyType)
    for wid in (clients.value if clients else []):
        w = d.create_resource_object("window", wid)
        try:
            if w.get_wm_name() == title:
                yield w
        except Exception:
            pass


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__.strip().splitlines()[2].strip())
    title = PREFIX + sys.argv[1]
    d = display.Display()
    if not d.has_extension("SHAPE"):
        return
    for _ in range(TRIES):
        found = list(cards(d, title))
        if found:
            for w in found:
                w.shape_mask(shape.SO.Set, shape.SK.Input, 0, 0, X.NONE)
            d.sync()
            return
        time.sleep(WAIT)


if __name__ == "__main__":
    main()
