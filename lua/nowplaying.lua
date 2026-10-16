--[[
  Now Playing card: album art on the left, track / artist / album beside it,
  and a progress bar along the bottom -- the shape macOS uses for its Now
  Playing widget.

  Track data and the path to a already-converted PNG of the album art come from
  bin/nowplaying.py via a Lua table literal in $XDG_RUNTIME_DIR. This file only
  draws; in particular it never touches the network, and it never scales the
  art, because the script writes it at exactly the size drawn here.
--]]

require 'cairo'
pcall(require, 'cairo_xlib')

local HOME = os.getenv('HOME')
local w = dofile(HOME .. '/.config/conky/gruvbox-dark/widget-common.lua')
local D = dofile(HOME .. '/.config/conky/gruvbox-dark/lua/draw.lua')
local E = dofile(HOME .. '/.config/conky/gruvbox-dark/lua/exec.lua')
local CACHE = (os.getenv('XDG_RUNTIME_DIR') or '/tmp')
              .. '/conky-gruvbox/nowplaying.lua'

-- Must match the layout constants at the top of bin/nowplaying.py.
local ART     = 76
local ART_R   = 8       -- album art corner radius
local GAP_X   = 12      -- between the art and the text column
local BAR_H   = 4
local HEAD_LINE = 18
local Y0 = w.PAD + HEAD_LINE + 6

local TEXT_X = w.PAD + ART + GAP_X
local TEXT_W = w.WIDTH - ART - GAP_X

local GLYPH_PLAY  = '\u{F040A}'
local GLYPH_PAUSE = '\u{F03E4}'
local GLYPH_STOP  = '\u{F04DB}'
local GLYPH_NOTE  = '\u{F075A}'

-- The MPRIS players on the bus, for lua/exec.lua: bin/nowplaying.py runs at
-- once when one opens or closes, every few seconds while any is open, and not
-- at all while none is -- one busctl call every 2 s is all that costs. If
-- busctl fails, poll as if a player were open rather than go blind.
E.key(function()
  local fh = io.popen('busctl --user --json=short call org.freedesktop.DBus '
                      .. '/org/freedesktop/DBus org.freedesktop.DBus ListNames 2>/dev/null')
  if not fh then return '?' end
  local names, found = fh:read('*a') or '', {}
  fh:close()
  if names == '' then return '?' end
  for n in names:gmatch('"(org%.mpris%.MediaPlayer2%.[^"]+)"') do
    found[#found + 1] = n
  end
  table.sort(found)
  return table.concat(found, ' ')
end, 2)

local function clock(sec)
  sec = math.max(0, math.floor(sec or 0))
  return string.format('%d:%02d', math.floor(sec / 60), sec % 60)
end

-- Album art, or a placeholder card of the same size when the script has not
-- finished fetching it. Clipped to a rounded square so it matches the widget.
local function draw_art(cr, path)
  local x, y = w.PAD, Y0
  local img = nil
  if path and path ~= '' then
    img = cairo_image_surface_create_from_png(path)
    if img and cairo_surface_status(img) ~= 0 then
      cairo_surface_destroy(img); img = nil
    end
  end

  cairo_save(cr)
  D.rrect(cr, x, y, ART, ART, ART_R)
  cairo_clip(cr)
  if img then
    cairo_set_source_surface(cr, img, x, y)
    cairo_paint(cr)
  else
    D.rgba(cr, w.pal.bg2, 0.55)
    cairo_paint(cr)
    D.face(cr, 30, false)
    D.rgba(cr, w.pal.fg4, 0.5)
    D.centred(cr, GLYPH_NOTE, x + ART / 2, y + ART / 2)
  end
  cairo_restore(cr)
  if img then cairo_surface_destroy(img) end

  -- A hairline keeps pale artwork from bleeding into the card background.
  D.rrect(cr, x + 0.5, y + 0.5, ART - 1, ART - 1, ART_R)
  D.rgba(cr, w.pal.fg0, 0.10)
  cairo_set_line_width(cr, 1)
  cairo_stroke(cr)
end

-- bin/nowplaying.py reads the position only every few seconds; between those
-- reads, a playing track has moved on by however long it has been since.
local function position(d)
  local pos = d.position or 0
  if d.status == 'Playing' and d.stamp then
    pos = pos + math.max(0, os.time() - d.stamp)
    if (d.length or 0) > 0 then pos = math.min(pos, d.length) end
  end
  return pos
end

local function draw_progress(cr, d)
  local x, y = w.PAD, Y0 + ART + 20
  local pos = position(d)
  local frac = 0
  if (d.length or 0) > 0 then
    frac = math.min(math.max(pos / d.length, 0), 1)
  end

  D.rrect(cr, x, y, w.WIDTH, BAR_H, BAR_H / 2)
  D.rgba(cr, w.pal.bg2, 0.7)
  cairo_fill(cr)
  if frac > 0 then
    D.rrect(cr, x, y, math.max(BAR_H, w.WIDTH * frac), BAR_H, BAR_H / 2)
    D.rgba(cr, d.status == 'Playing' and w.pal.aqua or w.pal.fg4, 1)
    cairo_fill(cr)
  end

  D.face(cr, 8, false)
  D.rgba(cr, w.pal.fg4, 1)
  D.left(cr, clock(pos), x, y - 5)
  if (d.length or 0) > 0 then
    D.right(cr, clock(d.length), x + w.WIDTH, y - 5)
  end
end

function conky_nowplaying_draw()
  if conky_window == nil then return end

  local ok, d = pcall(dofile, CACHE)
  if not ok or type(d) ~= 'table' or not d.title then return end

  local cs = cairo_xlib_surface_create(conky_window.display, conky_window.drawable,
                                       conky_window.visual,
                                       conky_window.width, conky_window.height)
  local cr = cairo_create(cs)

  draw_art(cr, d.art)

  -- Title, then artist, then album: decreasing size and contrast, so the eye
  -- lands on the track name first.
  D.face(cr, 11, true)
  D.rgba(cr, w.pal.fg0, 1)
  D.left(cr, D.fit(cr, d.title, TEXT_W), TEXT_X, Y0 + 14)

  if d.artist and d.artist ~= '' then
    D.face(cr, 9, false)
    D.rgba(cr, w.pal.fg1, 1)
    D.left(cr, D.fit(cr, d.artist, TEXT_W), TEXT_X, Y0 + 32)
  end

  if d.album and d.album ~= '' then
    D.face(cr, 8, false)
    D.rgba(cr, w.pal.fg4, 1)
    D.left(cr, D.fit(cr, d.album, TEXT_W), TEXT_X, Y0 + 47)
  end

  -- Transport state, with the glyph doing the work rather than a word.
  local glyph = (d.status == 'Playing' and GLYPH_PLAY)
             or (d.status == 'Paused' and GLYPH_PAUSE) or GLYPH_STOP
  D.face(cr, 11, false)
  D.rgba(cr, d.status == 'Playing' and w.pal.aqua or w.pal.fg4, 1)
  D.left(cr, glyph, TEXT_X, Y0 + 68)
  if d.source and d.source ~= '' then
    local gw = D.extents(cr, glyph).width
    D.face(cr, 8, false)
    D.rgba(cr, w.pal.fg4, 1)
    D.left(cr, D.fit(cr, d.source, TEXT_W - gw - 8), TEXT_X + gw + 8, Y0 + 68)
  end

  draw_progress(cr, d)

  cairo_destroy(cr)
  cairo_surface_destroy(cs)
  collectgarbage()
end
