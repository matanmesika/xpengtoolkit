#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A refused CONNECT is not always an auth failure — and must not always mint.

Measured on both live installs, 2026-10-17:

* `.88`  20:18:48 takeover notice → `Not authorized` 20:18:58, 20:19:11 →
  connected again 20:19:34.
* `.162` 21:00:37 takeover notice → refusals until 21:01:24 → reconnected;
  a watchdog-forced reconnect at 21:15:38 repeated the same burst and cleared
  by 21:17.

Those bursts are the previous session still ending: our own reconnect, or the
local renewal app's login (which must evict this client to mint the next pair)
followed by its credential import. Minting a new password cannot fix either, so
this module pins the contract:

1. a refusal while the session is ``settling`` retries with the SAME credential,
   on a timer, before the auth-failure callback is allowed to mint;
2. the ACCOUNT_KICK_OFF notice (service_type 17 / code 4003007) puts the client
   into that quiet state for a grace window;
3. once the quiet retries are spent, the old behaviour resumes, so a genuinely
   dead login still escalates.

Loaded by path with a `paho` stub: mqtt_client imports paho, which is not
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
_spec = importlib.util.spec_from_file_location(_component.mqtt_test_module_name("mqtt_client_settle_test"), MODULE_PATH)
assert _spec and _spec.loader
mqtt_client = importlib.util.module_from_spec(_spec)
sys.modules["mqtt_client_settle_test"] = mqtt_client
_spec.loader.exec_module(mqtt_client)


class _FakePaho:
    """The bit of paho's Client the connect/subscribe paths touch."""

    def __init__(self):
        self.subscribed: list[str] = []

    def subscribe(self, topic, qos=0):
        self.subscribed.append(topic)
        return (0, len(self.subscribed))


def _client():
    c = mqtt_client.XPengMQTTClient(
        client_id="xmart:app:10225403:4dc51b29c3dee8b8ea6f7648ce407ffa",
        password="P1",
        server="iot-app-global.xiaopeng.com:8885",
        user_id="10225403",
    )
    c._client = _FakePaho()
    return c


class _TimerRecorder:
    """Record threading.Timer schedules instead of waiting for them."""

    def __init__(self):
        self.delays: list[float] = []
        self.cancelled: list[bool] = []

    def __call__(self, delay, function):
        timer = mock.Mock()
        timer.daemon = False

        def _cancel():
            self.cancelled.append(True)

        def _start():
            self.delays.append(delay)

        timer.cancel = _cancel
        timer.start = _start
        self.function = function
        return timer


class TestTakeoverDetection(unittest.TestCase):
    def test_kick_off_notice_is_recognised(self):
        payload = {
            "service_type": 17,
            "code": 4003007,
            "message": "Account logged in on another device, local account logged out",
        }
        self.assertTrue(mqtt_client.is_account_takeover(payload))

    def test_str_values_are_recognised(self):
        self.assertTrue(
            mqtt_client.is_account_takeover({"service_type": "17", "code": "4003007"})
        )

    def test_other_notifications_are_not_takeovers(self):
        self.assertFalse(mqtt_client.is_account_takeover({"service_type": 19, "code": 200}))
        self.assertFalse(mqtt_client.is_account_takeover({"service_type": 17, "code": 200}))
        self.assertFalse(mqtt_client.is_account_takeover({}))
        self.assertFalse(mqtt_client.is_account_takeover(None))


class TestSettlingWindow(unittest.TestCase):
    def test_client_that_never_reconnected_is_not_settling(self):
        self.assertFalse(_client().settling)

    def test_own_reconnect_opens_the_window(self):
        c = _client()
        c._last_reconnect_at = mqtt_client.time.time()
        self.assertTrue(c.settling)

    def test_takeover_opens_the_window(self):
        c = _client()
        c.note_account_takeover("account logged in elsewhere")
        self.assertTrue(c.settling)

    def test_window_expires(self):
        c = _client()
        c._last_reconnect_at = (
            mqtt_client.time.time() - mqtt_client.SETTLE_WINDOW_SECONDS - 1
        )
        self.assertFalse(c.settling)


class TestRefusalHandling(unittest.TestCase):
    def test_settling_refusal_retries_without_minting(self):
        c = _client()
        c._last_reconnect_at = mqtt_client.time.time()
        auth_failure = mock.Mock()
        c.set_auth_failure_callback(auth_failure)
        with mock.patch.object(mqtt_client.threading, "Timer", _TimerRecorder()) as timer:
            c._on_connect(c._client, None, None, "Not authorized")
        self.assertFalse(auth_failure.called, "a settling refusal must not mint")
        self.assertEqual(timer.delays, [mqtt_client.SETTLE_RETRY_DELAYS[0]])
        self.assertEqual(c._settle_refusals, 1)

    def test_takeover_refusal_retries_without_minting(self):
        c = _client()
        c.note_account_takeover("renewal login")
        auth_failure = mock.Mock()
        c.set_auth_failure_callback(auth_failure)
        with mock.patch.object(mqtt_client.threading, "Timer", _TimerRecorder()):
            c._on_connect(c._client, None, None, "Not authorized")
        self.assertFalse(auth_failure.called)

    def test_quiet_retries_continue_while_the_grace_is_open(self):
        """Past the short budget, keep waiting: the import may still be landing."""
        c = _client()
        c.note_account_takeover("renewal login")   # opens the 300s grace
        auth_failure = mock.Mock()
        c.set_auth_failure_callback(auth_failure)
        recorder = _TimerRecorder()
        with mock.patch.object(mqtt_client.threading, "Timer", recorder):
            for _ in range(len(mqtt_client.SETTLE_RETRY_DELAYS)):
                c._on_connect(c._client, None, None, "Not authorized")
            self.assertFalse(auth_failure.called)
            c._on_connect(c._client, None, None, "Not authorized")
        self.assertFalse(auth_failure.called, "still inside the takeover grace")
        self.assertEqual(recorder.delays[-1], mqtt_client.SETTLE_RETRY_DELAYS[-1] * 2)

    def test_settle_retry_does_not_extend_its_own_grace(self):
        """Otherwise escalation could be suppressed for ever."""
        c = _client()
        old = mqtt_client.time.time() - mqtt_client.SETTLE_WINDOW_SECONDS - 30
        c._last_reconnect_at = old
        c.connect = mock.Mock()
        c.reconnect("settling: retry", settle=True)
        self.assertEqual(c._last_reconnect_at, old)
        self.assertFalse(c.settling)
        c.reconnect("watchdog")
        self.assertGreater(c._last_reconnect_at, old)
        self.assertTrue(c.settling)

    def test_non_settling_refusal_mints_immediately(self):
        c = _client()
        auth_failure = mock.Mock()
        c.set_auth_failure_callback(auth_failure)
        c._on_connect(c._client, None, None, "Not authorized")
        self.assertTrue(auth_failure.called)

    def test_success_resets_the_quiet_budget(self):
        c = _client()
        c._last_reconnect_at = mqtt_client.time.time()
        c._settle_refusals = 3
        c._subscribe_listener_topics = mock.Mock()
        c._on_connect(c._client, None, None, 0)
        self.assertEqual(c._settle_refusals, 0)
        self.assertTrue(c._connected)

    def test_settle_retry_reconnects_only_when_disconnected(self):
        c = _client()
        c.reconnect = mock.Mock(return_value=True)
        c._connected = True
        c._settle_retry()
        self.assertFalse(c.reconnect.called)
        c._connected = False
        c._settle_retry()
        self.assertTrue(c.reconnect.called)


if __name__ == "__main__":
    unittest.main(verbosity=2)
