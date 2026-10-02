--[[
  Weather card: click the refresh icon (top right, before the observation
  time) to pull fresh data from BOM now instead of waiting out the cache TTL.

  The fetch runs detached in bin/weather.py --refresh, so the conky process
  never blocks on the network. That script's lock is what the render reads to
  turn the icon into a yellow sync glyph while it runs, red if it failed.
--]]

local HOME   = os.getenv('HOME')
local SCRIPT = HOME .. '/.config/conky/gruvbox-dark/bin/weather.py'

-- Header geometry, in window pixels. The text box starts PAD+1 px inside the
-- window on both axes (see widget-common.lua). The stamp "<icon> HH:MM" is
-- right-aligned at size 8: measured 42 px wide, the icon its first 6 px, at
-- 96 dpi. The hit box pads the icon out to a target a finger-ish cursor can
-- hit without also swallowing the time.
local INSET    = 14 + 1
local STAMP_W  = 42
local ICON_W   = 6
local SLOP     = 6
local HEADER_H = 22   -- the size-10 header line

local function hit(x, y, w)
  local left = w - INSET - STAMP_W
  return x >= left - SLOP and x <= left + ICON_W + SLOP
     and y >= 0 and y <= INSET + HEADER_H
end

function conky_weather_mouse(ev)
  if ev.type ~= 'button_down' or ev.button ~= 'left' then return false end
  local w = (conky_window and conky_window.width) or 321
  if not hit(ev.x, ev.y, w) then return false end
  -- setsid + discarded output: detached, so neither this hook nor conky's
  -- main loop waits on BOM.
  os.execute('setsid -f ' .. SCRIPT .. ' --refresh >/dev/null 2>&1 </dev/null')
  return true
end
