#!/usr/bin/env python3
"""Battery for iPhones and iPads, read over USB *or* wi-fi via usbmux.

UPower already reports an iPhone's battery, but only while it is plugged in:
its iDevice backend discovers devices through udev (up-device-idevice.c calls
g_udev_client_query_by_subsystem and takes the UDID from ID_SERIAL_SHORT), and
a device on wi-fi produces no udev event and no sysfs node. upowerd also runs
as root via D-Bus activation, so it would not pick up the socket override
below even if discovery worked. Hence a separate reader.

The transport is Apple's own, not a workaround: a device with wi-fi sync on
advertises _apple-mobdev2._tcp over Bonjour, and the paired host opens a plain
TCP connection to lockdownd on port 62078. That is exactly what Finder does.
Stock usbmuxd 1.1.1 has no mDNS discovery at all, so netmuxd supplies it and
serves the result on its own socket -- see ~/.config/systemd/user/netmuxd.service.
Battery itself comes from the com.apple.mobile.battery lockdown domain, the
same one Xcode's Devices window and Apple Configurator read.

Two rounds of subprocess, for a reason: the identity of a device (its name and
class) never changes, so it is looked up once per UDID and cached for the life
of the login session, leaving one ideviceinfo call per device per refresh. Those
run concurrently, because a wi-fi device answers in ~0.5s against ~0.05s over
USB and the card must not pay that cost once per device in series.

Like airpods.py this is a refinement, not a dependency: every failure path
returns nothing and leaves the UPower view of the world alone.
"""

import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

CACHE_DIR = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
IDENTITY = os.path.join(CACHE_DIR, "conky-gruvbox", "idevices.json")

# conky is started from an XFCE autostart entry, which does not necessarily
# inherit the systemd user environment, so ~/.config/environment.d cannot be
# relied on here. Point at netmuxd explicitly when nothing else has.
os.environ.setdefault("USBMUXD_SOCKET_ADDRESS",
                      f"UNIX:{CACHE_DIR}/netmuxd.sock")

# A wi-fi device that has dropped off the network answers by not answering.
# netmuxd only lists devices it holds a live connection to, so that should not
# happen, but the card refreshes every 5s and must never be the thing that
# blocks -- so the budget stays well under one refresh. A device on wi-fi
# normally replies in ~0.5s, which leaves this a comfortable 4x headroom, and
# because the calls run concurrently one stalled device costs this once rather
# than once per device.
TIMEOUT = 2

# Both codepoints are already in the verified GLYPH table in batteries.py, i.e.
# they were checked by rendering in Maple Mono NF CN rather than by name. Do
# not add more without doing the same; this font's Material Design block is old
# enough that plenty of documented codepoints draw something unrelated.
PHONE_GLYPH  = "\U000F011C"   # cellphone
TABLET_GLYPH = "\U000F04F6"   # tablet
UNKNOWN      = "\U000F0091"   # battery-unknown

# DeviceClass -> (glyph, UPower DeviceKind used for ordering in batteries.py).
CLASS = {
    "iPhone": (PHONE_GLYPH,  8),
    "iPod":   (PHONE_GLYPH,  9),
    "iPad":   (TABLET_GLYPH, 10),
    "Watch":  (PHONE_GLYPH,  8),
}

# "James’ iPhone 16 Pro" -> "iPhone 16 Pro". Apple builds the default name from
# the account holder's, and it is the same owner for every device on this card,
# so the possessive is pure width. Note the apostrophe is usually U+2019 and a
# name already ending in s takes a bare apostrophe, which is why this is a
# regex and not the "'s " split shorten() does in batteries.py.
POSSESSIVE = re.compile(r"^\S+[’']s?\s+")


