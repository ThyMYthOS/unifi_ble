#!/usr/bin/env python3
"""Validate the unifi_ble integration against the installed Home Assistant.

Run in YOUR terminal (the 3.14 venv), from the repo root:

    .venv/bin/python tools/validate_ha.py

It checks the version-sensitive spots that can't be tested without a real HA:
imports, the habluetooth scanner API, a live push(), and the asyncssh keypair.
Each check prints PASS/FAIL and the run continues so you get the full picture.
"""
from __future__ import annotations

import asyncio
import importlib.metadata as md
import inspect
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_fails = 0


def check(name, fn):
    global _fails
    try:
        result = fn()
        print(f"  PASS  {name}" + (f"  -> {result}" if result else ""))
    except Exception as exc:  # noqa: BLE001
        _fails += 1
        print(f"  FAIL  {name}: {exc!r}")
        traceback.print_exc()


def main() -> int:
    print("=== versions ===")
    print("  python", sys.version.split()[0])
    for pkg in ("homeassistant", "habluetooth", "bleak", "asyncssh", "cryptography"):
        try:
            print(f"  {pkg} {md.version(pkg)}")
        except Exception:
            print(f"  {pkg} MISSING")

    print("\n=== imports ===")
    check("import asyncssh", lambda: __import__("asyncssh").__version__)
    check("import habluetooth", lambda: __import__("habluetooth") and "ok")
    check("import HA bluetooth",
          lambda: __import__("homeassistant.components.bluetooth", fromlist=["x"]) and "ok")
    check("import custom_components.unifi_ble",
          lambda: __import__("custom_components.unifi_ble", fromlist=["x"]) and "ok")

    print("\n=== HA scanner API ===")
    from homeassistant.components.bluetooth import async_register_scanner
    check("async_register_scanner signature",
          lambda: str(inspect.signature(async_register_scanner)))

    from custom_components.unifi_ble.scanner import UnifiBleScanner
    from custom_components.unifi_ble.bleconn import Advertisement

    scanner_box = {}

    def make_scanner():
        s = UnifiBleScanner("AA:BB:CC:DD:EE:FF", "UniFi AP test",
                            connector=None, connectable=False)
        scanner_box["s"] = s
        return type(s).__mro__[1].__name__

    check("UnifiBleScanner(scanner_id, name, connector, connectable)", make_scanner)

    async def push_flow():
        s = scanner_box["s"]
        unsetup = s.async_setup()
        adv = Advertisement(address="CD:2A:06:46:34:22", rssi=-70,
                            local_name="Govee_H7037_3422",
                            manufacturer_data={0x8843: bytes.fromhex("ec00020100")})
        s.push(adv)
        addrs = [d.address for d in s.discovered_devices]
        unsetup()
        return f"discovered={addrs}"

    if "s" in scanner_box:
        check("scanner.async_setup() + push()", lambda: asyncio.run(push_flow()))

    print("\n=== asyncssh keypair (ssh.py logic) ===")

    def keypair():
        import asyncssh
        key = asyncssh.generate_private_key("ssh-ed25519", comment="home-assistant-unifi-ble")
        priv = key.export_private_key().decode()
        pub = key.export_public_key().decode().strip()
        key2 = asyncssh.import_private_key(priv)
        assert key2.export_public_key().decode().strip() == pub, "round-trip mismatch"
        return pub[:48] + " ..."

    check("Ed25519 generate/export/import round-trip", keypair)

    print(f"\n{'ALL CHECKS PASSED' if _fails == 0 else f'{_fails} CHECK(S) FAILED'}")
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
