#!/bin/sh
# Start the gruvbox-dark widget stack the way this session needs it.
#
# X11: exactly as before -- `conky-gruvbox-dark start`, which brings the copy
#      in ~/.config/conky/gruvbox-dark up to date and runs ../start.sh.
# Wayland: the same cards, through wayland/card.lua, started and looked after
#      by wayland/bin/stack.py (which stands in for bin/reflow.py and
#      bin/focusdim.py). Needs Wayfire with the ipc, ipc-rules and
#      layer-output plugins -- see wayland/README.md.
#
# This is what the login entry runs, so one entry serves both sessions.
DIR="$HOME/.config/conky/gruvbox-dark"
SETUP=/usr/bin/conky-gruvbox-dark

if [ "${XDG_SESSION_TYPE:-}" = x11 ] || [ -z "${WAYLAND_DISPLAY:-}" ]; then
    [ -x "$SETUP" ] && exec "$SETUP" start
    exec "$DIR/start.sh"
fi

# The package's file sync is not tied to X11; only its start is.
[ -x "$SETUP" ] && "$SETUP" update >/dev/null 2>&1

# Take down a previous run in THIS session: the watcher (it holds a lock, so a
# second one would only exit) and the cards. Another session's -- a nested
# compositor's, say -- are told apart by their WAYLAND_DISPLAY.
ours() {
    tr '\0' '\n' < "/proc/$1/environ" 2>/dev/null |
        grep -qx "WAYLAND_DISPLAY=$WAYLAND_DISPLAY"
}
for pid in $(pgrep -u "$USER" -f "$DIR/wayland/bin/stack.py"); do
    ours "$pid" && kill "$pid" 2>/dev/null
done
for pid in $(pgrep -u "$USER" -x conky); do
    if ours "$pid" && tr '\0' '\n' < "/proc/$pid/environ" |
        grep -q '^CONKY_GRUVBOX_CARD='; then
        kill "$pid" 2>/dev/null
    fi
done

# Let the panel come up first: layer-output finds the layer shell through
# the first client to bind it, and until then a card opens on whichever
# output has the focus (stack.py would move it, but with a blink).
sleep "${CONKY_DELAY:-5}"

# Start from the stack table rather than from last run's pinned positions,
# as ../start.sh does.
rm -f "${XDG_RUNTIME_DIR:-/tmp}"/conky-gruvbox/*.place \
      "${XDG_RUNTIME_DIR:-/tmp}"/conky-gruvbox/*.shown

exec "$DIR/wayland/bin/stack.py"
