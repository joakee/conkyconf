#!/usr/bin/env python3
"""Per-bud and case battery for AirPods, read from podctld.

Why this exists: what BlueZ publishes as org.bluez.Battery1 for AirPods is a
single coarse number and, on this setup, an implausible flat 100%. The real
per-1% figures for the left bud, the right bud and the case live on Apple's
AAP control channel (L2CAP PSM 0x1001), and podctld -- the AirPods daemon from
~/Projects/podctl -- already holds that channel open permanently for its own
case-open popup.

That channel serves ONE client, so the only workable arrangement is to let the
daemon own it and ask the daemon. `podctl status --json` is a ~1 ms round trip
over a unix socket to a process that already has the numbers, the device name
and the address, which is cheap enough to take on every conky tick.

History, because the shape of this file still shows it: it used to decode
Apple's encrypted BLE proximity-pairing advertisement itself, using the IRK and
AES key LibrePods had negotiated and left in its config, and to open its own
AAP socket when it could. Both fought podctld for the single AAP session, and
both failed silently -- the card quietly served BlueZ's flat 100% instead. With
LibrePods retired here, nothing in this module touches Bluetooth, the radio or
LibrePods' config any more. The last version that did is kept in
attic/airpods.py.pre-podctl.
"""

import json
import os
import shutil
import subprocess
import sys
import time

CACHE_DIR = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"

# podctl installs into ~/.local/bin, which the session PATH conky inherits does
# not always carry -- hence the fallback in podctl_read().
PODCTL         = os.environ.get("AIRPODS_PODCTL", "podctl")
PODCTL_TIMEOUT = int(os.environ.get("AIRPODS_PODCTL_TIMEOUT", "3"))

# How long the last reading keeps being shown after podctld stops answering.
# This now covers one thing only -- the daemon being restarted or briefly wedged
# -- so it is short. A daemon that answers and says the AirPods are disconnected
# is not covered by it at all: that is a real answer, and the card drops them.
HOLD    = int(os.environ.get("AIRPODS_HOLD", "120"))
READING = os.path.join(CACHE_DIR, "conky-gruvbox", "airpods.json")

# Checked by rendering, not by name -- see the note on GLYPH in batteries.py.
# The obvious "earbuds" codepoint draws an aerial in this font.
GLYPH_BUD  = "\U000F02CB"   # headphones
GLYPH_CASE = "\U000F02CC"   # headphones in a box, which reads as the case

# Told apart from None, because the two want opposite treatment: a daemon that
# did not answer should leave the last reading on the card, while a daemon that
# says the buds are gone should take the entry away at once.
DISCONNECTED = object()


# ── the daemon ────────────────────────────────────────────────
def podctl_read():
    """podctld's view: (components, device name, address), DISCONNECTED, or None.

    The components mapping is {component: {pct, charging}}, the shape build()
    consumes. None means podctld could not be reached at all -- not installed,
    not running, or too slow -- and DISCONNECTED means it answered and the
    AirPods are not connected.

    Never raises: a missing CLI, a dead daemon and malformed JSON are all just
    one of those two answers."""
    exe = shutil.which(PODCTL)
    if exe is None and os.sep not in PODCTL:
        exe = os.path.expanduser("~/.local/bin/" + PODCTL)
    if not exe or not os.path.exists(exe):
        return None
    try:
        out = subprocess.run([exe, "status", "--json"], capture_output=True,
                             text=True, timeout=PODCTL_TIMEOUT)
        state = json.loads(out.stdout)["data"]
    except Exception:
        return None
    if not state.get("connected"):
        return DISCONNECTED

    # podctld's battery object is left / right / case; an AirPods Max would
    # report under a key that is not here yet.
    bat = state.get("battery") or {}
    comps = {}
    for name in ("left", "right", "case"):
        pct = bat.get(name)
        # null is a component the daemon has no reading for -- the case while
        # the lid is shut, or a bud that is not reporting.
        if isinstance(pct, int) and 1 <= pct <= 100:
            comps[name] = {"pct": pct,
                           "charging": bool(bat.get(name + "_charging"))}
    if not comps:
        # Connected, but nothing to show yet: the AAP link comes up a second or
        # two before the first battery notification lands.
        return None
    return comps, state.get("name", ""), state.get("address")


# ── the last good reading ─────────────────────────────────────
def _save_reading(r):
    try:
        os.makedirs(os.path.dirname(READING), exist_ok=True)
        tmp = READING + ".tmp"
        with open(tmp, "w") as fh:
            json.dump({**r, "ts": time.time()}, fh)
        os.replace(tmp, READING)
    except OSError:
        pass


def _load_reading():
    try:
        with open(READING) as fh:
            r = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(r, dict) or not r.get("components"):
        return None
    if time.time() - float(r.get("ts", 0)) > HOLD:
        return None
    return r


