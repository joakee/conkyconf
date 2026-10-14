--[[
  Weather card: click the refresh icon (top right, before the observation
  time) to pull fresh data from BOM now instead of waiting out the cache TTL.

  The fetch runs detached in bin/weather.py --refresh, so the conky process
  never blocks on the network. That script's lock is what the render reads to
  turn the icon into a yellow sync glyph while it runs, red if it failed.

  Also draws the 24 h temperature and rainfall graphs at the bottom of the
  card, from lua_draw_hook_post. bin/weather.py decides which graphs show (no
  rain graph when no rain is forecast), reserves their space in the text and
  drops the series in $XDG_RUNTIME_DIR; this file only draws them.
--]]

require 'cairo'
-- conky >= 1.12 moved cairo_xlib_surface_create into its own module.
pcall(require, 'cairo_xlib')

local HOME   = os.getenv('HOME')
local SCRIPT = HOME .. '/.config/conky/gruvbox-dark/bin/weather.py'
local w = dofile(HOME .. '/.config/conky/gruvbox-dark/widget-common.lua')
local D = dofile(HOME .. '/.config/conky/gruvbox-dark/lua/draw.lua')
local GRAPHS = (os.getenv('XDG_RUNTIME_DIR') or '/tmp')
               .. '/conky-gruvbox/weather-graphs.lua'

-- Header geometry, in window pixels. The text box starts PAD+1 px inside the
-- window on both axes (see widget-common.lua). The stamp "<icon> HH:MM" is
-- right-aligned at size 8: measured 42 px wide, the icon its first 6 px, at
-- 96 dpi. The hit box pads the icon out to a target a finger-ish cursor can
-- hit without also swallowing the time.
local INSET    = 14 + 1
local STAMP_W  = 42
local ICON_W   = 6
local SLOP     = 6
local HEADER_H = 22   -- the size-10 header line

local function hit(x, y, w)
  local left = w - INSET - STAMP_W
  return x >= left - SLOP and x <= left + ICON_W + SLOP
     and y >= 0 and y <= INSET + HEADER_H
end

function conky_weather_mouse(ev)
  if ev.type ~= 'button_down' or ev.button ~= 'left' then return false end
  local w = (conky_window and conky_window.width) or 321
  if not hit(ev.x, ev.y, w) then return false end
  -- setsid + discarded output: detached, so neither this hook nor conky's
  -- main loop waits on BOM.
  os.execute('setsid -f ' .. SCRIPT .. ' --refresh >/dev/null 2>&1 </dev/null')
  return true
end

-- ── 24 h graphs ───────────────────────────────────────────────
-- Must match the graph layout constants in bin/weather.py, which reserves the
-- space these consume at the bottom of the card.
local GRAPH_GAP = 8
local LABEL_H   = 13
local PLOT_H    = 24
local GRAPH_H   = GRAPH_GAP + LABEL_H + PLOT_H
local LABEL_PX  = 10
local LINE_W    = 1.6

local STYLE = {
  temp = { name = 'temp · 24h', colour = 'yellow' },
  rain = { name = 'rain · 24h', colour = 'blue' },
}

local function graph(cr, g, x, top, width)
  local p, st = w.pal, STYLE[g.kind]
  local v = g.v
  if not st or #v < 2 then return end

  D.face(cr, LABEL_PX, false)
  D.rgba(cr, p.fg4, 1)
  D.left(cr, st.name, x, top + LABEL_H - 3)
  D.rgba(cr, p.fg1, 1)
  D.right(cr, g.label, x + width, top + LABEL_H - 3)

  -- Rain is scaled from zero, and to at least 1 mm/h, so a passing drizzle
  -- reads as a drizzle rather than filling the plot. Temperature is scaled
  -- to its own range: the label carries the numbers.
  local lo, hi = math.huge, -math.huge
  for _, n in ipairs(v) do lo, hi = math.min(lo, n), math.max(hi, n) end
  if g.kind == 'rain' then lo, hi = 0, math.max(hi, 1) end
  if hi - lo < 1 then lo, hi = lo - 0.5, hi + 0.5 end

  local y0, y1 = top + LABEL_H + PLOT_H - 1, top + LABEL_H + 2
  local function px(i) return x + (i - 1) * width / (#v - 1) end
  local function py(n) return y0 - (n - lo) / (hi - lo) * (y0 - y1) end

  -- Faint guides every 6 hours, so the shape can be read against the clock.
  cairo_set_line_width(cr, 1)
  D.rgba(cr, p.bg2, 0.7)
  for i = 7, #v - 1, 6 do
    cairo_move_to(cr, math.floor(px(i)) + 0.5, y1)
    cairo_line_to(cr, math.floor(px(i)) + 0.5, y0)
  end
  cairo_move_to(cr, x, y0 + 0.5)
  cairo_line_to(cr, x + width, y0 + 0.5)
  cairo_stroke(cr)

  cairo_move_to(cr, px(1), py(v[1]))
  for i = 2, #v do cairo_line_to(cr, px(i), py(v[i])) end
  local line = cairo_copy_path(cr)
  cairo_line_to(cr, px(#v), y0)
  cairo_line_to(cr, px(1), y0)
  cairo_close_path(cr)
  D.rgba(cr, p[st.colour], 0.18)
  cairo_fill(cr)

  cairo_append_path(cr, line)
  cairo_path_destroy(line)
  cairo_set_line_width(cr, LINE_W)
  cairo_set_line_join(cr, CAIRO_LINE_JOIN_ROUND)
  cairo_set_line_cap(cr, CAIRO_LINE_CAP_ROUND)
  D.rgba(cr, p[st.colour], 1)
  cairo_stroke(cr)
end

function conky_weather_draw()
  if conky_window == nil then return end
  local ok, gs = pcall(dofile, GRAPHS)
  if not ok or type(gs) ~= 'table' or #gs == 0 then return end

  local cs = cairo_xlib_surface_create(conky_window.display, conky_window.drawable,
                                       conky_window.visual,
                                       conky_window.width, conky_window.height)
  local cr = cairo_create(cs)

  -- Stacked up from the bottom of the content box, which is where the
  -- reservation bin/weather.py printed ends up, whatever the text above it.
  local bottom = conky_window.height - (w.PAD + 1)
  local top = bottom - #gs * GRAPH_H
  for i, g in ipairs(gs) do
    graph(cr, g, w.PAD + 1, top + (i - 1) * GRAPH_H + GRAPH_GAP, w.WIDTH)
  end

  cairo_destroy(cr)
  cairo_surface_destroy(cs)
  collectgarbage()
end
