#!/usr/bin/env python3
"""Per-bud and case battery for AirPods, decoded from Apple's BLE advertisement.

Why this exists: what BlueZ publishes as org.bluez.Battery1 for AirPods is a
single coarse number and, on this setup, an implausible flat 100%. Apple sends
the real figures in its proximity-pairing advertisement (manufacturer 0x004C,
type 0x07). The plaintext part of that advert carries battery only as 4-bit
nibbles -- 10% granularity -- but the last 16 bytes are AES-encrypted and hold
per-1% levels for the left bud, the right bud and the case.

The decoding here follows LibrePods (https://github.com/librepods-org/librepods,
linux/ble/blemanager.cpp and linux/battery.hpp), and it reuses the two keys
LibrePods has already negotiated and stored in its own config:

  magicAccIRK     resolves the rotating random address, i.e. tells us which of
                  the ~100 anonymous BLE advertisers is actually this user's
  magicAccEncKey  decrypts the 16-byte payload (AES-128, single block)

There are two sources, and the good one is tried first:

  AAP   the Apple Accessory Protocol control channel, L2CAP PSM 0x1001 on the
        already-connected link. Handshake, ask for notifications, and the
        AirPods report every component at 1% precision in about two seconds --
        no radio scanning, no waiting for the buds to feel like advertising.
        The catch is that the channel serves ONE host: if an iPhone nearby has
        Bluetooth on it holds the session and the channel opens but never
        answers, which is indistinguishable from a hang except by timeout.
  BLE   the proximity-pairing advertisement, decoded as described below. Only
        used when AAP is unavailable.

Reading is passive: BlueZ caches ManufacturerData for advertisers it has seen,
so when something else is already scanning -- LibrePods runs a continuous LE
scan -- the properties are simply read off the existing device object at no
radio cost at all.

That alone is not dependable, because LibrePods stops scanning and does not
resume (its onScanFinished restarts only `if (discoveryAgent->isActive())`,
which is false precisely when a scan has just finished). So when the cached
advert goes stale this module runs its OWN short scan, detached and rate
limited, and backs off when the scan keeps finding nothing -- which is the
normal state when the AirPods are away or shut in their case. The passive path
still costs nothing whenever anyone else is scanning.
"""

import fcntl
import json
import os
import re
import select
import socket
import subprocess
import sys
import time

CONF = os.path.expanduser("~/.config/AirPodsTrayApp/AirPodsTrayApp.conf")
CACHE_DIR = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
STATE = os.path.join(CACHE_DIR, "conky-gruvbox", "airpods.state")

# BlueZ keeps serving the last advertisement it saw long after the device has
# gone. Nothing on org.bluez.Device1 says when that was, so freshness is judged
# by the bytes themselves: the encrypted payload changes on every advert (they
# arrive every second or two), so a payload that has not moved in this long
# means nobody is hearing the AirPods any more.
STALE = int(os.environ.get("AIRPODS_STALE", "180"))

# How long a decoded reading keeps being shown after the advert it came from is
# gone. This is what makes intermittent scanning usable at all: BlueZ deletes
# the advertiser's device object soon after a scan stops, so without a held
# reading the card could only show the AirPods for the few seconds per minute
# that a scan happens to be running. It has to be generous, because AirPods
# advertise in bursts rather than continuously and a scan can easily miss
# several in a row. Earbuds discharge slowly enough that a ten-minute-old
# figure is still a useful one; well beyond that it stops being true.
HOLD = int(os.environ.get("AIRPODS_HOLD", "600"))
READING = os.path.join(CACHE_DIR, "conky-gruvbox", "airpods.json")

# Self-scanning. SCAN_SECS is how long one scan runs; SCAN_MIN is the shortest
# gap between scans, doubling up to SCAN_MAX for each consecutive scan that
# finds nothing. The backoff is what keeps this cheap: AirPods in a bag never
# advertise, and without it the radio would be scanning 1 second in 8 forever.
SCAN_SECS = int(os.environ.get("AIRPODS_SCAN_SECS", "15"))
SCAN_MIN  = int(os.environ.get("AIRPODS_SCAN_MIN", "120"))
SCAN_MAX  = int(os.environ.get("AIRPODS_SCAN_MAX", "900"))
# AAP costs no radio time, so when it works it can be polled far more often
# than a BLE scan; the socket is held for only the couple of seconds it takes.
AAP_PSM     = 0x1001
AAP_TIMEOUT = int(os.environ.get("AIRPODS_AAP_TIMEOUT", "8"))
AAP_MIN     = int(os.environ.get("AIRPODS_AAP_MIN", "45"))

