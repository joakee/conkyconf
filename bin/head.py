#!/usr/bin/env python3
"""Resolve which Xinerama head is the primary display.

Conky positions a window with `xinerama_head` plus a gap measured from that
head's corner, so it only needs the INDEX. bin/reflow.py re-derives positions
itself and writes them to the root window, so it needs the head's ORIGIN too --
its y offset is exactly what was missing before, which parked the whole stack
on whichever monitor happened to sit at the top of the root window.

Both read the answer from here so there is one definition of "primary" and no
second copy to drift.

Three ways to say which head that is, tried in order:

  1. M.OUTPUT in widget-common.lua ($CONKY_HEAD_OUTPUT overrides it) -- an
     XRandR output NAME, as `xrandr` prints them. Preferred: names are the only
     one of these three that survives a hotplug unchanged. Skipped if the output
     is not connected or is switched off, so closing the lid or undocking falls
     through to the next rule rather than stranding the stack on a dead monitor.
  2. Whatever XRandR marks primary (`xrandr --output NAME --primary`). Note this
     machine keeps primary UNSET on purpose -- xfwm4 4.17+ draws the Alt+Tab
     switcher only on the primary monitor -- so in practice rule 1 or 3 answers.
  3. $CONKY_HEAD, else head 0.

In each case the CRTC geometry is matched against the Xinerama screen list to
get the index, since that is what conky's xinerama_head counts in.

Run it directly to see what it resolves to:  bin/head.py -v
"""

import os
import re
import sys
from collections import namedtuple

Head = namedtuple("Head", "index x y width height source")

COMMON = os.path.expanduser("~/.config/conky/gruvbox-dark/widget-common.lua")

FALLBACK = int(os.environ.get("CONKY_HEAD") or 0)


def wanted_output(path=COMMON):
    """The output name to pin to, or None. Env first so a single run can be
    pointed elsewhere without editing the shared config."""
    env = os.environ.get("CONKY_HEAD_OUTPUT")
    if env is not None:
        return env.strip() or None
    try:
        with open(path) as fh:
            m = re.search(r"^\s*M\.OUTPUT\s*=\s*'([^']*)'", fh.read(), re.M)
    except OSError:
        return None
    return (m.group(1) or None) if m else None


def _resources(d, root):
    """_current asks the server what it already knows; the plain call makes it
    re-probe every output, which takes long enough to be felt when
    bin/reflow.py asks on every pass."""
    try:
        return root.xrandr_get_screen_resources_current()
    except Exception:
        return root.xrandr_get_screen_resources()


def _crtc_geometry(d, res, info):
    """(x, y, w, h) of the CRTC an output is driving, or None if it is dark."""
    if not info.crtc:                    # connected but switched off, or unwired
        return None
    c = d.xrandr_get_crtc_info(info.crtc, res.config_timestamp)
    return (c.x, c.y, c.width, c.height)


# Output XIDs are stable while the server's view of the hardware is, but names
# cost a round trip each to read and there are fifteen outputs on this machine.
# Remember the last XID that answered to a name and re-check just that one --
# a hit is one round trip, a miss rebuilds the map and is no worse than not
# caching at all.
_BY_NAME = {}


def _output_info_named(d, res, name):
    out = _BY_NAME.get(name)
    if out is not None:
        try:
            info = d.xrandr_get_output_info(out, res.config_timestamp)
            if info.name == name:
                return info
        except Exception:
            pass
    for out in res.outputs:
        try:
            info = d.xrandr_get_output_info(out, res.config_timestamp)
        except Exception:
            continue
        _BY_NAME[info.name] = out
        if info.name == name:
            return info
    return None


def _named_geometry(d, root, name):
    """(x, y, w, h) of the output called `name`, or None if it is absent/dark."""
    try:
        res = _resources(d, root)
        info = _output_info_named(d, res, name)
        return _crtc_geometry(d, res, info) if info else None
    except Exception:
        return None


def _screens(d):
    """Xinerama screens as (x, y, w, h), in head-index order."""
    try:
        return [(s.x, s.y, s.width, s.height)
                for s in d.xinerama_query_screens().screens]
    except Exception:
        return []


def _primary_geometry(d, root):
    """(x, y, w, h) of the XRandR primary output, or None if there is none."""
    try:
        out = root.xrandr_get_output_primary().output
        if not out:                      # 0 == None; no primary is set
            return None
        res = _resources(d, root)
        info = d.xrandr_get_output_info(out, res.config_timestamp)
        return _crtc_geometry(d, res, info)
    except Exception:
        return None


def resolve(d=None, root=None):
    """The primary head. Never raises: degrades to the fallback index, and to
    a single full-screen head if the X server has no Xinerama at all."""
    if d is None:
        from Xlib import display
        d = display.Display()
    if root is None:
        root = d.screen().root

    screens = _screens(d)
    if not screens:
        g = root.get_geometry()
        return Head(0, 0, 0, g.width, g.height, "no-xinerama")

    # A geometry the Xinerama list does not carry has no index for conky to
    # use, so treat it as no answer and try the next rule. That is the mirrored
    # case, mainly: two outputs at one position show up as a single screen.
    name = wanted_output()
    if name:
        geom = _named_geometry(d, root, name)
        if geom in screens:
            i = screens.index(geom)
            return Head(i, *screens[i], "output:" + name)

    geom = _primary_geometry(d, root)
    if geom in screens:
        i = screens.index(geom)
        return Head(i, *screens[i], "randr-primary")

    i = FALLBACK if 0 <= FALLBACK < len(screens) else 0
    return Head(i, *screens[i], "fallback")


def main():
    try:
        h = resolve()
    except Exception as e:                # no display, no python-xlib, ...
        sys.stderr.write("head: %s\n" % e)
        print("%d 0 0 0 0" % FALLBACK)    # conky can still use the index
        return 1
    if "-v" in sys.argv or "--verbose" in sys.argv:
        sys.stderr.write("head %d at %d,%d %dx%d (%s)\n"
                         % (h.index, h.x, h.y, h.width, h.height, h.source))
    print("%d %d %d %d %d" % (h.index, h.x, h.y, h.width, h.height))
    return 0


if __name__ == "__main__":
    sys.exit(main())
