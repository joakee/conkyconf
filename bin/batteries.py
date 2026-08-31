#!/usr/bin/env python3
"""Enumerate every battery UPower knows about, for the conky batteries card.

UPower is the same source xfce4-power-manager reads, so whatever shows up in
its Devices tab shows up here: the laptop pack, HID++ peripherals behind a
Logitech receiver, and Bluetooth devices that publish org.bluez.Battery1.

Two device classes are read elsewhere and merged in, because UPower's own
answer for them is wrong or absent. Both modules are optional -- if either
raises or has nothing current, UPower's view is used unchanged.

  AirPods    what BlueZ publishes is one coarse number, so airpods.py decodes
             Apple's BLE advertisement instead and returns the left bud, right
             bud and case separately; those replace UPower's single entry for
             the same headphones.
  iOS        UPower only sees an iPhone or iPad while it is plugged in, since
             its iDevice backend discovers devices through udev. idevices.py
             goes through usbmux instead, which reaches them over wi-fi too,
             and its entry replaces UPower's whenever both have the device.

Two outputs, both produced in one run:

  * a Lua table literal in $XDG_RUNTIME_DIR that lua/batteries.lua pulls in
    with dofile() to draw the rings. Lua cannot afford to block the draw hook
    on D-Bus round trips, and emitting the consumer's own syntax saves
    shipping a JSON parser written in Lua.
  * conky markup on stdout: the card header, plus a ${voffset} tall enough to
    reserve the ring block. That is what makes the card grow and shrink with
    the number of devices -- conky sizes the window to its text, so reserving
    the space as text is what moves the window edge, and picom rounds
    whatever rectangle results.

Talking to UPower goes through busctl rather than a D-Bus binding so the
script stays stdlib-only, matching bin/weather.py.
"""

import json
import os
import subprocess
import sys

# ── layout, in pixels ─────────────────────────────────────────
# Mirrored by lua/batteries.lua; the two must agree or the rings will not sit
# inside the space reserved for them here. WIDTH matches widget-common.lua.
WIDTH    = 292
COLS     = 3
RING_D   = 52     # ring outer diameter
LABEL_H  = 26     # percentage + device name under each ring
ROW_GAP  = 10
ROW_H    = RING_D + LABEL_H
HEADER_H = 6      # padding under conky's own header line, before the rings
# conky charges a full line height for the line the ${voffset} itself sits on,
# so the reservation has to give that back or the card runs tall. Measured on
# this setup with the default size 9 face.
LINE_H   = 19

BUS   = ["busctl", "--system", "--json=short", "call", "org.freedesktop.UPower"]
IFACE = "org.freedesktop.UPower.Device"
CACHE_DIR = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
CACHE = os.path.join(CACHE_DIR, "conky-gruvbox", "batteries.lua")

# UPower DeviceKind -> Nerd Font Material Design glyph.
#
# Every codepoint below was checked by RENDERING it in Maple Mono NF CN, not by
# looking up its name. This font carries an older Material Design Icons block
# than the current upstream table, so plenty of documented codepoints draw
# something unrelated here: F0B3B ("watch") is the digit 2, F0CB8 ("touchpad")
# is a playlist icon, F1041 ("stylus") is a tripod, F17CE ("earbuds") is an
# aerial. Kinds with no glyph that survived that check fall through to
# UNKNOWN_GLYPH rather than being given a wrong picture.
GLYPH = {
    3:  "\U000F0079",  # ups            -> battery
    5:  "\U000F037D",  # mouse
    6:  "\U000F030C",  # keyboard
    8:  "\U000F011C",  # phone          -> cellphone
    9:  "\U000F011C",  # media player
    10: "\U000F04F6",  # tablet
    11: "\U000F0322",  # computer       -> laptop
    12: "\U000F02B4",  # gaming input   -> controller
    13: "\U000F03EB",  # pen            -> pencil
    14: "\U000F037D",  # touchpad       -> mouse (no touchpad glyph renders)
    17: "\U000F02CE",  # headset        (with boom mic, unlike headphones)
    18: "\U000F04C3",  # speakers
    19: "\U000F02CB",  # headphones
    21: "\U000F04C3",  # other audio
    23: "\U000F042A",  # printer
    28: "\U000F00AF",  # bluetooth generic
}
LAPTOP_GLYPH  = "\U000F0322"
UNKNOWN_GLYPH = "\U000F0091"  # battery-unknown

