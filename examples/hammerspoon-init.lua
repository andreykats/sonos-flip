-- Example Hammerspoon hotkeys (https://www.hammerspoon.org) for a Mac at the desk.
-- Add to ~/.hammerspoon/init.lua and set the two values below.
--   Cmd+Alt+Ctrl+S  desk mode, then select the Mac's sound output for the speakers
--   Cmd+Alt+Ctrl+T  theater mode

local FLIP = "http://192.168.1.10:8080"   -- the sonos-flip host and [http] port
-- Mac output to select after a desk switch, exactly as in System Settings > Sound > Output:
-- the pair_name from config.toml for AirPlay, or the headphone jack (e.g. "External Headphones")
-- when the Mac is connected to the speakers' line-in.
local MAC_OUTPUT = "Desk"

local function setOutput()
  -- AirPlay targets may not be visible to hs.audiodevice (the headphone jack always is).
  -- If it isn't found, the notification asks you to pick the output from the menu bar.
  local dev = hs.audiodevice.findOutputByName(MAC_OUTPUT)
  if dev then
    dev:setDefaultOutputDevice()
    hs.notify.show("Sonos", "Desk mode", "Output set to " .. MAC_OUTPUT)
  else
    hs.notify.show("Sonos", "Desk mode", "Pair ready — select '" .. MAC_OUTPUT .. "' in Sound output")
  end
end

local function flipTo(mode, done)
  hs.http.asyncPost(FLIP .. "/" .. mode, "", nil, function(code, body)
    if code ~= 200 then
      hs.notify.show("Sonos", "Flip failed", tostring(code) .. " " .. tostring(body))
      return
    end
    if done then done() end
  end)
end

-- ⌘⌥⌃ S : desk mode (grab speakers)
hs.hotkey.bind({"cmd", "alt", "ctrl"}, "S", function()
  hs.notify.show("Sonos", "Switching to desk…", "")
  flipTo("desk", setOutput)
end)

-- ⌘⌥⌃ T : theater mode (give them back)
hs.hotkey.bind({"cmd", "alt", "ctrl"}, "T", function()
  hs.notify.show("Sonos", "Switching to theater…", "")
  flipTo("theater")
end)
