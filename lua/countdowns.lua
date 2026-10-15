--[[
  Countdowns card: click anywhere on it to open the Countdowns app, where the
  list is edited. The app is a Gtk.Application, so a second click just raises
  the window that is already open.

  Also the time left on each countdown: bin/countdowns.py lays the card out,
  leaving a ${lua_parse countdown TARGET} where each time goes, and conky
  calls that every second to keep them ticking. The script itself runs only
  when the app has saved a change (this reads the file every second, which
  is next to free) and once a minute, which is how a finished countdown
  drops off.
--]]

local HOME = os.getenv('HOME')
local APP  = HOME .. '/.local/bin/countdowns'
local DATA = (os.getenv('XDG_DATA_HOME') or (HOME .. '/.local/share'))
             .. '/countdowns/countdowns.json'
local FONT = 'Maple Mono NF CN'
local E = dofile(HOME .. '/.config/conky/gruvbox-dark/lua/exec.lua')
dofile(HOME .. '/.config/conky/gruvbox-dark/lua/clickable.lua')

E.key(function()
  local fh = io.open(DATA)
  if not fh then return 'none' end      -- still worth a look once a minute
  local s = fh:read('*a')
  fh:close()
  return s
end)

-- TARGET is a Unix time, rounded up to the second. Green once done, red
-- under an hour, orange under a day, blue beyond.
function conky_countdown(target)
  local left = (tonumber(target) or 0) - os.time()
  local colour, value
  if left <= 0 then
    colour, value = '${color6}', 'done'
  else
    colour = (left < 3600 and '${color7}') or (left < 86400 and '${color1}')
             or '${color4}'
    local d, h = left // 86400, left % 86400 // 3600
    local m, s = left % 3600 // 60, left % 60
    value = string.format('%02d:%02d:%02d', h, m, s)
    if d > 0 then value = d .. 'd ' .. value end
  end
  return colour .. '${font ' .. FONT .. ':bold:size=11}' .. value .. '${font}'
end

function conky_countdowns_mouse(ev)
  if ev.type ~= 'button_down' or ev.button ~= 'left' then return false end
  os.execute('setsid -f ' .. APP .. ' >/dev/null 2>&1 </dev/null')
  return true
end
