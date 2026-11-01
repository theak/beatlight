"""beatlight server: turns the PCM copy from the ALSA plugin into a bass level for the
ESP32 LED strip (UDP 'S' 'V' 0x01 <level 0-255>, ~60 packets/s).

Pipeline per source: mono sum -> decimate by 3 (14.7 kHz) -> 2nd-order low-pass ->
RMS per 1/60 s frame -> auto gain (range jumps to each new peak, relaxes with an
~8 s time constant) -> level = 255 * rms / range, with a noise gate and a minimum
range so silence and dither stay at 0.

Timing: the ALSA plugin copies each chunk as the player writes it, tagged with the
index of its first frame since the PCM was opened, which is ALSA's appl_ptr for that
frame. The sound card's status file reports hw_ptr, the frame the DAC is playing right
now, so frame i leaves the DAC at  now + (i - hw_ptr) / 44100.  Each level is sent at
that moment, plus DELAY_MS for anything after the DAC (e.g. an AV receiver's processing).
Without the status file it falls back to arrival time plus FALLBACK_LEAD_MS.

Album colours (optional): shairport-sync publishes the current cover art to MQTT
<topic>/cover, and any other player can publish there too. Each
new cover becomes a palette of up to 5 vivid colours, darkest first and the "hero"
colour last ('S' 'V' 0x02 <n> + n x RGB). It's sent at once while music plays, then
every 5 s; the ESP falls back to its own palette when the packets stop.
"""

import array
import colorsys
import heapq
import io
import math
import os
import select
import socket
import struct
import time
import urllib.parse

SOCK_PATH = "/run/beatlight/tap.sock"
CARD_STATUS = os.environ.get("CARD_STATUS", "/host-asound/card0/pcm0p/sub0/status")
PORT = int(os.environ.get("BEATLIGHT_PORT", "7001"))
# The ESP32's IP address. Comma-separate several to send the same packets to each, with
# an optional :port, e.g. "192.168.1.50,192.168.1.10:7002".
TARGETS = [(h, int(p) if p else PORT) for h, _, p in
           (t.strip().partition(":") for t in os.environ["BEATLIGHT_HOST"].split(","))]
DELAY_MS = float(os.environ.get("DELAY_MS", "0"))
# DELAY_MS_<source> overrides it for one player, e.g. DELAY_MS_S for source "s"
LOWPASS_HZ = float(os.environ.get("LOWPASS_HZ", "150"))
RELEASE_S = float(os.environ.get("RELEASE_S", "8"))
GATE = 10 ** (float(os.environ.get("GATE_DBFS", "-70")) / 20)
MIN_RANGE = 10 ** (float(os.environ.get("MIN_RANGE_DBFS", "-45")) / 20)

RATE = 44100
DECIMATE = 3
FS = RATE / DECIMATE           # 14700 Hz
FPS = 60
FRAME_LEN = int(FS / FPS)      # 245 decimated samples = 1/60 s
FULL_SCALE = 2 ** 31 * 2 * DECIMATE   # sum of 3 stereo pairs of S32
HDR = struct.Struct("<cQ")     # source id, index of the chunk's first frame
RELEASE = math.exp(-1 / FPS / RELEASE_S)
STATUS_EVERY = 0.25            # s between card status reads while playing
FALLBACK_LEAD = float(os.environ.get("FALLBACK_LEAD_MS", "350")) / 1000


def lowpass(f0, fs, q=math.sqrt(0.5)):
    w = 2 * math.pi * f0 / fs
    alpha = math.sin(w) / (2 * q)
    cw = math.cos(w)
    a0 = 1 + alpha
    b0 = (1 - cw) / 2 / a0
    return b0, 2 * b0, b0, -2 * cw / a0, (1 - alpha) / a0


B0, B1, B2, A1, A2 = lowpass(LOWPASS_HZ, FS)

MQTT_URL = os.environ.get("MQTT_URL", "")          # mqtt://user:pass@host:port
MQTT_TOPIC = os.environ.get("MQTT_TOPIC", "shairport")
PALETTE_SIZE = 5
PALETTE_EVERY = 5.0


def palette(image_bytes):
    """Up to PALETTE_SIZE distinct colours from a cover, least to most vivid."""
    from PIL import Image
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    img.thumbnail((64, 64))
    q = img.quantize(16, method=Image.Quantize.MEDIANCUT)
    pal = q.getpalette()
    counts = sorted(q.getcolors(), reverse=True)        # (pixel count, index)
    total = sum(c for c, _ in counts)
    cands = []
    for count, i in counts:
        r, g, b = pal[3 * i:3 * i + 3]
        h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
        # Prefer common, saturated, not-too-dark colours; greys and near-black score low.
        score = count / total * (0.15 + s) * (0.2 + v)
        cands.append((score, h, s, v, (r, g, b)))
    cands.sort(reverse=True)
    chosen, rgbs = [], []
    for score, h, s, v, rgb in cands:
        # skip near-duplicates (RGB distance; hue alone treats white and red as equal)
        if all(math.dist(rgb, c) > 60 for c in rgbs):
            chosen.append((h, s, v))
            rgbs.append(rgb)
        if len(chosen) == PALETTE_SIZE:
            break
    if max(s for _, s, _ in chosen) < 0.15:             # black and white: no palette
        return []
    if len(chosen) < 2:                                 # a one-colour cover: add a dim shade
        h, s, v = chosen[0]
        chosen.append((h, s, v * 0.35))
    # Muted colours look washed out and dark ones nearly off on LEDs: boost both a
    # little, then order darkest/least vivid first.
    out = [(h, min(1.0, s * 1.3 + 0.05), max(v, 0.3)) for h, s, v in chosen]
    out.sort(key=lambda c: c[2] * (0.5 + 0.5 * c[1]))
    return [tuple(round(x * 255) for x in colorsys.hsv_to_rgb(*c)) for c in out]


