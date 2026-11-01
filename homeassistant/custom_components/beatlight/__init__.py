"""Beatlight relay: passes the album palette the beatlight server sends over UDP to open
dashboards over the Home Assistant websocket, for beatlight-card's colours when the
player has no album art. It creates no entities, so nothing is written to the recorder.
Level packets are ignored.

configuration.yaml:

    beatlight:
      port: 7002   # optional; add <this host>:7002 to the server's BEATLIGHT_HOST

Dashboards subscribe with {"type": "beatlight/subscribe"} and get {"palette": [[r, g, b],
...]} events, the current one straight away.
"""

import asyncio

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant, callback
import homeassistant.helpers.config_validation as cv

DOMAIN = "beatlight"

CONFIG_SCHEMA = vol.Schema(
    {DOMAIN: vol.Schema({vol.Optional("port", default=7002): cv.port})},
    extra=vol.ALLOW_EXTRA,
)


class Relay(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self.listeners: set = set()
        self.palette: dict | None = None

    def datagram_received(self, data: bytes, addr) -> None:
        if data[:3] != b"SV\x02" or len(data) < 4 or len(data) != 4 + 3 * data[3]:
            return
        msg = {"palette": [list(data[i:i + 3]) for i in range(4, len(data), 3)]}
        if msg == self.palette:
            return              # resent every 5 s; only changes go out
        self.palette = msg
        for listener in list(self.listeners):
            listener(msg)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    relay = Relay()
    transport, _ = await hass.loop.create_datagram_endpoint(
        lambda: relay, local_addr=("0.0.0.0", config[DOMAIN]["port"])
    )
    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, lambda _: transport.close())
    hass.data[DOMAIN] = relay
    websocket_api.async_register_command(hass, ws_subscribe)
    return True


@websocket_api.websocket_command({vol.Required("type"): "beatlight/subscribe"})
@callback
def ws_subscribe(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    relay: Relay = hass.data[DOMAIN]
    msg_id = msg["id"]

    @callback
    def forward(event: dict) -> None:
        connection.send_message(websocket_api.event_message(msg_id, event))

    relay.listeners.add(forward)
    connection.subscriptions[msg_id] = lambda: relay.listeners.discard(forward)
    connection.send_result(msg_id)
    if relay.palette:
        forward(relay.palette)
