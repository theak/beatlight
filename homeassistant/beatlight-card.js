// beatlight-card: a compact media player tile with a slowly drifting, breathing gradient
// in the album's colours while music plays.
//
//   type: custom:beatlight-card
//   entity: media_player.living_room
//   name: Living Room            # optional
//   kind: AirPlay                # optional; subtitle when the player sends no track info
//
// Or several players for one room (e.g. Music Assistant playing through shairport-sync),
// in priority order:
//
//   entities:
//     - entity: media_player.living_room_ma
//       kind: Music Assistant
//     - entity: media_player.living_room_airplay
//       kind: AirPlay
//
// The card shows the player that's playing and keeps controlling it after a pause, even
// if the player then reports idle (Music Assistant's Spotify Connect does), so play
// resumes the right one. It hides itself RESUME_MIN minutes after everything stops.
//
// Colours: from the entity's album art, else the palette the beatlight server took from
// the cover art for the LEDs (via the beatlight relay integration), else neutral greys.
// The animation is pure CSS (opacity and transform only), so it runs on the GPU.

const RESUME_MIN = 3;
// On resume, Music Assistant's output (e.g. shairport-sync) reports playing a couple of
// seconds before Music Assistant does. Stay on the last player this long meanwhile.
const HANDOFF_MS = 5000;
const GREYS = [[60, 62, 66], [48, 50, 54], [34, 35, 38]];
const TONES = [0.3, 0.26, 0.19]; // gradient lightness, dark enough for white text

function hsl(r, g, b) {
  r /= 255; g /= 255; b /= 255;
  const mx = Math.max(r, g, b), mn = Math.min(r, g, b), l = (mx + mn) / 2;
  let h = 0, s = 0;
  if (mx !== mn) {
    const d = mx - mn;
    s = l > 0.5 ? d / (2 - mx - mn) : d / (mx + mn);
    h = mx === r ? (g - b) / d + (g < b ? 6 : 0) : mx === g ? (b - r) / d + 2 : (r - g) / d + 4;
    h /= 6;
  }
  return [h, s, l];
}

function rgb(h, s, l) {
  const f = (n) => {
    const k = (n + h * 12) % 12, a = s * Math.min(l, 1 - l);
    return Math.round(255 * (l - a * Math.max(-1, Math.min(k - 3, 9 - k, 1))));
  };
  return [f(0), f(8), f(4)];
}

// Three dark, distinct colours from an image: weighted hue bins, picks at least two
// bins apart, re-toned to TONES.
function extract(img) {
  const N = 40, cv = document.createElement("canvas");
  cv.width = cv.height = N;
  const ctx = cv.getContext("2d");
  ctx.drawImage(img, 0, 0, N, N);
  const d = ctx.getImageData(0, 0, N, N).data;
  const bins = Array.from({ length: 18 }, (_, i) => ({ w: 0, h: 0, s: 0, i }));
  for (let i = 0; i < d.length; i += 4) {
    const [h, s, l] = hsl(d[i], d[i + 1], d[i + 2]);
    if (l < 0.08 || l > 0.93) continue;
    const w = s * (1 - Math.abs(l - 0.5) * 1.2) + 0.03, b = bins[Math.floor(h * 18) % 18];
    b.w += w; b.h += h * w; b.s += s * w;
  }
  const picks = [];
  for (const b of bins.filter((b) => b.w > 0).sort((a, b) => b.w - a.w)) {
    if (picks.every((p) => Math.min(Math.abs(p.i - b.i), 18 - Math.abs(p.i - b.i)) >= 2)) picks.push(b);
    if (picks.length === 3) break;
  }
  if (!picks.length) return GREYS;
  while (picks.length < 3) picks.push(picks[0]);
  return picks.map((p, i) => rgb(p.h / p.w, Math.min(0.62, p.s / p.w), TONES[i]));
}

// The same treatment for the server's LED palette (vivid colours, darkest first).
function retone(colours) {
  const picks = colours.map((c) => hsl(...c)).sort((a, b) => b[1] - a[1]).slice(0, 3);
  if (!picks.length || picks[0][1] < 0.1) return GREYS;
  while (picks.length < 3) picks.push(picks[0]);
  return picks.map(([h, s], i) => rgb(h, Math.min(0.62, s), TONES[i]));
}

const glowOf = (pal) => {
  const [h, s] = hsl(...pal.reduce((a, c) => (hsl(...c)[1] > hsl(...a)[1] ? c : a), pal[0]));
  return rgb(h, Math.min(0.7, s + 0.15), 0.62);
};

// The server's palette, shared by every card. One websocket subscription per page, only
// while a card is on screen; without the relay integration the cards just skip it.
const relay = {
  cards: new Set(),
  sub: null,
  palette: null,
  sync() {
    const want = this.hass && this.cards.size && !document.hidden;
    if (want && !this.sub) {
      this.sub = this.hass.connection
        .subscribeMessage((m) => {
          if (!m.palette) return;
          this.palette = retone(m.palette);
          this.cards.forEach((c) => c.paletteChanged());
        }, { type: "beatlight/subscribe" })
        .catch(() => null);
    } else if (!want && this.sub) {
      this.sub.then((unsub) => unsub && unsub());
      this.sub = null;
    }
  },
};
document.addEventListener("visibilitychange", () => relay.sync());