def run(args):
    """stdout of a libimobiledevice call, or None if it failed or hung."""
    try:
        out = subprocess.run(args, capture_output=True, text=True,
                             timeout=TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout if out.returncode == 0 else None


def udids():
    """Every reachable device, as (udid, network?) pairs.

    -l and -n are disjoint here: without --network, libimobiledevice looks only
    at USB. A device on both -- plugged in while wi-fi sync is on -- is taken
    over USB, which is both faster and not dependent on netmuxd."""
    found = {}
    for flag, network in (("-n", True), ("-l", False)):
        out = run(["idevice_id", flag])
        for line in (out or "").split():
            found[line.strip()] = network        # -l runs last and wins
    return sorted(found.items())


def lockdown(udid, network, domain=None):
    """One lockdown domain as a dict, or None. Empty values are dropped so a
    caller can rely on a present key meaning a real reading."""
    args = ["ideviceinfo", "-u", udid]
    if network:
        args.append("-n")
    if domain:
        args += ["-q", domain]
    out = run(args)
    if out is None:
        return None
    d = {}
    for line in out.splitlines():
        k, sep, v = line.partition(":")
        if sep and v.strip():
            d[k.strip()] = v.strip()
    return d


def load_identity():
    try:
        with open(IDENTITY) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_identity(ident):
    try:
        os.makedirs(os.path.dirname(IDENTITY), exist_ok=True)
        tmp = IDENTITY + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(ident, fh)
        os.replace(tmp, IDENTITY)
    except OSError:
        pass                  # a cold identity lookup next time is survivable


def identify(udid, network, ident):
    """Name and class for a device, looked up at most once per session."""
    if udid in ident:
        return ident[udid]
    info = lockdown(udid, network)
    if not info or "DeviceName" not in info:
        return None
    ident[udid] = {"name": info["DeviceName"],
                   "class": info.get("DeviceClass", "")}
    return ident[udid]


def battery(udid, network):
    return lockdown(udid, network, "com.apple.mobile.battery")


def read():
    """One dict per reachable iOS device, shaped like batteries.py's own rows."""
    devices = udids()
    if not devices:
        return []

    ident = load_identity()
    known = len(ident)

    with ThreadPoolExecutor(max_workers=max(1, len(devices))) as pool:
        infos = list(pool.map(lambda d: identify(d[0], d[1], ident), devices))
        cells = list(pool.map(lambda d: battery(d[0], d[1]), devices))

    if len(ident) != known:
        save_identity(ident)

    out = []
    for (udid, network), info, cell in zip(devices, infos, cells):
        if not info or not cell:
            continue
        try:
            pct = round(float(cell["BatteryCurrentCapacity"]))
        except (KeyError, TypeError, ValueError):
            continue

        charging = cell.get("BatteryIsCharging") == "true"
        external = cell.get("ExternalConnected") == "true"
        topped   = cell.get("FullyCharged") == "true" and external
        glyph, kind = CLASS.get(info["class"], (UNKNOWN, 8))

        out.append({
            "name":  POSSESSIVE.sub("", " ".join(info["name"].split())) or "iOS",
            "glyph": glyph,
            "pct":   pct,
            # A real per-1% figure, so it is flagged exact the way UPower's
            # BatteryLevel 1 ("none", i.e. no coarse bucket) is.
            "coarse":   False,
            "level":    1,
            # UPower calls a topped-up device charging-and-full (State 4), and
            # lua/batteries.lua badges that green. Match it, or a device left
            # on the charger overnight would lose its badge.
            "charging": charging or topped,
            "full":     topped,
            # Which transport this reading came in over. lua/batteries.lua
            # marks the network ones, so an unplugged phone is visibly still
            # being read rather than looking like a ring nobody updated.
            "network":  network,
            "_kind":    kind,
            "_model":   info["name"],
        })
    return out


if __name__ == "__main__":
    for d in read():
        print(f"  {d['glyph']}  {d['name']:<18} {d['pct']:>3}%"
              f"{' (charging)' if d['charging'] else ''}")
    sys.exit(0)
