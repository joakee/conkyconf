# Wayland mode

These files run the same widget stack in a Wayland session (Xfce on Wayfire).
Nothing outside this folder changes, so X11 runs exactly as it did before.
The widgets in `widgets/`, the drawers in `lua/` and the data scripts in `bin/`
are shared by both sessions. Edit them in one place and both pick it up.

`start.sh` is the login entry for both sessions. On X11 it runs
`conky-gruvbox-dark start`, as the autostart entry always did. On Wayland it
starts `bin/stack.py`.

| File | What it does |
| --- | --- |
| `card.lua` | The conky config for one card: `CONKY_GRUVBOX_CARD=30-cpu.conf conky -c card.lua`. It runs the widget file unchanged, after redirecting its `widget-common.lua` and its scripts to the files below. |
| `widget-common.lua` | The X11 file, loaded as is, with `M.card`'s output adjusted for Wayland: a bottom-layer layer-shell surface, margins in place of X positions, and the namespace `conky-gruvbox-<card>@<M.OUTPUT>`. |
| `lua/compat.lua` | Gives the Cairo drawers, which call `cairo_xlib_surface_create`, conky's Wayland surface instead. Also paints each card's rounded, translucent background, which picom used to round, and does the focus fade. |
| `bin/run`, `bin/cardvis.py` | Runs a `bin/` script with a Wayland `cardvis`. A card that hides itself (countdowns, now playing) writes `<card>.shown`, and `stack.py` acts on it. |
| `bin/stack.py` | Starts the cards and restarts any that exit. Keeps them on `M.OUTPUT`, reflows the stack, parks hidden cards off screen, makes the frosted backdrops, and fades the cards while a window has focus. Together this replaces `reflow.py` and `focusdim.py`. `stack.py --show` prints where each card is. |

## Requirements

- **conky 1.25.4 or newer.** The packaged 1.25.2 snapshot aborts on startup
  under Wayland: it calls `set_font(0)` before any font is loaded (fixed
  upstream in f7f573f0). `stack.py` uses `~/.local/opt/conky/bin/conky` if it
  exists, otherwise `/usr/bin/conky`. `$CONKY_BIN` overrides both.
- **Wayfire plugins:**
  - `ipc` and `ipc-rules`;
  - `layer-output`, which puts each card on the monitor its namespace names
    (conky can't choose one under Wayland).
- **ImageMagick and `xfconf-query`**, for the frosted backdrop (below).

## How it differs from X11

- **Moving a card means reloading it.** Conky sets a layer surface's margins
  once, when it creates the surface. A card that has to move is pinned (its
  `<card>.place` file is written) and reloaded, which recreates it at the new
  spot. It disappears for about a second while that happens. This only happens
  when a card above it changes height.
- **Cards don't slide with the workspace.** Layer surfaces stay put during a
  workspace switch, so the picom slide isn't replicated.
- **The cards frost themselves.** Only the wallpaper is ever behind them, so
  `stack.py` blurs it once with ImageMagick (the image and fit come from
  xfdesktop's settings for the output) and gives each card its piece,
  `<card>.backdrop.png` in the run directory, to paint under its tint.
  Wayfire's blur looks the same, but it re-blurs behind a card every time
  the card redraws, which cost a dropped frame or two each second. Changing
  the wallpaper is picked up within 10 seconds.
- **The focus fade keeps the frosting.** As on X11, the card turns more
  see-through while a window has focus, but what shows through is the
  blurred wallpaper rather than the sharp one. `stack.py` writes the level to `wl-level` in the run directory and sends
  each card SIGUSR2 (conky's "redraw now") for every step of the fade.
- **Card heights can differ slightly from X11.** Fonts are laid out by Pango
  rather than Xft, so a card can end up a few pixels taller or shorter. The
  stack is planned from the heights on screen, so the gaps stay even.
