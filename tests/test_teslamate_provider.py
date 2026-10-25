#!/usr/bin/env python3
"""The Tesla-compatible provider, exercised over real HTTP and a real websocket.

    python3 tests/test_teslamate_provider.py

This test needs ``aiohttp`` (which Home Assistant ships with). On a machine
without it the module reports the skip and exits 0, so the repo's plain-python
test runner stays green:

    python3 -m venv /tmp/provtest && /tmp/provtest/bin/pip install aiohttp
    /tmp/provtest/bin/python tests/test_teslamate_provider.py

It walks the exact sequence TeslaMate 4.4.0 performs against a third-party
provider: POST the token endpoint, GET /api/1/products, GET the vehicle, GET
vehicle_data with the endpoint list query, then open /streaming/ and subscribe.
The JSON is then checked against the rules in teslamate's own code - integer
ids, all five sections, millisecond timestamps, an integer ``expires_in`` and a
non-empty ``refresh_token``, and a 13-field stream value decoded the way
``TeslaApi.Stream.Data`` decodes it.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import pathlib
import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _component  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PKG = _component.component_dir()
TOKEN = "test-provider-token"

try:
    import aiohttp

    HAVE_AIOHTTP = True
except ImportError:  # pragma: no cover - the repo's plain-python environment
    HAVE_AIOHTTP = False


#: A parked G6 snapshot, the same shape a merged MQTT payload has.
PARKED_RAW = {
    "instant": {
        "charge": {
            "battery_soc": 64,
            "max_range_charge": 90,
            "charging_state": 15,
            "charge_gun_status": 0,
            "charge_connector_status": 7,
            "slow_charge_port_status": 2,
        },
        "odometer": {"total_mileage": 12345.6, "avalible_driving_distance": 412.0},
        "drive": {"shift_state": 3},
        "ext": {"vehicleMode": 0},
        "hvac": {"hvac_on": 0, "hvac_inner_temp_float": 21.5,
                 "hvac_temp_float": 22.0, "hvac_wind_speed": 0},
        "door": {"driver_door_lock_st": 1, "driver_door_ajar_st": 0,
                 "passenger_door_ajar_st": 0, "rear_left_door_ajar_st": 0,
                 "rear_right_door_ajar_st": 0, "bonnet_ajar_st": 0},
        "tail": {"tail_status": 0},
        "tpms": {"tpms_pressure_fl": 250.0, "tpms_pressure_fr": 251.0,
                 "tpms_pressure_rl": 249.0, "tpms_pressure_rr": 248.0},
    },
    "interval": {
        "drive": {"latitude": -33.8688, "longitude": 151.2093,
                  "speed": 0, "angle": 90},
        "charge": {"power": 0, "voltage": 0, "electricity": 0},
    },
}

DRIVING_RAW = json.loads(json.dumps(PARKED_RAW))
DRIVING_RAW["instant"]["drive"]["shift_state"] = 1
DRIVING_RAW["instant"]["ext"]["vehicleMode"] = 2
DRIVING_RAW["interval"]["drive"]["speed"] = 60.0


def _load_modules():
    """Import the integration's modules as ``custom_components.xpeng.*``.

    The provider uses relative imports, so it needs its package to exist in
    ``sys.modules``; nothing here imports Home Assistant.
    """
    root = PKG.parent
    for name in ("custom_components", "custom_components.xpeng"):
        if name not in sys.modules:
            module = types.ModuleType(name)
            module.__path__ = [str(root if name == "custom_components" else PKG)]
            sys.modules[name] = module
    mapping = importlib.import_module("custom_components.xpeng.teslamate_mapping")
    provider = importlib.import_module("custom_components.xpeng.teslamate_provider")
    return mapping, provider


class _RecordingSource:
    def __init__(self, snapshots):
        self._snapshots = list(snapshots)
        self._listeners = []

    def snapshots(self):
        return self._snapshots

    def update(self, snapshots):
        self._snapshots = list(snapshots)
        for listener in list(self._listeners):
            listener()

    def add_listener(self, callback):
        self._listeners.append(callback)

        def remove():
            if callback in self._listeners:
                self._listeners.remove(callback)

        return remove


if HAVE_AIOHTTP:  # pragma: no branch - the whole suite is skipped without it
    mapping, provider_mod = _load_modules()

    def snapshot(raw, **overrides):
        values = dict(
            vin="L1NNSGHA0SB000000",
            name="G6",
            series="G6",
            raw=raw,
            now_ms=1_700_000_000_000,
            data_ms=1_700_000_000_000,
            data_age_seconds=0.0,
        )
        values.update(overrides)
        return mapping.Telemetry(**values)

    COLUMNS = ("time", *mapping.STREAM_COLUMNS)

    def decode_stream_value(value: str) -> dict:
        """Mirror TeslaApi.Stream.decode_frame! + Data.into!."""
        parts = value.split(",")
        assert len(parts) == 13, f"expected 13 fields, got {len(parts)}: {value!r}"
        raw = dict(zip(COLUMNS, parts))
        decoded = {}
        for key in COLUMNS:
            text = raw[key]
            if text == "":
                decoded[key] = None
            elif key in ("est_lat", "est_lng", "odometer"):
                decoded[key] = float(text)
            elif key == "shift_state":
                decoded[key] = text
            else:
                decoded[key] = int(text)
        return decoded

    class TestTeslaMateProvider(unittest.IsolatedAsyncioTestCase):
        async def asyncSetUp(self):
            self.source = _RecordingSource([snapshot(PARKED_RAW)])
            self.provider = provider_mod.TeslaMateProvider(
                self.source,
                provider_mod.ProviderOptions(
                    token=TOKEN,
                    port=0,
                    bind="127.0.0.1",
                    heartbeat_seconds=0.2,
                ),
            )
            await self.provider.async_start()
            self.session = aiohttp.ClientSession()
            self.auth = {"Authorization": f"Bearer {TOKEN}"}

        async def asyncTearDown(self):
            await self.session.close()
            await self.provider.async_stop()

        # -- helpers ---------------------------------------------------

        async def get_json(self, path, auth=True):
            headers = dict(self.auth) if auth else {}
            async with self.session.get(self.provider.url + path, headers=headers) as response:
                return response.status, response.content_type, await response.json()

        async def vehicle(self):
            status, _, body = await self.get_json("/api/1/products")
            self.assertEqual(status, 200)
            return body["response"][0]

        # -- auth ------------------------------------------------------

        async def test_token_endpoint_matches_the_refresh_contract(self):
            # TeslaMate posts a JSON body; only the bearer token matters here.
            async with self.session.post(
                self.provider.url + "/oauth2/v3/token",
                headers=self.auth,
                json={
                    "grant_type": "refresh_token",
                    "scope": "openid email offline_access",
                    "client_id": "ownerapi",
                    "refresh_token": None,
                },
            ) as response:
                self.assertEqual(response.status, 200)
                self.assertTrue(response.content_type.startswith("application/json"))
                body = await response.json()
            self.assertEqual(body["access_token"], TOKEN)
            # Auth.save/1 validates both fields, so the first response must
            # carry a non-empty refresh token even though it was posted null.
            self.assertTrue(body["refresh_token"])
            self.assertIsInstance(body["expires_in"], int)
            self.assertEqual(body["token_type"], "Bearer")

        async def test_token_endpoint_requires_the_provider_token(self):
            async with self.session.post(
                self.provider.url + "/oauth2/v3/token", json={}
            ) as response:
                self.assertEqual(response.status, 401)
                body = await response.json()
            self.assertEqual(body["error"], "invalid_token")

        async def test_api_requires_bearer(self):
            async with self.session.get(self.provider.url + "/api/1/products") as response:
                self.assertEqual(response.status, 401)
                self.assertEqual(response.content_type, "application/json")

        async def test_wrong_bearer_is_401(self):
            async with self.session.get(
                self.provider.url + "/api/1/products",
                headers={"Authorization": "Bearer wrong"},
            ) as response:
                self.assertEqual(response.status, 401)

        async def test_healthz_is_open(self):
            async with self.session.get(self.provider.url + "/healthz") as response:
                self.assertEqual(response.status, 200)
                body = await response.json()
            self.assertTrue(body["ok"])
            self.assertTrue(body["token_required"])

        # -- product listing ------------------------------------------

        async def test_products_list(self):
            status, content_type, body = await self.get_json("/api/1/products")
            self.assertEqual(status, 200)
            self.assertEqual(content_type, "application/json")
            self.assertIsInstance(body["response"], list)
            self.assertEqual(body["count"], 1)
            vehicle = body["response"][0]
            # TeslaMate stores these in integer columns and silently drops list
            # entries without "vehicle_id".
            self.assertIsInstance(vehicle["id"], int)
            self.assertIsInstance(vehicle["vehicle_id"], int)
            self.assertEqual(vehicle["vin"], "L1NNSGHA0SB000000")
            self.assertIn(vehicle["state"], ("online", "asleep", "offline"))
            self.assertIsInstance(vehicle["display_name"], str)

        async def test_vehicle_by_id(self):
            vehicle = await self.vehicle()
            status, _, body = await self.get_json(
                f"/api/1/vehicles/{vehicle['id']}"
            )
            self.assertEqual(status, 200)
            self.assertEqual(body["response"]["id"], vehicle["id"])
            # identify/1 needs this object on the online transition.
            self.assertIsInstance(body["response"]["vehicle_config"], dict)

        async def test_unknown_vehicle_is_not_found(self):
            status, _, body = await self.get_json("/api/1/vehicles/999999999")
            self.assertEqual(status, 404)
            self.assertEqual(body["error"], "not_found")

        # -- vehicle_data ---------------------------------------------

        async def test_vehicle_data_matches_teslamates_acceptance_guard(self):
            vehicle = await self.vehicle()
            status, content_type, body = await self.get_json(
                "/api/1/vehicles/{}/vehicle_data?endpoints="
                "charge_state;climate_state;closures_state;drive_state;"
                "gui_settings;location_data;vehicle_config;vehicle_state;"
                "vehicle_data_combo".format(vehicle["id"])
            )
            self.assertEqual(status, 200)
            self.assertEqual(content_type, "application/json")
            data = body["response"]
            # The guard in vehicle.ex requires all five to be structs.
            for section in ("drive_state", "charge_state", "climate_state",
                            "vehicle_state", "vehicle_config"):
                self.assertIsInstance(data[section], dict, section)
            for section in ("drive_state", "charge_state", "climate_state",
                            "vehicle_state"):
                self.assertIsInstance(data[section]["timestamp"], int, section)
            self.assertEqual(data["drive_state"]["latitude"], -33.8688)
            self.assertIsInstance(data["drive_state"]["power"], int)
            self.assertIn(data["drive_state"]["shift_state"], ("P", "R", "N", "D"))
            self.assertIsNone(data["charge_state"]["charger_phases"])
            self.assertEqual(data["vehicle_state"]["locked"], True)
            json.dumps(data)

        async def test_timestamps_never_go_backwards(self):
            vehicle = await self.vehicle()
            first = (await self.get_json(
                f"/api/1/vehicles/{vehicle['id']}/vehicle_data"))[2]["response"]
            second = (await self.get_json(
                f"/api/1/vehicles/{vehicle['id']}/vehicle_data"))[2]["response"]
            self.assertGreaterEqual(
                second["drive_state"]["timestamp"], first["drive_state"]["timestamp"]
            )
            self.assertGreater(
                second["drive_state"]["timestamp"], 1_000_000_000_000
            )

        async def test_charge_energy_is_tracked_across_a_session(self):
            vehicle = await self.vehicle()
            # Start charging at 64%, climb to 70% on an 87.5 kWh pack.
            charging = json.loads(json.dumps(PARKED_RAW))
            charging["instant"]["charge"].update(
                {"charging_state": 2, "charge_gun_status": 4}
            )
            charging["interval"]["charge"]["power"] = 11.0
            self.source.update([snapshot(charging)])
            await self.get_json(f"/api/1/vehicles/{vehicle['id']}/vehicle_data")

            charging["instant"]["charge"]["battery_soc"] = 70
            self.source.update([snapshot(charging)])
            _, _, body = await self.get_json(
                f"/api/1/vehicles/{vehicle['id']}/vehicle_data"
            )
            charge = body["response"]["charge_state"]
            self.assertEqual(charge["charging_state"], "Charging")
            self.assertAlmostEqual(charge["charge_energy_added"], 5.25, places=2)

        async def test_charging_grace_bridges_a_blip_but_not_an_unplug(self):
            vehicle = await self.vehicle()
            charging = json.loads(json.dumps(PARKED_RAW))
            charging["instant"]["charge"].update(
                {"charging_state": 2, "charge_gun_status": 4}
            )
            charging["interval"]["charge"]["power"] = 11.0
            self.source.update([snapshot(charging)])
            await self.get_json(f"/api/1/vehicles/{vehicle['id']}/vehicle_data")

            # The measured G6 behaviour: the state code alternates to 15
            # mid-session while the cable stays in. That must not end the
            # TeslaMate session.
            blip = json.loads(json.dumps(charging))
            blip["instant"]["charge"]["charging_state"] = 15
            blip["interval"]["charge"]["power"] = 0
            self.source.update([snapshot(blip)])
            _, _, body = await self.get_json(
                f"/api/1/vehicles/{vehicle['id']}/vehicle_data"
            )
            self.assertEqual(body["response"]["charge_state"]["charging_state"],
                             "Charging")

            # Unplugging is real evidence and ends it immediately, so a drive
            # straight after a charge is not delayed by the grace period.
            unplugged = json.loads(json.dumps(blip))
            unplugged["instant"]["charge"]["charge_gun_status"] = 0
            unplugged["instant"]["charge"]["charge_connector_status"] = 7
            self.source.update([snapshot(unplugged)])
            _, _, body = await self.get_json(
                f"/api/1/vehicles/{vehicle['id']}/vehicle_data"
            )
            self.assertNotIn(
                body["response"]["charge_state"]["charging_state"],
                ("Charging", "Starting"),
            )

        async def test_asleep_state_and_quiet_stream(self):
            vehicle = await self.vehicle()
            self.source.update([
                snapshot(PARKED_RAW, data_age_seconds=mapping.ASLEEP_SECONDS + 1)
            ])
            _, _, body = await self.get_json(
                f"/api/1/vehicles/{vehicle['id']}/vehicle_data"
            )
            self.assertEqual(body["response"]["state"], "asleep")

        async def test_no_telemetry_at_all_is_offline(self):
            vehicle = await self.vehicle()
            self.source.update([
                snapshot({}, data_ms=0, data_age_seconds=None, has_data=False)
            ])
            _, _, body = await self.get_json(f"/api/1/vehicles/{vehicle['id']}")
            self.assertEqual(body["response"]["state"], "offline")

        # -- streaming -------------------------------------------------

        async def test_stream_subscribe_and_update(self):
            vehicle = await self.vehicle()
            ws_url = self.provider.stream_url + "/streaming/"
            async with self.session.ws_connect(ws_url) as ws:
                await ws.send_json({
                    "msg_type": "data:subscribe_oauth",
                    "token": TOKEN,
                    "value": mapping.subscribe_value(),
                    "tag": str(vehicle["vehicle_id"]),
                })
                message = await asyncio.wait_for(ws.receive_json(), timeout=5)
                self.assertEqual(message["msg_type"], "data:update")
                self.assertEqual(message["tag"], str(vehicle["vehicle_id"]))
                decoded = decode_stream_value(message["value"])
                self.assertEqual(decoded["shift_state"], "P")
                self.assertIsNotNone(decoded["power"])
                self.assertAlmostEqual(decoded["est_lat"], -33.8688, places=4)

                # New telemetry must push a new frame at once, without waiting
                # for the next poll.
                self.source.update([snapshot(DRIVING_RAW)])
                message = await asyncio.wait_for(ws.receive_json(), timeout=5)
                decoded = decode_stream_value(message["value"])
                self.assertEqual(decoded["shift_state"], "D")
                self.assertGreater(decoded["speed"], 0)

        async def test_stream_rejects_a_bad_token_the_way_teslamate_expects(self):
            # Any client_error value other than this prefix raises on TeslaMate's
            # side; "Can't validate token" is the one it handles.
            async with self.session.ws_connect(
                self.provider.stream_url + "/streaming/"
            ) as ws:
                await ws.send_json({
                    "msg_type": "data:subscribe_oauth",
                    "token": "wrong",
                    "value": mapping.subscribe_value(),
                    "tag": "123",
                })
                message = await asyncio.wait_for(ws.receive_json(), timeout=5)
            self.assertEqual(message["msg_type"], "data:error")
            self.assertEqual(message["error_type"], "client_error")
            self.assertTrue(message["value"].startswith("Can't validate token"))

        async def test_stream_heartbeat_keeps_the_connection_alive(self):
            vehicle = await self.vehicle()
            async with self.session.ws_connect(
                self.provider.stream_url + "/streaming/"
            ) as ws:
                await ws.send_json({
                    "msg_type": "data:subscribe_oauth",
                    "token": TOKEN,
                    "value": mapping.subscribe_value(),
                    "tag": str(vehicle["vehicle_id"]),
                })
                first = await asyncio.wait_for(ws.receive_json(), timeout=5)
                # heartbeat_seconds=0.2 in the test provider.
                second = await asyncio.wait_for(ws.receive_json(), timeout=5)
            self.assertEqual(first["msg_type"], "data:update")
            self.assertEqual(second["msg_type"], "data:update")

        async def test_stream_is_quiet_when_the_car_is_asleep(self):
            # Subscribe while awake, then let the car fall asleep: no frames, so
            # TeslaMate's 30s stream timeout handles it exactly as with a Tesla.
            vehicle = await self.vehicle()
            async with self.session.ws_connect(
                self.provider.stream_url + "/streaming/"
            ) as ws:
                await ws.send_json({
                    "msg_type": "data:subscribe_oauth",
                    "token": TOKEN,
                    "value": mapping.subscribe_value(),
                    "tag": str(vehicle["vehicle_id"]),
                })
                await asyncio.wait_for(ws.receive_json(), timeout=5)
                self.source.update([
                    snapshot(PARKED_RAW,
                             data_age_seconds=mapping.ASLEEP_SECONDS + 1)
                ])
                with self.assertRaises(asyncio.TimeoutError):
                    await asyncio.wait_for(ws.receive_json(), timeout=1.0)



    # ── Home Assistant wiring, against the real coordinator class ──────────

    def _load_real_coordinator():
        """Import ``coordinator.py`` with stand-ins for its Home Assistant bits.

        The point is to run ``async_start_teslamate_provider`` - the function the
        integration actually calls - against the real ``XPengDataCoordinator``
        class rather than a duck-typed object, so an attribute rename or a
        changed listener API fails here instead of on a user's install.
        """

        class _CoordinatorBase:
            def __class_getitem__(cls, item):  # noqa: N805
                return cls

            def __init__(self, *args, **kwargs):
                self._listeners = []

            def async_add_listener(self, callback, context=None):
                self._listeners.append(callback)

                def remove():
                    if callback in self._listeners:
                        self._listeners.remove(callback)

                return remove

            def async_set_updated_data(self, data):
                self.data = data
                for callback in list(self._listeners):
                    callback()

        ha = types.ModuleType("homeassistant")
        core = types.ModuleType("homeassistant.core")
        core.HomeAssistant = type("HomeAssistant", (), {})
        core.callback = lambda fn: fn
        helpers = types.ModuleType("homeassistant.helpers")
        storage = types.ModuleType("homeassistant.helpers.storage")
        storage.Store = type("Store", (), {})
        updater = types.ModuleType("homeassistant.helpers.update_coordinator")
        updater.DataUpdateCoordinator = _CoordinatorBase
        for name, module in (
            ("homeassistant", ha),
            ("homeassistant.core", core),
            ("homeassistant.helpers", helpers),
            ("homeassistant.helpers.storage", storage),
            ("homeassistant.helpers.update_coordinator", updater),
        ):
            sys.modules[name] = module
        ha.core = core
        ha.helpers = helpers

        const = types.ModuleType("custom_components.xpeng.const")
        const.DOMAIN = "xpeng"
        sys.modules["custom_components.xpeng.const"] = const
        # Other test modules install lightweight XPeng module stubs in
        # sys.modules. Reuse of one of those stubs made this integration test
        # order-dependent and could mask the real coordinator behavior.
        for name in (
            "command_policy", "drive_evidence", "energy_guard", "replay_guard",
            "vin_descriptor", "coordinator",
        ):
            sys.modules.pop(f"custom_components.xpeng.{name}", None)
        for name in ("command_policy", "vin_descriptor", "coordinator"):
            full = f"custom_components.xpeng.{name}"
            spec = importlib.util.spec_from_file_location(full, PKG / f"{name}.py")
            module = importlib.util.module_from_spec(spec)
            sys.modules[full] = module
            spec.loader.exec_module(module)
        return sys.modules["custom_components.xpeng.coordinator"]

    class TestHomeAssistantWiring(unittest.IsolatedAsyncioTestCase):
        """The integration's own start helper, over the real coordinator class."""

        async def asyncSetUp(self):
            self.coordinator_module = _load_real_coordinator()
            self.raw = {"code": 200, "msg_content": PARKED_RAW}
            coordinator = object.__new__(
                self.coordinator_module.XPengDataCoordinator
            )
            coordinator._listeners = []
            coordinator.vin = "L1NNSGHA0SB000000"
            coordinator._last_full_data = self.raw
            coordinator.data = self.raw
            coordinator.poll_status = {
                "meaningful_silence_seconds": 5.0,
                "mqtt_connected": True,
            }
            coordinator.weather = {"realtimeTemperature": 19.5}
            coordinator.vehicle_appearance = {"colorName": "Silver"}
            coordinator.usable_battery_kwh = 87.5
            coordinator.xos_version = "XOS 5.6.0"
            coordinator.nick_name = "G6"
            coordinator.series_name = "G6"
            coordinator.vehicle_type_name = None
            from datetime import datetime, timezone

            coordinator.last_data_timestamp = datetime.now(timezone.utc)
            self.coordinator = coordinator
            self.provider = await provider_mod.async_start_teslamate_provider(
                coordinator,
                provider_mod.ProviderOptions(
                    token=TOKEN, port=0, bind="127.0.0.1",
                    heartbeat_seconds=0.2,
                ),
            )
            self.assertIsNotNone(self.provider)
            self.session = aiohttp.ClientSession()
            self.auth = {"Authorization": f"Bearer {TOKEN}"}

        async def asyncTearDown(self):
            await self.session.close()
            if self.provider is not None:
                await self.provider.async_stop()

        async def test_the_real_coordinator_becomes_a_vehicle(self):
            async with self.session.get(
                self.provider.url + "/api/1/products", headers=self.auth
            ) as response:
                body = await response.json()
            vehicle = body["response"][0]
            self.assertEqual(vehicle["state"], "online")
            self.assertEqual(vehicle["display_name"], "G6")

            async with self.session.get(
                self.provider.url
                + f"/api/1/vehicles/{vehicle['id']}/vehicle_data",
                headers=self.auth,
            ) as response:
                data = (await response.json())["response"]
            # Values only the coordinator attributes can supply.
            self.assertEqual(data["vehicle_state"]["vehicle_name"], "G6")
            self.assertEqual(data["vehicle_config"]["exterior_color"], "Silver")
            self.assertEqual(data["vehicle_state"]["car_version"], "XOS 5.6.0")
            self.assertAlmostEqual(data["climate_state"]["outside_temp"], 19.5)
            self.assertEqual(data["charge_state"]["battery_level"], 64)

        async def test_a_coordinator_push_reaches_the_stream(self):
            async with self.session.get(
                self.provider.url + "/api/1/products", headers=self.auth
            ) as response:
                vehicle = (await response.json())["response"][0]
            async with self.session.ws_connect(
                self.provider.stream_url + "/streaming/"
            ) as ws:
                await ws.send_json({
                    "msg_type": "data:subscribe_oauth",
                    "token": TOKEN,
                    "value": mapping.subscribe_value(),
                    "tag": str(vehicle["vehicle_id"]),
                })
                first = decode_stream_value(
                    (await asyncio.wait_for(ws.receive_json(), timeout=5))["value"]
                )
                self.assertEqual(first["shift_state"], "P")

                # This is exactly what a real MQTT push does: async_set_updated_data
                # runs the provider's listener, which broadcasts a frame.
                self.coordinator.async_set_updated_data(
                    {"code": 200, "msg_content": DRIVING_RAW}
                )
                # A heartbeat may publish the cached P frame after the D
                # update. Assert that the coordinator's push reaches the
                # stream within the deadline instead of assuming the last of
                # an arbitrary number of frames must still be the newest.
                driving_frame = None
                loop = asyncio.get_running_loop()
                deadline = loop.time() + 5
                while loop.time() < deadline and driving_frame is None:
                    message = await asyncio.wait_for(
                        ws.receive_json(), timeout=max(0.01, deadline - loop.time())
                    )
                    frame = decode_stream_value(message["value"])
                    if frame["shift_state"] == "D" and frame["speed"] > 0:
                        driving_frame = frame
                self.assertIsNotNone(
                    driving_frame, "the MQTT-like coordinator push never reached the stream"
                )


else:

    class TestTeslaMateProviderSkipped(unittest.TestCase):
        @unittest.skip(
            "aiohttp is not installed; run this file with a venv that has it "
            "(the provider itself runs inside Home Assistant, which ships aiohttp)"
        )
        def test_http_and_websocket_contract(self):
            pass


if __name__ == "__main__":
    unittest.main(verbosity=2)
