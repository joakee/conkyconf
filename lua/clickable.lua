--[[
  For cards with a lua_mouse_hook. Conky 1.25 makes an undecorated window
  click-through (an empty X input shape), so without this a click on the card
  goes to the desktop under it and the hook never runs.

    dofile(DIR .. '/lua/clickable.lua')                    -- in the card's Lua
    conky.config.lua_draw_hook_pre = 'clickable <name>'   -- in its widget

  On the card's first draw, bin/clickable.py gives the window its input back.
  A reload starts a fresh Lua state, so it runs again then, which is harmless.
  Not needed on Wayland, where it does nothing: there is no X display in
  conky_window, and wayland/card.lua's chained pre hook passes no name.
--]]

local SCRIPT = os.getenv('HOME') .. '/.config/conky/gruvbox-dark/bin/clickable.py'

local done = false

function conky_clickable(name)
  if done or conky_window == nil then return end
  done = true
  if conky_window.display == nil or not name then return end
  -- Detached: it may wait a moment for the WM to manage the window.
  os.execute('setsid -f ' .. SCRIPT .. ' ' .. name
             .. ' >/dev/null 2>&1 </dev/null')
end