SCAN_STATE = os.path.join(CACHE_DIR, "conky-gruvbox", "airpods.scan")
SCAN_LOCK  = os.path.join(CACHE_DIR, "conky-gruvbox", "airpods.scan.lock")

# Checked by rendering, not by name -- see the note on GLYPH in batteries.py.
# The obvious "earbuds" codepoint draws an aerial in this font.
GLYPH_BUD  = "\U000F02CB"   # headphones
GLYPH_CASE = "\U000F02CC"   # headphones in a box, which reads as the case


# ── AES-128, single block ─────────────────────────────────────
def _aes(key, block, encrypt):
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        c = Cipher(algorithms.AES(key), modes.ECB())
        op = c.encryptor() if encrypt else c.decryptor()
        return op.update(block) + op.finalize()
    except ImportError:
        pass
    # openssl is a certainty on this box even if python-cryptography is not.
    out = subprocess.run(
        ["openssl", "enc", "-aes-128-ecb", "-nopad",
         "-e" if encrypt else "-d", "-K", key.hex()],
        input=block, capture_output=True, timeout=5)
    if out.returncode != 0 or len(out.stdout) != 16:
        raise RuntimeError("openssl AES failed")
    return out.stdout


def _ah(irk, prand):
    """The Bluetooth Core ah() hash. Its inputs and outputs are little-endian
    while AES works big-endian, hence the reversals."""
    r = bytes(prand) + b"\x00" * 13
    return _aes(irk[::-1], r[::-1], True)[::-1][:3]


def resolves(addr, irk):
    """True if this resolvable private address was generated from this IRK."""
    try:
        b = bytes(int(x, 16) for x in addr.split(":"))[::-1]
    except ValueError:
        return False
    return len(b) == 6 and b[:3] == _ah(irk, b[3:6])


# ── LibrePods' config ─────────────────────────────────────────
def qt_unescape(s):
    """Undo QSettings' INI escaping. Note \\xHH is GREEDY over hex digits and is
    written WITHOUT zero padding, so '\\x6@' is the single byte 0x06 then '@'."""
    simple = {"a": 7, "b": 8, "f": 12, "n": 10, "r": 13, "t": 9, "v": 11,
              "0": 0, "\\": 92, '"': 34}
    out, i = bytearray(), 0
    while i < len(s):
        if s[i] != "\\":
            out += s[i].encode()
            i += 1
            continue
        i += 1
        if i >= len(s):
            break
        if s[i] == "x":
            i += 1
            j = i
            while j < len(s) and s[j] in "0123456789abcdefABCDEF":
                j += 1
            out.append(int(s[i:j], 16) & 0xFF)
            i = j
        elif s[i] in simple:
            out.append(simple[s[i]])
            i += 1
        else:
            out += s[i].encode()
            i += 1
    return bytes(out)


def load_conf():
    """The IRK, the encryption key and the device name LibrePods is managing."""
    conf = {}
    with open(CONF, encoding="utf-8", errors="surrogateescape") as fh:
        for line in fh:
            m = re.match(r'(magicAccEncKey|magicAccIRK)\s*=\s*"?@ByteArray\((.*)\)"?\s*$',
                         line.rstrip("\n"))
            if m:
                conf[m.group(1)] = qt_unescape(m.group(2))
                continue
            m = re.match(r"deviceName\s*=\s*(.+?)\s*$", line)
            if m:
                conf["deviceName"] = m.group(1).strip('"')
    if len(conf.get("magicAccIRK", b"")) != 16 or len(conf.get("magicAccEncKey", b"")) != 16:
        raise ValueError("LibrePods keys missing or not 16 bytes")
    return conf


# ── the advertisement ─────────────────────────────────────────
def bluez_objects():
    out = subprocess.run(
        ["busctl", "--system", "--json=short", "call", "org.bluez", "/",
         "org.freedesktop.DBus.ObjectManager", "GetManagedObjects"],
        capture_output=True, text=True, timeout=10)
    if out.returncode != 0:
        raise RuntimeError("cannot reach org.bluez")
    return json.loads(out.stdout)["data"][0]


