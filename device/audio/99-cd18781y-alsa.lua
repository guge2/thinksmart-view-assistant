alsa_monitor.rules = alsa_monitor.rules or {}

table.insert(alsa_monitor.rules, {
  matches = {
    {
      { "device.name", "equals", "alsa_card.platform-c051000.sound-card" },
    },
  },
  apply_properties = {
    ["api.alsa.use-acp"] = true,
    ["api.alsa.use-ucm"] = true,
    ["api.acp.auto-profile"] = true,
    ["api.acp.auto-port"] = true,
  },
})

table.insert(alsa_monitor.rules, {
  matches = {
    { { "node.name", "equals", "alsa_output.platform-c051000.sound-card.HiFi__hw_cd18781y_0__sink" } },
    { { "node.name", "equals", "alsa_output.platform-c051000.sound-card.playback.0.0" } },
    { { "node.name", "equals", "alsa_input.platform-c051000.sound-card.HiFi__hw_cd18781y_1__source" } },
  },
  apply_properties = {
    ["node.pause-on-idle"] = false,
    ["session.suspend-timeout-seconds"] = 0,
    ["audio.format"] = "S16LE",
    ["audio.rate"] = 48000,
    ["audio.channels"] = 2,
    -- This is a PCM-node property; setting it on the card does not disable mmap.
    ["api.alsa.disable-mmap"] = true,
    -- Playback and capture share the ADSP clock; distinct clock names make
    -- PipeWire rate-match the speaker, and that jitter defeats echo cancellation.
    ["clock.name"] = "api.alsa.cd18781y",
  },
})

-- With a shared clock the follower is never rate-matched, so it must be the
-- capture side: a following speaker drifts into the DMA read position and
-- replays audio one DMA buffer late, which sounds like a doubled echo.
table.insert(alsa_monitor.rules, {
  matches = {
    { { "node.name", "equals", "alsa_output.platform-c051000.sound-card.HiFi__hw_cd18781y_0__sink" } },
    { { "node.name", "equals", "alsa_output.platform-c051000.sound-card.playback.0.0" } },
  },
  apply_properties = {
    ["priority.driver"] = 2000,
    -- Wake on period IRQs. With timer scheduling the whole-period pointer made
    -- the clock estimate run 6.5% fast; the capture follower then resynced
    -- about twice a second, inserting silence or dropping 1024 frames.
    ["api.alsa.disable-tsched"] = true,
    -- ADSP reports position in whole periods. Reserve explicit pointer-jitter
    -- headroom without PipeWire halving the eight-period playback buffer.
    -- At 2048 the fill dipped to one period, and a stream joining or leaving
    -- the graph (music paused for a conversation) underran it several times in a row.
    ["api.alsa.disable-batch"] = true,
    -- Keep stream joins and departures from changing the AEC graph quantum.
    ["node.force-quantum"] = 960,
    ["api.alsa.period-size"] = 960,
    ["api.alsa.headroom"] = 4096,
  },
})

table.insert(alsa_monitor.rules, {
  matches = {
    { { "node.name", "equals", "alsa_input.platform-c051000.sound-card.HiFi__hw_cd18781y_1__source" } },
  },
  apply_properties = {
    ["api.alsa.headroom"] = 768,
  },
})

table.insert(alsa_monitor.rules, {
  matches = {
    { { "node.name", "equals", "alsa_output.platform-c051000.sound-card.playback.2.0" } },
    { { "node.name", "equals", "alsa_output.platform-c051000.sound-card.playback.4.0" } },
    { { "node.name", "equals", "alsa_input.platform-c051000.sound-card.capture.4.0" } },
  },
  apply_properties = {
    ["node.disabled"] = true,
  },
})
