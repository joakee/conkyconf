--[[
  Ring gauges for the batteries card, in the style of the macOS Batteries
  widget: one ring per device, the device's glyph centred inside it, the
  charge under it and the device name under that. Charging devices get a
  bolt badge on the ring, the way macOS badges a charging device green.

  A device that reports two batteries -- the earbuds of any TWS set -- stays a
  SINGLE item and splits its ring instead: the left half of the ring is the
  left bud and the right half is the right bud, each filling from 12 o'clock
  towards 6 and coloured by its own level. Left maps to left, so the shape
  says which is which without a second label.

  Device data comes from bin/batteries.py via a Lua table literal it drops in
  $XDG_RUNTIME_DIR -- see that script for why. This file only draws.

  conky calls conky_batteries_draw() from lua_draw_hook_post, i.e. after the
  card's own text is on screen, so the header drawn by conky and the rings
  drawn here land in the same window.
--]]

require 'cairo'
-- conky >= 1.12 moved cairo_xlib_surface_create into its own module.
pcall(require, 'cairo_xlib')

local HOME = os.getenv('HOME')
local w = dofile(HOME .. '/.config/conky/gruvbox-dark/widget-common.lua')
local D = dofile(HOME .. '/.config/conky/gruvbox-dark/lua/draw.lua')
local CACHE = (os.getenv('XDG_RUNTIME_DIR') or '/tmp')
              .. '/conky-gruvbox/batteries.lua'

-- Must match the layout constants at the top of bin/batteries.py: that script
-- reserves the vertical space these numbers consume.
local COLS    = 3
local RING_D  = 52
local STROKE  = 6
local ROW_GAP = 10
local LABEL_H = 26
local ROW_H   = RING_D + LABEL_H

-- Distance from the top of the window to the top of the first ring. The card's
-- inner margin, plus the height of the header line conky drew above us, plus
-- the HEADER_H gap the script reserved. Measured on this setup at size 10.
local HEAD_LINE = 18
local Y0 = w.PAD + HEAD_LINE + 6

-- Clear space at 12 and 6 o'clock, in radians, that separates the two halves
-- of a split ring so the pair reads as two gauges even at full charge.
local SPLIT_GAP = 0.10
local TOP = -math.pi / 2

local GLYPH_PX = 21
local PCT_PX   = 12
local NAME_PX  = 9

-- Mark for a device whose reading arrived over the network rather than a
-- cable. F05A9 (mdi-wifi) was chosen by RENDERING it in Maple Mono NF CN at
-- NAME_PX, per the warning in bin/batteries.py, and it stays legible there.
-- The check is not ceremony: F0EBE, two codepoints away in the same block,
-- draws a pair of dividers in this font, and F1EB -- the other clean wifi
-- mark -- is a Font Awesome codepoint rather than the Material Design block
-- every other glyph on this card comes from.
local WIFI_GLYPH = '\u{F05A9}'
local WIFI_GAP   = 3

-- gruvbox colour for a charge. Devices that only report a coarse bucket are
-- coloured by the bucket, so a mouse reporting "normal" is not painted amber
-- just because UPower turned that bucket into the number 55.
local function colour(d, pct)
  pct = pct or d.pct
  local p = w.pal
  if d.charging then return p.aqua end
  if d.coarse then
    if d.level == 4 then return p.red end       -- critical
    if d.level == 3 then return p.orange end    -- low
    return p.green                              -- normal / high / full
  end
  if pct > 50 then return p.green end
  if pct > 25 then return p.yellow end
  if pct > 12 then return p.orange end
  return p.red
end

-- One half of a split ring. side = 1 is the right half, sweeping clockwise
-- from 12 o'clock; side = -1 is the left half, sweeping anticlockwise. A nil
-- frac draws only the track, which is how a bud sitting in the case shows up.
local function half_ring(cr, cx, cy, r, side, frac, fill)
  local span = math.pi - 2 * SPLIT_GAP
  local a0 = TOP + side * SPLIT_GAP
  local sweep = function(a1)
    if side < 0 then cairo_arc_negative(cr, cx, cy, r, a0, a1)
    else cairo_arc(cr, cx, cy, r, a0, a1) end
    cairo_stroke(cr)
  end
  D.rgba(cr, w.pal.bg2, 0.6)
  sweep(a0 + side * span)
  if frac and frac > 0.004 then
    D.rgba(cr, fill, 1)
    sweep(a0 + side * span * math.min(frac, 1))
  end
end

