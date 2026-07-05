#!/usr/bin/env python3
"""blectl — a bluetoothctl/gatttool-style CLI for a UniFi AP's BLE radio.

Replicates the common BlueZ command-line BLE tools (`bluetoothctl` scanning +
device management and its `gatt` sub-menu, plus `gatttool`-style read/write/notify)
on top of the AP's `bleconnd` API, via BleConnClient.

Run against an SSH-forwarded bleconnd port (stdlib only):
    tools/py tools/blectl.py --host 127.0.0.1 --port 8383

or directly over SSH (needs asyncssh — run with the HA venv):
    .venv/bin/python tools/blectl.py --ap 192.168.10.20 --key ~/.ssh/id_ed25519 \
        [--jump-host 192.168.10.1]

Type `help` at the prompt for the command list.
"""
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import sys
import types
from pathlib import Path

_BASE = Path(__file__).resolve().parent.parent / "custom_components" / "unifi_ble"


def _load(mod: str):
    """Load a component module by path under a synthetic package (for ssh.py's
    relative import), without triggering the HA-dependent package __init__."""
    if "ublepkg" not in sys.modules:
        pkg = types.ModuleType("ublepkg")
        pkg.__path__ = [str(_BASE)]
        sys.modules["ublepkg"] = pkg
    spec = importlib.util.spec_from_file_location(f"ublepkg.{mod}", _BASE / f"{mod}.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules[f"ublepkg.{mod}"] = m
    spec.loader.exec_module(m)
    return m


bleconn = _load("bleconn")


def _norm_mac(addr: str) -> str:
    """Normalize an address to bleconnd form: lower-case hex, no separators."""
    return addr.replace(":", "").replace("-", "").lower()


def _disp_mac(mac: str) -> str:
    """Format a colon-less hex MAC as upper-case AA:BB:CC:DD:EE:FF."""
    mac = _norm_mac(mac)
    return ":".join(mac[i:i + 2] for i in range(0, 12, 2)).upper()


class BleCtl:
    """Interactive session: one AP session, a discovered-device table, and at
    most one active GATT connection with its cached attribute database."""

    HELP = """commands:
  scan on|off              start/stop printing newly discovered devices
  devices                  list discovered devices
  info <addr>              show a discovered device's advertisement
  connect <addr>           open a GATT connection
  disconnect               close the active connection
  list-attributes          list the connected device's GATT database
  read <handle>            read a characteristic/descriptor value (hex handle ok)
  write <handle> <hex>     write a characteristic/descriptor value
  notify <handle> on|off   subscribe/unsubscribe to a characteristic
  mtu                      show the negotiated MTU
  help                     show this help
  quit | exit              leave"""

    def __init__(self, client) -> None:
        """Bind to a (not yet running) BleConnClient and reset session state."""
        self._client = client
        self._scanning = False
        self._devices: dict[str, object] = {}      # mac(no colons) -> Advertisement
        self._conn: int | None = None
        self._conn_mac: str | None = None
        self._chars: dict[int, dict] = {}          # value handle -> char info
        self._descs: dict[int, dict] = {}          # handle -> descriptor info
        self._cccd_for: dict[int, int] = {}        # char handle -> CCCD handle
        self._notifying: set[int] = set()

    # ---- advertisement sink --------------------------------------------------

    def on_adv(self, adv) -> None:
        """Record every advertisement; print new devices while `scan on`."""
        new = adv.address not in {_disp_mac(m) for m in self._devices}
        self._devices[_norm_mac(adv.address)] = adv
        if self._scanning and new:
            name = f" {adv.local_name!r}" if adv.local_name else ""
            print(f"[NEW] {adv.address} rssi={adv.rssi}{name}")

    def on_state(self, state: str) -> None:
        """Print backend lifecycle transitions (connect/scan/disconnect)."""
        print(f"[bleconnd] {state}")

    # ---- helpers -------------------------------------------------------------

    @staticmethod
    def _parse_handle(text: str) -> int:
        """Parse a handle given as decimal or 0x-prefixed hex."""
        return int(text, 16) if text.lower().startswith("0x") else int(text)

    def _index_db(self, services: list[dict]) -> None:
        """Build handle lookup tables from a discovered GATT database."""
        self._chars.clear()
        self._descs.clear()
        self._cccd_for.clear()
        for s in services:
            for c in s["characteristics"]:
                self._chars[c["handle"]] = c
                for d in c["descriptors"]:
                    self._descs[d["handle"]] = d
                    if str(d["uuid"]).lower()[4:8] == "2902":
                        self._cccd_for[c["handle"]] = d["handle"]

    # ---- command handlers ----------------------------------------------------

    async def do_scan(self, arg: str) -> None:
        """`scan on|off` — toggle live printing of discovered devices."""
        self._scanning = arg.strip().lower() == "on"
        print(f"scan {'on' if self._scanning else 'off'}")

    async def do_devices(self, arg: str) -> None:
        """`devices` — list every device seen so far, strongest first."""
        rows = sorted(self._devices.values(), key=lambda a: a.rssi, reverse=True)
        for a in rows:
            name = f" {a.local_name!r}" if a.local_name else ""
            conn = " [connectable]" if a.connectable else ""
            print(f"  {a.address} rssi={a.rssi}{name}{conn}")
        print(f"({len(rows)} device(s))")

    async def do_info(self, arg: str) -> None:
        """`info <addr>` — dump a discovered device's parsed advertisement."""
        adv = self._devices.get(_norm_mac(arg))
        if adv is None:
            print("unknown device (scan first)")
            return
        print(f"  address:      {adv.address} ({adv.address_type})")
        print(f"  rssi:         {adv.rssi}")
        print(f"  connectable:  {adv.connectable}")
        if adv.local_name:
            print(f"  name:         {adv.local_name!r}")
        if adv.tx_power is not None:
            print(f"  tx_power:     {adv.tx_power}")
        for u in adv.service_uuids:
            print(f"  service:      {u}")
        for k, v in adv.service_data.items():
            print(f"  service-data: {k} = {v.hex()}")
        for k, v in adv.manufacturer_data.items():
            print(f"  mfr-data:     0x{k:04x} = {v.hex()}")

    async def do_connect(self, arg: str) -> None:
        """`connect <addr>` — open a GATT connection and discover the database."""
        if self._conn is not None:
            print("already connected; disconnect first")
            return
        mac = _norm_mac(arg)
        print(f"connecting to {_disp_mac(mac)} ...")
        self._conn = await self._client.gatt_connect(mac)
        self._conn_mac = mac
        print(f"connected (handle {self._conn}, mtu {self._client.connection_mtu(self._conn)})")
        self._index_db(await self._client.gatt_discover(self._conn))
        print(f"discovered {len(self._chars)} characteristic(s); "
              f"run `list-attributes`")

    async def do_disconnect(self, arg: str) -> None:
        """`disconnect` — close the active connection."""
        if self._conn is None:
            print("not connected")
            return
        await self._client.gatt_disconnect(self._conn)
        print(f"disconnected from {_disp_mac(self._conn_mac)}")
        self._conn = self._conn_mac = None
        self._chars.clear(); self._descs.clear(); self._cccd_for.clear()
        self._notifying.clear()

    async def do_list_attributes(self, arg: str) -> None:
        """`list-attributes` — print the connected device's services, characteristics
        (with properties) and descriptors, each with its handle."""
        if self._conn is None:
            print("not connected")
            return
        services = await self._client.gatt_discover(self._conn)
        self._index_db(services)
        for s in services:
            print(f"Service {s['uuid']}  (handle 0x{s['handle']:04x})")
            for c in s["characteristics"]:
                props = ",".join(c["properties"])
                print(f"  Characteristic {c['uuid']}  (handle 0x{c['handle']:04x})  [{props}]")
                for d in c["descriptors"]:
                    print(f"    Descriptor   {d['uuid']}  (handle 0x{d['handle']:04x})")

    async def do_read(self, arg: str) -> None:
        """`read <handle>` — read a characteristic or descriptor value."""
        if self._conn is None:
            print("not connected")
            return
        h = self._parse_handle(arg.strip())
        if h in self._chars:
            data = await self._client.gatt_read_char(self._conn, h)
        elif h in self._descs:
            data = await self._client.gatt_read_desc(self._conn, h)
        else:
            print("unknown handle"); return
        print(f"  value: {data.hex()}  ({data!r})")

    async def do_write(self, arg: str) -> None:
        """`write <handle> <hex>` — write a characteristic or descriptor value."""
        if self._conn is None:
            print("not connected")
            return
        parts = arg.split()
        if len(parts) != 2:
            print("usage: write <handle> <hex>"); return
        h = self._parse_handle(parts[0])
        try:
            data = bytes.fromhex(parts[1])
        except ValueError:
            print("value must be hex"); return
        if h in self._chars:
            await self._client.gatt_write_char(self._conn, h, data)
        elif h in self._descs:
            await self._client.gatt_write_desc(self._conn, h, data)
        else:
            print("unknown handle"); return
        print("written")

    async def do_notify(self, arg: str) -> None:
        """`notify <handle> on|off` — (un)subscribe to a characteristic."""
        if self._conn is None:
            print("not connected")
            return
        parts = arg.split()
        if len(parts) != 2 or parts[1].lower() not in ("on", "off"):
            print("usage: notify <handle> on|off"); return
        h = self._parse_handle(parts[0])
        cccd = self._cccd_for.get(h)
        if cccd is None:
            print("characteristic has no CCCD (not notifiable)"); return
        if parts[1].lower() == "on":
            def cb(data: bytes, h=h) -> None:
                print(f"[notify 0x{h:04x}] {data.hex()}")
            indicate = ("indicate" in self._chars[h]["properties"]
                        and "notify" not in self._chars[h]["properties"])
            await self._client.gatt_start_notify(self._conn, h, cccd, cb, indicate)
            self._notifying.add(h)
            print(f"notifications enabled on 0x{h:04x}")
        else:
            await self._client.gatt_stop_notify(self._conn, h, cccd)
            self._notifying.discard(h)
            print(f"notifications disabled on 0x{h:04x}")

    async def do_mtu(self, arg: str) -> None:
        """`mtu` — show the active connection's negotiated MTU."""
        if self._conn is None:
            print("not connected")
            return
        print(f"  mtu: {self._client.connection_mtu(self._conn)}")

    async def dispatch(self, line: str) -> bool:
        """Route one command line to its handler; return False to exit."""
        line = line.strip()
        if not line:
            return True
        cmd, _, arg = line.partition(" ")
        cmd = cmd.lower()
        if cmd in ("quit", "exit"):
            return False
        if cmd == "help":
            print(self.HELP)
            return True
        handler = {
            "scan": self.do_scan, "devices": self.do_devices, "info": self.do_info,
            "connect": self.do_connect, "disconnect": self.do_disconnect,
            "list-attributes": self.do_list_attributes, "gatt": self.do_list_attributes,
            "read": self.do_read, "write": self.do_write, "notify": self.do_notify,
            "mtu": self.do_mtu,
        }.get(cmd)
        if handler is None:
            print(f"unknown command: {cmd} (try `help`)")
            return True
        try:
            await handler(arg)
        except Exception as exc:  # noqa: BLE001 - keep the REPL alive on any error
            print(f"error: {exc}")
        return True


def _make_transport(args):
    """Build the bleconnd transport: SSH when --key is given, else plain TCP."""
    if args.key:
        import asyncssh
        sshmod = _load("ssh")
        key = asyncssh.read_private_key(args.key)
        return sshmod.SshTunnelTransport(
            args.ap, args.user, args.port, key,
            jump_host=args.jump_host, jump_user=args.jump_user)
    return bleconn.TcpTransport(args.host, args.port)


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1", help="forwarded bleconnd host")
    ap.add_argument("--port", type=int, default=8383, help="bleconnd port")
    ap.add_argument("--ap", help="AP host for direct SSH (implies --key)")
    ap.add_argument("--key", help="SSH private key file (enables SSH transport)")
    ap.add_argument("--user", default="admin", help="AP SSH username")
    ap.add_argument("--jump-host", default=None)
    ap.add_argument("--jump-user", default=None)
    args = ap.parse_args()

    ctl_holder: dict[str, BleCtl] = {}

    def on_adv(adv):
        ctl_holder["ctl"].on_adv(adv)

    def on_state(state):
        ctl_holder["ctl"].on_state(state)

    client = bleconn.BleConnClient(
        _make_transport(args), on_advertisement=on_adv, on_state=on_state)
    ctl = BleCtl(client)
    ctl_holder["ctl"] = ctl

    task = asyncio.create_task(client.run())
    loop = asyncio.get_running_loop()
    print("blectl — type `help`. Waiting for the AP session...")
    try:
        while True:
            try:
                line = await loop.run_in_executor(None, input, "[blectl]# ")
            except (EOFError, KeyboardInterrupt):
                break
            if not await ctl.dispatch(line):
                break
    finally:
        await client.stop()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
