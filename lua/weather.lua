--[[
  Weather card: click the refresh icon (top right, before the observation
  time) to pull fresh data from BOM now instead of waiting out the cache TTL.

  The fetch runs detached in bin/weather.py --refresh, so the conky process
  never blocks on the network. That script's lock is what the render reads to
  turn the icon into a yellow sync glyph while it runs, red if it failed.
  The card renders every 30 s (lua/exec.lua), so a click also switches it to
  every second for long enough to see the refresh through.

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
local E = dofile(HOME .. '/.config/conky/gruvbox-dark/lua/exec.lua')
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
  -- Render every second until the fetch (20 s timeout) has had time to land,
  -- so the icon shows it running and the new reading appears at once.
  E.hot(25)
  return true
end

-- ── 24 h graphs ───────────────────────────────────────────────
-- Must match the graph layout constants in bin/weather.py, which reserves the
-- space these consume at the bottom of the card.
local GRAPH_GAP = 8
local LABEL_H   = 13
local PLOT_H    = 24
local GRAPH_H   = GRAPH_GAP + LABEL_H + PLOT_H
local AXIS_H    = 16
local TICK      = 2   -- every hour
local TICK_MAJOR = 4  -- the labelled ones
local LABEL_EVERY = 2 -- hours between time labels
local LABEL_PX  = 10
local LINE_W    = 1.6
-- Room kept clear around a label where the hour lines pass behind it.
local KNOCKOUT  = 2
-- The temperature block: one column per hour, the hour over its forecast,
-- like the iOS weather app. Twelve is as many as fit the card legibly.
local HOURS     = 12
-- Extra space under it when a graph follows (TEMP_AFTER in bin/weather.py).
local TEMP_AFTER = 8

local STYLE = {
  temp = { name = 'temp · 12h', colour = 'yellow' },
  rain = { name = 'rain · 24h', colour = 'blue' },
}

-- Where one graph's parts sit: its label baseline, and the top (y1) and
-- baseline (y0) of its plot.
local function frame(top)
  return { base = top + LABEL_H - 3,
           y1 = top + LABEL_H + 2,
           y0 = top + LABEL_H + PLOT_H - 1 }
end

-- Both labels of a graph, with the boxes their ink covers.
local function labels(cr, g, f, x, width)
  D.face(cr, LABEL_PX, false)
  local out = {}
  for _, l in ipairs({ { STYLE[g.kind].name, 'left' }, { g.label, 'right' } }) do
    local e = D.extents(cr, l[1])
    local lx = l[2] == 'left' and x or x + width - e.width - e.x_bearing
    out[#out + 1] = { text = l[1], x = lx, base = f.base, colour = l[2] == 'left' and 'fg4' or 'fg1',
                      box = { lx + e.x_bearing, f.base + e.y_bearing, e.width, e.height } }
  end
  return out
end

-- One vertical line per hour, from the top plot to the bottom one -- through
-- the gap between them, so the graphs read as one chart on one time axis --
-- with the line at every 6th hour a little stronger. The labels in that gap
-- are clipped out, so no line runs through text.
local function grid(cr, n, x, width, ytop, ybot, holes)
  cairo_save(cr)
  cairo_set_fill_rule(cr, CAIRO_FILL_RULE_EVEN_ODD)
  cairo_rectangle(cr, x - 1, ytop - 1, width + 2, ybot - ytop + 2)
  for _, b in ipairs(holes) do
    cairo_rectangle(cr, b[1] - KNOCKOUT, b[2] - KNOCKOUT,
                    b[3] + 2 * KNOCKOUT, b[4] + 2 * KNOCKOUT)
  end
  cairo_clip(cr)
  cairo_set_line_width(cr, 1)
  for major = 0, 1 do
    D.rgba(cr, w.pal.bg2, major == 1 and 0.9 or 0.45)
    for i = 1, n do
      if ((i - 1) % 6 == 0) == (major == 1) then
        local tx = math.floor(x + (i - 1) * width / (n - 1)) + 0.5
        cairo_move_to(cr, tx, ytop)
        cairo_line_to(cr, tx, ybot)
      end
    end
    cairo_stroke(cr)
  end
  cairo_restore(cr)
end

-- Hour ticks under the bottom graph's baseline, and the hour under every
-- LABEL_EVERY-th. Only the bottom graph has them: the graphs share the axis.
local function axis(cr, n, t0, x, base, width)
  local p = w.pal
  cairo_set_line_width(cr, 1)
  D.rgba(cr, p.fg4, 0.8)
  for i = 1, n do
    local tx = math.floor(x + (i - 1) * width / (n - 1)) + 0.5
    cairo_move_to(cr, tx, base)
    cairo_line_to(cr, tx,
                  base + ((i - 1) % LABEL_EVERY == 0 and TICK_MAJOR or TICK))
  end
  cairo_stroke(cr)
  if not t0 then return end

  D.face(cr, LABEL_PX, false)
  D.rgba(cr, p.fg4, 1)
  local y = base + AXIS_H - 1
  for i = 1, n, LABEL_EVERY do
    -- Centred on its tick, even at the ends: two digits overhang the plot by
    -- less than the card's padding.
    local s = os.date('%H', t0 + (i - 1) * 3600)
    local tx = x + (i - 1) * width / (n - 1)
    local e = D.extents(cr, s)
    D.left(cr, s, tx - e.width / 2 - e.x_bearing, y)
  end
end

-- The value range a graph is plotted over. Rain is scaled from zero, and to
-- at least 1 mm/h, so a passing drizzle reads as a drizzle rather than filling
-- the plot.
local function range(g)
  local lo, hi = math.huge, -math.huge
  for _, n in ipairs(g.v) do lo, hi = math.min(lo, n), math.max(hi, n) end
  if g.kind == 'rain' then lo, hi = 0, math.max(hi, 1) end
  if hi - lo < 1 then lo, hi = lo - 0.5, hi + 0.5 end
  return lo, hi
end

-- Colour for a temperature, blended between gruvbox stops. Fixed in degrees
-- rather than scaled to the day's range, so a colour always means the same
-- thing: blue is cold, green mild, yellow warm, red hot.
local TEMP_STOPS = {
  { 0,  'blue' }, { 10, 'aqua' }, { 18, 'green' },
  { 24, 'yellow' }, { 30, 'orange' }, { 36, 'red' },
}

local function rgb(hex)
  local n = tonumber(hex, 16)
  return math.floor(n / 65536) % 256, math.floor(n / 256) % 256, n % 256
end

local function temp_colour(cr, t)
  local s = TEMP_STOPS
  local hex
  if t <= s[1][1] then hex = w.pal[s[1][2]]
  elseif t >= s[#s][1] then hex = w.pal[s[#s][2]]
  else
    for i = 2, #s do
      if t <= s[i][1] then
        local f = (t - s[i - 1][1]) / (s[i][1] - s[i - 1][1])
        local r0, g0, b0 = rgb(w.pal[s[i - 1][2]])
        local r1, g1, b1 = rgb(w.pal[s[i][2]])
        hex = string.format('%02x%02x%02x',
          math.floor(r0 + (r1 - r0) * f + 0.5),
          math.floor(g0 + (g1 - g0) * f + 0.5),
          math.floor(b0 + (b1 - b0) * f + 0.5))
        break
      end
    end
  end
  D.rgba(cr, hex, 1)
end

-- The temperature block: the hour (in the card's dim label colour) over the
-- forecast for it, in HOURS evenly spaced columns across the content width.
-- The horizontal span the temperature row covers, for a typical two-digit
-- reading: from the first column's number to the last one's degree sign. The
-- rain graph is fitted to it so the two line up. Taken from a template rather
-- than the day's readings, so the graph does not change width with them.
local function hours_span(cr, x, width)
  D.face(cr, LABEL_PX, false)
  local col = width / HOURS
  local shift = D.extents(cr, '°').x_advance / 2
  local num, full = D.extents(cr, '00'), D.extents(cr, '00°')
  local first = x + 0.5 * col - shift
  local last = x + (HOURS - 0.5) * col - shift
  return first - num.width / 2,
         last - num.width / 2 - num.x_bearing + full.x_bearing + full.width
end

local function hours(cr, g, f, x, width, t0)
  local p = w.pal
  local n = math.min(HOURS, #g.v)
  local col = width / n
  local hour_base = f.y1 + 8
  D.face(cr, LABEL_PX, false)
  -- Each number sits centred under its hour, with the degree sign hanging off
  -- to its right. Left at that, the whole block would sit off centre by half
  -- a degree sign, so both rows are shifted back by that much.
  local shift = D.extents(cr, '°').x_advance / 2
  for i = 1, n do
    local cx = x + (i - 0.5) * col - shift
    local hr = t0 and os.date('%H', t0 + (i - 1) * 3600) or ''
    local he = D.extents(cr, hr)
    D.rgba(cr, p.fg4, 1)
    D.left(cr, hr, cx - he.width / 2 - he.x_bearing, hour_base)
    local t = string.format('%.0f°', g.v[i])
    -- Centre the digits, not the digits plus the degree sign, so a column's
    -- number sits under its hour.
    local num = D.extents(cr, string.format('%.0f', g.v[i]))
    temp_colour(cr, g.v[i])
    D.left(cr, t, cx - num.width / 2 - num.x_bearing, f.y0)
  end
end

local function curve(cr, g, f, x, width)
  local p, v = w.pal, g.v
  local colour = p[STYLE[g.kind].colour]
  local lo, hi = range(g)

  local function px(i) return x + (i - 1) * width / (#v - 1) end
  local function py(n) return f.y0 - (n - lo) / (hi - lo) * (f.y0 - f.y1) end

  cairo_set_line_width(cr, 1)
  D.rgba(cr, p.bg2, 0.9)
  cairo_move_to(cr, x, f.y0 + 0.5)
  cairo_line_to(cr, x + width, f.y0 + 0.5)
  cairo_stroke(cr)

  cairo_move_to(cr, px(1), py(v[1]))
  for i = 2, #v do cairo_line_to(cr, px(i), py(v[i])) end
  local line = cairo_copy_path(cr)
  cairo_line_to(cr, px(#v), f.y0)
  cairo_line_to(cr, px(1), f.y0)
  cairo_close_path(cr)
  D.rgba(cr, colour, 0.18)
  cairo_fill(cr)

  cairo_append_path(cr, line)
  cairo_path_destroy(line)
  cairo_set_line_width(cr, LINE_W)
  cairo_set_line_join(cr, CAIRO_LINE_JOIN_ROUND)
  cairo_set_line_cap(cr, CAIRO_LINE_CAP_ROUND)
  D.rgba(cr, colour, 1)
  cairo_stroke(cr)
  cairo_set_line_cap(cr, CAIRO_LINE_CAP_BUTT)
end

function conky_weather_draw()
  if conky_window == nil then return end
  local ok, gs = pcall(dofile, GRAPHS)
  if not ok or type(gs) ~= 'table' or #gs == 0 then return end
  for _, g in ipairs(gs) do
    if not STYLE[g.kind] or #g.v < 2 then return end
  end

  local cs = cairo_xlib_surface_create(conky_window.display, conky_window.drawable,
                                       conky_window.visual,
                                       conky_window.width, conky_window.height)
  local cr = cairo_create(cs)

  -- Stacked up from the bottom of the content box, which is where the
  -- reservation bin/weather.py printed ends up, whatever the text above it.
  -- The time axis under the curves is only there when there is a curve: the
  -- temperature block labels its own hours.
  local x, width = w.PAD + 1, w.WIDTH
  local curves = {}
  for i, g in ipairs(gs) do
    if g.kind ~= 'temp' then curves[#curves + 1] = i end
  end
  local heights, total = {}, 0
  for i, g in ipairs(gs) do
    heights[i] = GRAPH_H + ((g.kind == 'temp' and i < #gs) and TEMP_AFTER or 0)
    total = total + heights[i]
  end
  local top = conky_window.height - (w.PAD + 1) - total
              - (#curves > 0 and AXIS_H or 0)
  local frames, texts, holes = {}, {}, {}
  for i, g in ipairs(gs) do
    frames[i] = frame(top + GRAPH_GAP)
    top = top + heights[i]
    for _, l in ipairs(labels(cr, g, frames[i], x, width)) do
      texts[#texts + 1] = l
      -- only labels between the first curve and the last are crossed by grid
      if #curves > 1 and i > curves[1] then holes[#holes + 1] = l.box end
    end
  end

  if #curves > 0 then
    -- The plots match the temperature row's width when it is shown, rather
    -- than running the full content width beneath it.
    local px, pw = x, width
    if #curves < #gs then
      local l, r = hours_span(cr, x, width)
      px, pw = math.floor(l + 0.5), math.floor(r - l + 0.5)
    end
    local first, last = frames[curves[1]], frames[curves[#curves]]
    local n = #gs[curves[1]].v
    grid(cr, n, px, pw, first.y1, last.y0, holes)
    for _, i in ipairs(curves) do curve(cr, gs[i], frames[i], px, pw) end
    axis(cr, n, gs.t0, px, last.y0 + 1, pw)
  end
  for i, g in ipairs(gs) do
    if g.kind == 'temp' then hours(cr, g, frames[i], x, width, gs.t0) end
  end

  D.face(cr, LABEL_PX, false)
  for _, l in ipairs(texts) do
    D.rgba(cr, w.pal[l.colour], 1)
    D.left(cr, l.text, l.x, l.base)
  end

  cairo_destroy(cr)
  cairo_surface_destroy(cs)
  collectgarbage()
end
