#!/bin/sh
# wttr.in -> conky markup for the gruvbox-dark weather widget.
#
#   https://github.com/chubin/wttr.in
#
# Prints the widget body on stdout for ${execpi}. Never blocks on the network:
# the cache is always rendered immediately and a refresh is forked into the
# background when it goes stale, so a slow or unreachable wttr.in shows the
# last good reading instead of freezing the conky process. wttr.in asks that
# clients not poll hard, hence the 15 minute TTL.
#
# Env:
#   WEATHER_LOCATION  city / airport code / "" to geolocate by public IP
#   WEATHER_TTL       seconds before a refresh is triggered (default 900)
#   WEATHER_UNITS     "m" metric (default), "u" USCS

set -u

CACHE_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/conky-gruvbox"
CACHE="$CACHE_DIR/weather"
LOCK="$CACHE_DIR/weather.lock"
TTL="${WEATHER_TTL:-900}"
LOCATION="${WEATHER_LOCATION:-}"
UNITS="${WEATHER_UNITS:-m}"

# %l location  %C condition  %t temp  %f feels-like
# %D dawn  %S sunrise  %z zenith  %s sunset  %d dusk
FMT='%l|%C|%t|%f|%D|%S|%z|%s|%d'
URL="https://wttr.in/${LOCATION}?format=${FMT}&${UNITS}"

mkdir -p "$CACHE_DIR"

fetch() {
    out=$(curl -sf --max-time "$1" -A 'curl/conky-gruvbox' "$URL" 2>/dev/null) || return 1
    # A valid reply has all 9 fields and is not an error page.
    n=$(printf '%s' "$out" | awk -F'|' '{print NF}')
    [ "${n:-0}" -eq 9 ] || return 1
    case $out in *'<html'*|*'Unknown location'*|*'Sorry'*) return 1 ;; esac
    printf '%s\n' "$out" > "$CACHE.tmp" && mv -f "$CACHE.tmp" "$CACHE"
}

# Manual refresh (the clickable icon in the widget header): ignore the TTL and
# fetch synchronously, so the caller knows whether it actually worked. The lock
# keeps it from racing a background refresh already in flight.
if [ "${1:-}" = "--refresh" ]; then
    ( flock -w 30 9 || exit 1; fetch 20 ) 9>"$LOCK"
    exit $?
fi

stale=1
if [ -f "$CACHE" ]; then
    age=$(( $(date +%s) - $(stat -c %Y "$CACHE" 2>/dev/null || echo 0) ))
    [ "$age" -lt "$TTL" ] && stale=0
fi

if [ ! -f "$CACHE" ]; then
    # Nothing to show at all: fetch synchronously once, briefly, so the widget
    # is populated on first paint rather than blank for a whole interval.
    fetch 8 || true
elif [ "$stale" -eq 1 ]; then
    # Detached refresh. stdout/stderr must be closed or execpi waits on us.
    ( flock -n 9 || exit 0; fetch 20 ) 9>"$LOCK" >/dev/null 2>&1 &
fi

if [ ! -f "$CACHE" ]; then
    printf '${color1}${font Maple Mono NF CN:size=10}\xf3\xb0\x96\x90${font}  ${color5}${font Maple Mono NF CN:size=8}WEATHER${font}\n'
    printf '${voffset 2}${color5}waiting for wttr.in\n'
    exit 0
fi

updated=$(date -d "@$(stat -c %Y "$CACHE" 2>/dev/null || echo 0)" '+%H:%M' 2>/dev/null) || updated=''
[ -n "$updated" ] && updated="󰑐 $updated"

awk -v upd="$updated" -F'|' '
function trim(s) { gsub(/^[ \t]+|[ \t]+$/, "", s); return s }
function hm(s)   { s = trim(s); return (length(s) >= 5) ? substr(s, 1, 5) : s }
# conky re-parses execpi output, so strip anything that could look like markup
function safe(s) { gsub(/[${}]/, "", s); return trim(s) }
function mins(s,  p) { split(s, p, ":"); return p[1] * 60 + p[2] }

{
    loc  = safe($1); cond = safe($2)
    temp = safe($3); feel = safe($4)
    sub(/^\+/, "", temp); sub(/^\+/, "", feel)
    dawn = hm($5); sunrise = hm($6); zenith = hm($7); sunset = hm($8); dusk = hm($9)

    day = 1
    if (sunrise ~ /^[0-9]/ && sunset ~ /^[0-9]/) {
        now = mins(strftime("%H:%M"))
        day = (now >= mins(sunrise) && now < mins(sunset))
    }

    c = tolower(cond)
    # Order matters: the more specific patterns must be tested first.
    if      (c ~ /thunder|storm/)                 g = "\363\260\226\223"        # lightning
    else if (c ~ /hail|ice pellet/)               g = "\363\260\226\222"        # hail
    else if (c ~ /snow|sleet|blizzard/)           g = "\363\260\226\230"        # snowy
    else if (c ~ /heavy rain|torrential/)         g = "\363\260\226\226"        # pouring
    else if (c ~ /rain|drizzle|shower/)           g = "\363\260\226\227"        # rainy
    else if (c ~ /fog|mist|haze|smoke/)           g = "\363\260\226\221"        # fog
    else if (c ~ /partly|patchy/)                 g = day ? "\363\260\226\225" : "\363\260\274\261"
    else if (c ~ /cloud|overcast/)                g = "\363\260\226\220"        # cloudy
    else if (c ~ /clear|sunny|fair/)              g = day ? "\363\260\226\231" : "\363\260\226\224"
    else                                          g = "\363\260\226\220"

    F = "Maple Mono NF CN"
    printf "${color1}${font %s:size=10}%s${font}  ${color5}${font %s:size=8}WEATHER${alignr}%s${font}\n", F, g, F, upd
    printf "${voffset 2}${color3}${font %s:bold:size=15}%s${font}${color5}${offset 8}${voffset -4}feels %s${voffset 4}\n", F, temp, feel
    printf "${voffset 2}${color2}%s\n", cond
    printf "${color5}${font %s:size=8}%s${font}\n", F, loc
    printf "${voffset 4}${color5}dawn${alignr}${color2}%s\n", dawn
    printf "${color5}sunrise${alignr}${color2}%s\n", sunrise
    printf "${color5}zenith${alignr}${color2}%s\n", zenith
    printf "${color5}sunset${alignr}${color2}%s\n", sunset
    printf "${color5}dusk${alignr}${color2}%s\n", dusk
}
' "$CACHE"
