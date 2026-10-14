"""Let a conky card hide itself while it has nothing to show -- Wayland half.

bin/cardvis.py hides a card by withdrawing its X window. Under Wayland there
is no window to withdraw from outside the client, so this only records what
the card wants, in $XDG_RUNTIME_DIR/conky-gruvbox/<name>.shown ("1" or "0"),
and wayland/bin/stack.py does the hiding: it parks the card off screen and
closes the stack up over it, and brings it back when the file says so.

Same interface as bin/cardvis.py, which the card scripts import by name;
wayland/bin/run makes sure this is the one they get.
"""

import os

STATE_DIR = os.path.join(os.environ.get("XDG_RUNTIME_DIR")
                         or f"/run/user/{os.getuid()}", "conky-gruvbox")


def set_visible(name, want):
    """Show or hide the conky card `name` (as in M.stack). Cheap to call every
    tick: the file is only written when the wanted state changes."""
    path = os.path.join(STATE_DIR, name + ".shown")
    value = "1" if want else "0"
    try:
        with open(path) as f:
            if f.read() == value:
                return
    except OSError:
        pass
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(path + ".tmp", "w") as f:
            f.write(value)
        os.replace(path + ".tmp", path)
    except OSError:
        pass                           # never break the card over this
