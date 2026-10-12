#!/usr/bin/env python3
"""FuelWatch WA -> conky markup for the gruvbox-dark fuel widget.

Source is FuelWatch's public RSS feed, the same one the fuelwatchwa and
fuelwatcher projects wrap:

  https://www.fuelwatch.wa.gov.au/fuelwatch/fuelWatchRSS?Product=1

With no Region/Suburb parameter the feed is every reporting site in the
state (~940 of them for ULP, Kununurra to Esperance) despite its channel
title saying "All Metro Regions". It is up to ~700 KB of XML, so the script
reduces it to the handful of numbers the card shows and caches only those.

Clicking the fuel label in the header (lua/fuel.lua -> --cycle) steps
through PRODUCTS; the choice is kept in fuel.product and each product has
its own cache. The feed never names the product it is serving, so the ids
below were checked by price ordering (91 < 95 < 98, LPG cheapest, both
diesels dearest) and match the fuelwatcher project's table. 3, 7-9 and
12-30 return nothing. FuelWatch only publishes two diesels: plain (4) and
"brand diesel" (11), the premium lines like Vortex and V-Power.

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
  FUEL_PRODUCT   product shown before the first click (default 1, ULP 91)
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
TOP = int(os.environ.get("FUEL_TOP", "5"))
REGIONS = [r for r in os.environ.get("FUEL_REGIONS", "25,26,27").split(",") if r]

# FuelWatch product id -> header label, in click order.
PRODUCTS = {
    "1": "ULP 91",
    "2": "PULP 95",
    "6": "PULP 98",
    "4": "DIESEL",
    "11": "BRAND DIESEL",
    "5": "LPG",
    "10": "E85",
}

URL = "https://www.fuelwatch.wa.gov.au/fuelwatch/fuelWatchRSS?Product={}"
UA = "conky-gruvbox-fuel/1.0 (personal desktop widget)"

CACHE_DIR = os.path.join(
    os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")),
    "conky-gruvbox",
)
# Which product is selected; survives restarts.
CHOICE = os.path.join(CACHE_DIR, "fuel.product")


def paths(product):
    """cache, lock, and attempt file (whose mtime = when the last refresh was
    attempted, successful or not) for one product."""
    base = os.path.join(CACHE_DIR, f"fuel-{product}")
    return base + ".json", base + ".lock", base + ".attempt"


def selected():
    try:
        with open(CHOICE) as f:
            p = f.read().strip()
    except FileNotFoundError:
        p = os.environ.get("FUEL_PRODUCT", "1")
    return p if p in PRODUCTS else "1"

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


def sites(url, timeout, required=True):
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
    if not out and required:
        raise ValueError(f"no priced sites in {url}")
    return out


def fetch(product, timeout):
    """Pull the feeds and replace the cache atomically. Raises on failure,
    which leaves the previous good data in place."""
    url = URL.format(product)
    state = sites(url, timeout)
    local = state
    if REGIONS:
        local = []
        for r in REGIONS:
            # A region with none of this fuel is normal (no E85 in
            # East/Hills), not a failed fetch.
            local += sites(f"{url}&Region={r}", timeout, required=False)

    # Ties are common (whole chains price identically), so break them by name
    # to keep the list stable between refreshes.
    local.sort(key=lambda s: (s["price"], s["name"]))
    data = {
        "date": max(s["date"] for s in state),
        "avg": sum(s["price"] for s in state) / len(state),
        "count": len(state),
        "cheapest": local[:TOP],
    }
    cache = paths(product)[0]
    tmp = cache + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, cache)
    return data


def touch(path):
    with open(path, "a"):
        pass
    os.utime(path)


def refresh(product):
    """Fetch now under the lock. If a refresh is already running this one is
    redundant and just leaves."""
    _, lock, attempt = paths(product)
    with open(lock, "w") as lk:
        try:
            fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return 0
        touch(attempt)
        try:
            fetch(product, 30)
        except Exception as exc:
            print(f"refresh failed: {exc}", file=sys.stderr)
            return 1
    return 0


def refreshing(product):
    """True while a --refresh holds this product's lock."""
    try:
        with open(paths(product)[1], "a") as lk:
            fcntl.flock(lk, fcntl.LOCK_SH | fcntl.LOCK_NB)
        return False
    except OSError:
        return True


def cycle(step):
    """Select the next (+1) or previous (-1) product. The render picks it up
    on its next tick and starts a fetch itself if the cache needs one."""
    order = list(PRODUCTS)
    p = order[(order.index(selected()) + step) % len(order)]
    tmp = CHOICE + ".tmp"
    with open(tmp, "w") as f:
        f.write(p)
    os.replace(tmp, CHOICE)
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


