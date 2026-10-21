#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Mint on demand: the app's model, not a 45s rotation.

The XPENG app has no mint timer. ``MqttAccountManager.hasAccount()`` decides
whether the cached credential is usable (uid matches, ``expireTime`` in the
future), and ``MqttClient.checkConnect()`` mints only when it is not - right
before connecting. A live session is never rotated.

The integration used to mint every 45s instead. Every mint is another chance for
the broker to see a password the rotation has superseded, and the renewal app's
validity poll reads the credentials sensor - so rotating while connected also
risked triggering a renewal that evicts the very session it observed.

What this module pins:

1. the expiry is taken from the mint (``expireTime`` is epoch milliseconds);
2. an expired/unknown credential is replaced through the provider before the
   CONNECT, and a live one is not;
3. a provider failure falls back to the credential in hand rather than refusing
   to connect;
4. a healthy connection counts as evidence for the credentials sensor, because
   there are no mints while connected to stamp it with.

Loaded by path with a `paho` stub - mqtt_client imports paho, which is not
installed on this host, but it does not import Home Assistant.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
import types
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import _component  # noqa: E402

MODULE_PATH = _component.module_path("mqtt_client.py")


def _stub_paho() -> None:
    try:
        import paho.mqtt.client  # noqa: F401

        return
    except ImportError:
        pass

    class _Any(types.ModuleType):
        def __getattr__(self, name):
            child = _Any(f"{self.__name__}.{name}")
            setattr(self, name, child)
            return child

    paho = _Any("paho")
    mqtt = _Any("paho.mqtt")
    client = _Any("paho.mqtt.client")
    mqtt.client = client
    paho.mqtt = mqtt
    sys.modules.setdefault("paho", paho)
    sys.modules.setdefault("paho.mqtt", mqtt)
    sys.modules.setdefault("paho.mqtt.client", client)


_stub_paho()
_spec = importlib.util.spec_from_file_location(_component.mqtt_test_module_name("mqtt_client_mint_test"), MODULE_PATH)
assert _spec and _spec.loader
mqtt_client = importlib.util.module_from_spec(_spec)
sys.modules["mqtt_client_mint_test"] = mqtt_client
_spec.loader.exec_module(mqtt_client)


def _client(**kwargs):
    return mqtt_client.XPengMQTTClient(
        client_id="xmart:app:10225403:4dc51b29c3dee8b8ea6f7648ce407ffa",
        password="P1",
        server="iot-app-global.xiaopeng.com:8885",
        user_id="10225403",
        **kwargs,
    )


def _minted(password="P2", expires_in=70.0):
    return {
        "clientId": "xmart:app:10225403:4dc51b29c3dee8b8ea6f7648ce407ffa",
        "password": password,
        "serverSsl": "iot-app-global.xiaopeng.com:8885",
        "expires_at": mqtt_client.time.time() + expires_in,
    }


class TestExpiry(unittest.TestCase):
    def test_epoch_milliseconds_are_converted(self):
        self.assertAlmostEqual(
            mqtt_client.expires_at_from_account({"expireTime": 1792231242220}),
            1792231242.22,
            places=2,
        )

    def test_expire_in_seconds_is_relative_to_now(self):
        now = mqtt_client.time.time()
        result = mqtt_client.expires_at_from_account({"expireInSec": 72})
        self.assertAlmostEqual(result, now + 72, delta=2)

    def test_missing_or_garbage_is_unknown(self):
        for payload in (None, {}, {"expireTime": None}, {"expireTime": "x"}):
            self.assertEqual(mqtt_client.expires_at_from_account(payload), 0.0)

    def test_unknown_expiry_counts_as_expired(self):
        self.assertTrue(_client().credentials_expired)

    def test_a_live_credential_is_not_expired(self):
        self.assertFalse(_client(expires_at=mqtt_client.time.time() + 70).credentials_expired)

    def test_the_margin_makes_a_credential_expire_early(self):
        almost = mqtt_client.time.time() + mqtt_client.CREDENTIAL_EXPIRY_MARGIN - 1
        self.assertTrue(_client(expires_at=almost).credentials_expired)


class TestProvider(unittest.TestCase):
    def test_expired_credential_is_replaced_before_connecting(self):
        c = _client()  # no expiry => unknown => expired
        provider = mock.Mock(return_value=_minted("P2"))
        c.set_credentials_provider(provider)
        c._ensure_credentials()
        provider.assert_called_once()
        self.assertEqual(c._password, "P2")
        self.assertFalse(c.credentials_expired)

    def test_live_credential_is_not_re_minted(self):
        c = _client(expires_at=mqtt_client.time.time() + 70)
        provider = mock.Mock(return_value=_minted())
        c.set_credentials_provider(provider)
        c._ensure_credentials()
        provider.assert_not_called()
        self.assertEqual(c._password, "P1")

    def test_provider_failure_keeps_the_credential_in_hand(self):
        c = _client()
        c.set_credentials_provider(mock.Mock(side_effect=RuntimeError("network")))
        c._ensure_credentials()   # must not raise
        self.assertEqual(c._password, "P1")

    def test_provider_returning_nothing_keeps_the_credential_in_hand(self):
        c = _client()
        c.set_credentials_provider(mock.Mock(return_value=None))
        c._ensure_credentials()
        self.assertEqual(c._password, "P1")

    def test_connect_mints_before_building_the_client(self):
        """The provider is called from connect(), not from a timer."""
        c = _client()
        provider = mock.Mock(return_value=_minted("P3"))
        c.set_credentials_provider(provider)

        class _FakePahoClient:
            def __init__(self, **kwargs):
                self.credentials = None

            def username_pw_set(self, username, password=None):
                self.credentials = (username, password)

            def tls_set_context(self, context):
                pass

            def reconnect_delay_set(self, **kwargs):
                pass

            def connect(self, host, port, keepalive=60):
                pass

            def loop_start(self):
                pass

        class _FakeMqttModule:
            MQTTv311 = 4

            class CallbackAPIVersion:
                VERSION2 = "VERSION2"

            Client = _FakePahoClient

        with mock.patch.object(mqtt_client, "mqtt", _FakeMqttModule), \
             mock.patch.object(mqtt_client, "build_tls_context", mock.Mock()), \
             mock.patch.object(mqtt_client.time, "sleep", mock.Mock()):
            c.connect()
            self.assertEqual(c._client.credentials[1], "P3")
        provider.assert_called_once()
        self.assertEqual(c._password, "P3")

    def test_stage_credentials_records_the_expiry(self):
        c = _client()
        expires = mqtt_client.time.time() + 55
        c.stage_credentials(password="P9", expires_at=expires)
        self.assertEqual(c.expires_at, expires)
        self.assertFalse(c.credentials_expired)


class TestConnectedEvidence(unittest.TestCase):
    """The sensor rule lives in health.py; this is the input it now needs."""

    def test_connected_is_exposed_for_the_sensor(self):
        c = _client()
        self.assertFalse(c.connected)
        c._connected = True
        self.assertTrue(c.connected)


if __name__ == "__main__":
    unittest.main(verbosity=2)
