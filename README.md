# beatlight

An LED strip that dances to whatever your Linux music player is playing: AirPlay via
shairport-sync, or any other ALSA player. It reacts to the bass of the actual audio,
so there's no microphone and it doesn't depend on the amplifier's volume. Each light
change is timed to the moment its sound leaves the sound card. It also takes its
colours from the album art.

```
player -> ALSA "beatlight" device -> the sound card (playback is unchanged)
                 |
                 +-> copy of the audio -> beatlight server -> UDP -> ESP32 -> WS2812B strip
                                          (bass level, timed       (ESPHome)
                                           to the DAC; album
                                           colours from MQTT)
```

There are three parts:

- **`alsa/`**: a small ALSA plugin. It passes audio through to the sound card untouched
  and sends a copy to the server. Sends never block, so playback never waits on, or
  fails because of, the lights.
- **`server/`**: a Python service (Docker). It turns the audio into a bass level ~60
  times a second. It reads the sound card's playback position so each level goes out
  when its audio is actually heard. It also turns album art into a colour palette.
- **`esphome/`**: firmware for an ESP32-S3 driving a WS2812B strip. Each bass hit
  slides a colour gradient along the strip, which then glides back. The gradient is a
  sunset palette, or the album's colours when available. Everything is tunable live
  from Home Assistant.
- **`homeassistant/`** (optional): a compact media player card for dashboards, with a
  slow gradient in the album's colours. It comes with a tiny relay integration that
  passes the server's album palette to the card, for players that give Home Assistant
  no cover art.

## Hardware

- An ESP32-S3 dev board (the `esp32-s3-devkitc-1` profile).
- A WS2812B strip (36 LEDs here) with its own 5 V supply.
  - Data goes from GPIO14 through a 470 Ω resistor to DIN. 3.3 V data works without a
    level shifter.
  - Connect the supply's GND to the ESP32's GND.
  - Powering the ESP32 separately (USB) is the most reliable setup.
- The Linux box that plays the music, with Docker.

## Setup

### 1. Flash the ESP32

```sh
cd esphome
cp secrets.yaml.example secrets.yaml   # fill in your wifi and generate the keys
esphome run beatlight.yaml
```

Adjust `led_pin`, `num_leds` and `onboard_led_pin` at the top if your wiring differs.
Give the ESP32 a DHCP reservation, since the server sends to its IP address. mDNS
names often don't resolve inside containers.

### 2. Build and install the ALSA plugin

The plugin must be built against the same libc and alsa-lib as the player that loads
it. For the official shairport-sync image (Alpine):

```sh
docker run --rm -v "$PWD/alsa:/src" -w /src alpine:3.23 sh -c \
  'apk add build-base alsa-lib-dev && gcc -O2 -Wall -shared -fPIC -DPIC \
   -o libasound_module_pcm_beatlight.so pcm_beatlight.c -lasound'
```

Leave out `-DPIC` and ALSA can't load the plugin. On a Debian/Ubuntu host, install
`libasound2-dev` and run the same `gcc` command.

The player then needs three things:

- `alsa/asound.conf` installed as `/etc/asound.conf`, with `hw:CARD=PCH,DEV=0` changed
  to your card (`aplay -L` lists them).
- The plugin folder at `/opt/beatlight`.
- A shared folder at `/run/beatlight` for the socket.

Then set the player's output device to `beatlight`. For shairport-sync, that's
`alsa = { output_device = "beatlight"; };`.
`docker-compose.example.yaml` shows all of this.

For several players on one card, add a device per player in `asound.conf`. Give each a
different one-letter `source`.

### 3. Run the server

```sh
docker compose -f docker-compose.example.yaml up -d
```

Settings (environment variables):

| Variable | Default | |
|---|---|---|
| `BEATLIGHT_HOST` | (required) | The ESP32's IP address. Comma-separate several to send the same packets to each, with an optional `:port`, e.g. `192.168.1.50,192.168.1.10:7002` |
| `BEATLIGHT_PORT` | `7001` | Its UDP port |
| `DELAY_MS` | `0` | Extra delay after the DAC, e.g. for an AV receiver's processing. `DELAY_MS_<source>` overrides it for one player |
| `CARD_STATUS` | `/host-asound/card0/pcm0p/sub0/status` | The card's status file (mount `/proc/asound` at `/host-asound`). Without it, timing is estimated |
| `LOWPASS_HZ` | `150` | Bass cutoff |
| `RELEASE_S` | `8` | How fast the auto gain relaxes after a loud passage |
| `GATE_DBFS` / `MIN_RANGE_DBFS` | `-70` / `-45` | Keep silence and dither dark |
| `MQTT_URL` | (off) | `mqtt://user:pass@host:1883`, for album colours |
| `MQTT_TOPIC` | `shairport` | Cover art is read from `<topic>/cover` |

### 4. Album colours (optional)

shairport-sync can publish cover art to MQTT. Enable its `mqtt` section with
`publish_cover = "yes"` and set `MQTT_URL` on the server. Any other player can publish
image bytes to the same `<topic>/cover`.

The server picks up to 5 vivid, distinct colours from each cover and sends them every
5 s while music plays. Black-and-white covers keep the sunset palette. The strip fades
back to sunset 30 s after colours stop arriving.

