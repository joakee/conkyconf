# conkyconf

Custom gruvbox-dark Conky desktop widget stack: rounded gruvbox-themed cards
for CPU, memory, weather, now-playing, and battery status, driven by Lua
drawing helpers and Python data-source scripts.

## Layout

- `start.sh` — launches the widget stack.
- `widget-common.lua` — shared drawing/layout helpers used by all widgets.
- `lua/` — per-widget Lua rendering (`draw.lua`, `batteries.lua`, `nowplaying.lua`).
- `bin/` — Python data-source scripts (weather, battery, AirPods, now-playing,
  head/reflow window management, focus dimming).
- `widgets/` — individual widget `.conf` definitions (CPU, memory, weather,
  now-playing, batteries).
- `attic/` — earlier/replaced versions kept for reference.

## Notes

- Battery percentage for AirPods is read via the AAP L2CAP protocol (PSM
  0x1001), falling back to the encrypted BLE advertisement when a nearby
  iPhone has stolen that session.
- Weather data comes from the Bureau of Meteorology's public `/fwo/` products.
- `bin/reflow.py` re-reads the monitor head origin on every pass so a monitor
  hotplug doesn't strand the widget stack off-screen.
