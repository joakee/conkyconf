--[[
  Countdowns card: click anywhere on it to open the Countdowns app, where the
  list is edited. The app is a Gtk.Application, so a second click just raises
  the window that is already open.
--]]

local APP = os.getenv('HOME') .. '/.local/bin/countdowns'

function conky_countdowns_mouse(ev)
  if ev.type ~= 'button_down' or ev.button ~= 'left' then return false end
  os.execute('setsid -f ' .. APP .. ' >/dev/null 2>&1 </dev/null')
  return true
end
