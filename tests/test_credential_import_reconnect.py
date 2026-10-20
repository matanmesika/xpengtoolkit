"""Regression tests for XPENG renewal import racing an MQTT reconnect.

Run the production coroutine isolated from HA imports, with the exact reconnect()
return contract. No real tokens, broker, Home Assistant or vehicle required.
"""
from __future__ import annotations

import ast
import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch


SOURCE = (
    Path(__file__).resolve().parents[1]
    / "custom_components/xpeng/credential_import.py"
)


def load_apply(successes: list):
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    function = next(
        node for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "_apply_to_running_client"
    )
    function.decorator_list = []
    source = ast.Module(
        body=[
            ast.ImportFrom(
                module="__future__",
                names=[ast.alias(name="annotations")],
                level=0,
            ),
            function,
        ],
        type_ignores=[],
    )
    namespace = {
        "asyncio": asyncio,
        "_LOGGER": logging.getLogger(__name__),
        "_record_credential_success": lambda refresh_state: successes.append(refresh_state),
    }
    exec(compile(ast.fix_missing_locations(source), str(SOURCE), "exec"), namespace)
    return namespace["_apply_to_running_client"]


class MockClient:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.staged = []
        self.reasons = []
        self.queries = []

    def stage_credentials(self, **kwargs):
        self.staged.append(kwargs)

    def reconnect(self, reason):
        self.reasons.append(reason)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def send_query(self, vin):
        self.queries.append(vin)


class MockHass:
    async def async_add_executor_job(self, fn, *args):
        return fn(*args)


def run_apply(outcomes, vin="TEST-VIN"):
    successes = []
    client = MockClient(outcomes)
    coord = SimpleNamespace(
        stamps=0,
        renewed=None,
        set_credential_timestamp=lambda: None,
        set_last_renewal=lambda ts: None,
    )
    applied = []
    coord.set_credential_timestamp = lambda: applied.append("credential")
    coord.set_last_renewal = lambda ts: applied.append(("renewed", ts))
    live = {"coordinator": coord, "refresh_state": {"last_error": "old"}}
    sleeper = []
    async def fake_sleep(seconds):
        sleeper.append(seconds)

    async def go():
        with patch.object(asyncio, "sleep", new=fake_sleep):
            await load_apply(successes)(
                MockHass(), live, client,
                client_id="xmart:app:TEST:DEVICE",
                password="NOT-A-REAL-PASSWORD",
                server="broker.invalid",
                vin=vin,
                renewed_at=1234567,
                expires_at=1234590,
            )

    return go, client, applied, successes, sleeper


class TestCredentialImportReconnect(unittest.TestCase):
    def test_busy_lock_retries_and_then_succeeds(self):
        go, client, applied, successes, sleeps = run_apply([False, False, True])
        asyncio.run(go())
        self.assertEqual(len(client.staged), 1)
        self.assertEqual(len(client.reasons), 3)
        self.assertEqual(client.queries, ["TEST-VIN"])
        self.assertEqual(sleeps, [1, 1])
        self.assertEqual(applied, ["credential", ("renewed", 1234567)])
        self.assertEqual(len(successes), 1)

    def test_all_busy_reconnects_fail_closed_for_addon_retry(self):
        go, client, applied, successes, sleeps = run_apply([False] * 6)
        with self.assertRaisesRegex(RuntimeError, "retry saved pair"):
            asyncio.run(go())
        self.assertEqual(len(client.staged), 1)
        self.assertEqual(len(client.reasons), 6)
        self.assertEqual(client.queries, [])
        self.assertEqual(sleeps, [1] * 5)
        self.assertEqual(applied, [])
        self.assertEqual(successes, [])

    def test_immediate_success_requires_no_sleep(self):
        go, client, applied, successes, sleeps = run_apply([True])
        asyncio.run(go())
        self.assertEqual(len(client.reasons), 1)
        self.assertEqual(sleeps, [])
        self.assertEqual(len(successes), 1)

    def test_reconnect_exception_is_not_reported_as_success(self):
        go, client, applied, successes, sleeps = run_apply([OSError("broker unavailable")])
        with self.assertRaises(OSError):
            asyncio.run(go())
        self.assertEqual(applied, [])
        self.assertEqual(successes, [])

    def test_no_vehicle_id_does_not_query_vehicle(self):
        go, client, applied, successes, sleeps = run_apply([True], vin="")
        asyncio.run(go())
        self.assertEqual(client.queries, [])
        self.assertEqual(len(successes), 1)

    def test_reauth_notice_only_cleared_after_success(self):
        tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
        node = next(
            n for n in tree.body if isinstance(n, ast.AsyncFunctionDef)
            and n.name == "async_import_credentials"
        )
        calls = [
            (n.lineno, n.func.id)
            for n in ast.walk(node)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            and n.func.id in {"_clear_reauth_repair", "_apply_to_running_client"}
        ]
        loc = dict((name, line) for line, name in calls)
        self.assertGreater(loc["_clear_reauth_repair"], loc["_apply_to_running_client"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
