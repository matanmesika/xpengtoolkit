"""Resolve which copy of the integration a test should load.

There are two trees in this working directory:

* ``xpeng-google/custom_components/xpeng`` - the public-cut copy that ships.
  THIS IS THE DEFAULT, because a test that passes against a tree nobody ships
  is not evidence about the thing that ships. Nine tests here used to load the
  other copy, so "all tests pass" was partly certifying a different
  integration.
* ``custom_components/xpeng`` - a private live installation being developed in
  parallel. It legitimately runs ahead on some features, so it can still be
  tested, explicitly:

      XPENG_COMPONENT=live python3 tests/test_watchdog_policy.py

Why a module and not an env var read in each test: the choice has to be one
place, or the two trees drift out of test coverage silently - which is exactly
what happened. Every test loads its target through ``component_dir()``.
"""

from __future__ import annotations

import os
import pathlib

#: Repo root (the directory holding tests/, tools/ and both component trees).
ROOT = pathlib.Path(__file__).resolve().parent.parent

# Two layouts exist and both must work:
#   * the development working copy, where the shipping tree is nested inside
#     xpeng-google/ and the private live tree sits at the root;
#   * the published repository, where custom_components/ IS the root and there
#     is no second tree at all.
_IN_WORKING_COPY = (ROOT / "xpeng-google" / "custom_components" / "xpeng")
_AT_ROOT = ROOT / "custom_components" / "xpeng"

if _IN_WORKING_COPY.is_dir():
    _SHIPPING = _IN_WORKING_COPY
    _LIVE = _AT_ROOT
else:
    # Published layout: only one tree, and it is the one that ships.
    _SHIPPING = _AT_ROOT
    _LIVE = _AT_ROOT

ALIASES = {
    "shipping": _SHIPPING,
    "public": _SHIPPING,
    "xpeng-google": _SHIPPING,
    "live": _LIVE,
    ".88": _LIVE,
    "88": _LIVE,
    "custom_components": _LIVE,
}


def component_dir() -> pathlib.Path:
    """The component directory to test against.

    Honours ``XPENG_COMPONENT`` (a name from ``ALIASES``, or any filesystem
    path). Defaults to the shipping copy.
    """
    requested = os.environ.get("XPENG_COMPONENT", "").strip()
    if not requested:
        return _SHIPPING

    alias = ALIASES.get(requested.lower())
    if alias is not None:
        return alias

    candidate = pathlib.Path(requested).expanduser()
    if not candidate.is_absolute():
        candidate = ROOT / candidate
    return candidate


def module_path(filename: str) -> pathlib.Path:
    """Path to a module inside the selected component directory.

    Raises a clear error rather than letting a test fail with a confusing
    FileNotFoundError deep in importlib - a missing module is the first thing
    worth knowing when a test is pointed at the wrong tree.
    """
    path = component_dir() / filename
    if not path.is_file():
        raise SystemExit(
            f"{filename} not found in {component_dir()}.\n"
            "That tree may not have this module yet. Run against the copy that "
            "does, e.g. XPENG_COMPONENT=live, or port the module."
        )
    return path


def assert_is_default_target() -> None:
    """Guard against a test silently loading the wrong tree.

    A `git checkout` once reverted the redirect in one test file, which put it
    back on the private tree without anything failing - the suite stayed green
    while no longer testing what ships. Tests that resolve their target here can
    call this to make that mistake loud.
    """
    chosen = component_dir()
    if chosen != _SHIPPING:
        print(
            f"NOTE: testing {chosen.name} at {chosen} "
            f"(XPENG_COMPONENT is set; the shipping copy is the default)"
        )


def mqtt_test_module_name(name: str) -> str:
    """Give path-loaded MQTT modules a package for their relative imports.

    A standalone spec named `mqtt_client_under_test` has no parent, so it
    cannot resolve `.location_freshness`. Real HA imports it as
    `custom_components.xpeng.mqtt_client`. Reproduce only the package context
    here, without importing Home Assistant.
    """
    import sys
    import types

    package_name = "_xpeng_component_test_package"
    pkg = sys.modules.get(package_name)
    if pkg is None:
        pkg = types.ModuleType(package_name)
        pkg.__path__ = [str(component_dir())]
        sys.modules[package_name] = pkg
    return f"{package_name}.{name}"
