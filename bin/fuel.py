#!/usr/bin/env python3
"""FuelWatch WA -> conky markup for the gruvbox-dark fuel widget.

Source is FuelWatch's public RSS feed, the same one the fuelwatchwa and
fuelwatcher projects wrap:

  https://www.fuelwatch.wa.gov.au/fuelwatch/fuelWatchRSS?Product=1

Product 1 is unleaded 91. With no Region/Suburb parameter the feed is every
reporting site in the state (~940 of them, Kununurra to Esperance) despite
its channel title saying "All Metro Regions". It is ~700 KB of XML, so the
script reduces it to the handful of numbers the card shows and caches only
those.

The average is statewide; the cheapest list is Perth metro only. Items carry
no region, so the metro sites come from separate per-region requests:
FuelWatch's own metro is regions 25 North of River, 26 South of River and
27 East/Hills (Joondalup/Yanchep to Rockingham/Baldivis, Midland, Armadale;
Mandurah is Peel, not metro). Without that, the top of the list was a remote
roadhouse 600 km north.

WA prices are fixed for 24 h from 06:00 (stations lodge them the afternoon
before), so the data changes once a day. The cache is refreshed when it is
older than FUEL_TTL or when it holds a day other than today, but a failed
attempt is not retried for FUEL_RETRY seconds -- the render runs every
minute and the feed is not small.

Prints the widget body on stdout for ${execpi}. Never blocks on the network
once a cache exists: the refresh is forked into the background, same pattern
as weather.py.

Env:
  FUEL_TTL       seconds before a refresh is triggered (default 3600)
  FUEL_RETRY     seconds between attempts after a failure (default 300)
  FUEL_PRODUCT   FuelWatch product id (default 1, unleaded 91)
  FUEL_TOP       how many cheapest sites to list (default 5)
  FUEL_REGIONS   FuelWatch region ids the cheapest list is drawn from
                 (default 25,26,27 = Perth metro; empty = whole state)
"""

import datetime as dt
import fcntl
import json
import os
import subprocess
import sys
import time

TTL = int(os.environ.get("FUEL_TTL", "3600"))
RETRY = int(os.environ.get("FUEL_RETRY", "300"))
PRODUCT = os.environ.get("FUEL_PRODUCT", "1")
TOP = int(os.environ.get("FUEL_TOP", "5"))
REGIONS = [r for r in os.environ.get("FUEL_REGIONS", "25,26,27").split(",") if r]

URL = f"https://www.fuelwatch.wa.gov.au/fuelwatch/fuelWatchRSS?Product={PRODUCT}"
UA = "conky-gruvbox-fuel/1.0 (personal desktop widget)"

CACHE_DIR = os.path.join(
    os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")),
    "conky-gruvbox",
)
CACHE = os.path.join(CACHE_DIR, "fuel.json")
LOCK = os.path.join(CACHE_DIR, "fuel.lock")
# mtime = when the last refresh was attempted, successful or not.
ATTEMPT = os.path.join(CACHE_DIR, "fuel.attempt")

FONT = "Maple Mono NF CN"
GLYPH = "\U000F0298"  # gas-station, verified by rendering in Maple Mono NF CN

# Row geometry. ${goto} counts from the window's left edge, so the card's
# inner padding (widget-common.lua's M.PAD) has to be added back in -- see the
# same note in weather.py. Text widths are Maple Mono's advance at 96 dpi.
PAD = 14
NAME_X = PAD + 48
TEXT_W = 292 - (NAME_X - PAD)  # room for name + suburb
CH9, CH8 = 7.2, 6.4  # px per char at size 9 / size 8
GUTTER = 10  # minimum space between a name and its suburb


def sites(url, timeout):
    import gzip
    import urllib.request
    import xml.etree.ElementTree as ET

    req = urllib.request.Request(
        url, headers={"User-Agent": UA, "Accept-Encoding": "gzip"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)

    out = []
    for item in ET.fromstring(raw).iter("item"):
        try:
            price = float(item.findtext("price"))
        except (TypeError, ValueError):
            continue
        out.append(
            {
                "price": price,
                "name": (item.findtext("trading-name") or "").strip(),
                "suburb": (item.findtext("location") or "").strip(),
                "date": (item.findtext("date") or "").strip(),
            }
        )
    if not out:
        raise ValueError(f"no priced sites in {url}")
    return out


def fetch(timeout):
    """Pull the feeds and replace the cache atomically. Raises on failure,
    which leaves the previous good data in place."""
    state = sites(URL, timeout)
    local = state
    if REGIONS:
        local = []
        for r in REGIONS:
            local += sites(f"{URL}&Region={r}", timeout)

    # Ties are common (whole chains price identically), so break them by name
    # to keep the list stable between refreshes.
    local.sort(key=lambda s: (s["price"], s["name"]))
    data = {
        "date": max(s["date"] for s in state),
        "avg": sum(s["price"] for s in state) / len(state),
        "count": len(state),
        "cheapest": local[:TOP],
    }
    tmp = CACHE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, CACHE)
    return data


