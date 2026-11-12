#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A broker refusal must not be mistaken for an expired login.

    python3 tests/test_credentials_superseded.py
    python3 -m unittest discover -s tests -v

The credential refresh replaces the MQTT password every ~45s, but paho's live
client keeps the password it was *built* with. So a refusal can mean either
"the login expired" (mint a new password via the API) or "you presented a
password we already retired" (the correct one is in memory — just use it).

Treating the second case as the first costs an API round trip and then sits in
an exponential cooldown for up to 15 minutes while the password that works is
already held. Measured 2026-10-07: eight refusals in twenty minutes, and 15 in
three hours, none of them a genuinely expired login.

This is the regression test for that distinction.
"""

from __future__ import annotations

import importlib.util
import inspect
import pathlib
import sys
import types
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import _component  # noqa: E402

COMPONENT = _component.component_dir()


class _FakeClient:
    """Records what the CONNECT packet would carry."""

    def __init__(self, *a, **kw):
        self.username = None
        self.password = None

    def username_pw_set(self, username, password=None):
        self.username = username
        self.password = password

    def __getattr__(self, _name):
        return lambda *a, **kw: None


def _load_module():
    paho = types.ModuleType("paho")
    mqtt = types.ModuleType("paho.mqtt")
    client_mod = types.ModuleType("paho.mqtt.client")
    client_mod.Client = _FakeClient
    client_mod.MQTTv311 = 4
    client_mod.CallbackAPIVersion = types.SimpleNamespace(VERSION2=2)
    mqtt.client = client_mod
    paho.mqtt = mqtt
    sys.modules.update({
        "paho": paho,
        "paho.mqtt": mqtt,
        "paho.mqtt.client": client_mod,
    })

    spec = importlib.util.spec_from_file_location(
        _component.mqtt_test_module_name("mqtt_client_under_test"), COMPONENT / "mqtt_client.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["mqtt_client_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


class TestCredentialsSuperseded(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_module()

    def _client(self, password="pw-old"):
        # cert_dir is a shipping-copy constructor kwarg; the live copy takes the
        # cert path from a module-level default instead, so pass it only when
        # the signature accepts it. This test exercises credential bookkeeping,
        # not TLS setup.
        kwargs = {"client_id": "xmart:app:1:abc", "password": password,
                  "server": "broker:8885", "user_id": "1"}
        if "cert_dir" in inspect.signature(
            self.mod.XPengMQTTClient.__init__
        ).parameters:
            kwargs["cert_dir"] = "/nonexistent"
        return self.mod.XPengMQTTClient(**kwargs)

    def test_fresh_client_is_not_superseded(self):
        """Nothing has rotated yet, so a refusal means a real problem."""
        c = self._client()
        c._client_password = c._password        # what client creation records
        self.assertFalse(c.credentials_superseded)

    def test_rotated_password_is_detected_as_superseded(self):
        """This is the case that used to burn an API call and a cooldown."""
        c = self._client(password="pw-old")
        c._client_password = "pw-old"           # client built with the old one
        c._password = "pw-new"                  # refresh moved on
        self.assertTrue(c.credentials_superseded)

    def test_empty_client_password_is_never_superseded(self):
        """Before a client exists there is nothing to compare against."""
        c = self._client(password="pw-new")
        c._client_password = ""
        self.assertFalse(c.credentials_superseded)

    def test_applying_current_credentials_clears_the_flag(self):
        """The sync must leave the signal accurate, not stuck true."""
        c = self._client(password="pw-old")
        c._client_password = "pw-old"
        c._password = "pw-new"
        frozen = _FakeClient()
        c._client = frozen

        self.assertTrue(c.credentials_superseded)
        c._apply_current_credentials()

        self.assertFalse(c.credentials_superseded, "must not still claim staleness")
        self.assertEqual(frozen.password, "pw-new", "paho must present the new one")
        self.assertEqual(frozen.username, "xmart:app:1:abc")

    def test_apply_is_safe_without_a_client(self):
        """It runs from _on_disconnect, which can fire before connect."""
        c = self._client()
        c._client = None
        c._apply_current_credentials()          # must not raise

    def test_apply_is_safe_when_a_client_rejects_the_call(self):
        """A paho hiccup must not take down the disconnect path."""
        c = self._client(password="pw-old")
        c._password = "pw-new"

        class Angry(_FakeClient):
            def username_pw_set(self, *a, **kw):
                raise RuntimeError("nope")

        c._client = Angry()
        c._apply_current_credentials()          # must swallow, not raise
        self.assertIsInstance(c._client, Angry)


if __name__ == "__main__":
    unittest.main(verbosity=2)
