#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The identity-scoped MQTT topic path: {device_id} / {user_id} expansion.

The XPENG broker scopes topics by identity: vehicle state by the account uid
(``Xmart/Msg/User/{uid}``) and one push channel by the *device* id
(``Xp/Msg/Push/<deviceId>``, which the app subscribes on every connect). The
listener option therefore accepts placeholders and expands them from the
identity this client actually authenticated as - the device id the broker minted
into the clientId. "The app subscribes it" and "this account is granted it" are
different questions; the listener sensor's SUBACK record answers the second, and
these tests pin how the topic is built for that probe.

    python3 tests/test_mqtt_device_topic.py
    python3 -m unittest discover -s tests -v

paho is stubbed when it is not installed, so the topic logic is testable on any
machine (the module only needs paho at connect time).
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
import types
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import _component  # noqa: E402

DEVICE_ID = "4dc51b29c3dee8b8ea6f7648ce407ffa"   # 32 hex, what the broker mints
USER_ID = "10225403"
CLIENT_ID = f"xmart:app:{USER_ID}:{DEVICE_ID}"


def _stub_paho() -> None:
    """Provide a stand-in for paho when it is not installed."""
    try:
        import paho.mqtt.client  # noqa: F401

        return
    except ImportError:
        pass

    paho = types.ModuleType("paho")
    mqtt_pkg = types.ModuleType("paho.mqtt")
    client_mod = types.ModuleType("paho.mqtt.client")

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

    class _CallbackAPIVersion:
        VERSION2 = "VERSION2"

    client_mod.Client = _Client
    client_mod.MQTTv311 = 4
    client_mod.CallbackAPIVersion = _CallbackAPIVersion
    paho.mqtt = mqtt_pkg
    mqtt_pkg.client = client_mod
    sys.modules.update(
        {"paho": paho, "paho.mqtt": mqtt_pkg, "paho.mqtt.client": client_mod}
    )


def _load() -> types.ModuleType:
    _stub_paho()
    path = _component.module_path("mqtt_client.py")
    spec = importlib.util.spec_from_file_location(_component.mqtt_test_module_name("mqtt_client_device_topic"), path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["mqtt_client_device_topic"] = module
    spec.loader.exec_module(module)
    return module


mqtt_client = _load()


def _client(client_id: str = CLIENT_ID, device_id: str = "") -> object:
    return mqtt_client.XPengMQTTClient(
        client_id=client_id,
        password="pw",
        server="broker.example:8885",
        user_id=USER_ID,
        device_id=device_id,
    )


class TestPlaceholderExpansion(unittest.TestCase):
    def test_parse_expands_both_placeholders(self):
        topics = mqtt_client.parse_listener_topics(
            "Xp/Msg/Push/{device_id}, Mona/Msg/User/{user_id}",
            device_id=DEVICE_ID,
            user_id=USER_ID,
        )
        self.assertEqual(
            topics,
            [f"Xp/Msg/Push/{DEVICE_ID}", f"Mona/Msg/User/{USER_ID}"],
        )

    def test_parse_without_values_keeps_placeholder(self):
        self.assertEqual(
            mqtt_client.parse_listener_topics("Xp/Msg/Push/{device_id}"),
            ["Xp/Msg/Push/{device_id}"],
        )

    def test_unknown_placeholder_is_not_touched(self):
        self.assertEqual(
            mqtt_client.parse_listener_topics(
                "Xmart/Car/+/State/{vid}", device_id=DEVICE_ID
            ),
            ["Xmart/Car/+/State/{vid}"],
        )
        self.assertEqual(
            mqtt_client.unresolved_topic_placeholders("Xmart/Car/+/State/{vid}"),
            [],
        )

    def test_512_cap_applies_after_expansion(self):
        # 500 + 12 = 511 before expansion: kept as typed.
        self.assertEqual(
            mqtt_client.parse_listener_topics("T" * 500 + "{device_id}"),
            ["T" * 500 + "{device_id}"],
        )
        # Same template, 32-char id: 532 > 512, dropped.
        self.assertEqual(
            mqtt_client.parse_listener_topics(
                "T" * 500 + "{device_id}", device_id=DEVICE_ID
            ),
            [],
        )

    def test_blank_and_comma_noise_are_still_ignored(self):
        self.assertEqual(
            mqtt_client.parse_listener_topics(" , ,\nXmart/Msg/User/{user_id}\n\n"),
            ["Xmart/Msg/User/{user_id}"],
        )


class TestDeviceIdentity(unittest.TestCase):
    def test_device_id_comes_from_the_client_id(self):
        client = _client()
        self.assertEqual(client.device_id, DEVICE_ID)
        self.assertEqual(client.push_topic, f"Xp/Msg/Push/{DEVICE_ID}")

    def test_explicit_device_id_wins(self):
        other = "a" * 32
        client = _client(device_id=other)
        self.assertEqual(client.device_id, other)

    def test_three_field_client_id_has_no_device_id(self):
        client = _client(client_id=f"xmart:app:{USER_ID}")
        self.assertEqual(client.device_id, "")
        self.assertEqual(client.push_topic, "")

    def test_set_device_id_fills_a_blank_identity(self):
        client = _client(client_id=f"xmart:app:{USER_ID}")
        client.set_device_id(DEVICE_ID)
        self.assertEqual(client.push_topic, f"Xp/Msg/Push/{DEVICE_ID}")

    def test_app_style_client_id_with_version_suffix_still_parses(self):
        # The app appends `:2.0:V<versionName>`; the device segment is unchanged.
        client = _client(client_id=f"{CLIENT_ID}:2.0:V3.11.0")
        self.assertEqual(client.device_id, DEVICE_ID)


class TestConfigureListener(unittest.TestCase):
    def test_topics_are_expanded_before_subscribing(self):
        client = _client()
        client.configure_listener(
            enabled=True,
            topics=["Xp/Msg/Push/{device_id}", "Mona/Msg/User/{user_id}"],
            discovery_enabled=False,
        )
        self.assertEqual(
            client._listener_topics,
            [f"Xp/Msg/Push/{DEVICE_ID}", f"Mona/Msg/User/{USER_ID}"],
        )

    def test_unresolvable_placeholder_is_dropped_not_subscribed_literally(self):
        client = _client(client_id=f"xmart:app:{USER_ID}")  # no device id
        client.configure_listener(
            enabled=True,
            topics=["Xp/Msg/Push/{device_id}", "Xmart/Msg/User/{user_id}"],
            discovery_enabled=False,
        )
        self.assertEqual(client._listener_topics, [f"Xmart/Msg/User/{USER_ID}"])

    def test_discovery_candidates_are_still_appended_once(self):
        client = _client()
        client.configure_listener(
            enabled=True,
            topics=["Xmart/Msg/User/{user_id}"],
            discovery_enabled=True,
            discovery_candidates=("Xmart/Tbox/+/StatPack", "Xmart/Tbox/+/StatPack"),
        )
        self.assertEqual(
            client._listener_topics,
            [f"Xmart/Msg/User/{USER_ID}", "Xmart/Tbox/+/StatPack"],
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