-- ── one device ────────────────────────────────────────────────
local function draw_device(cr, d, cx, top, cell)
  local r  = (RING_D - STROKE) / 2
  local cy = top + RING_D / 2
  local p  = w.pal

  cairo_set_line_width(cr, STROKE)
  cairo_set_line_cap(cr, CAIRO_LINE_CAP_ROUND)

  local split = (d.pct_l ~= nil) or (d.pct_r ~= nil)
  if split then
    half_ring(cr, cx, cy, r, -1, d.pct_l and d.pct_l / 100, colour(d, d.pct_l))
    half_ring(cr, cx, cy, r,  1, d.pct_r and d.pct_r / 100, colour(d, d.pct_r))
  else
    D.rgba(cr, p.bg2, 0.6)
    cairo_arc(cr, cx, cy, r, 0, 2 * math.pi)
    cairo_stroke(cr)

    local frac = math.min(math.max(d.pct / 100, 0), 1)
    if frac > 0.001 then
      D.rgba(cr, colour(d), 1)
      -- from 12 o'clock, clockwise, like the macOS ring
      cairo_arc(cr, cx, cy, r, TOP, TOP + 2 * math.pi * frac)
      cairo_stroke(cr)
    end
  end

  D.face(cr, GLYPH_PX, false)
  D.rgba(cr, p.fg1, 0.92)
  D.centred(cr, d.glyph, cx, cy)

  -- Charging badge: a filled disc in the card's own background so the ring
  -- reads as interrupted behind it, then the bolt on top.
  if d.charging then
    local bx, by = cx + r * 0.74, cy + r * 0.74
    D.rgba(cr, p.bg0h, 1)
    cairo_arc(cr, bx, by, 8, 0, 2 * math.pi)
    cairo_fill(cr)
    D.face(cr, 12, false)
    D.rgba(cr, d.full and p.green or p.aqua, 1)
    D.centred(cr, '\u{F140B}', bx, by)   -- lightning-bolt
  end

  local label
  if split then
    -- Both numbers stay visible; they collapse to one when the buds agree,
    -- which they usually do.
    if d.pct_l and d.pct_r and d.pct_l ~= d.pct_r then
      label = d.pct_l .. '/' .. d.pct_r .. '%'
    else
      label = (d.pct_l or d.pct_r) .. '%'
    end
  else
    label = (d.coarse and '~' or '') .. d.pct .. '%'
  end
  D.face(cr, PCT_PX, true)
  D.rgba(cr, p.fg0, 1)
  D.centred(cr, label, cx, top + RING_D + 9)

  -- Name, preceded by the wifi mark when the reading came in over the network.
  -- Without it an unplugged phone is indistinguishable from a ring that has
  -- silently gone stale, which is the one thing this card must not be.
  D.face(cr, NAME_PX, false)
  local name_y = top + RING_D + 21
  if d.network then
    local mark_w = D.extents(cr, WIFI_GLYPH).width
    local name   = D.fit(cr, d.name, cell - 6 - mark_w - WIFI_GAP)
    local e      = D.extents(cr, name)
    -- Baseline is taken from the name alone, so the text sits on exactly the
    -- line it would without the mark and the glyph aligns to it rather than
    -- dragging it around.
    local base   = name_y - e.height / 2 - e.y_bearing
    local x      = cx - (mark_w + WIFI_GAP + e.width) / 2
    D.rgba(cr, p.blue, 1)
    D.left(cr, WIFI_GLYPH, x, base)
    D.rgba(cr, p.fg4, 1)
    D.left(cr, name, x + mark_w + WIFI_GAP, base)
  else
    D.rgba(cr, p.fg4, 1)
    D.centred(cr, D.fit(cr, d.name, cell - 6), cx, name_y)
  end
end

-- ── entry point ───────────────────────────────────────────────
function conky_batteries_draw()
  if conky_window == nil then return end

  local ok, devices = pcall(dofile, CACHE)
  if not ok or type(devices) ~= 'table' or #devices == 0 then return end

  local cs = cairo_xlib_surface_create(conky_window.display, conky_window.drawable,
                                       conky_window.visual,
                                       conky_window.width, conky_window.height)
  local cr = cairo_create(cs)

  -- Fewer than COLS devices spread across the full width rather than huddling
  -- at the left edge, which is what keeps a one- or two-device card balanced.
  local cols = math.min(#devices, COLS)
  local cell = w.WIDTH / cols

  for i, d in ipairs(devices) do
    local col = (i - 1) % cols
    local row = math.floor((i - 1) / cols)
    draw_device(cr, d,
                w.PAD + cell * (col + 0.5),
                Y0 + row * (ROW_H + ROW_GAP),
                cell)
  end

  cairo_destroy(cr)
  cairo_surface_destroy(cs)
  collectgarbage()
end
