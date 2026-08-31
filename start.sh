#!/bin/sh
# Launch the gruvbox-dark widget stack -- one conky window per widget.
# Add/remove widgets by adding/removing files in widgets/ and editing the
# stack table in widget-common.lua (positions are derived from it).
#
# NOTE: calls conky by absolute path on purpose. ~/.local/bin/conky is a shim
# that redirects a bare `conky` here, so using the bare name would recurse.
CONKY=/usr/bin/conky
DIR="$HOME/.config/conky/gruvbox-dark"

# Which monitor the stack lands on is M.OUTPUT in widget-common.lua, by output
# NAME -- no RandR primary is set here, for the xfwm4 alt-tab switcher's sake
# (see the xfwm4-alttab-primary-monitor memory), and a Xinerama index is not
# something to pin to either since a hotplug renumbers them. Set CONKY_HEAD_OUTPUT
# to override for one run; CONKY_HEAD still sets the head index of last resort.

pkill -x conky 2>/dev/null
# let the WM/panel settle so placement lands on the right Xinerama head
sleep "${CONKY_DELAY:-5}"

for conf in "$DIR"/widgets/*.conf; do
    "$CONKY" -c "$conf" >/dev/null 2>&1 &
done

# Cards marked `auto` resize themselves, and conky reads gap_y only once, so
# the gap under a card that has shrunk would stay open at whatever the nominal
# height reserved. This watches for the resize and closes it back up. It holds
# a lock, so re-running this script will not stack up watchers; if it is not
# running the stack simply falls back to fixed positions.
"$DIR/bin/reflow.py" >/dev/null 2>&1 &

# Fade the cards back while a window has the focus, full brightness when the
# desktop does. Also holds a lock, so re-running this script will not stack
# them up; if it is not running the cards simply stay at full brightness.
"$DIR/bin/focusdim.py" >/dev/null 2>&1 &

