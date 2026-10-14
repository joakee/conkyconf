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

local RUNDIR = (os.getenv('XDG_RUNTIME_DIR') or '/tmp') .. '/conky-gruvbox'

-- The card: colour r, g, b and alpha (0-1), corner radius, and its frosted
-- backdrop if there is one. Set by the pre hook.
local card = nil

-- The piece of blurred wallpaper behind this card, which wayland/bin/stack.py
-- writes to <card>.backdrop.png; <card>.backdrop is a stamp it changes
-- whenever it writes a new one. nil until there is one.
local backdrop = { stamp = false, img = nil }

local function load_backdrop(name)
  local fh = io.open(RUNDIR .. '/' .. name .. '.backdrop')
  local stamp = fh and fh:read('*l')
  if fh then fh:close() end
  if stamp ~= backdrop.stamp then
    if backdrop.img then cairo_surface_destroy(backdrop.img) end
    backdrop.stamp, backdrop.img = stamp, nil
    if stamp then
      local img = cairo_image_surface_create_from_png(
        RUNDIR .. '/' .. name .. '.backdrop.png')
      if cairo_surface_status(img) == 0 then
        backdrop.img = img
      else
        cairo_surface_destroy(img)
      end
    end
  end
  return backdrop.img
end

-- The card itself: its piece of blurred wallpaper, if it has one, under a
-- rounded rectangle in the colour and alpha conky's own flat fill had (picom
-- used to round that off, and Wayfire to blur behind it).
local function paint_card(cr, width, height)
  cairo_save(cr)
  rounded(cr, width, height, card.radius)
  cairo_clip(cr)
  if card.img then
    cairo_set_source_surface(cr, card.img, 0, 0)
    cairo_paint(cr)
  end
  cairo_set_source_rgba(cr, card.r, card.g, card.b, card.a)
  cairo_paint(cr)
  cairo_restore(cr)
end

-- The pre hook. Arguments come from card.lua: colour as rrggbb, alpha 0-255,
-- corner radius, the card's name, and the widget's own draw_pre hook, if it
-- had one, to run afterwards.
function conky_gruvbox_background(colour, alpha, radius, name, next_hook)
  if conky_window == nil then return end
  local cs = conky_surface()
  if cs == nil then return end
  local n = tonumber(colour, 16) or 0
  card = { r = (n >> 16 & 255) / 255, g = (n >> 8 & 255) / 255,
           b = (n & 255) / 255, a = (tonumber(alpha) or 255) / 255,
           radius = tonumber(radius) or 0, img = load_backdrop(name) }
  local cr = cairo_create(cs)
  paint_card(cr, conky_window.width, conky_window.height)
  cairo_destroy(cr)
  return call_hook(next_hook)
end

-- Fading the card back while a window has the focus (wayland/bin/stack.py
-- writes the level, 0-1, to LEVEL_FILE and has every card redraw as it
-- changes): the card turns more see-through, as the X11 cards did, but what
-- shows through stays frosted.
--
-- Done after everything else is drawn: scale the whole surface by the level
-- (DEST_IN), then put back underneath it (DEST_OVER) what the card is in
-- front of. With a backdrop that is the blurred wallpaper, so for level L
-- every pixel ends up L of the card over 1 - L of the frosted wallpaper.
-- Without one, the compositor shows the wallpaper through, unblurred; there
-- only the content fades, into just enough of the card colour that a pixel
-- with nothing on it ends up as it was: for alpha a, a(1 - L) / (1 - La).
local LEVEL_FILE = RUNDIR .. '/wl-level'

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
  local width, height = conky_window.width, conky_window.height
  local cr = cairo_create(cs)
  cairo_set_operator(cr, CAIRO_OPERATOR_DEST_IN)
  cairo_paint_with_alpha(cr, l)
  if card.img then
    cairo_set_operator(cr, CAIRO_OPERATOR_DEST_OVER)
    rounded(cr, width, height, card.radius)
    cairo_clip(cr)
    cairo_set_source_surface(cr, card.img, 0, 0)
    cairo_paint(cr)
  else
    local a = card.a
    cairo_set_operator(cr, CAIRO_OPERATOR_DEST_OVER)
    cairo_set_source_rgba(cr, card.r, card.g, card.b, a * (1 - l) / (1 - l * a))
    rounded(cr, width, height, card.radius)
    cairo_fill(cr)
  end
  cairo_destroy(cr)
end