class BeatlightCard extends HTMLElement {
  setConfig(config) {
    if (!config.entity && !config.entities) throw new Error("entity is required");
    this._config = config;
    this._players = (config.entities || [{ entity: config.entity, kind: config.kind }])
      .map((p) => (typeof p === "string" ? { entity: p } : p));
    if (!this._players.every((p) => p.entity)) throw new Error("entity is required");
    this._picture = undefined;
  }

  set hass(hass) {
    this._hass = relay.hass = hass;
    if (!this.shadowRoot) this._build();
    const target = this._target(hass);
    if (this.hidden !== !target) {
      this.hidden = !target;
      this.dispatchEvent(new Event("card-visibility-changed"));
    }
    this._entity = (target || this._players[0]).entity;
    const st = hass.states[this._entity], a = st?.attributes || {};
    const name = this._config.name || a.friendly_name || this._entity;
    // Any of the card's players: during a handoff the shown one may not report it yet.
    const playing = this._playing =
      this._players.some((p) => hass.states[p.entity]?.state === "playing");
    const shown = st?.state === "idle" && target ? "paused" : st?.state || "unavailable";
    const state = shown[0].toUpperCase() + shown.slice(1);
    // The song as the headline and "Artist · Living Room" under it; without track info,
    // the player's name over "AirPlay · Playing".
    this._el.title.textContent = a.media_title || name;
    this._el.sub.textContent = a.media_title
      ? [a.media_artist, name].filter(Boolean).join(" · ")
      : [(target || this._players[0]).kind, state].filter(Boolean).join(" · ");
    this._el.play.icon = playing ? "mdi:pause" : "mdi:play";
    this._el.card.classList.toggle("playing", playing);
    if (a.entity_picture !== this._picture) this._setPicture(a.entity_picture);
    relay.sync();
  }

  // The player to show and control: the first one playing; else the one last seen
  // playing, if it paused or went idle only recently; else the most recently stopped.
  // A player that has only just started doesn't take over from that one straight away.
  _target(hass) {
    const recent = (p) => {
      const st = hass.states[p.entity];
      return st && (st.state === "paused" || st.state === "idle") &&
        Date.now() - new Date(st.last_changed) < RESUME_MIN * 60000;
    };
    const playing = this._players.find((p) => hass.states[p.entity]?.state === "playing");
    if (playing) {
      const started = Date.now() - new Date(hass.states[playing.entity].last_changed);
      if (playing !== this._active && this._active && recent(this._active) &&
          started < HANDOFF_MS) return this._active;
      return (this._active = playing);
    }
    if (this._active && recent(this._active)) return this._active;
    const changed = (p) => new Date(hass.states[p.entity].last_changed);
    return this._players.filter(recent).sort((x, y) => changed(y) - changed(x))[0];
  }

  getCardSize() { return 1; }
  getGridOptions() { return { columns: 12, rows: 1, min_rows: 1 }; }

  connectedCallback() {
    relay.cards.add(this);
    relay.sync();
  }

  disconnectedCallback() {
    relay.cards.delete(this);
    relay.sync();
  }

