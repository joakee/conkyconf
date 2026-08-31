#!/usr/bin/env python3
"""Now Playing card: whatever MPRIS says is on, with its album art.

MPRIS is the freedesktop standard every Linux media player speaks, so this is
player-agnostic -- Spotify, mpv, VLC, browsers, anything owning a bus name
under org.mpris.MediaPlayer2.*. Talking to it goes through busctl rather than a
D-Bus binding so the script stays stdlib-only, matching bin/weather.py and
bin/batteries.py.

Two outputs, like bin/batteries.py: a Lua table literal that lua/nowplaying.lua
reads to draw the card, and conky markup on stdout reserving the vertical space
the drawing needs.

Album art is fetched OUT OF BAND. mpris:artUrl is usually an http(s) URL on the
player's CDN, and a network fetch on conky's render path would freeze the card
for the length of the timeout, so a miss spawns a detached download and draws
without art until it lands. Art is converted to PNG at exactly the size it will
be drawn, because cairo can only load PNG and scaling at draw time is both
slower and softer.
"""

import base64
import hashlib
import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request

# ── layout, in pixels ─────────────────────────────────────────
# Mirrored by lua/nowplaying.lua; the two must agree.
WIDTH    = 292
ART      = 76     # album art is square
BAR_H    = 4
HEADER_H = 6      # padding under conky's own header line
FOOT_H   = 18     # times row + progress bar under the art
LINE_H   = 19     # conky charges a line height for the ${voffset} line itself

BUS = ["busctl", "--user", "--json=short"]
PLAYER_IFACE = "org.mpris.MediaPlayer2.Player"
CACHE_DIR = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
CACHE = os.path.join(CACHE_DIR, "conky-gruvbox", "nowplaying.lua")
ART_DIR = os.path.join(CACHE_DIR, "conky-gruvbox", "art")
UA = "conky-gruvbox-nowplaying/1.0 (personal desktop widget)"

GLYPH = "\U000F075A"   # music-note; verified by rendering, see batteries.py


def bus_json(*args, timeout=5):
    out = subprocess.run(BUS + list(args), capture_output=True, text=True,
                         timeout=timeout)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip() or "busctl failed")
    return json.loads(out.stdout)["data"][0]


def players():
    names = bus_json("call", "org.freedesktop.DBus", "/org/freedesktop/DBus",
                     "org.freedesktop.DBus", "ListNames")
    return [n for n in names if n.startswith("org.mpris.MediaPlayer2.")]


def unwrap(v):
    """busctl's JSON wraps every value as {"type":..., "data":...}, nested for
    variants and arrays."""
    if isinstance(v, dict) and "data" in v and "type" in v:
        return unwrap(v["data"])
    if isinstance(v, list):
        return [unwrap(x) for x in v]
    if isinstance(v, dict):
        return {k: unwrap(x) for k, x in v.items()}
    return v


def source_name(bus):
    """A human name for the player: org.mpris.MediaPlayer2.spotify -> Spotify.
    Browsers and some players append .instanceNNN, which is noise here."""
    leaf = bus[len("org.mpris.MediaPlayer2."):]
    leaf = leaf.split(".instance")[0].split(".")[0]
    return leaf.replace("_", " ").title()


def player_state(name):
    props = unwrap(bus_json("call", name, "/org/mpris/MediaPlayer2",
                            "org.freedesktop.DBus.Properties", "GetAll", "s",
                            PLAYER_IFACE))
    meta = props.get("Metadata") or {}

    def first(x):
        if isinstance(x, list):
            return x[0] if x else ""
        return x or ""

    return {
        "bus":      name,
        "source":   source_name(name),
        "status":   props.get("PlaybackStatus", "Stopped"),
        "title":    first(meta.get("xesam:title")),
        "artist":   ", ".join(meta.get("xesam:artist") or []) or first(meta.get("xesam:albumArtist")),
        "album":    first(meta.get("xesam:album")),
        "art_url":  first(meta.get("mpris:artUrl")),
        "length":   int(meta.get("mpris:length") or 0) // 1000000,
        "position": int(props.get("Position") or 0) // 1000000,
    }