def find_advert(irk):
    """The freshest 0x07 proximity-pairing advert from the device this IRK owns."""
    best = None
    for ifaces in bluez_objects().values():
        d = ifaces.get("org.bluez.Device1")
        if not d or d.get("AddressType", {}).get("data") != "random":
            continue
        addr = d.get("Address", {}).get("data", "")
        raw = bytes(d.get("ManufacturerData", {}).get("data", {})
                     .get("76", {}).get("data", []))
        # Cheap checks before the AES in resolves(): 27 bytes is the full
        # proximity-pairing message, and data[2] == 0 means it is still in
        # pairing mode, where the fields mean something else.
        if len(raw) < 27 or raw[0] != 0x07 or raw[2] == 0x00:
            continue
        if not resolves(addr, irk):
            continue
        rssi = d.get("RSSI", {}).get("data", -999)
        if best is None or rssi > best[2]:
            best = (addr, raw, rssi)
    return best


def _freshness(raw):
    """Seconds since the advertisement payload last changed. See STALE."""
    now = time.time()
    prev, prev_t = "", 0.0
    try:
        with open(STATE) as fh:
            prev, t = fh.read().split()
            prev_t = float(t)
    except (OSError, ValueError):
        pass
    if raw.hex() != prev:
        try:
            os.makedirs(os.path.dirname(STATE), exist_ok=True)
            tmp = STATE + ".tmp"
            with open(tmp, "w") as fh:
                fh.write(f"{raw.hex()} {now}")
            os.replace(tmp, STATE)
        except OSError:
            pass
        return 0.0
    return now - prev_t


def decode(raw, enc_key):
    """Levels for left bud, right bud and case, in percent.

    The plaintext status byte says which bud is 'primary', and that decides
    which of the two decrypted bytes is the left one. A component reporting 0
    or 0x7F has no reading -- 0x7F is what a bud sends when it is shut in the
    case, and LibrePods treats both as unknown."""
    status = raw[5]
    primary_left = bool(status & 0x20)
    dec = _aes(enc_key, raw[-16:], False)

    def one(byte):
        level = byte & 0x7F
        if level == 0 or level > 100:
            return None
        return {"pct": level, "charging": bool(byte & 0x80)}

    return {
        "left":  one(dec[1 if primary_left else 2]),
        "right": one(dec[2 if primary_left else 1]),
        "case":  one(dec[3]),
        "in_case": bool(status & 0x40),
        "model_id": (raw[3] << 8) | raw[4],
    }


def start_scan(seconds=20):
    """Run an LE discovery for a moment. Only for --scan: the conky path stays
    passive and rides on the scan LibrePods already runs.

    This has to be bluetoothctl rather than a busctl StartDiscovery call. A
    BlueZ discovery session belongs to the D-Bus connection that asked for it,
    and busctl exits the instant the call returns -- so the session is torn
    down immediately, StartDiscovery reports success, and the adapter never
    actually scans. bluetoothctl holds its connection open for the duration."""
    try:
        subprocess.run(["bluetoothctl", "--timeout", str(seconds), "scan", "on"],
                       capture_output=True, timeout=seconds + 15)
    except (OSError, subprocess.TimeoutExpired):
        pass


# ── AAP, the control channel ──────────────────────────────────
# Packet vocabulary, from LibrePods' linux/airpods_packets.h.
AAP_HANDSHAKE = bytes.fromhex("00000400010002000000000000000000")
AAP_FEATURES  = bytes.fromhex("040004004d00d700000000000000")
AAP_NOTIFY    = bytes.fromhex("040004000f00ffffffffff")
AAP_ACK       = bytes.fromhex("01000400")
AAP_FEAT_ACK  = bytes.fromhex("040004002b00")
AAP_BATTERY   = bytes.fromhex("040004000400")
# Component type byte -> which battery. Status: 1 charging, 2 discharging,
# 4 disconnected (a bud in the case, or a case with no buds in it).
AAP_COMPONENT = {0x01: "headset", 0x02: "right", 0x04: "left", 0x08: "case"}


def device_address(name):
    """The BD address of the connected device LibrePods is managing. Matched by
    the name in its config, since that is the only identifier it stores."""
    for ifaces in bluez_objects().values():
        d = ifaces.get("org.bluez.Device1")
        if not d or not d.get("Connected", {}).get("data"):
            continue
        for key in ("Alias", "Name"):
            if d.get(key, {}).get("data") == name:
                return d.get("Address", {}).get("data")
    return None


