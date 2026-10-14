Custom gruvbox-dark Conky desktop widget stack: rounded gruvbox-themed cards
for CPU, memory, weather, fuel prices, now-playing, and battery status, driven by Lua
drawing helpers and Python data-source scripts.

<img height="600" alt="image" src="https://github.com/user-attachments/assets/fbb91432-a957-4726-8d8b-b4aab1edbae2" />


## Layout

- `start.sh` — launches the widget stack.
- `widget-common.lua` — shared drawing/layout helpers used by all widgets.
- `lua/` — per-widget Lua rendering (`draw.lua`, `batteries.lua`, `nowplaying.lua`).
- `bin/` — Python data-source scripts (weather, fuel, battery, AirPods, now-playing,
  head/reflow window management, focus dimming).
- `widgets/` — individual widget `.conf` definitions (CPU, memory, weather,
  fuel, now-playing, batteries).
- `wayland/` — the same stack running under Wayland (Wayfire); see `wayland/README.md`.
- `attic/` — earlier/replaced versions kept for reference.
- `arch/` — Arch Linux package (`PKGBUILD`) and its `conky-gruvbox-dark` setup command.

## Installing on Arch

```sh
cd arch && makepkg -si      # Maple Mono NF CN comes from the AUR (maple-mono-nf-cn)
conky-gruvbox-dark install  # as your user: copies the stack to ~/.config/conky/gruvbox-dark,
                            # starts it at login, and starts it now
```

The package keeps a "clean" copy in `/usr/share/conky-gruvbox-dark`; the widgets
run from the copy in `~/.config/conky/gruvbox-dark` (edit this one if you wanna customise). 
At each login, `conky-gruvbox-dark start` brings that copy up to date with the package:
files you haven't changed are updated, files you have are kept, with the new version
written beside them as `.pacnew`. `conky-gruvbox-dark uninstall` stops the widgets
and deletes the login entry

Under Wayland, `conky-gruvbox-dark start` runs `wayland/start.sh` instead of `start.sh`,
so the same login entry works in both sessions. Wayland needs conky 1.25.4 or newer and
Wayfire's `layer-output` plugin; `wayland/README.md` has the details.

## Notes

- ~~Battery percentage for AirPods is read via the AAP L2CAP protocol (PSM
  0x1001), falling back to the encrypted BLE advertisement when a nearby
  iPhone has stolen that session.~~ Now pulls data from [podctl](https://github.com/Rockykln/podctl).
- Weather data comes from the Australian Government's Bureau of Meteorology's public `/fwo/` products.
  Observations update every 30 minutes and the forecast a few times a day;
  the widget caches for 15 minutes. Click the refresh icon in the weather
  card's header to fetch immediately (`lua/weather.lua` → `bin/weather.py
  --refresh`): it turns yellow while fetching and red if the fetch failed.
  - Potentially looking to implement other data sources in the future
- Fuel prices come from FuelWatch WA's public RSS feed: the
  statewide average across every reporting site, then the five cheapest in
  Perth metro (FuelWatch regions 25/26/27; `FUEL_REGIONS` to change).
  WA prices change once a day at 06:00; the date in the header turns red if
  the cached prices are not today's.
  Click the fuel label in the card's header to cycle ULP 91 → PULP 95 →
  PULP 98 → diesel → brand diesel → LPG → E85 (right-click goes back).
- `bin/reflow.py` re-reads the monitor head origin on every pass so a monitor
  hotplug doesn't strand the widget stack off-screen.
- Updated to target the window positioning changes introduced in Conky 1.25. YMMV for older versions