def start_mqtt(on_cover):
    """Call on_cover(bytes) for each cover a player publishes (b'' = no art)."""
    if not MQTT_URL:
        return
    import paho.mqtt.client as mqtt
    u = urllib.parse.urlparse(MQTT_URL)
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    if u.username:
        client.username_pw_set(urllib.parse.unquote(u.username),
                               urllib.parse.unquote(u.password or ""))

    def on_connect(c, _userdata, _flags, reason, _props):
        print(f"{time.strftime('%H:%M:%S')} MQTT: {reason}, watching {MQTT_TOPIC}/cover", flush=True)
        c.subscribe(f"{MQTT_TOPIC}/cover")

    client.on_connect = on_connect
    client.on_message = lambda c, _, msg: on_cover(msg.payload)
    client.connect_async(u.hostname, u.port or 1883)
    client.loop_start()


def read_card():
    """(monotonic time, hw_ptr, appl_ptr, trigger_time) while the card runs, else None."""
    try:
        with open(CARD_STATUS) as f:
            text = f.read()
    except OSError:
        return None
    t = time.monotonic()
    fields = {k.strip(): v.strip() for k, v in
              (line.split(":", 1) for line in text.splitlines() if ":" in line)}
    if fields.get("state") != "RUNNING":
        return None
    try:
        return (t, int(fields["hw_ptr"]), int(fields["appl_ptr"]),
                float(fields["trigger_time"]))
    except (KeyError, ValueError):
        return None


class Stream:
    """One PCM open of one player (the plugin's frame index restarts at 0)."""

    def __init__(self, source, first_index, now):
        self.source = source
        self.delay = float(os.environ.get(f"DELAY_MS_{source.upper()}", DELAY_MS)) / 1000
        self.pos = first_index          # frame index just past the last received frame
        self.last_data = now
        # Frame i leaves the DAC at base + i / RATE. Estimated until the card is read.
        self.base = now + FALLBACK_LEAD - first_index / RATE
        self.card_synced = False
        self.trigger = None
        self.offset = 0                 # appl_ptr - our frame index
        self.x1 = self.x2 = self.y1 = self.y2 = 0.0
        self.acc = 0.0
        self.n = 0
        self.range = MIN_RANGE
        self.leftover = array.array("i")
        print(f"{time.strftime('%H:%M:%S')} stream start: {source}", flush=True)

    def sync(self, card):
        t, hw_ptr, appl_ptr, trigger = card
        if trigger != self.trigger:
            # First sight of this run of the card, or a restart after an underrun or a
            # flush (both move appl_ptr): line our frame index up with appl_ptr. They
            # match from a fresh open, but not after a flush, nor when this process
            # started mid-stream, so measure it (good to within a chunk, ~12 ms).
            self.trigger = trigger
            offset = appl_ptr - self.pos
            # within a chunk of 0 means they match exactly (a fresh open): keep it exact
            self.offset = 0 if abs(offset) <= 1024 else offset
        base = t + (self.offset - hw_ptr) / RATE
        if not self.card_synced or abs(base - self.base) > 0.05:
            self.base = base
            self.card_synced = True
            print(f"{time.strftime('%H:%M:%S')} {self.source}: synced to the card, "
                  f"{(appl_ptr - hw_ptr) / RATE * 1000:.0f} ms queued ahead of the DAC",
                  flush=True)
        else:
            self.base += 0.2 * (base - self.base)

    def feed(self, index, pcm, now, out):
        self.last_data = now
        if index != self.pos:            # chunks were dropped: restart the frame
            self.leftover = array.array("i")
            self.acc, self.n = 0.0, 0
        samples = self.leftover
        first = index - len(samples) // 2   # frame index of samples[0]
        samples.frombytes(pcm)
        usable = len(samples) - len(samples) % (2 * DECIMATE)
        x1, x2, y1, y2 = self.x1, self.x2, self.y1, self.y2
        acc, n = self.acc, self.n
        for i in range(0, usable, 2 * DECIMATE):
            x = (samples[i] + samples[i + 1] + samples[i + 2] + samples[i + 3]
                 + samples[i + 4] + samples[i + 5]) / FULL_SCALE
            y = B0 * x + B1 * x1 + B2 * x2 - A1 * y1 - A2 * y2
            x2, x1, y2, y1 = x1, x, y1, y
            acc += y * y
            n += 1
            if n == FRAME_LEN:
                # due when the middle of this 1/60 s frame leaves the DAC
                mid = first + i // 2 + DECIMATE - FRAME_LEN * DECIMATE // 2
                out.append((self.base + mid / RATE + self.delay,
                            self.level(math.sqrt(acc / n))))
                acc, n = 0.0, 0
        self.x1, self.x2, self.y1, self.y2 = x1, x2, y1, y2
        self.acc, self.n = acc, n
        self.leftover = samples[usable:]
        self.pos = index + len(pcm) // 8
        if not self.card_synced:
            # The plugin sends as the player writes, so arrival + FALLBACK_LEAD is a
            # fair guess; follow it if we fell behind (e.g. after a backlog).
            est = now + FALLBACK_LEAD - self.pos / RATE
            if abs(est - self.base) > 0.1:
                self.base = est

    def level(self, rms):
        self.range = max(rms, self.range * RELEASE, MIN_RANGE)
        if rms < GATE:
            return 0
        return min(255, int(255 * rms / self.range))


