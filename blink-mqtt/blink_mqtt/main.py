import asyncio
import logging

import uvicorn

from . import config
from .auth import AuthManager
from .bridge import Bridge
from .mqtt_client import MQTTClient
from .web import create_app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
_LOGGER = logging.getLogger(__name__)


async def main():
    loop = asyncio.get_running_loop()

    auth_manager = AuthManager()
    bridge_event = asyncio.Event()

    mqtt = MQTTClient(
        host=config.MQTT_HOST,
        port=config.MQTT_PORT,
        user=config.MQTT_USER,
        password=config.MQTT_PASSWORD,
        loop=loop,
    )

    def _session_dead():
        """Blink session lost: mark HA entities unavailable and stop the bridge."""
        mqtt.set_available(False)
        bridge_event.set()

    auth_manager.on_session_dead = _session_dead

    app = create_app(auth_manager, bridge_event)
    app.state.mqtt = mqtt

    server_cfg = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=8765,
        log_level="warning",
    )
    server = uvicorn.Server(server_cfg)

    await asyncio.gather(
        server.serve(),
        _supervise(auth_manager, mqtt, bridge_event),
    )


async def _supervise(auth_manager: AuthManager, mqtt: MQTTClient, bridge_event: asyncio.Event):
    """Try restoring credentials, then wait for auth and manage bridge lifecycle."""
    _LOGGER.info("Attempting credential restore...")
    await auth_manager.restore()

    if auth_manager.blink:
        bridge_event.set()

    bridge_task: asyncio.Task | None = None

    while True:
        await bridge_event.wait()
        bridge_event.clear()

        if bridge_task and not bridge_task.done():
            bridge_task.cancel()
            try:
                await bridge_task
            except asyncio.CancelledError:
                pass

        if auth_manager.blink:
            bridge = Bridge(auth_manager, mqtt, bridge_event)
            bridge_task = asyncio.create_task(_run_bridge(bridge))
        else:
            mqtt.set_available(False)
            _LOGGER.info("Not authenticated — waiting for login via web UI")


async def _run_bridge(bridge: Bridge):
    try:
        _LOGGER.info("Bridge starting")
        await bridge.run()
    except asyncio.CancelledError:
        bridge.stop()
        raise
    except Exception as e:
        _LOGGER.error("Bridge crashed: %s", e)


def run():
    asyncio.run(main())


if __name__ == "__main__":
    run()
