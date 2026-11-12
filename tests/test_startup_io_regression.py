"""Regression tests for XPENG startup tenant-file I/O and tracker import.

Executes real integration helper functions using mocked Home Assistant/API objects;
does not require XPENG login tokens, a vehicle, or HA to be installed.
"""
from __future__ import annotations

import ast
import asyncio
import json
import logging
import os
from pathlib import Path
import sys
import threading
from types import ModuleType, SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
INTEGRATION = ROOT / "custom_components" / "xpeng"
FUNCTIONS = {"_available_tenants", "_resolve_configured_device_id",
             "_tenant_device_id", "_tokens_device_id"}


class FakeHass:
    def __init__(self):
        self.jobs = []
        self.updates = []
        self.config_entries = SimpleNamespace(async_update_entry=self._update)

    def _update(self, entry, options):
        self.updates.append(options)

    async def async_add_executor_job(self, callback, *args):
        self.jobs.append(callback.__name__)
        return await asyncio.to_thread(callback, *args)


@pytest.fixture
def integration(monkeypatch):
    source = (INTEGRATION / "__init__.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    helpers = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
               and n.name in FUNCTIONS]
    assert {n.name for n in helpers} == FUNCTIONS
    assert all(isinstance(n, ast.AsyncFunctionDef) for n in helpers
               if n.name in {"_available_tenants", "_resolve_configured_device_id"})

    pkg = ModuleType("xpeng_io_regression")
    pkg.__path__ = []
    api = ModuleType("xpeng_io_regression.xpeng_api")
    thread_ids = []
    minted = []

    def read_file(path):
        thread_ids.append(threading.get_ident())
        return json.loads(Path(path).read_text(encoding="utf-8"))

    def mint_device_id():
        minted.append(True)
        return "minted-new-id"

    api.load_tenant_cache = read_file
    api.load_login_tokens = read_file
    api.deserialize_tenant = lambda data: data
    api.mint_device_id = mint_device_id
    api.DEVICE_ID_MAX_LENGTH = 32
    monkeypatch.setitem(sys.modules, pkg.__name__, pkg)
    monkeypatch.setitem(sys.modules, api.__name__, api)

    globals_ = {
        "__name__": pkg.__name__,
        "__package__": pkg.__name__,
        "os": os,
        "_LOGGER": logging.getLogger(__name__),
        "CONF_TENANT_DATA": "tenant_data",
        "CONF_TOKEN_FILE": "token_file",
        "CONF_CONFIGURED_DEVICE_ID": "configured_device_id",
        "_warn_on_device_id_mismatch": lambda *args: None,
    }
    module = ast.Module(body=[
        ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
        *helpers,
    ], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(INTEGRATION), "exec"), globals_)
    return SimpleNamespace(funcs=globals_, thread_ids=thread_ids, minted=minted)


def entry(data=None, options=None):
    return SimpleNamespace(entry_id="regression", data=data or {}, options=options or {})


def test_tenant_cache_read_uses_executor_and_preserves_existing_identity(integration, tmp_path):
    path = tmp_path / "tenant.json"
    path.write_text('{"deviceId":"original-car-identity"}', encoding="utf-8")
    main_thread = threading.get_ident()
    hass = FakeHass()
    value = asyncio.run(integration.funcs["_resolve_configured_device_id"](
        hass, entry(), str(path), ""))
    assert value == "original-car-identity"
    assert hass.updates == [{"configured_device_id": "original-car-identity"}]
    assert hass.jobs == ["read_file"]
    assert all(t != main_thread for t in integration.thread_ids)
    assert integration.minted == []


def test_token_file_read_and_existence_check_use_executor(integration, tmp_path, monkeypatch):
    path = tmp_path / "tokens.json"
    path.write_text('{"deviceId":"token-identity"}', encoding="utf-8")
    hass = FakeHass()
    main_thread = threading.get_ident()
    old_isfile = os.path.isfile

    def instrumented_isfile(path):
        integration.thread_ids.append(threading.get_ident())
        return old_isfile(path)

    monkeypatch.setattr(os.path, "isfile", instrumented_isfile)
    value = asyncio.run(integration.funcs["_resolve_configured_device_id"](
        hass, entry(options={"token_file": str(path)}), "", ""))
    assert value == "token-identity"
    assert hass.jobs == ["instrumented_isfile", "read_file"]
    assert len(integration.thread_ids) == 2
    assert all(t != main_thread for t in integration.thread_ids)
    assert integration.minted == []


def test_configured_identity_is_never_overwritten(integration, tmp_path):
    path = tmp_path / "tenant.json"
    path.write_text('{"deviceId":"older-identity"}', encoding="utf-8")
    hass = FakeHass()
    value = asyncio.run(integration.funcs["_resolve_configured_device_id"](
        hass, entry(), str(path), "preconfigured"))
    assert value == "preconfigured"
    assert hass.updates == []
    assert integration.minted == []


def test_embedded_tenant_precedes_file(integration, tmp_path):
    path = tmp_path / "tenant.json"
    path.write_text('{"deviceId":"from-file"}', encoding="utf-8")
    value = asyncio.run(integration.funcs["_resolve_configured_device_id"](
        FakeHass(), entry(data={"tenant_data":{"deviceId":"embedded"}}), str(path), ""))
    assert value == "embedded"
    assert integration.minted == []


def test_missing_cache_is_nonfatal(integration, tmp_path):
    value = asyncio.run(integration.funcs["_resolve_configured_device_id"](
        FakeHass(), entry(), str(tmp_path / "missing.json"), ""))
    assert value == "minted-new-id"
    assert integration.minted == [True]


def test_bad_cache_is_nonfatal(integration, tmp_path):
    path = tmp_path / "tenant.json"
    path.write_text("{malformed-json", encoding="utf-8")
    value = asyncio.run(integration.funcs["_resolve_configured_device_id"](
        FakeHass(), entry(), str(path), ""))
    assert value == "minted-new-id"


def test_real_setup_entry_awaits_identity_resolution():
    module = ast.parse((INTEGRATION / "__init__.py").read_text(encoding="utf-8"))
    setup = next(n for n in module.body if isinstance(n, ast.AsyncFunctionDef)
                 and n.name == "async_setup_entry")
    awaited = [n for n in ast.walk(setup)
               if isinstance(n, ast.Await) and isinstance(n.value, ast.Call)
               and isinstance(n.value.func, ast.Name)
               and n.value.func.id == "_resolve_configured_device_id"]
    assert len(awaited) == 1


def test_tracker_import_is_modern_and_registry_unique_id_is_unchanged():
    code = (INTEGRATION / "device_tracker.py").read_text(encoding="utf-8")
    module = ast.parse(code)
    imports = [n for n in module.body if isinstance(n, ast.ImportFrom)]
    assert any(n.module == "homeassistant.components.device_tracker"
               and {"TrackerEntity", "SourceType"}.issubset(
                   {a.name for a in n.names}) for n in imports)
    assert not any(n.module == "homeassistant.components.device_tracker.config_entry"
                   for n in imports)
    assert 'f"xpeng_{self._vin}_location"' in code
