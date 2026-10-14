--[[
  ${execpi} with a cache, for cards whose script output only changes now and
  then:

    ${lua_parse cached_exec 30 /path/to/script.py}

  runs the script every 30 s, not every update. Each run is a fresh Python
  interpreter, ~60-170 ms of CPU, and four cards doing that every second added
  up to most of a core.

  Two ways to have it run sooner:

  * E.hot(secs), from a mouse hook that has started something the card should
    show as it happens -- a refresh, a fuel type change. For that long the
    script runs on every update again, as execpi 1 did.
  * E.key(fn, every), for a card that can tell cheaply whether its script has
    anything new to say. fn() sums up what the output depends on as a string
    (the countdowns file, the MPRIS players on the bus); it is called every
    `every` s, and the script runs as soon as the answer changes. An empty
    answer means there is nothing to poll for, so the interval alone does not
    run the script either -- the Now Playing card costs one busctl call, not a
    Python, while no player is open.

  lua_parse re-parses the cached text each update, as execpi does, so markup in
  it keeps working. Each card is its own conky process with its own Lua state,
  so one cache, one hot window and one key per state is all a card needs.
--]]

local E = {}

local out, ran, hot_until = nil, nil, 0
local key_fn, key_every, key, key_at = nil, 1, nil, nil

function E.hot(secs)
  hot_until = os.time() + secs
end

function E.key(fn, every)
  key_fn, key_every = fn, every or 1
end

function conky_cached_exec(interval, ...)
  local now = os.time()
  local changed, idle = false, false
  if key_fn then
    if key_at == nil or now - key_at >= key_every then
      local k = key_fn() or ''
      changed, key, key_at = (k ~= key), k, now
    end
    idle = (key == '')
  end
  if out == nil or changed or now <= hot_until
     or (not idle and now - ran >= tonumber(interval)) then
    local fh = io.popen(table.concat({ ... }, ' ') .. ' 2>/dev/null')
    if fh then
      out = fh:read('*a') or ''
      fh:close()
      ran = now
    end
  end
  -- execpi drops the trailing newline too; it would add a blank line.
  return (string.gsub(out or '', '\n$', ''))
end

return E