def parse_battery(pkt):
    """A BATTERY_STATUS notification -> {component: {pct, charging}}.

    Layout after the 6-byte header: a count, then five bytes per component --
    type, 0x01, level, status, 0x01. The two constant bytes are checked because
    a mis-framed packet would otherwise decode as plausible percentages."""
    if not pkt.startswith(AAP_BATTERY) or len(pkt) < 7:
        return None
    count = pkt[6]
    if count > 3 or len(pkt) != 7 + 5 * count:
        return None
    out = {}
    for i in range(count):
        o = 7 + 5 * i
        if pkt[o + 1] != 0x01 or pkt[o + 4] != 0x01:
            return None
        name = AAP_COMPONENT.get(pkt[o])
        level, status = pkt[o + 2], pkt[o + 3]
        if not name or status == 0x04 or not 1 <= level <= 100:
            continue                       # disconnected or no reading
        out[name] = {"pct": level, "charging": status == 0x01}
    return out or None


def aap_read(addr, timeout=None):
    """Battery straight off the control channel, or None.

    Never raises: every failure here -- no such device, channel held by another
    host, firmware that does not answer -- just means fall back to the advert."""
    timeout = timeout or AAP_TIMEOUT
    s = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_SEQPACKET, socket.BTPROTO_L2CAP)
    try:
        s.settimeout(timeout)
        s.connect((addr, AAP_PSM))
        s.send(AAP_HANDSHAKE)
        stage, end = 0, time.time() + timeout
        while time.time() < end:
            r, _, _ = select.select([s], [], [], 0.5)
            if not r:
                continue
            pkt = s.recv(1024)
            if not pkt:
                break
            if pkt.startswith(AAP_ACK) and stage < 1:
                s.send(AAP_FEATURES); stage = 1
            elif pkt.startswith(AAP_FEAT_ACK) and stage < 2:
                s.send(AAP_NOTIFY); stage = 2
            got = parse_battery(pkt)
            if got:
                return got
        return None
    except (OSError, ValueError):
        return None
    finally:
        try:
            s.close()
        except OSError:
            pass


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


# ── self-scanning ─────────────────────────────────────────────
def _scan_state():
    try:
        with open(SCAN_STATE) as fh:
            st = json.load(fh)
        return {"last": float(st.get("last", 0)),
                "interval": max(SCAN_MIN, min(SCAN_MAX, int(st.get("interval", SCAN_MIN)))),
                "fails": int(st.get("fails", 0))}
    except (OSError, ValueError, TypeError):
        return {"last": 0.0, "interval": SCAN_MIN, "fails": 0}


def _save_scan_state(st):
    try:
        os.makedirs(os.path.dirname(SCAN_STATE), exist_ok=True)
        tmp = SCAN_STATE + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(st, fh)
        os.replace(tmp, SCAN_STATE)
    except OSError:
        pass


def adapter_scanning():
    try:
        out = subprocess.run(
            ["busctl", "--system", "get-property", "org.bluez", "/org/bluez/hci0",
             "org.bluez.Adapter1", "Discovering"],
            capture_output=True, text=True, timeout=5)
        return out.stdout.strip().endswith("true")
    except Exception:
        return False


