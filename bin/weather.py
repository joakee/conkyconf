#!/usr/bin/env python3
"""Bureau of Meteorology -> conky markup for the gruvbox-dark weather widget.

Two public BOM products, both over HTTPS and both carrying the Bureau's
standard copyright terms (http://www.bom.gov.au/other/copyright.shtml):

  observations  /fwo/IDW60801/IDW60801.94608.json
                Perth Metro AWS, station 009225 -- an actual thermometer about
                4 km from the CBD, reporting every ~10 minutes.
  forecast      /fwo/IDW12300.xml
                Perth metropolitan precis, used only for the condition
                descriptor: the AWS reports no present-weather field.

Deliberately NOT api.weather.bom.gov.au. That endpoint is the undocumented
backend for the Bureau's own website and every response it returns says
"You must not use, copy or share it". The two products above are the
publicly published equivalents and carry no such restriction.

Sun times are computed locally with the NOAA solar position algorithm. They
are pure astronomy -- latitude, longitude and the date -- so they need no
network. That is what made leaving wttr.in possible: bundling twilight times
with the weather was the only thing it did that BOM does not.

Prints the widget body on stdout for ${execpi}. Never blocks on the network:
the cache is always rendered immediately and a refresh is forked into the
background once it goes stale, so a slow or unreachable BOM shows the last
good reading rather than freezing the conky process.

Env:
  WEATHER_TTL      seconds before a refresh is triggered (default 900)
  WEATHER_WMO      station WMO id (default 94608, Perth Metro)
  WEATHER_OBS_ID   observations product (default IDW60801, Western Australia)
  WEATHER_FCST_ID  precis forecast product (default IDW12300, Perth metro)
  WEATHER_AREA     forecast area within that product (default Perth)
  WEATHER_LAT      override the latitude used for the sun times
  WEATHER_LON      override the longitude used for the sun times
"""

import datetime as dt
import fcntl
import gzip
import json
import math
import os
import re
import subprocess
import sys
import urllib.request
import xml.etree.ElementTree as ET

TTL = int(os.environ.get("WEATHER_TTL", "900"))
WMO = os.environ.get("WEATHER_WMO", "94608")
OBS_ID = os.environ.get("WEATHER_OBS_ID", "IDW60801")
FCST_ID = os.environ.get("WEATHER_FCST_ID", "IDW12300")
AREA = os.environ.get("WEATHER_AREA", "Perth")

OBS_URL = f"https://www.bom.gov.au/fwo/{OBS_ID}/{OBS_ID}.{WMO}.json"
FCST_URL = f"https://www.bom.gov.au/fwo/{FCST_ID}.xml"
UA = "conky-gruvbox-weather/1.0 (personal desktop widget)"

CACHE_DIR = os.path.join(
    os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")),
    "conky-gruvbox",
)
CACHE = os.path.join(CACHE_DIR, "weather.json")
LOCK = os.path.join(CACHE_DIR, "weather.lock")

FONT = "Maple Mono NF CN"

# Material Design Icons weather glyphs, by BOM forecast_icon_code. A tuple is
# (day, night). BOM's codes are a small fixed enum, which is why this can be a
# lookup instead of the substring matching the wttr.in condition text needed.
GLYPH = {
    1:  "\U000F0599",                          # sunny
    2:  "\U000F0594",                          # clear (night)
    3:  ("\U000F0595", "\U000F0F31"),          # partly cloudy
    4:  "\U000F0590",                          # cloudy
    6:  "\U000F0591",                          # haze
    8:  "\U000F0597",                          # light rain
    9:  "\U000F059D",                          # windy
    10: "\U000F0591",                          # fog
    11: "\U000F0597",                          # shower
    12: "\U000F0596",                          # rain
    13: "\U000F0591",                          # dust
    14: "\U000F0598",                          # frost
    15: "\U000F0598",                          # snow
    16: "\U000F0593",                          # storm
    17: "\U000F0597",                          # light shower
    18: "\U000F0596",                          # heavy shower
    19: "\U000F0898",                          # tropical cyclone
}
FALLBACK = ("\U000F0599", "\U000F0594")


