#!/usr/bin/env python3
"""Run the integration's real SSH transport against a live AP, with a key file.

This exercises the exact production path (custom_components/unifi_ble/ssh.py
SshTunnelTransport -> bleconnd -> BleConnClient) without Home Assistant, using an
SSH private key you pass on the command line. The matching public key must be
provisioned in UniFi Device SSH Authentication.

Examples:
    tools/run_against_ap.py --host 192.168.10.20 --key ~/.ssh/id_ed25519
    tools/run_against_ap.py --host 192.168.10.20 --key key --jump-host 192.168.10.1
    tools/run_against_ap.py --host 192.168.10.20 --key key --user admin --duration 30

Run it with the project's HA venv Python, e.g.:
    .venv/bin/python tools/run_against_ap.py --host <ap> --key <keyfile>
"""
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import sys
import types
from pathlib import Path

import asyncssh

# Load bleconn.py and ssh.py from the integration under a synthetic package so
# ssh.py's `from .bleconn import Transport` relative import resolves, without
# importing the HA-dependent package __init__.
_BASE = Path(__file__).resolve().parent.parent / "custom_components" / "unifi_ble"
_pkg = types.ModuleType("ublepkg")
_pkg.__path__ = [str(_BASE)]
sys.modules["ublepkg"] = _pkg


def _load(mod):
    spec = importlib.util.spec_from_file_location(f"ublepkg.{mod}", _BASE / f"{mod}.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules[f"ublepkg.{mod}"] = m
    spec.loader.exec_module(m)
    return m


bleconn = _load("bleconn")
sshmod = _load("ssh")


def _fmt(adv) -> str:
    parts = [f"{adv.address} rssi={adv.rssi:>4}"]
    if adv.local_name:
        parts.append(f"name={adv.local_name!r}")
    if adv.tx_power is not None:
        parts.append(f"tx={adv.tx_power}")
    if adv.service_uuids:
        parts.append("svc=[" + ",".join(u.split("-")[0][-4:] for u in adv.service_uuids) + "]")
    if adv.service_data:
        parts.append("svcdata={" + ",".join(f"{k.split('-')[0][-4:]}:{v.hex()}"
                                             for k, v in adv.service_data.items()) + "}")
    if adv.manufacturer_data:
        parts.append("mfr={" + ",".join(f"0x{k:04x}:{v.hex()}"
                                        for k, v in adv.manufacturer_data.items()) + "}")
    if adv.connectable:
        parts.append("conn")
    return "  ".join(parts)


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", required=True, help="AP IP/hostname")
    ap.add_argument("--key", required=True, help="path to the SSH private key file")
    ap.add_argument("--user", default="admin", help="AP SSH username (default: admin)")
    ap.add_argument("--port", type=int, default=8383, help="bleconnd port (default 8383)")
    ap.add_argument("--ssh-port", type=int, default=22, help="SSH port (default 22)")
    ap.add_argument("--jump-host", default=None, help="jump host (e.g. UDM/gateway)")
    ap.add_argument("--jump-user", default=None, help="jump host SSH user")
    ap.add_argument("--passphrase", default=None, help="private key passphrase, if any")
    ap.add_argument("--scan-phys", default="1M", help="comma list: 1M,coded")
    ap.add_argument("--duration", type=int, default=20, help="seconds to scan")
    args = ap.parse_args()

    try:
        key = asyncssh.read_private_key(args.key, passphrase=args.passphrase)
    except (OSError, asyncssh.KeyImportError) as exc:
        print(f"error: cannot load key {args.key!r}: {exc}", file=sys.stderr)
        return 2

    phys = tuple(p.strip() for p in args.scan_phys.split(",") if p.strip())
    transport = sshmod.SshTunnelTransport(
        args.host, args.user, args.port, key,
        jump_host=args.jump_host, jump_user=args.jump_user,
        ssh_port=args.ssh_port,
    )

    seen: dict[str, object] = {}

    def on_adv(adv) -> None:
        if adv.address not in seen:
            seen[adv.address] = adv
            print(_fmt(adv))

    def on_state(state: str) -> None:
        print(f"* {state}", file=sys.stderr)

    client = bleconn.BleConnClient(
        transport, scan_phys=phys, on_advertisement=on_adv, on_state=on_state)

    via = f" via {args.jump_host}" if args.jump_host else ""
    print(f"connecting to {args.user}@{args.host}:{args.ssh_port}{via} "
          f"-> bleconnd:{args.port}, scanning {phys} for {args.duration}s\n",
          file=sys.stderr)

    task = asyncio.create_task(client.run())
    try:
        await asyncio.sleep(args.duration)
    except (asyncio.CancelledError, KeyboardInterrupt):
        pass
    finally:
        await client.stop()
        task.cancel()
        if transport.observed_host_keys:
            print("\nhost keys seen (for pinning):", file=sys.stderr)
            for host, hk in transport.observed_host_keys.items():
                print(f"  {host}: {hk}", file=sys.stderr)
        if client.adapter_mac:
            print(f"AP BLE adapter MAC: {client.adapter_mac}", file=sys.stderr)
        print(f"\n{len(seen)} unique device(s) seen.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        raise SystemExit(130)