def kick_scan():
    """Start a scan in the background if one is due. Never blocks the caller --
    a scan takes SCAN_SECS and conky re-renders every few seconds."""
    st = _scan_state()
    if time.time() - st["last"] < st["interval"]:
        return
    # Someone else already has the radio scanning and there is still no advert;
    # adding a second scan would not find one either.
    if adapter_scanning():
        return
    try:
        subprocess.Popen([sys.executable, os.path.abspath(__file__), "--refresh"],
                         start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


def refresh():
    """One scan, under a lock so concurrent conky ticks cannot stack them up.
    Records the attempt BEFORE scanning, so a scan that hangs or is killed
    still counts against the rate limit."""
    os.makedirs(os.path.dirname(SCAN_LOCK), exist_ok=True)
    with open(SCAN_LOCK, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return 0                      # another scan already running

        st = _scan_state()
        st["last"] = time.time()
        _save_scan_state(st)

        # Fast path: ask the AirPods directly. Costs a couple of seconds and no
        # radio time, and unlike the advert it works while they sit in your ears.
        try:
            conf = load_conf()
            addr = device_address(conf.get("deviceName", ""))
            comps = aap_read(addr) if addr else None
        except Exception:
            conf, addr, comps = None, None, None
        if comps and conf:
            out = build(comps, conf, addr, "aap")
            if out:
                _save_reading(out)
                st.update(fails=0, interval=AAP_MIN, last=time.time())
                _save_scan_state(st)
                return 0

        # Slow path: make them advertise at us instead.
        start_scan(SCAN_SECS)
        try:
            found = read(scan=False, cache=False) is not None
        except Exception:
            found = False
        if found:
            st.update(fails=0, interval=SCAN_MIN)
        else:
            st["fails"] += 1
            st["interval"] = min(SCAN_MAX, SCAN_MIN * (2 ** st["fails"]))
        st["last"] = time.time()
        _save_scan_state(st)
        return 0 if found else 1


# ── public entry point ────────────────────────────────────────
def short_name(conf):
    """"James's AirPods Pro" -> "AirPods Pro": the owner prefix is the same on
    every device here and only costs label width."""
    base = conf.get("deviceName", "AirPods")
    for sep in ("\u2019s ", "'s "):
        if sep in base:
            return base.split(sep, 1)[1]
    return base


def build(d, conf, addr, via):
    """Components dict -> the card entries bin/batteries.py expects. Shared by
    both sources so an AAP reading and an advert reading are indistinguishable
    downstream."""
    base = short_name(conf)
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
    elif d.get("headset"):                 # AirPods Max: one battery, one ring
        h = d["headset"]
        parts.append({"name": base, "glyph": GLYPH_BUD, "pct": h["pct"],
                      "coarse": False, "level": 1, "charging": h["charging"],
                      "full": False})
    if d.get("case"):
        parts.append({"name": "Case", "glyph": GLYPH_CASE,
                      "pct": d["case"]["pct"], "coarse": False, "level": 1,
                      "charging": d["case"]["charging"], "full": False})
    if not parts:
        return None
    return {"device_name": conf.get("deviceName", ""), "address": addr,
            "via": via, "components": parts}


def read(scan=True, cache=True):
    """Battery components for the AirPods, or None if there is nothing current.

    When there is no fresh advert, the last decoded reading is served for up to
    HOLD seconds (unless `cache` is off) and, if `scan` is set, a background
    scan is started to top it up. The scan is never waited on.

    Returns a list of dicts shaped like bin/batteries.py's own device entries,
    plus `device_name` so the caller can drop UPower's duplicate of the same
    headphones."""
    conf = load_conf()
    hit = find_advert(conf["magicAccIRK"])
    if hit is None or _freshness(hit[1]) > STALE:
        if scan:
            kick_scan()
        return _load_reading() if cache else None
    addr, raw, _ = hit

    d = decode(raw, conf["magicAccEncKey"])
    out = build(d, conf, addr, "ble")
    if out is None:
        return _load_reading() if cache else None
    _save_reading(out)
    return out


def diagnose():
    """Why is there no reading? The three causes need different fixes, so say
    which one it is rather than one vague line."""
    try:
        disc = subprocess.run(
            ["busctl", "--system", "get-property", "org.bluez", "/org/bluez/hci0",
             "org.bluez.Adapter1", "Discovering"],
            capture_output=True, text=True, timeout=5).stdout.strip()
        scanning = disc.endswith("true")
    except Exception:
        return "cannot reach the Bluetooth adapter"

    seen = 0
    try:
        for ifaces in bluez_objects().values():
            d = ifaces.get("org.bluez.Device1")
            if not d:
                continue
            raw = bytes(d.get("ManufacturerData", {}).get("data", {})
                         .get("76", {}).get("data", []))
            if len(raw) >= 27 and raw[0] == 0x07:
                seen += 1
    except Exception:
        pass

    aap = "unknown"
    try:
        conf = load_conf()
        addr = device_address(conf.get("deviceName", ""))
        if addr is None:
            aap = "the device is not connected, so the control channel is out"
        else:
            aap = ("answering" if aap_read(addr, timeout=6)
                   else "not answering -- another host (an iPhone nearby with "
                        "Bluetooth on) holds the single AAP session")
    except Exception as e:
        aap = f"could not be tried ({e})"

    st = _scan_state()
    due = max(0, int(st["last"] + st["interval"] - time.time()))
    backoff = (f" AAP control channel: {aap}. Own scan: {st['fails']} "
               f"consecutive misses, next in {due}s (every {st['interval']}s).")
    if not scanning:
        return ("nothing is scanning for BLE advertisements right now, so "
                "BlueZ has no advert to read." + backoff)
    if seen == 0:
        return ("scanning, but no device in range is broadcasting an Apple "
                "proximity-pairing advert (type 0x07). AirPods broadcast it "
                "reliably when the case lid is opened nearby; connected buds "
                "with the case shut or away often do not broadcast at all."
                + backoff)
    return (f"scanning and {seen} proximity-pairing advert(s) in range, but "
            "none resolves against this IRK, so none of them is this device."
            + backoff)


def main():
    if "--refresh" in sys.argv:
        return refresh()
    if "--scan" in sys.argv:
        start_scan(SCAN_SECS)
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