def get(url, timeout):
    req = urllib.request.Request(
        url, headers={"User-Agent": UA, "Accept-Encoding": "gzip"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
        return raw


def fetch(timeout):
    """Pull both products and replace the cache atomically. Raises on failure,
    which leaves the previous good reading in place."""
    obs = json.loads(get(OBS_URL, timeout))["observations"]
    head, rec = obs["header"][0], obs["data"][0]
    if rec.get("air_temp") is None:
        raise ValueError("observation has no air_temp")

    # The forecast is a nice-to-have; a missing condition should not throw away
    # a perfectly good temperature reading.
    icon, precis = None, ""
    temp_min, temp_max = None, None
    uv_index, uv_category = None, None
    try:
        root = ET.fromstring(get(FCST_URL, timeout))
        for area in root.iter("area"):
            if area.get("description") != AREA:
                continue
            if area.get("type") == "location":
                period = next(iter(area), None)
                if period is not None:
                    for el in period:
                        if el.get("type") == "forecast_icon_code":
                            icon = int(el.text)
                        elif el.get("type") == "precis":
                            precis = (el.text or "").strip().rstrip(".")
                # Whichever half-day has already happened is missing its
                # min/max (there is no "yesterday's high" to report late at
                # night), so take the first period that has both -- today's
                # while its max is still ahead of it, tomorrow's once it is
                # not.
                for p in area:
                    pmin = pmax = None
                    for el in p:
                        if el.get("type") == "air_temperature_minimum":
                            pmin = float(el.text)
                        elif el.get("type") == "air_temperature_maximum":
                            pmax = float(el.text)
                    if pmin is not None and pmax is not None:
                        temp_min, temp_max = pmin, pmax
                        break
            elif area.get("type") == "metropolitan":
                # The UV alert lives in this summary area, not the location
                # one, and drops off the same way once today's peak UV time
                # has passed.
                for p in area:
                    for el in p:
                        if el.get("type") != "uv_alert" or not el.text:
                            continue
                        m = re.search(r"reach (\d+)\s*\[([^\]]+)\]", el.text)
                        if m:
                            uv_index = int(m.group(1))
                            uv_category = m.group(2)
                    if uv_index is not None:
                        break
    except Exception:
        pass

    data = {
        "loc": ", ".join(x for x in (rec.get("name"), head.get("state")) if x),
        "temp": rec["air_temp"],
        "feels": rec.get("apparent_t"),
        "humidity": rec.get("rel_hum"),
        "wind_spd": rec.get("wind_spd_kmh"),
        "temp_min": temp_min,
        "temp_max": temp_max,
        "uv_index": uv_index,
        "uv_category": uv_category,
        "cond": precis,
        "icon": icon,
        "obs_local": rec.get("local_date_time_full", ""),
        "lat": rec.get("lat"),
        "lon": rec.get("lon"),
        "station": (rec.get("name") or "") + " " + str(rec.get("wmo") or ""),
    }
    tmp = CACHE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, CACHE)
    return data


def solar(lat, lon, tz_offset_hours, date):
    """NOAA solar position. Returns local clock minutes for dawn, sunrise,
    solar noon, sunset and dusk. Twilight is the civil definition (-6 deg)."""
    jd = date.toordinal() + 1721424.5
    t = (jd - 2451545.0) / 36525.0
    L0 = (280.46646 + t * (36000.76983 + t * 0.0003032)) % 360
    M = 357.52911 + t * (35999.05029 - 0.0001537 * t)
    e = 0.016708634 - t * (0.000042037 + 0.0000001267 * t)
    Mr = math.radians(M)
    C = (
        math.sin(Mr) * (1.914602 - t * (0.004817 + 0.000014 * t))
        + math.sin(2 * Mr) * (0.019993 - 0.000101 * t)
        + math.sin(3 * Mr) * 0.000289
    )
    omega = math.radians(125.04 - 1934.136 * t)
    lam = L0 + C - 0.00569 - 0.00478 * math.sin(omega)
    eps = 23 + (26 + (21.448 - t * (46.815 + t * (0.00059 - t * 0.001813))) / 60) / 60
    eps += 0.00256 * math.cos(omega)
    decl = math.degrees(
        math.asin(math.sin(math.radians(eps)) * math.sin(math.radians(lam)))
    )
    y = math.tan(math.radians(eps / 2)) ** 2
    L0r = math.radians(L0)
    eot = 4 * math.degrees(
        y * math.sin(2 * L0r)
        - 2 * e * math.sin(Mr)
        + 4 * e * y * math.sin(Mr) * math.cos(2 * L0r)
        - 0.5 * y * y * math.sin(4 * L0r)
        - 1.25 * e * e * math.sin(2 * Mr)
    )
    noon = 720 - 4 * lon - eot + tz_offset_hours * 60

    def ha(alt):
        la, de = math.radians(lat), math.radians(decl)
        c = (math.cos(math.radians(90 - alt)) - math.sin(la) * math.sin(de)) / (
            math.cos(la) * math.cos(de)
        )
        return math.degrees(math.acos(max(-1.0, min(1.0, c))))

    return {
        "dawn": noon - 4 * ha(-6),
        "sunrise": noon - 4 * ha(-0.833),
        "zenith": noon,
        "sunset": noon + 4 * ha(-0.833),
        "dusk": noon + 4 * ha(-6),
    }


def hm(minutes):
    m = int(round(minutes)) % 1440
    return f"{m // 60:02d}:{m % 60:02d}"


def safe(s):
    """conky re-parses execpi output, so strip anything that could look like
    markup before it reaches the terminal."""
    return "".join(c for c in str(s) if c not in "${}").strip()


def header(glyph, right=""):
    return (
        f"${{color1}}${{font {FONT}:size=10}}{glyph}${{font}}  "
        f"${{color5}}${{font {FONT}:size=8}}WEATHER"
        f"${{alignr}}{right}${{font}}"
    )


def main():
    os.makedirs(CACHE_DIR, exist_ok=True)

    if len(sys.argv) > 1 and sys.argv[1] == "--refresh":
        # Manual refresh: ignore the TTL, fetch synchronously, and report
        # success through the exit status. The lock keeps it from racing a
        # background refresh already in flight.
        with open(LOCK, "w") as lk:
            try:
                fcntl.flock(lk, fcntl.LOCK_EX)
                fetch(20)
            except Exception as exc:
                print(f"refresh failed: {exc}", file=sys.stderr)
                return 1
        return 0

    age = None
    if os.path.exists(CACHE):
        age = dt.datetime.now().timestamp() - os.stat(CACHE).st_mtime

    data = None
    if age is None:
        # Nothing to show at all: one short synchronous fetch, so the widget is
        # populated on first paint rather than blank for a whole interval.
        try:
            data = fetch(8)
        except Exception:
            pass
    elif age >= TTL:
        # Detached refresh. Output must be discarded or execpi waits on us.
        subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "--refresh"],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    if data is None:
        try:
            with open(CACHE) as f:
                data = json.load(f)
        except Exception:
            print(header("\U000F0590"))
            print("${voffset 2}${color5}waiting for BOM")
            return 0

    now = dt.datetime.now().astimezone()
    lat = float(os.environ.get("WEATHER_LAT", data.get("lat") or -31.95))
    lon = float(os.environ.get("WEATHER_LON", data.get("lon") or 115.86))
    tz = now.utcoffset().total_seconds() / 3600.0
    sun = solar(lat, lon, tz, now.date())

    minutes = now.hour * 60 + now.minute
    day = sun["sunrise"] <= minutes < sun["sunset"]

    g = GLYPH.get(data.get("icon"), FALLBACK)
    if isinstance(g, tuple):
        g = g[0] if day else g[1]

    # The timestamp is the Bureau's observation time, not when we fetched it:
    # what matters is how old the reading is, not how old our copy is.
    stamp = ""
    ts = data.get("obs_local", "")
    if len(ts) >= 12:
        stamp = f"\U000F0450 {ts[8:10]}:{ts[10:12]}"

    temp = data.get("temp")
    feels = data.get("feels")
    cond = safe(data.get("cond") or "")
    loc = safe(data.get("loc") or "")
    humidity = data.get("humidity")
    tmin, tmax = data.get("temp_min"), data.get("temp_max")
    uv_index = data.get("uv_index")
    wind_spd = data.get("wind_spd")

    print(header(g, stamp))
    print(
        f"${{voffset 2}}${{color3}}${{font {FONT}:bold:size=15}}{temp:g}°C${{font}}"
        f"${{color5}}${{offset 8}}${{voffset -4}}"
        + (f"feels {feels:g}°C" if feels is not None else "")
        + "${voffset 4}"
    )
    print(f"${{voffset 2}}${{color2}}{cond}")
    print(f"${{color5}}${{font {FONT}:size=8}}{loc}${{font}}")
    # Two columns sharing each row: readings on the left (as many as BOM
    # gave us), sun times on the right (always all five). ${goto} places
    # both columns at fixed pixel offsets from the card's left edge, since
    # ${offset}/${alignr} only move relative to whatever came before and
    # can't hold a column steady when the left cell is sometimes absent.
    left_col = []
    if tmin is not None:
        left_col.append(("min", f"{tmin:g}°C"))
    if tmax is not None:
        left_col.append(("max", f"{tmax:g}°C"))
    if humidity is not None:
        left_col.append(("%rh", f"{humidity:g}%"))
    if uv_index is not None:
        left_col.append(("UV", f"{uv_index}"))
    if wind_spd is not None:
        left_col.append(("wind", f"{wind_spd:g} km/h"))

    right_col = [
        ("dawn", hm(sun["dawn"])),
        ("sunrise", hm(sun["sunrise"])),
        ("zenith", hm(sun["zenith"])),
        ("sunset", hm(sun["sunset"])),
        ("dusk", hm(sun["dusk"])),
    ]

    # ${goto} measures from the window's true left edge, not the inner
    # content box border_inner_margin carves out -- so it needs that margin
    # (widget-common.lua's M.PAD) added back in, or this column sits flush
    # against the frame while everything else respects the card's padding.
    PAD = 14
    LEFT_LABEL, LEFT_VAL, RIGHT_LABEL = PAD, PAD + 62, PAD + 168

    for i, (rlabel, rval) in enumerate(right_col):
        left = ""
        if i < len(left_col):
            llabel, lval = left_col[i]
            left = f"${{color5}}{llabel}${{goto {LEFT_VAL}}}${{color2}}{lval}"
        pre = "${voffset 4}" if i == 0 else ""
        print(
            f"{pre}${{goto {LEFT_LABEL}}}{left}"
            f"${{goto {RIGHT_LABEL}}}${{color5}}{rlabel}"
            f"${{alignr}}${{color2}}{rval}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
