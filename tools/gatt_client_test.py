#!/usr/bin/env python3
"""End-to-end test of BleConnClient's GATT layer (Phase B) against a live device.

    python3 tools/gatt_client_test.py --host 127.0.0.1 --port 8383 --mac <mac>
"""
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import sys
from pathlib import Path

_BLECONN = (Path(__file__).resolve().parent.parent
            / "custom_components" / "unifi_ble" / "bleconn.py")
_spec = importlib.util.spec_from_file_location("uble_bleconn", _BLECONN)
_mod = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _mod
_spec.loader.exec_module(_mod)
BleConnClient, TcpTransport = _mod.BleConnClient, _mod.TcpTransport


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8383)
    ap.add_argument("--mac", required=True, help="peer mac (no colons)")
    ap.add_argument("--notify-secs", type=int, default=12)
    args = ap.parse_args()

    scanning = asyncio.Event()
    client = BleConnClient(
        TcpTransport(args.host, args.port),
        on_advertisement=lambda adv: None,
        on_state=lambda s: (print(f"[state] {s}"), scanning.set()
                            if s == "scanning" else None),
    )
    task = asyncio.create_task(client.run())
    try:
        await asyncio.wait_for(scanning.wait(), 15)

        print(f"connecting to {args.mac} ...")
        ch = await client.gatt_connect(args.mac, timeout=30)
        print(f"connected: connHandle={ch} mtu={client.connection_mtu(ch)}")

        print("discovering services ...")
        services = await client.gatt_discover(ch)
        notify = None
        for s in services:
            print(f"  service {s['uuid']} (handle {s['handle']})")
            for c in s["characteristics"]:
                print(f"    char {c['uuid']} props={c['properties']} "
                      f"handle={c['handle']} descs={[d['uuid'] for d in c['descriptors']]}")
                if notify is None and ("notify" in c["properties"]
                                       or "indicate" in c["properties"]):
                    cccd = next((d["handle"] for d in c["descriptors"]
                                 if str(d["uuid"]).lower() == "2902"), None)
                    if cccd is not None:
                        notify = (c["handle"], cccd, "indicate" in c["properties"]
                                  and "notify" not in c["properties"])

        # Read the first readable characteristic.
        for s in services:
            for c in s["characteristics"]:
                if "read" in c["properties"]:
                    val = await client.gatt_read_char(ch, c["handle"])
                    print(f"read {c['uuid']} = {val.hex()}  ({val!r})")
                    break
            else:
                continue
            break

        if notify:
            chandle, cccd, indicate = notify
            count = 0

            def on_notify(data: bytes, chandle=chandle):
                nonlocal count
                count += 1
                print(f"  notify char {chandle}: {data.hex()}")

            print(f"subscribing to char {chandle} (cccd {cccd}, "
                  f"{'indicate' if indicate else 'notify'}) for {args.notify_secs}s ...")
            await client.gatt_start_notify(ch, chandle, cccd, on_notify, indicate)
            await asyncio.sleep(args.notify_secs)
            await client.gatt_stop_notify(ch, chandle, cccd)
            print(f"received {count} notification(s)")

        print("disconnecting ...")
        await client.gatt_disconnect(ch)
        print("RESULT: PASS ✅")
        return 0
    finally:
        await client.stop()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