def main():
    os.makedirs(os.path.dirname(SOCK_PATH), exist_ok=True)
    try:
        os.unlink(SOCK_PATH)
    except FileNotFoundError:
        pass
    rx = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    rx.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
    rx.bind(SOCK_PATH)
    os.chmod(SOCK_PATH, 0o666)
    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    tx.setblocking(False)
    print(f"beatlight: {SOCK_PATH} -> {", ".join(f"{h}:{p}" for h, p in TARGETS)}, delay {DELAY_MS:.0f} ms, card status "
          f"{'found' if os.path.exists(CARD_STATUS) else 'MISSING'}", flush=True)

    streams = {}
    active = None
    queue = []          # (due, seq, level)
    seq = 0
    zeros_left = 0      # trailing 0 packets still to send after music stops
    quiet_frames = 0
    next_status = 0.0

    def send_all(packet):
        for target in TARGETS:
            try:
                tx.sendto(packet, target)
            except OSError:
                pass

    def send(level):
        send_all(bytes((0x53, 0x56, 0x01, level)))

    art = {"packet": None, "next": 0.0}     # set from the MQTT thread
    saved = os.path.join(os.path.dirname(SOCK_PATH), "palette.bin")
    try:                                    # the last palette survives a restart
        with open(saved, "rb") as f:
            art["packet"] = f.read() or None
    except OSError:
        pass

    def on_cover(data):
        try:
            if data == b"--":   # shairport's "no cover", also sent mid-track: keep the last
                return
            colours = palette(data) if data else []
        except Exception as e:              # not an image, or an odd one: keep the last
            print(f"{time.strftime('%H:%M:%S')} cover art unreadable ({len(data)} bytes, "
                  f"{data[:8].hex()}): {e}", flush=True)
            return
        art["packet"] = (bytes((0x53, 0x56, 0x02, len(colours)))
                         + bytes(x for c in colours for x in c)) if colours else None
        art["next"] = 0.0
        try:
            with open(saved, "wb") as f:
                f.write(art["packet"] or b"")
        except OSError:
            pass
        print(f"{time.strftime('%H:%M:%S')} palette: "
              + (" ".join("#%02x%02x%02x" % c for c in colours) or "none (no art)"), flush=True)

    start_mqtt(on_cover)

    while True:
        now = time.monotonic()
        timeout = max(0.0, queue[0][0] - now) if queue else 0.1
        if select.select([rx], [], [], min(timeout, 0.05))[0]:
            data = rx.recv(65536)
            now = time.monotonic()
            if len(data) > HDR.size:
                source, index = HDR.unpack_from(data)
                source = source.decode()
                st = streams.get(source)
                if st is None or index < st.pos or now - st.last_data > 5:
                    st = streams[source] = Stream(source, index, now)
                    next_status = 0.0
                active = st
                frames = []
                st.feed(index, data[HDR.size:], now, frames)
                for due, level in frames:
                    heapq.heappush(queue, (due, seq, level))
                    seq += 1

        now = time.monotonic()
        if active and now - active.last_data < 1 and now >= next_status:
            next_status = now + STATUS_EVERY
            card = read_card()
            if card:
                active.sync(card)

        while queue and queue[0][0] <= now:
            due, _, level = heapq.heappop(queue)
            if now - due > 0.1:
                continue            # too late to be useful
            # Silence while the player keeps the device open: send a few 0s, then stop.
            quiet_frames = quiet_frames + 1 if level == 0 else 0
            if quiet_frames <= 3:
                send(level)
            if level:
                zeros_left = 3

        packet = art["packet"]
        if packet and active and now - active.last_data < 1 and now >= art["next"]:
            art["next"] = now + PALETTE_EVERY
            send_all(packet)

        idle = all(now - s.last_data > 0.3 for s in streams.values())
        if idle and not queue and zeros_left:
            send(0)
            zeros_left -= 1


if __name__ == "__main__":
    main()
