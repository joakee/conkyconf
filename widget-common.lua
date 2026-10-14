--[[
  gruvbox-dark conky widgets -- shared palette, window style and layout.

  Each widget is its own conky window, so picom rounds and blurs them
  independently: that is what gives the macOS-widget look. Card positions
  are DERIVED from the stack below -- change a height (or reorder / delete
  an entry) and everything under it reflows automatically.
--]]

local M = {}

M.font = 'Maple Mono NF CN'

-- gruvbox dark
M.pal = {
  bg0h   = '1d2021',  bg2  = '504945',
  fg0    = 'fbf1c7',  fg1  = 'ebdbb2',  fg4    = 'a89984',
  gray   = '928374',
  red    = 'fb4934',  green= 'b8bb26',  yellow = 'fabd2f',
  blue   = '83a598',  purple='d3869b',  aqua   = '8ec07c',
  orange = 'fe8019',
}

-- ── geometry ──────────────────────────────────────────────────
M.WIDTH = 292   -- content width; card width = WIDTH + 2*PAD = 320
M.PAD   = 14    -- inner padding
M.GAP   = 12    -- vertical space between cards
M.TOP   = 34    -- distance from top of the screen to the first card
M.SIDE  = 28    -- visible margin from the left/right edge of the screen
M.ALIGN = 'top_left'  -- 'top_left' or 'top_right': which edge to hug
-- Which monitor the stack lives on, as an XRandR output NAME (`xrandr` lists
-- them). Set it to '' to fall back to whatever is marked primary.
--
-- Deliberately not the primary flag: xfwm4 4.17+ draws the Alt+Tab switcher
-- only on the primary monitor, so this machine keeps primary unset on purpose
-- and there is nothing for bin/head.py to read. A name is the better handle
-- regardless -- it is the only one of the three that a hotplug leaves alone,
-- where the primary flag gets cleared outright and Xinerama indices renumber.
M.OUTPUT = 'eDP-1'

-- The above, resolved to the Xinerama head index conky counts in, by
-- bin/head.py -- see there for the full order of preference and how it degrades
-- when that output is unplugged or switched off. Resolved once at parse time,
-- because that is the only time conky reads xinerama_head; bin/reflow.py
-- re-resolves it continuously and writes the positions itself, which is what
-- keeps the stack on the right monitor after a hotplug rather than merely at
-- startup.
M.HEAD = (function()
  local fh = io.popen(os.getenv('HOME')
    .. '/.config/conky/gruvbox-dark/bin/head.py 2>/dev/null')
  if not fh then return 0 end
  local first = fh:read('*l')
  fh:close()
  return tonumber(first and first:match('^(%d+)')) or 0
end)()
M.ALPHA = 115   -- card background opacity, 0-255

-- Conky's gap_y positions the TEXT, not the window: it places the window
-- PAD+1 px higher and makes it 2*PAD+2 px taller than the content box. Both
-- were measured on this setup. Correcting for them here lets `h` in the stack
-- below mean the real on-screen card height, which is what you want to edit.
-- The offset is actually a pixel larger on the cards that draw in Cairo, so
-- conky's own placement is not quite uniform (the memory/weather gap comes out
-- at 11 px, not 12). bin/reflow.py measures the windows instead of predicting
-- them, and irons that out along with everything else.
M.CHROME = 2 * M.PAD + 2
M.SHIFT  = M.PAD + 1

-- Full card as drawn = content + chrome. A half card is that, less one GAP,
-- split in two -- so a left/right pair spans exactly the same width as a
-- full-width card and the stack keeps a single flush edge.
M.CARD_W = M.WIDTH + M.CHROME
M.HALF_W = math.floor((M.CARD_W - M.GAP) / 2)

-- ── the widget stack, top to bottom ───────────────────────────
-- h = card height in pixels as drawn on screen.
-- half = 'l'/'r' puts two cards side by side on one row; they must be
-- adjacent entries, and the row is as tall as the taller of the two.
-- auto = true leaves the height unpinned so conky sizes the window to its
-- text, letting the card grow and shrink at runtime. `h` is then only the
-- nominal conky places the cards below it by, since it reads gap_y once at
-- startup and never again. bin/reflow.py removes that limitation: it watches
-- the cards and re-derives every position from the heights actually on screen,
-- so an auto card anywhere in the stack keeps its gaps as it resizes, and pins
-- each card to its spot (see M.card). Still give it a sensible height here --
-- it is what the stack looks like in the moment before the watcher's first
-- pass, and if the watcher is not running.
M.stack = {
  { name = 'cpu',       h = 132, interval = 1,  auto = true },
  { name = 'memory',    h = 127, interval = 2  },
  { name = 'batteries',  h = 130, interval = 5,  auto = true },
  { name = 'weather',    h = 290, interval = 1,  auto = true },
  { name = 'fuel',       h = 180, interval = 1  },
  { name = 'nowplaying', h = 146, interval = 1,  auto = true },
  { name = 'countdowns', h = 90,  interval = 1,  auto = true },
}

