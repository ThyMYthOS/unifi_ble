#!/usr/bin/env python3
"""Exercise the async BleConnClient against one or more forwarded AP ports and
print advertisements as Home Assistant would receive them.

    tools/py tools/scan_ha.py --targets 127.0.0.1:8383 --duration 20
    tools/py tools/scan_ha.py --targets 127.0.0.1:8383,127.0.0.1:8384
"""
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import sys
from pathlib import Path

# Load bleconn.py by path so we don't trigger the HA-dependent package __init__.
_BLECONN = (Path(__file__).resolve().parent.parent
            / "custom_components" / "unifi_ble" / "bleconn.py")
_spec = importlib.util.spec_from_file_location("uble_bleconn", _BLECONN)
_mod = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _mod          # needed for slots=True dataclass resolution
_spec.loader.exec_module(_mod)
Advertisement, BleConnClient, TcpTransport = (
    _mod.Advertisement, _mod.BleConnClient, _mod.TcpTransport)


def fmt(src: str, a: Advertisement) -> str:
    parts = [f"[{src}] {a.address} rssi={a.rssi:>4}"]
    if a.local_name:
        parts.append(f"name={a.local_name!r}")
    if a.tx_power is not None:
        parts.append(f"tx={a.tx_power}")
    if a.service_uuids:
        parts.append(f"svc={[u[4:8] if u.endswith(a.service_uuids[0][8:]) else u for u in a.service_uuids]}")
    if a.service_data:
        parts.append("svcdata={" + ", ".join(f"{k[4:8]}:{v.hex()}" for k, v in a.service_data.items()) + "}")
    if a.manufacturer_data:
        parts.append("mfr={" + ", ".join(f"0x{k:04x}:{v.hex()}" for k, v in a.manufacturer_data.items()) + "}")
    if a.connectable:
        parts.append("conn")
    return "  ".join(parts)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--targets", default="127.0.0.1:8383",
                    help="comma list of host:port forwarded to APs' bleconnd")
    ap.add_argument("--scan-phys", default="1M")
    ap.add_argument("--duration", type=int, default=20)
    args = ap.parse_args()

    phys = tuple(p.strip() for p in args.scan_phys.split(",") if p.strip())
    seen: dict[str, Advertisement] = {}
    clients: list[BleConnClient] = []
    tasks: list[asyncio.Task] = []

    for target in args.targets.split(","):
        host, _, port = target.strip().partition(":")
        src = target.strip()

        def on_adv(a: Advertisement, src=src) -> None:
            first = a.address not in seen
            seen[a.address] = a
            if first:
                print(fmt(src, a))

        def on_state(s: str, src=src) -> None:
            print(f"[{src}] * {s}")

        c = BleConnClient(TcpTransport(host, int(port)), scan_phys=phys,
                          on_advertisement=on_adv, on_state=on_state)
        clients.append(c)
        tasks.append(asyncio.create_task(c.run()))

    await asyncio.sleep(args.duration)
    for c in clients:
        await c.stop()
    for t in tasks:
        t.cancel()
    print(f"\n{len(seen)} unique device(s) across {len(clients)} AP(s).")


if __name__ == "__main__":
    asyncio.run(main())
