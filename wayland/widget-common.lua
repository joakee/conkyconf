--[[
  gruvbox-dark conky widgets -- the Wayland half of widget-common.lua.

  Everything that is not about the window -- palette, geometry, the stack
  table, pinned positions -- is the X11 file's, loaded as-is, so there is one
  copy of it to edit. Only M.card's output is adjusted, for conky's Wayland
  backend:

  * out_to_wayland. Each card is a wlr-layer-shell surface on the BOTTOM
    layer -- conky picks that from the `below` hint the X11 config already
    sets -- which is the Wayland spelling of "under every window, on every
    workspace, not in the taskbar or pager".
  * own_window_namespace names the surface conky-gruvbox-<name>@<output>:
    the X11 window title, so bin/stack.py can find each card through
    Wayfire's IPC, plus the monitor it belongs on. Conky has no setting for
    that under Wayland -- layer surfaces open on whichever output has the
    focus -- so the Wayfire plugin layer-output reads it off the namespace
    (source: ~/gitclones/wayfire-layer-output). The output is M.OUTPUT, or
    $CONKY_HEAD_OUTPUT as with bin/head.py; Wayland calls outputs by the same
    connector names XRandR does.
  * gap_x/gap_y become layer-shell margins, which place the SURFACE's
    corner. X11 conky placed the text there and the window M.SHIFT up and to
    the left of it, so the shift comes off here to land the card where it was.
  * There is no picom to round the corners, so the card's background is not
    conky's flat fill any more but a rounded rectangle drawn in Cairo (see
    lua/compat.lua), in the same colour and alpha.
  * xinerama_head goes, for the namespace above. So do the X11-only settings,
    and the two conky 1.25 renamed are given their new names.

  Loaded in place of widget-common.lua by wayland/card.lua, which is what
  each widget's own `dofile(... widget-common.lua)` ends up reaching.
--]]

local DIR = os.getenv('HOME') .. '/.config/conky/gruvbox-dark'

-- loadfile, not dofile: card.lua points dofile of the X11 file back here.
local M = assert(loadfile(DIR .. '/widget-common.lua'))()

M.RADIUS = 10   -- corner radius, as picom's corner-radius was
M.WL_OUTPUT = os.getenv('CONKY_HEAD_OUTPUT') or M.OUTPUT

local x11_card = M.card

function M.card(name)
  local c = x11_card(name)

  c.output_backend       = 'wayland'
  c.own_window_namespace = 'conky-gruvbox-' .. name .. '@' .. M.WL_OUTPUT
  c.xinerama_head        = nil
  c.own_window_class     = nil
  c.use_xft              = nil
  c.double_buffer        = nil
  c.text_alpha, c.xftalpha = c.xftalpha, nil

  c.gap_x = c.gap_x - M.SHIFT
  c.gap_y = c.gap_y - M.SHIFT

  -- Fully transparent fill; lua/compat.lua paints the rounded card instead
  -- (card.lua hooks it in, once the widget has set its own Lua up).
  c.own_window_colour = nil
  c.own_window_color  = '00' .. M.pal.bg0h
  return c
end

return M