def _forget_reading():
    """Drop the held reading. Called when podctld says the AirPods are gone, so
    that putting them away clears the card instead of leaving a stale ring
    behind for HOLD seconds."""
    try:
        os.unlink(READING)
    except OSError:
        pass


# ── public entry point ────────────────────────────────────────
def short_name(name):
    """"James's AirPods Pro" -> "AirPods Pro": the owner prefix is the same on
    every device here and only costs label width."""
    base = name or "AirPods"
    for sep in ("’s ", "'s "):
        if sep in base:
            return base.split(sep, 1)[1]
    return base


def build(d, name, addr):
    """Components mapping -> the card entries bin/batteries.py expects."""
    base = short_name(name)
    parts = []

    # The two buds are ONE entry carrying both readings: pct_l / pct_r make the
    # drawer split the ring in half rather than draw a second card. `pct` is
    # the lower of the two, which is what the ring is coloured by and what
    # matters when you are deciding whether to charge them.
    buds = [c for c in (d.get("left"), d.get("right")) if c]
    if buds:
        parts.append({
            "name":     base,
            "glyph":    GLYPH_BUD,
            "pct":      min(c["pct"] for c in buds),
            "pct_l":    d["left"]["pct"] if d.get("left") else None,
            "pct_r":    d["right"]["pct"] if d.get("right") else None,
            "coarse":   False,
            "level":    1,
            "charging": any(c["charging"] for c in buds),
            "full":     False,
        })
    if d.get("case"):
        parts.append({"name": "Case", "glyph": GLYPH_CASE,
                      "pct": d["case"]["pct"], "coarse": False, "level": 1,
                      "charging": d["case"]["charging"], "full": False})
    if not parts:
        return None
    return {"device_name": name, "address": addr, "via": "podctl",
            "components": parts}


def read(cache=True):
    """Battery components for the AirPods, or None if there is nothing current.

    Returns a list of dicts shaped like bin/batteries.py's own device entries,
    plus `device_name` so the caller can drop UPower's duplicate of the same
    headphones.

    When podctld cannot be reached the last reading is served for up to HOLD
    seconds (unless `cache` is off), which is what keeps the card steady across
    a daemon restart."""
    got = podctl_read()
    if got is DISCONNECTED:
        _forget_reading()
        return None
    if got is None:
        return _load_reading() if cache else None

    comps, name, addr = got
    out = build(comps, name, addr)
    if out is None:
        return _load_reading() if cache else None
    _save_reading(out)
    return out


def diagnose():
    """Why is there no reading? Three causes, three different fixes."""
    exe = shutil.which(PODCTL)
    if exe is None and os.sep not in PODCTL:
        exe = os.path.expanduser("~/.local/bin/" + PODCTL)
    if not exe or not os.path.exists(exe):
        return (f"podctl is not installed where this can find it (tried "
                f"{PODCTL!r} on PATH, then ~/.local/bin). Install podctl, or "
                f"point AIRPODS_PODCTL at it.")
    try:
        out = subprocess.run([exe, "status", "--json"], capture_output=True,
                             text=True, timeout=PODCTL_TIMEOUT)
    except Exception as e:
        return f"running {exe} failed ({e})."
    try:
        state = json.loads(out.stdout)["data"]
    except Exception:
        err = (out.stderr or out.stdout or "").strip().splitlines()
        return ("podctld is not answering -- check "
                "`systemctl --user status podctld`"
                + (f" ({err[0]})" if err else "") + ".")
    if not state.get("connected"):
        return ("podctld is up but the AirPods are not connected, so it has "
                "no battery to report.")
    return ("podctld is up and says the AirPods are connected, but no "
            "component reported a usable level yet -- the AAP link takes a "
            "second or two after connecting to send the first battery frame.")


def main():
    try:
        r = read()
    except Exception as e:
        print(f"unavailable: {e}", file=sys.stderr)
        return 1
    if r is None:
        print("no reading: " + diagnose(), file=sys.stderr)
        return 1
    age = time.time() - float(r.get("ts", time.time()))
    when = f"  ({int(age)}s old, held)" if age > 5 else ""
    print(f"{r['device_name']}  via {r.get('via', '?').upper()} "
          f"{r['address']}{when}")
    for c in r["components"]:
        if c.get("pct_l") is not None or c.get("pct_r") is not None:
            detail = "  L %s  R %s" % (c.get("pct_l") or "--", c.get("pct_r") or "--")
        else:
            detail = ""
        print(f"  {c['glyph']}  {c['name']:<16} {c['pct']:>3}%"
              + ("  (charging)" if c["charging"] else "") + detail)
    return 0


if __name__ == "__main__":
    sys.exit(main())