def refresh():
    """Fetch now under the lock. If a refresh is already running this one is
    redundant and just leaves."""
    with open(LOCK, "w") as lk:
        try:
            fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return 0
        with open(ATTEMPT, "a"):
            pass
        os.utime(ATTEMPT)
        try:
            fetch(30)
        except Exception as exc:
            print(f"refresh failed: {exc}", file=sys.stderr)
            return 1
    return 0


def since(path):
    try:
        return time.time() - os.stat(path).st_mtime
    except FileNotFoundError:
        return None


def safe(s):
    """conky re-parses execpi output, so strip anything that could look like
    markup."""
    return "".join(c for c in str(s) if c not in "${}#").strip()


def fit(name, suburb):
    """Truncate the name so name + suburb share one row without colliding."""
    room = int((TEXT_W - GUTTER - len(suburb) * CH8) / CH9)
    if len(name) > room:
        name = name[: max(room - 1, 1)].rstrip() + "…"
    return name


def header(right=""):
    return (
        f"${{color1}}${{font {FONT}:size=10}}{GLYPH}${{font}}  "
        f"${{color5}}${{font {FONT}:size=8}}FUEL · ULP 91"
        f"${{alignr}}{right}${{font}}"
    )


def main():
    os.makedirs(CACHE_DIR, exist_ok=True)

    if len(sys.argv) > 1 and sys.argv[1] == "--refresh":
        return refresh()

    today = dt.date.today().isoformat()
    data = None
    try:
        with open(CACHE) as f:
            data = json.load(f)
    except Exception:
        pass

    if data is None and since(ATTEMPT) is None:
        # Nothing to show at all: one synchronous fetch so the card is
        # populated on first paint rather than blank for a whole interval.
        try:
            data = fetch(10)
        except Exception:
            pass
        with open(ATTEMPT, "a"):
            pass
        os.utime(ATTEMPT)
    else:
        stale = (
            data is None
            or data.get("date") != today
            or (since(CACHE) or 0) >= TTL
        )
        last = since(ATTEMPT)
        if stale and (last is None or last >= RETRY):
            # Detached. Output must be discarded or execpi waits on us.
            subprocess.Popen(
                [sys.executable, os.path.abspath(__file__), "--refresh"],
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

    if data is None:
        print(header())
        print("${voffset 2}${color5}waiting for FuelWatch")
        return 0

    # The date is the day the prices are FOR, not when they were fetched.
    # Red when it is not today: yesterday's prices are no longer on the board.
    try:
        d = dt.date.fromisoformat(data["date"])
        stamp = f"{d.day} {d:%b}"
    except (KeyError, ValueError):
        stamp = "?"
    if data.get("date") != today:
        stamp = f"${{color7}}{stamp}${{color5}}"

    print(header(stamp))
    print(
        f"${{voffset 2}}${{color3}}${{font {FONT}:bold:size=15}}"
        f"{data['avg']:.1f}${{font}}"
        f"${{color5}}${{offset 6}}${{voffset -4}}¢/L  state avg"
        f"${{alignr}}{data['count']} sites${{voffset 4}}"
    )
    # Conky ignores mid-text ${voffset} when it sizes the window, so every
    # pixel of it above is taken out of the bottom padding. A ${voffset} at
    # the END of the last line is counted, so the sum is paid back there --
    # plus 1 px, which is what it takes for the bottom margin to measure the
    # same as the top (19 px from the window edge to the ink, both ways).
    spent = 2 - 4 + 4
    rows = data.get("cheapest", [])
    for i, s in enumerate(rows):
        suburb = safe(s["suburb"]).title()
        name = fit(safe(s["name"]), suburb)
        gap = 6 if i == 0 else 1
        spent += gap
        tail = f"${{voffset {spent + 1}}}" if i == len(rows) - 1 else ""
        print(
            f"${{voffset {gap}}}${{color6}}{s['price']:.1f}"
            f"${{goto {NAME_X}}}${{color}}{name}"
            f"${{alignr}}${{color5}}${{font {FONT}:size=8}}{suburb}${{font}}{tail}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
