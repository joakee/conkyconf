--[[
  Loaded ahead of every card's own Lua on Wayland (see wayland/card.lua).

  The drawers in lua/ create their Cairo surface the X11 way:
      cairo_xlib_surface_create(conky_window.display, conky_window.drawable, ...)
  Under Wayland there is no display or drawable; conky hands out the surface
  it draws on through conky_surface() instead. So cairo_xlib is marked loaded
  -- the drawers' `pcall(require, 'cairo_xlib')` then leaves this definition
  alone -- and cairo_xlib_surface_create returns a new reference to that
  surface, which keeps the drawers' cairo_surface_destroy balanced.
--]]

require 'cairo'

package.loaded['cairo_xlib'] = package.loaded['cairo_xlib'] or true

function cairo_xlib_surface_create()
  local cs = conky_surface()
  if cs then cairo_surface_reference(cs) end
  return cs
end

local function rounded(cr, width, height, radius)
  local r = math.min(tonumber(radius) or 0, width / 2, height / 2)
  cairo_new_sub_path(cr)
  cairo_arc(cr, width - r, r, r, -math.pi / 2, 0)
  cairo_arc(cr, width - r, height - r, r, 0, math.pi / 2)
  cairo_arc(cr, r, height - r, r, math.pi / 2, math.pi)
  cairo_arc(cr, r, r, r, math.pi, 3 * math.pi / 2)
  cairo_close_path(cr)
end

local function call_hook(name)
  if name and name ~= '' then
    local hook = _G['conky_' .. name]
    if hook then return hook() end
  end
end

-- The card's colour, as r, g, b (0-1) and alpha (0-1); set by the pre hook.
local card = nil

-- The card itself: a rounded rectangle in the colour and alpha conky's own
-- flat fill had, which picom used to round off. Arguments come from card.lua:
-- colour as rrggbb, alpha 0-255, corner radius, and the widget's own
-- draw_pre hook, if it had one, to run afterwards.
function conky_gruvbox_background(colour, alpha, radius, next_hook)
  if conky_window == nil then return end
  local cs = conky_surface()
  if cs == nil then return end
  local n = tonumber(colour, 16) or 0
  card = { (n >> 16 & 255) / 255, (n >> 8 & 255) / 255, (n & 255) / 255,
           (tonumber(alpha) or 255) / 255, tonumber(radius) or 0 }
  local cr = cairo_create(cs)
  cairo_set_source_rgba(cr, card[1], card[2], card[3], card[4])
  rounded(cr, conky_window.width, conky_window.height, card[5])
  cairo_fill(cr)
  cairo_destroy(cr)
  return call_hook(next_hook)
end

-- Fading the card back while a window has the focus (wayland/bin/stack.py
-- writes the level, 0-1, to LEVEL_FILE and has every card redraw as it
-- changes). Only what is drawn ON the card fades: the background keeps its
-- alpha, because Wayfire's blur goes by alpha -- fade the whole surface, as
-- the X11 cards did, and the wallpaper shows through sharp.
--
-- Done after everything else is drawn: scale the whole surface by the level
-- (DEST_IN), then put back, underneath (DEST_OVER), just enough of the card
-- colour that a pixel with nothing on it ends up exactly as it was. For
-- alpha a and level L that is a(1 - L) / (1 - La); anything drawn on the
-- card comes out as L of itself over the card.
local LEVEL_FILE = (os.getenv('XDG_RUNTIME_DIR') or '/tmp')
                   .. '/conky-gruvbox/wl-level'

local function level()
  local fh = io.open(LEVEL_FILE)
  if not fh then return 1 end
  local l = tonumber(fh:read('*l'))
  fh:close()
  return math.max(0, math.min(1, l or 1))
end

function conky_gruvbox_fade(next_hook)
  call_hook(next_hook)
  if conky_window == nil or card == nil then return end
  local l = level()
  if l >= 1 then return end
  local cs = conky_surface()
  if cs == nil then return end
  local cr = cairo_create(cs)
  cairo_set_operator(cr, CAIRO_OPERATOR_DEST_IN)
  cairo_paint_with_alpha(cr, l)
  local a = card[4]
  cairo_set_operator(cr, CAIRO_OPERATOR_DEST_OVER)
  cairo_set_source_rgba(cr, card[1], card[2], card[3],
                        a * (1 - l) / (1 - l * a))
  rounded(cr, conky_window.width, conky_window.height, card[5])
  cairo_fill(cr)
  cairo_destroy(cr)
end
