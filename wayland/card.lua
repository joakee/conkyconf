--[[
  One card of the stack, run on Wayland:

    CONKY_GRUVBOX_CARD=30-cpu.conf conky -c .../wayland/card.lua

  The widget files in widgets/ stay exactly as they are for X11. This runs the
  named one, with three things swapped underneath it:

  * its `dofile(... widget-common.lua)` gets wayland/widget-common.lua, which
    is the X11 file with M.card's output adjusted for Wayland;
  * lua/compat.lua is loaded ahead of the widget's own Lua script. It gives the
    Cairo drawers (written against cairo_xlib) conky's Wayland surface,
    paints the rounded card background (the draw_pre hook), and fades the
    card back while a window has the focus (the draw_post hook);
  * the scripts in bin/ that the card's text runs go through wayland/bin/run.
--]]

local DIR  = os.getenv('HOME') .. '/.config/conky/gruvbox-dark'
local file = os.getenv('CONKY_GRUVBOX_CARD')
assert(file, 'card.lua: set CONKY_GRUVBOX_CARD to a file in widgets/')

local x11_dofile, w = dofile, nil
dofile = function(path, ...)
  if path == DIR .. '/widget-common.lua' then
    w = x11_dofile(DIR .. '/wayland/widget-common.lua')
    return w
  end
  return x11_dofile(path, ...)
end
local ok, err = pcall(x11_dofile, DIR .. '/widgets/' .. file)
dofile = x11_dofile
assert(ok, err)
assert(w, 'card.lua: ' .. file .. ' did not load widget-common.lua')

-- The card's data scripts run through wayland/bin/run, which gives them the
-- Wayland cardvis (a card hiding itself) in place of the X11 one.
if conky.text then
  conky.text = conky.text:gsub('(gruvbox%-dark/)bin/', '%1wayland/bin/run ')
end

local c = conky.config
local compat = DIR .. '/wayland/lua/compat.lua'
c.lua_load = c.lua_load and (compat .. ';' .. c.lua_load) or compat
-- The background goes under everything, so it is the draw_pre hook, and the
-- fade (see compat) over everything, so it is the draw_post hook. A widget's
-- own hooks are passed on, and called from them: the pre straight after the
-- background, the post before the fade.
c.lua_draw_hook_pre = string.format('gruvbox_background %s %d %d %s',
  w.pal.bg0h, w.ALPHA, w.RADIUS,
  (c.lua_draw_hook_pre or ''):gsub('^conky_', ''))
c.lua_draw_hook_post = 'gruvbox_fade ' ..
  (c.lua_draw_hook_post or ''):gsub('^conky_', '')