# Ordering: the machine you are sitting at first, then the things you hold,
# then the things you listen through, then whatever else turned up.
KIND_ORDER = {11: 0, 5: 1, 6: 2, 14: 3, 12: 4, 13: 5,
              17: 6, 19: 7, 18: 8, 21: 9, 8: 10, 10: 11}

# Vendor words worth dropping from a model string: they cost horizontal space
# and the glyph already says what the thing is.
VENDORS = ("Logitech", "Sony", "Apple", "Microsoft", "Jabra", "SteelSeries",
           "Bose", "Samsung", "Sennheiser", "Razer", "Corsair", "Dell", "HP",
           "Anker", "Soundcore", "JBL", "Beats", "Google", "Keychron")
# Trailing nouns the glyph already conveys.
SUFFIXES = (" Multi-Device Mouse", " Wireless Mouse", " Mouse", " Keyboard",
            " Headset", " Headphones", " Earbuds", " Speaker", " Bluetooth")


class Unreachable(Exception):
    """UPower could not be talked to at all -- distinct from it reporting no
    batteries. The two must not be confused: treating an outage as an empty
    result would overwrite a good cache and collapse the card."""


def prop_call(path):
    """Properties of one device, or None if that device has just gone away.

    A device vanishing between the enumeration and this call is routine --
    a mouse sleeping, headphones disconnecting -- so it is skipped rather
    than raised on."""
    out = subprocess.run(
        BUS + [path, "org.freedesktop.DBus.Properties", "GetAll", "s", IFACE],
        capture_output=True, text=True, timeout=5)
    if out.returncode != 0:
        return None
    return {k: v["data"] for k, v in json.loads(out.stdout)["data"][0].items()}


def enumerate_paths():
    out = subprocess.run(
        BUS + ["/org/freedesktop/UPower", "org.freedesktop.UPower",
               "EnumerateDevices"],
        capture_output=True, text=True, timeout=5)
    if out.returncode != 0:
        raise Unreachable(out.stderr.strip())
    return json.loads(out.stdout)["data"][0]


def shorten(name):
    name = " ".join(name.split())
    for v in VENDORS:
        if name.lower().startswith(v.lower() + " "):
            name = name[len(v) + 1:]
            break
    for s in SUFFIXES:
        if name.lower().endswith(s.lower()) and len(name) > len(s):
            name = name[:-len(s)]
            break
    # Possessive device names ("James's AirPods Pro") read better without the
    # owner, which is the same for every device here anyway.
    for sep in ("’s ", "'s "):
        if sep in name:
            name = name.split(sep, 1)[1]
            break
    return name.strip() or "Battery"


def collect():
    out = []
    paths = enumerate_paths()
    answered = 0
    for path in paths:
        if path.endswith("/DisplayDevice"):
            continue
        p = prop_call(path)
        if p is None:
            continue          # gone since the enumeration; see prop_call
        answered += 1
        if p.get("Type") == 1 or not p.get("IsPresent"):
            continue          # Type 1 is line power, not a battery

        kind = p.get("Type", 0)
        mains = bool(p.get("PowerSupply"))
        level = p.get("BatteryLevel", 0)
        # BatteryLevel 0 (unknown) / 1 (none) both mean "Percentage is real".
        # Anything else is a coarse bucket and UPower has synthesised a number
        # from it, so the reading gets flagged rather than shown as exact.
        coarse = level not in (0, 1)

        pct = p.get("Percentage")
        if pct is None or (pct <= 0 and coarse):
            continue

        name = shorten(p.get("Model") or p.get("NativePath") or "")
        if mains and kind in (0, 2):
            name, kind = "Laptop", 11   # UPower calls the internal pack "battery"

        out.append({
            "name":     name,
            "glyph":    LAPTOP_GLYPH if mains else GLYPH.get(kind, UNKNOWN_GLYPH),
            "pct":      round(float(pct)),
            "coarse":   coarse,
            "level":    level,
            "charging": p.get("State") in (1, 4, 5),
            "full":     p.get("State") == 4,
            "order":    (0 if mains else 1, KIND_ORDER.get(kind, 99), name),
            "_model":   p.get("Model") or "",
        })

    # Every single device refusing to answer is a bus that dropped out from
    # under us, not a machine that suddenly has no batteries.
    if paths and not answered:
        raise Unreachable("no device answered GetAll")

    out = swap_in_airpods(out)
    out = add_idevices(out)
    out.sort(key=lambda d: d["order"])
    for d in out:
        d.pop("order", None)
        d.pop("_model", None)
    return out