### 5. Turn it on with the music (Home Assistant)

The strip always boots off. Turning it on from HA without an effect starts the
`Beatlight` effect. A simple automation turns the strip on while your player is
playing and off otherwise.

### 6. Dashboard card (optional)

`homeassistant/beatlight-card.js` is a compact media player card:
- a round album-art thumbnail, the song over "Artist · Room", and previous / play-pause
  / next;
- behind it, a dark gradient in the album's colours that drifts slowly and breathes
  while music plays.

Copy it to `config/www/`. Add `/local/beatlight-card.js` as a JavaScript module under
Settings → Dashboards → Resources, then:

```yaml
type: custom:beatlight-card
name: Living Room
entities:              # or just `entity: media_player.x`
  - entity: media_player.living_room_music_assistant
    kind: Music Assistant
  - entity: media_player.living_room_airplay
    kind: AirPlay      # subtitle when the player sends no track info
```

With several players for one room (e.g. Music Assistant playing through shairport-sync),
the card shows the one that's playing and keeps controlling it after a pause. That
matters for Music Assistant's Spotify Connect, which reports idle once paused, so play
still resumes it. The card hides itself 3 minutes after everything pauses or stops.

**Album colours without cover art:** copy `homeassistant/custom_components/beatlight`
into `config/custom_components/`, add `beatlight:` to `configuration.yaml` and restart.
Then add `<home assistant's IP>:7002` to the server's `BEATLIGHT_HOST`. The relay passes
the server's album palette to open dashboards over Home Assistant's own websocket. It
creates no entities, so nothing is recorded. Without it, the card uses the art Home
Assistant has, or neutral greys.

## Home Assistant controls

- **Light "LED Strip"**: effects Beatlight, Rainbow, Color Wipe, Twinkle and Random
  Twinkle. The last four are wiring tests.
- **Album Colors** switch: off forces the sunset palette.
- **Mute** switch: fades the strip out while it stays on, so automations don't undo it.
- **Numbers** (restored across reboots):

  | Number | Range (default) | What it does |
  |---|---|---|
  | LED Brightness | 5–100 % (100) | Dims below the current cap |
  | Sunset Whiteness | 0–100 % (45) | Softens the sunset palette toward white |
  | Sensitivity | 25–300 % (100) | Scales the incoming level |
  | Decay Speed | 1–40 (8) | How fast the gradient slides back |
  | Gradient Spread | 1–14 (7) | How much of the gradient is on the strip at once |
  | Beat Brightness | 0–100 % (0) | Dims between hits and flashes on the beat |
  | Palette Fade | 0–10 s (1.5) | Crossfade length for palette changes |

- **Palette** sensor: the current colours, or "sunset".
- **Diagnostics**: uptime, WiFi signal, heap, loop time and reset reason.
- **Onboard LED**: the board's RGB LED mirrors the first strip LED as a beat indicator.

## UDP protocol

The ESP32 listens on UDP 7001. It's fire-and-forget on the LAN: no handshake, ack or
auth, so anything can drive it.

- **Level**: `'S' 'V' 0x01 <level 0-255>`, exactly 4 bytes, ~60/s while playing.
  - The level is already low-passed and auto-gained.
  - The ESP jumps up to each new level and decays by Decay Speed per ~16 ms frame.
  - No packet for 1 s counts as silence.
- **Palette**: `'S' 'V' 0x02 <n>` followed by n × `r g b`, with n = 2–8.
  - Colours go darkest first, brightest last. The strip shows them as a mirrored
    gradient, so the brightest colour is the peak.
  - Packets that aren't exactly 4 + 3n bytes are ignored.
  - Resend the palette every few seconds, or the strip falls back to sunset after 30 s.

## How the timing works

The plugin tags each chunk of audio with its frame index as the player writes it.
That's ALSA's `appl_ptr`, often a second or more ahead of what you hear. The server
reads the card's `hw_ptr` (the frame playing right now) from
`/proc/asound/.../status`. Frame *i* is then heard at `now + (i − hw_ptr) / 44100`,
and its level is sent at that moment. On a real card the light peaks land within a
millisecond of the audio leaving the DAC. Add `DELAY_MS` for anything after the DAC.

## Reliability notes

- **Playback never depends on the server.** If the server is stopped, crashes or
  restarts mid-song, the music plays on and the lights resume when it's back.
- **The plugin is the one hard dependency.** If its file is missing or built for the
  wrong libc, the player can't open the device. To bypass it, point the player back at
  your card (e.g. `plughw:CARD=PCH,DEV=0`), or replace the `beatlight` device in
  `asound.conf` with:

  ```
  pcm.beatlight { type plug; slave.pcm "hw:CARD=PCH,DEV=0" }
  ```
- **Current cap.** `color_correct` caps every channel at 59% (~1.3 A worst case for 36
  LEDs at full white). LED Brightness dims below that.
- **Watchdog freezes.** With ESPHome's default small RMT buffer, frames hung during
  flash writes (settings saves) and tripped the watchdogs. `use_dma: true` with
  `use_psram: false` fixed it. DMA without `use_psram: false` crashed at boot on a
  board without PSRAM.
- **WiFi.** `power_save_mode: none` keeps UDP latency low and stops the ESP dropping
  off the network.

## License

MIT
