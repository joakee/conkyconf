#!/usr/bin/env python3
"""Live countdowns for the conky countdowns card.

The countdowns themselves are owned by the Countdowns GTK app
(~/.local/bin/countdowns), which writes them to

    $XDG_DATA_HOME/countdowns/countdowns.json

with an atomic rename, so this never sees half a file. Only countdowns whose
switch is on in the app are shown here. Targets are local wall-clock times.

Emits conky markup (the card uses ${execpi 1}), so the card grows and shrinks
with the number of countdowns and bin/reflow.py keeps the gaps. Stdlib only,
matching the other scripts.
"""

import json
import math
import os
import time
from datetime import datetime

DATA = os.path.join(os.environ.get("XDG_DATA_HOME")
                    or os.path.expanduser("~/.local/share"),
                    "countdowns", "countdowns.json")

MAX_SHOWN = int(os.environ.get("COUNTDOWNS_MAX", 6))
# A countdown that has passed reads "done" for this long, then drops off.
DONE_HOLD = int(os.environ.get("COUNTDOWNS_DONE_HOLD", 6 * 3600))
LABEL_MAX = 22    # chars at size 9 that fit beside a size-11 bold time

# Row spacing, as ${voffset}s. Conky sizes the window WITHOUT the voffsets
# inside the text, so each row's net shift eats into the bottom padding and a
# full card ends up nearly touching its edge. A voffset at the very end of the
# last line IS counted, so the total is handed back there -- measured, that
# restores the bottom margin pixel for pixel whatever the row count.
ROW_GAP   = 4     # above each countdown
DATE_LIFT = -1    # tucks the date up under its label

FONT = "Maple Mono NF CN"
GLYPH = "\U000F051F"   # timer-sand, verified by rendering in this font


def esc(s):
    return s.replace("$", "$$")


def fmt_remaining(secs):
    s = max(0, math.ceil(secs))
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    hms = f"{h:02}:{m:02}:{s:02}"
    return f"{d}d {hms}" if d else hms


def colour(left):
    if left <= 0:
        return "${color6}"       # green: done
    if left < 3600:
        return "${color7}"       # red: under an hour
    if left < 86400:
        return "${color1}"       # orange: under a day
    return "${color4}"           # blue


def load():
    with open(DATA) as f:
        items = json.load(f).get("countdowns", [])
    out = []
    for it in items:
        if not it.get("enabled", True):
            continue
        try:
            t = datetime.fromisoformat(it["target"])
        except (KeyError, ValueError, TypeError):
            continue
        out.append((t.timestamp(), t, it.get("label") or "Countdown"))
    return sorted(out)


def main():
    print("${color1}${font %s:size=10}%s${font}  "
          "${color5}${font %s:size=8}COUNTDOWNS${font}" % (FONT, GLYPH, FONT))
    try:
        items = load()
    except FileNotFoundError:
        items = []
    except (OSError, ValueError, AttributeError):
        print("${voffset 2}${color7}countdowns.json unreadable${voffset 2}",
              end="")
        return

    now = time.time()
    rows = [r for r in items if r[0] - now > -DONE_HOLD][:MAX_SHOWN]
    if not rows:
        print("${voffset 2}${color5}nothing counting down${voffset 2}", end="")
        return

    lines = []
    for ts, t, label in rows:
        left = ts - now
        if len(label) > LABEL_MAX:
            label = label[:LABEL_MAX - 1] + "…"
        value = fmt_remaining(left) if left > 0 else "done"
        lines.append(
            "${voffset %d}${color0}%s${alignr}%s${font %s:bold:size=11}%s${font}\n"
            "${voffset %d}${color5}${font %s:size=8}%s${font}"
            % (ROW_GAP, esc(label), colour(left), FONT, value, DATE_LIFT,
               FONT, t.strftime("%a %-d %b %Y · %H:%M")))
    print("\n".join(lines)
          + "${voffset %d}" % ((ROW_GAP + DATE_LIFT) * len(lines)), end="")


if __name__ == "__main__":
    main()
