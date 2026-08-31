--[[
  Cairo helpers shared by the card drawers (lua/batteries.lua,
  lua/nowplaying.lua). Colours are gruvbox hex strings straight out of
  widget-common.lua's palette, so drawing code never handles raw floats.
--]]

local D = {}

local w = dofile(os.getenv('HOME') .. '/.config/conky/gruvbox-dark/widget-common.lua')

function D.rgba(cr, hex, a)
  local n = tonumber(hex, 16)
  cairo_set_source_rgba(cr,
    math.floor(n / 65536) % 256 / 255,
    math.floor(n / 256) % 256 / 255,
    n % 256 / 255, a or 1)
end

function D.face(cr, px, bold)
  cairo_select_font_face(cr, w.font, CAIRO_FONT_SLANT_NORMAL,
    bold and CAIRO_FONT_WEIGHT_BOLD or CAIRO_FONT_WEIGHT_NORMAL)
  cairo_set_font_size(cr, px)
end

function D.extents(cr, s)
  local e = cairo_text_extents_t:create()
  tolua.takeownership(e)
  cairo_text_extents(cr, s, e)
  return e
end

-- Centred on cx, with the glyphs' visual middle on cy.
function D.centred(cr, s, cx, cy)
  local e = D.extents(cr, s)
  cairo_move_to(cr, cx - e.width / 2 - e.x_bearing, cy - e.height / 2 - e.y_bearing)
  cairo_show_text(cr, s)
  cairo_new_path(cr)
end

-- Left aligned at x, sitting on the given baseline.
function D.left(cr, s, x, baseline)
  cairo_move_to(cr, x, baseline)
  cairo_show_text(cr, s)
  cairo_new_path(cr)
end

function D.right(cr, s, x, baseline)
  local e = D.extents(cr, s)
  cairo_move_to(cr, x - e.width - e.x_bearing, baseline)
  cairo_show_text(cr, s)
  cairo_new_path(cr)
end

-- Trim with an ellipsis until it fits `max` px. Steps back over UTF-8
-- continuation bytes so a multi-byte character is never split in half.
function D.fit(cr, s, max)
  if D.extents(cr, s).width <= max then return s end
  while #s > 1 do
    s = s:sub(1, -2)
    while #s > 1 and s:byte(#s) >= 0x80 and s:byte(#s) < 0xC0 do s = s:sub(1, -2) end
    if D.extents(cr, s .. '…').width <= max then return s .. '…' end
  end
  return s
end

function D.rrect(cr, x, y, width, height, r)
  r = math.min(r, width / 2, height / 2)
  cairo_new_sub_path(cr)
  cairo_arc(cr, x + width - r, y + r,          r, -math.pi / 2, 0)
  cairo_arc(cr, x + width - r, y + height - r, r, 0, math.pi / 2)
  cairo_arc(cr, x + r,         y + height - r, r, math.pi / 2, math.pi)
  cairo_arc(cr, x + r,         y + r,          r, math.pi, 3 * math.pi / 2)
  cairo_close_path(cr)
end

return D
