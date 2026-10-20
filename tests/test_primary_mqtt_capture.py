"""Raw MQTT capture must actually record selected primary and V2 frames.

Load the actual production methods through AST so this test has no Home Assistant
installation requirement. The paho topic matcher and actual journal writer run.
"""
from __future__ import annotations

import ast
import json
import logging
import pathlib
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

import paho.mqtt.client as mqtt

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "custom_components" / "xpeng"))
from capture import CaptureRecorder  # noqa: E402

MQTT_PATH = ROOT / "custom_components" / "xpeng" / "mqtt_client.py"
INTEGRATION_PATH = ROOT / "custom_components" / "xpeng" / "__init__.py"

METHODS = {
    "recv_topic", "recv_topic_v2", "_on_message", "_handle_listener_message",
    "_capture_frame_if_selected", "_subscribe_listener_topics",
}


def load_client_methods():
    source = ast.parse(MQTT_PATH.read_text(encoding="utf-8"))
    cls = next(node for node in source.body
               if isinstance(node, ast.ClassDef) and node.name == "XPengMQTTClient")
    chosen = [node for node in cls.body
              if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
              and node.name in METHODS]
    isolated = ast.ClassDef(
        name="IsolatedClient", bases=[], keywords=[], body=chosen,
        decorator_list=[],
    )
    module = ast.Module(body=[
        ast.ImportFrom(module="__future__",
                       names=[ast.alias(name="annotations")], level=0),
        isolated,
    ], type_ignores=[])
    ns = {"mqtt": mqtt, "time": time, "json": json,
          "_LOGGER": logging.getLogger(__name__)}
    exec(compile(ast.fix_missing_locations(module), str(MQTT_PATH), "exec"), ns)
    return ns["IsolatedClient"]


Client = load_client_methods()


def make_client(topics=()):
    client = Client()
    client._user_id = "TEST_USER"
    client._capture_topics = list(topics)
    client._capture_callback = None
    client._listener_callback = None
    client._listener_logged_topics = set()
    client._last_full_content = None
    client._state_callback = None
    client._subscription_mids = {}
    client._listener_enabled = False
    client._listener_topics = []
    client._client = None
    client._resolve_pending = lambda *args: None
    return client


def msg(topic, payload=b'{"service_type":19,"msg_type":4,"msg_content":{}}',
        qos=1, retain=False):
    return SimpleNamespace(topic=topic, payload=payload, qos=qos, retain=retain)


class TestMainStateCapture(unittest.TestCase):
    def test_primary_frame_writes_real_journal_before_parsing(self):
        client = make_client(["Xmart/Msg/User/{user_id}".replace("{user_id}", "TEST_USER")])
        with tempfile.TemporaryDirectory() as directory:
            writer = CaptureRecorder(directory, topics=client._capture_topics)
            writer.start()
            client._capture_callback = writer.submit
            client._on_message(None, None, msg(client.recv_topic, b"malformed mqtt payload"))
            writer.stop()
            self.assertEqual(writer.stats()["frames"], 1)
            self.assertEqual(writer.stats()["dropped"], 0)
            self.assertEqual(writer.stats()["parts"], 1)
            self.assertTrue(pathlib.Path(writer.parts[0]).exists())

    def test_no_primary_capture_when_only_can_candidates_selected(self):
        client = make_client(["Xmart/Car/+/State/CanPack1s"])
        seen = []
        client._capture_callback = lambda *a, **kw: seen.append((a, kw))
        client._on_message(None, None, msg(client.recv_topic))
        self.assertEqual(seen, [])

    def test_v2_frame_uses_same_keyword_only_recording_path(self):
        client = make_client(["Xmart/MsgV2/User/TEST_USER"])
        got = []
        def writer(topic, payload, *, qos=0, retain=False):
            got.append((topic, payload, qos, retain))
        client._capture_callback = writer
        client._on_message(None, None, msg(client.recv_topic_v2, b"\\x01\\x02", qos=2, retain=True))
        self.assertEqual(got, [(client.recv_topic_v2, b"\\x01\\x02", 2, True)])

    def test_capture_matches_broker_wildcards(self):
        client = make_client(["Xmart/Msg/#"])
        seen = []
        client._capture_callback = lambda topic, payload, *, qos, retain: seen.append(topic)
        client._on_message(None, None, msg(client.recv_topic))
        self.assertEqual(seen, [client.recv_topic])

    def test_callback_exception_does_not_escape_mqtt_loop(self):
        client = make_client(["Xmart/Msg/User/TEST_USER"])
        def broken(topic, payload, *, qos, retain):
            raise ValueError("simulated recorder failure")
        client._capture_callback = broken
        with self.assertLogs(level="ERROR") as records:
            client._on_message(None, None, msg(client.recv_topic))
        self.assertTrue(any("Raw MQTT capture callback failed" in entry
                            for entry in records.output))

    def test_duplicate_qos0_subscription_does_not_override_state_qos1(self):
        client = make_client()
        client._listener_enabled = True
        client._listener_topics = [
            client.recv_topic, client.recv_topic_v2,
            "Xmart/Car/+/State/CanPack1s",
        ]
        client._client = Mock()
        client._client.subscribe.return_value = (0, 42)
        client._subscribe_listener_topics()
        client._client.subscribe.assert_called_once_with(
            "Xmart/Car/+/State/CanPack1s", qos=0
        )

    def test_option_parser_expands_private_user_placeholder(self):
        tree = ast.parse(INTEGRATION_PATH.read_text(encoding="utf-8"))
        captures = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "capture_topics"
                    for target in node.targets)
        ]
        self.assertEqual(len(captures), 1)
        call = captures[0].value
        self.assertIsInstance(call, ast.BoolOp)
        parser = call.values[0]
        self.assertIsInstance(parser, ast.Call)
        kw_names = {k.arg for k in parser.keywords}
        self.assertIn("user_id", kw_names)
        self.assertIn("device_id", kw_names)


if __name__ == "__main__":
    unittest.main(verbosity=2)