-- ── pinned positions ──────────────────────────────────────────
-- Conky 1.25 puts its window back at gap_x/gap_y on EVERY update, so a card
-- bin/reflow.py has moved is dragged back to its nominal spot within one
-- update_interval and moved again by the watcher -- the card bobs. gap_y can
-- not be changed at runtime from a lua_load script (that runs in a separate Lua
-- state from the config), but a SIGUSR1 reload re-runs this file and keeps the
-- same window. So the watcher writes the spot it wants to <name>.place and
-- signals the card, and M.card reads it here. Both are in layout terms:
-- `top` from the head's top edge and `inset` from its aligned edge, the two
-- numbers the nominal gap_y/gap_x are derived from below.
--
-- <name>.placed records what this conky was actually configured with, as
-- "top inset uptime": the watcher compares it with what it wants, to know
-- whether a reload is needed, and bin/cardvis.py watches it change to notice a
-- reload (which maps the window again).
M.RUNDIR = (os.getenv('XDG_RUNTIME_DIR') or '/tmp') .. '/conky-gruvbox'

local function first_line(path)
  local fh = io.open(path)
  if not fh then return nil end
  local line = fh:read('*l')
  fh:close()
  return line
end

local function pinned(name)
  local top, inset = (first_line(M.RUNDIR .. '/' .. name .. '.place') or '')
                       :match('^(%-?%d+) (%-?%d+)$')
  if top then return tonumber(top), tonumber(inset) end
end

local function record(name, top, inset)
  local up = (first_line('/proc/uptime') or ''):match('^(%S+)') or '0'
  os.execute("mkdir -p '" .. M.RUNDIR .. "'")
  local path = M.RUNDIR .. '/' .. name .. '.placed'
  local fh = io.open(path .. '.tmp', 'w')
  if not fh then return end
  fh:write(string.format('%d %d %s\n', top, inset, up))
  fh:close()
  os.rename(path .. '.tmp', path)
end

-- Build the conky.config table for one card, deriving its y position
-- from the heights of every card above it in the stack -- or from the spot
-- bin/reflow.py has pinned it to, if it has.
function M.card(name)
  local top, card, lh = M.TOP, nil, 0
  for _, s in ipairs(M.stack) do
    if s.name == name then card = s break end
    if s.half == 'l' then
      lh = s.h                                  -- row open; y does not move yet
    elseif s.half == 'r' then
      top = top + math.max(lh, s.h) + M.GAP     -- row closes here
      lh = 0
    else
      top = top + s.h + M.GAP
    end
  end
  assert(card, "unknown widget: " .. tostring(name))

  -- Width, and how far this card is indented from the aligned screen edge.
  -- The near column is whichever one hugs that edge, so a right-aligned
  -- stack mirrors correctly without touching the stack table.
  local width, indent = M.WIDTH, 0
  if card.half then
    width = M.HALF_W - M.CHROME
    local near = (M.ALIGN == 'top_right') and 'r' or 'l'
    if card.half ~= near then indent = M.HALF_W + M.GAP end
  end

  local inset = M.SIDE + indent
  local pin_top, pin_inset = pinned(name)
  if pin_top then top, inset = pin_top, pin_inset end
  record(name, top, inset)

  local p = M.pal
  return {
    default_color = p.fg1,
    color0 = p.fg0,    color1 = p.orange, color2 = p.aqua,
    color3 = p.yellow, color4 = p.blue,   color5 = p.fg4,
    color6 = p.green,  color7 = p.red,    color8 = p.bg2,
    color9 = p.purple,

    own_window             = true,
    own_window_type        = 'normal',
    own_window_class       = 'conky',   -- lowercase: matches the picom shadow-exclude
    own_window_title       = 'conky-gruvbox-' .. name,
    own_window_hints       = 'undecorated,below,sticky,skip_taskbar,skip_pager',
    -- conky 1.24 removed own_window_argb_visual / own_window_argb_value /
    -- own_window_transparent: an ARGB visual is now picked automatically
    -- whenever a compositor is running (and conky falls back to an opaque
    -- depth-24 window if none is, so start after picom), and the card's
    -- opacity rides in own_window_colour's alpha channel as #AARRGGBB.
    -- M.ALPHA stays the single knob.
    own_window_colour      = string.format('%02x%s', M.ALPHA, p.bg0h),

    alignment           = M.ALIGN,
    xinerama_head       = M.HEAD,
    gap_x               = inset + M.SHIFT,
    gap_y               = top + M.SHIFT,
    minimum_width       = width,
    maximum_width       = width,
    -- CONKY_MEASURE=1 drops the pinned height so the card shrinks to its
    -- natural size; use it to re-measure after editing a widget's text.
    minimum_height      = (card.auto or os.getenv('CONKY_MEASURE'))
                          and 1 or (card.h - M.CHROME),
    border_inner_margin = M.PAD,
    border_outer_margin = 0,

    use_xft              = true,
    font                 = M.font .. ':size=9',
    xftalpha             = 1.0,
    override_utf8_locale = true,
    draw_shades          = false,
    draw_outline         = false,
    draw_borders         = false,
    use_spacer           = 'none',
    short_units          = true,
    pad_percents         = 2,
    format_human_readable= true,

    draw_graph_borders  = false,
    default_bar_width   = width,
    default_bar_height  = 5,

    background         = true,
    double_buffer      = true,
    update_interval    = card.interval or 1.0,
    total_run_times    = 0,
    no_buffers         = true,
    cpu_avg_samples    = 2,
    net_avg_samples    = 2,
    diskio_avg_samples = 2,
    temperature_unit   = 'celsius',
  }
end

return M