def swap_in_airpods(devices):
    """Replace UPower's single coarse AirPods entry with per-bud and case
    readings, when airpods.py can supply them. Anything going wrong in there
    leaves the UPower entry alone -- it is a refinement, not a dependency."""
    try:
        import airpods
        pods = airpods.read()
    except Exception:
        return devices
    if not pods:
        return devices

    devices = [d for d in devices if d["_model"] != pods["device_name"]]
    # Sorted in with the headphones they replace, and kept in L / R / case order.
    for i, c in enumerate(pods["components"]):
        c["order"] = (1, KIND_ORDER[19], "%d" % i)
        devices.append(c)
    return devices


def add_idevices(devices):
    """Merge in iPhones and iPads reached over usbmux -- which, with netmuxd
    supplying the mDNS discovery stock usbmuxd lacks, means over wi-fi as well
    as USB. See bin/idevices.py.

    UPower's entry for the same device is dropped rather than kept alongside:
    it appears only while the device is plugged in, so keeping both would make
    the phone show up twice at exactly the moment it is cabled."""
    try:
        import idevices
        found = idevices.read()
    except Exception:
        return devices
    if not found:
        return devices

    def key(name):
        return " ".join((name or "").split())

    # .get, not [], because swap_in_airpods has already run and its per-bud
    # rows are synthesised, not UPower's -- they carry no _model at all.
    seen = {key(d["_model"]) for d in found}
    devices = [d for d in devices if key(d.get("_model")) not in seen]
    for d in found:
        d["order"] = (1, KIND_ORDER.get(d.pop("_kind"), 99), d["name"])
        devices.append(d)
    return devices


def lua_str(s):
    """Quote a Python str as a Lua string. Lua strings are byte strings and the
    cache file is read as UTF-8, so the glyphs can go in literally."""
    out = s.replace("\\", "\\\\").replace('"', '\\"')
    return '"' + "".join(c if c >= " " else "\\%d" % ord(c) for c in out) + '"'


def lua_value(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    return lua_str(v)


def write_cache(devices):
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    rows = ",\n".join(
        "  {" + ", ".join(f"{k}={lua_value(v)}"
                          for k, v in d.items() if v is not None) + "}"
        for d in devices)
    tmp = CACHE + ".tmp"
    with open(tmp, "w") as fh:
        fh.write("return {\n" + rows + ("\n" if rows else "") + "}\n")
    os.replace(tmp, CACHE)


def show(devices):
    for d in devices:
        flag = "~" if d["coarse"] else " "
        chg = " (charging)" if d["charging"] else ""
        print(f"  {d['glyph']}  {d['name']:<18} {flag}{d['pct']:>3}%{chg}")


def main():
    if "--show" in sys.argv:
        show(collect())
        return 0

    try:
        devices = collect()
    except Exception:
        # A card that silently keeps its last good drawing beats one that
        # prints a traceback into the desktop.
        devices = None

    if devices is not None:
        try:
            write_cache(devices)
        except OSError:
            pass
        n = len(devices)
    else:
        # Enumeration failed; keep the last good cache and reserve the space it
        # still needs, so the card holds its size instead of collapsing.
        try:
            with open(CACHE) as fh:
                n = sum(1 for line in fh if line.startswith("  {"))
        except OSError:
            n = 0

    rows = max(1, -(-n // COLS))                      # ceil
    body = HEADER_H + rows * ROW_H + (rows - 1) * ROW_GAP if n else HEADER_H
    body = max(0, body - LINE_H)

    print("${color1}${font Maple Mono NF CN:size=10}\U000F0079${font}  "
          "${color5}${font Maple Mono NF CN:size=8}BATTERIES${font}")
    if not n:
        print("${voffset 2}${color5}no batteries reported")
    print("${voffset %d}" % body, end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
