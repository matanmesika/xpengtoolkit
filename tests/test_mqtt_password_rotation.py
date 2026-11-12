#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for detecting that the live MQTT client holds a superseded password.

    python3 tests/test_mqtt_password_rotation.py
    python3 -m unittest discover -s tests -v

The credential refresh replaces the password roughly every 45s while the paho
client keeps the one it was built with. A broker refusal then means "you
presented a retired password", not "the login expired" — and the two need
opposite responses. Getting it wrong cost, on 2026-10-07, eight `rc=Not
authorized` refusals in twenty minutes, each answered with a cooldown-delayed
API round trip while the correct password sat in memory the whole time.

Loaded by path with a `paho` stub: mqtt_client imports paho, which is not
installed on this host, but it does not import Home Assistant.
"""

from __future__ import annotations

import importlib.util
import pathlib
import re
import sys
import types
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import _component  # noqa: E402

MODULE_PATH = _component.module_path("mqtt_client.py")


def _stub_paho() -> None:
    """Provide a permissive `paho.mqtt.client` if paho is absent."""
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
_spec = importlib.util.spec_from_file_location(_component.mqtt_test_module_name("mqtt_client_under_test"), MODULE_PATH)
assert _spec and _spec.loader
mqtt_client = importlib.util.module_from_spec(_spec)
sys.modules["mqtt_client_under_test"] = mqtt_client
_spec.loader.exec_module(mqtt_client)


def _client(password: str = "P1"):
    return mqtt_client.XPengMQTTClient(
        client_id="xmart:app:12345678:abcdef",
        password=password,
        server="iot-app-global.xiaopeng.com:8885",
        user_id="12345678",
    )


class TestCredentialsSuperseded(unittest.TestCase):
    def test_a_client_that_has_never_connected_is_not_superseded(self):
        """No client built yet means no stale credential to blame.

        Reporting True here would send every auth failure down the "just
        reconnect" path, including the ones that genuinely need a new login.
        """
        self.assertFalse(_client().credentials_superseded)

    def test_matching_passwords_are_not_superseded(self):
        c = _client("P1")
        c._client_password = "P1"
        self.assertFalse(c.credentials_superseded)

    def test_a_rotated_password_is_superseded(self):
        """The live case: refresh replaced the password after the client was built."""
        c = _client("P1")
        c._client_password = "P1"
        c._password = "P2"
        self.assertTrue(c.credentials_superseded)

    def test_rotation_before_the_first_connect_is_not_superseded(self):
        """_password changed but no client holds anything, so nothing is stale."""
        c = _client("P1")
        c._password = "P2"
        self.assertFalse(c.credentials_superseded)

    def test_reconnecting_clears_the_superseded_state(self):
        """The fix must actually settle, or it becomes a reconnect loop.

        Rebuilding the client records the password it used, so the next check
        says False and the auth-failure path falls through to the API instead of
        reconnecting forever.
        """
        c = _client("P1")
        c._client_password = "P1"
        c._password = "P2"
        self.assertTrue(c.credentials_superseded)
        c._client_password = c._password  # what connect() does
        self.assertFalse(c.credentials_superseded)


class TestApplyCurrentCredentials(unittest.TestCase):
    """Paho retries with what the client was built with — so keep it current.

    This is the actual flap fix: a drop makes paho retry in 10-60s, the refresh
    retires the old password every ~45s, and the retry was therefore refused.
    Pushing the held credential onto the live client is what makes the retry
    present a valid one.
    """

    class _FakeClient:
        def __init__(self):
            self.calls = []

        def username_pw_set(self, client_id, password):
            self.calls.append((client_id, password))

    def test_pushes_the_current_password_onto_the_live_client(self):
        c = _client("P1")
        c._client = self._FakeClient()
        c._password = "P2"  # the refresh rotated it
        c._apply_current_credentials()
        self.assertEqual(c._client.calls, [("xmart:app:12345678:abcdef", "P2")])

    def test_it_clears_the_superseded_signal(self):
        """The client now presents P2, so nothing is stale any more.

        If this did not update, an auth failure would keep taking the local
        reconnect path for a staleness that no longer exists instead of falling
        through to the API when the login has genuinely expired.
        """
        c = _client("P1")
        c._client = self._FakeClient()
        c._client_password = "P1"
        c._password = "P2"
        self.assertTrue(c.credentials_superseded)
        c._apply_current_credentials()
        self.assertFalse(c.credentials_superseded)

    def test_no_client_is_harmless(self):
        c = _client("P1")
        c._password = "P2"
        c._apply_current_credentials()  # must not raise

    def test_an_empty_password_is_not_pushed(self):
        c = _client("P1")
        c._client = self._FakeClient()
        c._password = ""
        c._apply_current_credentials()
        self.assertEqual(c._client.calls, [])

    def test_a_client_that_raises_does_not_propagate(self):
        """This runs inside a paho callback; raising there would break the loop."""

        class _Angry:
            def username_pw_set(self, *a):
                raise RuntimeError("boom")

        c = _client("P1")
        c._client = _Angry()
        c._password = "P2"
        c._apply_current_credentials()  # must not raise


class TestRefusedConnectResyncs(unittest.TestCase):
    """A refused CONNECT must also correct the client's credentials.

    _on_disconnect is not guaranteed to fire for a refusal, so without this the
    retry repeats the same retired password and the refusal repeats — which is
    the residual single refusal per drop that survived the first fix.
    """

    class _FakeClient:
        def __init__(self):
            self.calls = []

        def username_pw_set(self, client_id, password):
            self.calls.append((client_id, password))

    def test_a_refused_connect_pushes_the_current_password(self):
        c = _client("P1")
        c._client = self._FakeClient()
        c._client_password = "P1"
        c._password = "P2"  # rotated since the client was built
        fired = []
        c.set_auth_failure_callback(lambda: fired.append(True))

        c._on_connect(c._client, None, None, "Not authorized")

        self.assertEqual(c._client.calls, [("xmart:app:12345678:abcdef", "P2")])
        self.assertEqual(fired, [True], "the refresh callback must still run")
        self.assertFalse(c.credentials_superseded)


class TestReconnectBackoffBeatsRotation(unittest.TestCase):
    """paho's reconnect backoff must not outrun the credential rotation.

    paho retries a CONNECT with the password the client was built with. The
    refresh replaces that password every `mqtt_refresh_interval` (45s by
    default), so a retry scheduled further out than one rotation presents a
    retired password and is refused as "Not authorized".

    `_apply_current_credentials()` re-syncs the password before each attempt,
    which fixed the bulk of this (15 refusals in 3h -> 16 over 6h on the live
    install). The residual case is a retry scheduled so far ahead that a
    rotation lands between the sync and the packet, so paho's max backoff is
    capped below one rotation.

    Asserted against the source because the value is handed to paho during
    `connect()`, and this test deliberately runs without a broker.
    """

    #: mqtt_refresh_interval default, from const.py.
    ROTATION_SECONDS = 45

    def test_max_delay_is_below_one_rotation(self):
        source = MODULE_PATH.read_text()
        m = re.search(
            r"reconnect_delay_set\(\s*min_delay\s*=\s*([0-9.]+)\s*,"
            r"\s*max_delay\s*=\s*([0-9.]+)\s*\)",
            source,
        )
        self.assertIsNotNone(m, "reconnect_delay_set(...) not found in mqtt_client.py")
        min_delay, max_delay = float(m.group(1)), float(m.group(2))

        self.assertLess(
            max_delay, self.ROTATION_SECONDS,
            f"max_delay={max_delay}s is not below the {self.ROTATION_SECONDS}s "
            "rotation; a retry this far out presents a retired password",
        )
        self.assertGreaterEqual(min_delay, 1.0, "a zero first retry would spin")
        self.assertLess(min_delay, max_delay, "min_delay must be below max_delay")

    def test_rotation_interval_matches_this_test(self):
        """If the default rotation moves, this bound must move with it."""
        const = (_component.component_dir() / "const.py").read_text()
        m = re.search(
            r"DEFAULT_MQTT_REFRESH_INTERVAL\s*[:=][^=]*=\s*([0-9.]+)", const
        )
        self.assertIsNotNone(m, "DEFAULT_MQTT_REFRESH_INTERVAL not found")
        self.assertEqual(
            float(m.group(1)), float(self.ROTATION_SECONDS),
            "the rotation default changed; re-check the reconnect backoff cap",
        )


class TestStageCredentials(unittest.TestCase):
    """Staging a freshly minted account for the next connect.

    A renewal hands the client a new clientId/password/server without a config
    entry reload, so these fields are the whole handover: whatever is staged here
    is what the next ``connect()`` presents. Staging must not connect on its own —
    ``reconnect()`` owns the lock that keeps two paho clients off one clientId.
    """

    def test_it_stages_the_id_password_and_server(self):
        c = _client("P1")
        c.stage_credentials(
            client_id="xmart:app:99999999:zzzz",
            password="P2",
            server="other-host:8885",
        )
        self.assertEqual(c._password, "P2")
        self.assertEqual(c._client_id, "xmart:app:99999999:zzzz")
        self.assertEqual(c._server, "other-host:8885")
        # The user id is the client channel, so it has to move with the id.
        self.assertEqual(c._user_id, "99999999")

    def test_empty_values_leave_what_is_there(self):
        c = _client("P1")
        c.stage_credentials(password="P2")
        self.assertEqual(c._password, "P2")
        self.assertEqual(c._client_id, "xmart:app:12345678:abcdef")
        self.assertEqual(c._user_id, "12345678")
        self.assertEqual(c._server, "iot-app-global.xiaopeng.com:8885")

    def test_a_client_id_that_is_not_the_expected_shape_keeps_the_channel(self):
        """A malformed id must not silently blank the topic the client reads."""
        c = _client("P1")
        c.stage_credentials(client_id="nonsense", password="P2")
        self.assertEqual(c._client_id, "nonsense")
        self.assertEqual(c._user_id, "12345678")

    def test_staging_does_not_connect(self):
        c = _client("P1")
        c.stage_credentials(password="P2")
        self.assertIsNone(c._client)


if __name__ == "__main__":
    unittest.main(verbosity=2)