  _build() {
    this.attachShadow({ mode: "open" }).innerHTML = `
      <style>
        ha-card { position: relative; height: 76px; border-radius: 18px; overflow: hidden;
          isolation: isolate; background: #1d1e20; border: 1px solid rgba(255,255,255,0.07);
          box-sizing: border-box; }
        .layer { position: absolute; inset: 0; pointer-events: none; }
        /* A 180%-wide gradient sliding back and forth (one way every 12 s), and a glow
           around the art breathing every 6 s. Both pause, dimmed, when not playing. */
        .grad { right: auto; width: 180%; opacity: 0.42; transition: opacity 1s ease;
          animation: drift 12s ease-in-out infinite alternate paused; }
        .grad > div { position: absolute; inset: 0; transition: opacity 600ms ease; }
        .bloom { transform-origin: 36px 50%; opacity: 0;
          animation: breathe 6s ease-in-out infinite paused; }
        .playing .grad { opacity: 0.7; }
        .playing .grad, .playing .bloom { animation-play-state: running; }
        @keyframes drift { from { transform: translateX(0); } to { transform: translateX(-44.4%); } }
        @keyframes breathe {
          0%, 100% { opacity: 0.15; transform: scale(0.9); }
          50% { opacity: 0.6; transform: scale(1.15); }
        }
        @media (prefers-reduced-motion: reduce) { .grad, .bloom { animation: none; } }
        .scrim { background: linear-gradient(90deg, rgba(18,19,20,0) 0%, rgba(18,19,20,0.1) 50%,
          rgba(18,19,20,0.4) 100%); }
        .row { position: relative; height: 100%; display: flex; align-items: center; gap: 8px;
          padding: 0 8px 0 12px; box-sizing: border-box; }
        .info { flex: 1; min-width: 0; display: flex; align-items: center; gap: 12px;
          height: 100%; cursor: pointer; }
        .art { flex: none; width: 48px; height: 48px; border-radius: 50%; overflow: hidden;
          box-shadow: 0 2px 8px rgba(0,0,0,0.35); background: rgba(255,255,255,0.1);
          display: flex; align-items: center; justify-content: center;
          --mdc-icon-size: 20px; color: rgba(255,255,255,0.7); }
        .art img { width: 100%; height: 100%; object-fit: cover; }
        .text { min-width: 0; }
        .title, .sub { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .title { font-size: 15px; font-weight: 500; color: #fff; }
        .sub { font-size: 13px; color: rgba(255,255,255,0.76); }
        /* Big targets for a wall tablet: 64px tall, the full width between neighbours. The
           hand cursor covers the gaps too, and the icons never take the pointer. */
        .controls { flex: none; display: flex; gap: 8px; cursor: pointer; }
        button ha-icon { pointer-events: none; }
        button { flex: none; width: 52px; height: 64px; border: none; border-radius: 14px;
          background: none; color: #fff; cursor: pointer; display: flex; align-items: center;
          justify-content: center; padding: 0; --mdc-icon-size: 26px; }
        button.play { width: 60px; background: rgba(0,0,0,0.28); }
        /* Touchscreens keep :hover on a tapped button until the next tap elsewhere, so
           hover is for mice only; touch gets a highlight just while pressed. */
        @media (hover: hover) {
          button:hover { background: rgba(255,255,255,0.08); }
          button.play:hover { background: rgba(0,0,0,0.4); }
        }
        button:active { background: rgba(255,255,255,0.12); }
        button.play:active { background: rgba(0,0,0,0.45); }
      </style>
      <ha-card>
        <div class="layer grad"><div></div><div></div></div>
        <div class="layer bloom"></div>
        <div class="layer scrim"></div>
        <div class="row">
          <div class="info">
            <div class="art"><ha-icon icon="mdi:music-note"></ha-icon></div>
            <div class="text"><div class="title"></div><div class="sub"></div></div>
          </div>
          <div class="controls">
            <button data-action="media_previous_track"><ha-icon icon="mdi:skip-previous"></ha-icon></button>
            <button class="play" data-action="media_play_pause"><ha-icon icon="mdi:play"></ha-icon></button>
            <button data-action="media_next_track"><ha-icon icon="mdi:skip-next"></ha-icon></button>
          </div>
        </div>
      </ha-card>`;
    const $ = (s) => this.shadowRoot.querySelector(s);
    this._el = {
      card: $("ha-card"), grads: [...this.shadowRoot.querySelectorAll(".grad > div")],
      bloom: $(".bloom"), art: $(".art"), title: $(".title"), sub: $(".sub"),
      play: $(".play ha-icon"),
    };
    this._gradI = 0;
    this.shadowRoot.querySelectorAll("button").forEach((b) =>
      b.addEventListener("click", () => {
        // play/pause rather than a toggle, so an idle player (stopped on pause) resumes
        let action = b.dataset.action;
        if (action === "media_play_pause")
          action = this._playing ? "media_pause" : "media_play";
        this._hass.callService("media_player", action, { entity_id: this._entity });
      }));
    $(".info").addEventListener("click", () =>
      this.dispatchEvent(new CustomEvent("hass-more-info",
        { detail: { entityId: this._entity }, bubbles: true, composed: true })));
    this.paletteChanged();
  }

  _setPicture(url) {
    this._picture = url;
    this._artPalette = null;
    const art = this._el.art;
    if (!url) {
      art.innerHTML = '<ha-icon icon="mdi:music-note"></ha-icon>';
      this.paletteChanged();
      return;
    }
    const img = new Image();
    img.onload = () => {
      if (this._picture !== url) return;
      try { this._artPalette = extract(img); } catch (e) { this._artPalette = null; }
      art.replaceChildren(img);
      this.paletteChanged();
    };
    img.src = url;
  }

  // Cross-fade to the current palette (album art, else the server's, else greys).
  paletteChanged() {
    if (!this._el) return;
    const pal = this._artPalette || relay.palette || GREYS, key = JSON.stringify(pal);
    if (key === this._palKey) return;
    this._palKey = key;
    const glow = glowOf(pal), rgba = (c, a) => `rgba(${c.join(",")},${a})`;
    const [cur, next] = this._gradI ? this._el.grads : this._el.grads.slice().reverse();
    next.style.background = `linear-gradient(100deg, ${pal.map((c, i) => `rgb(${c}) ${i * 50}%`).join(", ")})`;
    next.style.opacity = 1;
    cur.style.opacity = 0;
    this._gradI ^= 1;
    this._el.bloom.style.background =
      `radial-gradient(ellipse 240px 120px at 36px 50%, ${rgba(glow, 0.55)}, ${rgba(glow, 0)} 70%)`;
  }
}

customElements.define("beatlight-card", BeatlightCard);
window.customCards = window.customCards || [];
window.customCards.push({
  type: "beatlight-card",
  name: "Beatlight Card",
  description: "Media player tile with a slow gradient in the album's colours.",
});