def pick():
    """The player worth showing: something actually playing beats something
    paused, and anything with a title beats a player sitting idle with none."""
    best = None
    rank = {"Playing": 0, "Paused": 1}
    for name in players():
        try:
            st = player_state(name)
        except Exception:
            continue
        if st["status"] == "Stopped" and not st["title"]:
            continue
        key = (rank.get(st["status"], 2), 0 if st["title"] else 1, name)
        if best is None or key < best[0]:
            best = (key, st)
    return best[1] if best else None


# ── album art ─────────────────────────────────────────────────
def art_path(url):
    return os.path.join(ART_DIR, hashlib.sha1(url.encode()).hexdigest() + ".png")


def fetch_art(url, dest):
    """Resolve artUrl to a PNG of exactly ART pixels. Handles the three forms
    players use: an http(s) CDN link, a file:// path, and an inline data: URI."""
    if url.startswith("data:"):
        head, _, payload = url.partition(",")
        raw = base64.b64decode(payload) if ";base64" in head \
            else urllib.parse.unquote_to_bytes(payload)
    elif url.startswith("file://"):
        with open(urllib.parse.unquote(urllib.parse.urlparse(url).path), "rb") as fh:
            raw = fh.read()
    else:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=10) as r:
            raw = r.read()

    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = dest + ".tmp"
    # -strip drops EXIF; ^ + extent centre-crops non-square art to a square.
    out = subprocess.run(
        ["magick", "-", "-strip", "-resize", f"{ART}x{ART}^",
         "-gravity", "center", "-extent", f"{ART}x{ART}", f"PNG32:{tmp}"],
        input=raw, capture_output=True, timeout=20)
    if out.returncode != 0 or not os.path.exists(tmp):
        raise RuntimeError("art conversion failed")
    os.replace(tmp, dest)
    prune_art()


ART_KEEP = 60


def prune_art():
    """One PNG per distinct album art adds up over a long session. The cache
    lives in XDG_RUNTIME_DIR so it clears at logout anyway, but keep a lid on
    it: drop the least recently used beyond ART_KEEP."""
    try:
        files = [os.path.join(ART_DIR, f) for f in os.listdir(ART_DIR)
                 if f.endswith(".png")]
    except OSError:
        return
    if len(files) <= ART_KEEP:
        return
    files.sort(key=lambda f: os.path.getmtime(f), reverse=True)
    for f in files[ART_KEEP:]:
        try:
            os.remove(f)
        except OSError:
            pass


def ensure_art(url):
    """Path to the cached art, or None. A miss is fetched in the background so
    the render path never blocks on the network."""
    if not url:
        return None
    dest = art_path(url)
    if os.path.exists(dest):
        return dest
    try:
        subprocess.Popen([sys.executable, os.path.abspath(__file__), "--art", url],
                         start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass
    return None


# ── cache ─────────────────────────────────────────────────────
def lua_str(s):
    out = str(s).replace("\\", "\\\\").replace('"', '\\"')
    return '"' + "".join(c if c >= " " else "\\%d" % ord(c) for c in out) + '"'


def lua_value(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    return lua_str(v)


def write_cache(d):
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    tmp = CACHE + ".tmp"
    body = ", ".join(f"{k}={lua_value(v)}" for k, v in d.items() if v is not None)
    with open(tmp, "w") as fh:
        fh.write("return {" + body + "}\n")
    os.replace(tmp, CACHE)


def main():
    if "--art" in sys.argv:
        url = sys.argv[sys.argv.index("--art") + 1]
        try:
            fetch_art(url, art_path(url))
        except Exception:
            return 1
        return 0

    try:
        st = pick()
    except Exception:
        st = None

    print("${color1}${font Maple Mono NF CN:size=10}" + GLYPH + "${font}  "
          "${color5}${font Maple Mono NF CN:size=8}NOW PLAYING${font}")

    if not st:
        try:
            write_cache({})
        except OSError:
            pass
        print("${voffset 2}${color5}nothing playing")
        print("${voffset 0}", end="")
        return 0

    st["art"] = ensure_art(st["art_url"]) or ""
    try:
        write_cache({k: v for k, v in st.items() if k != "art_url"})
    except OSError:
        pass

    body = HEADER_H + ART + FOOT_H - LINE_H
    print("${voffset %d}" % max(0, body), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