# A suburb next to one of these is part of a longer place or road name, not
# a label for the site, and must stay: "East Perth" in Perth, "Serpentine
# Falls" in Serpentine, "Malaga Drive" in Malaga, "Thornlie Square".
BEFORE = r"East|West|North|South|Upper|Lower|Mount|Mt|Port|Lake"
AFTER = (
    r"Falls|Vale|Valley|Hill|Hills|Heights|Park|Beach|Bay|Lake|Lakes|River|"
    r"Waters|Brook|Creek|Airport|Junction|Grove|Gardens|Square|Road|Rd|"
    r"Drive|Dr|Street|St|Avenue|Ave|Highway|Hwy"
)


def unplace(name, suburb):
    """Drop the suburb from the station's name when it repeats it, since the
    suburb has its own column: "Burk Alkimos" in Alkimos -> "Burk". Whole
    words only, any case, and not where it is part of a longer place name
    (see BEFORE/AFTER). A name that is nothing BUT the suburb is kept as is
    rather than left blank."""
    import re

    if not suburb:
        return name
    # Lookbehinds must be fixed width, so the BEFORE check is done per match.
    def drop(m):
        head = name[: m.start()].split()
        if head and re.fullmatch(BEFORE, head[-1], re.I):
            return m.group(0)
        return " "

    pat = rf"(?<!\w){re.escape(suburb)}(?!\w)(?! (?:{AFTER})\b)"
    out = re.sub(pat, drop, name, flags=re.I)
    out = re.sub(r"\(\s*\)", " ", out)  # "Crystal Brook (Lesmurdie)"
    out = re.sub(r"\s+", " ", out).strip(" -\u2013,/")
    return out or name


def fit(name, suburb):
    """Truncate the name so name + suburb share one row without colliding."""
    room = int((TEXT_W - GUTTER - len(suburb) * CH8) / CH9)
    if len(name) > room:
        name = name[: max(room - 1, 1)].rstrip() + "…"
    return name


def header(product, right=""):
    return (
        f"${{color1}}${{font {FONT}:size=10}}{GLYPH}${{font}}  "
        f"${{color5}}${{font {FONT}:size=8}}FUEL · {PRODUCTS[product]}"
        f"${{alignr}}{right}${{font}}"
    )


def main():
    os.makedirs(CACHE_DIR, exist_ok=True)

    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if arg == "--refresh":
        return refresh(sys.argv[2] if len(sys.argv) > 2 else selected())
    if arg == "--cycle":
        return cycle(int(sys.argv[2]) if len(sys.argv) > 2 else 1)

    product = selected()
    cache, _, attempt = paths(product)
    today = dt.date.today().isoformat()
    data = None
    try:
        with open(cache) as f:
            data = json.load(f)
    except Exception:
        pass

    stale = data is None or data.get("date") != today or since(cache) >= TTL
    last = since(attempt)
    if stale and (last is None or last >= RETRY) and not refreshing(product):
        # Detached, even with nothing cached: this runs every second and a
        # click must never freeze the card on a fetch. Output must be
        # discarded or execpi waits on us.
        subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "--refresh", product],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    if data is None:
        print(header(product))
        msg = "fetching" if refreshing(product) or last is None else "no data yet"
        print(f"${{voffset 2}}${{color5}}{msg} from FuelWatch")
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

    print(header(product, stamp))
    print(
        f"${{voffset 2}}${{color3}}${{font {FONT}:bold:size=15}}"
        f"{data['avg']:.1f}¢/L${{font}}"
        f"${{color5}}${{offset 8}}state avg"
        f"${{alignr}}{data['count']} sites"
    )
    # Conky ignores mid-text ${voffset} when it sizes the window, so every
    # pixel of it above is taken out of the bottom padding. A ${voffset} at
    # the END of the last line is counted, so the sum is paid back there --
    # less 2 px, which is what it takes for the bottom margin to measure the
    # same as the top (19 px from the window edge to the ink, both ways).
    spent = 2
    rows = data.get("cheapest", [])
    for i, s in enumerate(rows):
        suburb = safe(s["suburb"]).title()
        name = fit(unplace(safe(s["name"]), suburb), suburb)
        gap = 6 if i == 0 else 1
        spent += gap
        tail = f"${{voffset {spent - 2}}}" if i == len(rows) - 1 else ""
        print(
            f"${{voffset {gap}}}${{color6}}{s['price']:.1f}"
            f"${{goto {NAME_X}}}${{color}}{name}"
            f"${{alignr}}${{color5}}${{font {FONT}:size=8}}{suburb}${{font}}{tail}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
