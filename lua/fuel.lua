--[[
  Fuel card: click the "FUEL · ULP 91" label in the header to step through
  the fuel types (left = next, right = previous). bin/fuel.py --cycle only
  writes the choice; the card's next render (1 s) shows the cached prices for
  it, or fetches them in the background if there are none for today yet.
  Renders are every 30 s otherwise (lua/exec.lua); a click makes them every
  second for long enough to see that fetch (30 s timeout) through.
--]]

local DIR    = os.getenv('HOME') .. '/.config/conky/gruvbox-dark'
local SCRIPT = DIR .. '/bin/fuel.py'
local E = dofile(DIR .. '/lua/exec.lua')

-- Header geometry, in window pixels; see lua/weather.lua for where these come
-- from. The label is at most "FUEL · BRAND DIESEL" (~150 px from the left
-- edge at size 8), and the date stamp on the right is ~40 px, so everything
-- left of DATE_W from the right edge belongs to the label.
local INSET    = 14 + 1
local HEADER_H = 22
local DATE_W   = 70

function conky_fuel_mouse(ev)
  if ev.type ~= 'button_down' then return false end
  local step = (ev.button == 'left' and 1) or (ev.button == 'right' and -1)
  if not step then return false end
  local w = (conky_window and conky_window.width) or 321
  if ev.x > w - DATE_W or ev.y > INSET + HEADER_H then return false end
  os.execute('setsid -f ' .. SCRIPT .. ' --cycle ' .. step
             .. ' >/dev/null 2>&1 </dev/null')
  E.hot(35)
  return true
end
