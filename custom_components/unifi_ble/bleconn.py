"""Async client for the UniFi AP `bleconnd` JSON API (TCP :8383), plus a parser
that turns its pre-parsed advertisement structures into Home Assistant /
bleak advertisement fields.

The client is transport-agnostic: point it at a `host:port` that reaches an AP's
loopback `bleconnd` (in practice, an SSH-forwarded local port). It performs the
handshake, starts a scan, and invokes a callback for every advertisement, with
automatic reconnect.

Wire format (see also tools/bleconn.py):
    frame = type(1) | 0x01 | 0x00 0x00 | length(4, big-endian) | json-utf8
    logical message = envelope frame (type 1) + body frame (type 2)
"""
from __future__ import annotations

import asyncio
import json
import struct
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

FRAME_HDR = struct.Struct(">B B H I")
ENVELOPE, BODY = 1, 2

# Base UUID for 16-/32-bit Bluetooth SIG UUIDs.
_UUID_BASE = "-0000-1000-8000-00805f9b34fb"


@dataclass(slots=True)
class Advertisement:
    """Normalized advertisement, mapping 1:1 to habluetooth's on-advertisement args."""

    address: str                                  # "AA:BB:CC:DD:EE:FF"
    rssi: int
    local_name: str | None = None
    service_uuids: list[str] = field(default_factory=list)
    service_data: dict[str, bytes] = field(default_factory=dict)
    manufacturer_data: dict[int, bytes] = field(default_factory=dict)
    tx_power: int | None = None
    connectable: bool = False
    address_type: str = "public"


def _u16_uuid(v: int) -> str:
    return f"0000{v:04x}{_UUID_BASE}"


def _u32_uuid(v: int) -> str:
    return f"{v:08x}{_UUID_BASE}"


def _u128_uuid(b: bytes) -> str:
    # 16 bytes little-endian on the wire -> big-endian hyphenated string.
    h = b[::-1].hex()
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def parse_advertisement(body: dict) -> Advertisement | None:
    """Convert a `scanResult` body into an Advertisement, or None if malformed."""
    addr = body.get("addr")
    if not addr or "mac" not in addr:
        return None
    mac = addr["mac"].replace(":", "").replace("-", "")
    if len(mac) != 12:
        return None
    address = ":".join(mac[i:i + 2] for i in range(0, 12, 2)).upper()
    signal = body.get("signal") or {}
    adv = Advertisement(
        address=address,
        rssi=int(signal.get("strength", -127)),
        connectable=bool(body.get("connectable", False)),
        address_type=addr.get("type", "public"),
    )

    for ad in body.get("data", []):
        t = ad.get("type")
        try:
            val = bytes.fromhex(ad.get("value", ""))
        except ValueError:
            continue

        if t in (0x08, 0x09):                     # shortened / complete local name
            adv.local_name = val.decode("utf-8", "replace")
        elif t in (0x02, 0x03):                   # 16-bit service class UUIDs
            for i in range(0, len(val) - 1, 2):
                adv.service_uuids.append(_u16_uuid(int.from_bytes(val[i:i + 2], "little")))
        elif t in (0x04, 0x05):                   # 32-bit service class UUIDs
            for i in range(0, len(val) - 3, 4):
                adv.service_uuids.append(_u32_uuid(int.from_bytes(val[i:i + 4], "little")))
        elif t in (0x06, 0x07):                   # 128-bit service class UUIDs
            for i in range(0, len(val) - 15, 16):
                adv.service_uuids.append(_u128_uuid(val[i:i + 16]))
        elif t == 0x0A:                           # tx power level (signed)
            if val:
                adv.tx_power = int.from_bytes(val[:1], "little", signed=True)
        elif t == 0x16:                           # service data - 16-bit UUID
            if len(val) >= 2:
                adv.service_data[_u16_uuid(int.from_bytes(val[:2], "little"))] = val[2:]
        elif t == 0x20:                           # service data - 32-bit UUID
            if len(val) >= 4:
                adv.service_data[_u32_uuid(int.from_bytes(val[:4], "little"))] = val[4:]
        elif t == 0x21:                           # service data - 128-bit UUID
            if len(val) >= 16:
                adv.service_data[_u128_uuid(val[:16])] = val[16:]
        elif t == 0xFF:                           # manufacturer specific data
            if len(val) >= 2:
                adv.manufacturer_data[int.from_bytes(val[:2], "little")] = val[2:]
    if adv.service_uuids:
        # The advertisement and its scan response may repeat the same UUIDs.
        adv.service_uuids = list(dict.fromkeys(adv.service_uuids))
    return adv


def _encode_message(action: str, params: dict, msg_id: str) -> bytes:
    env = {"action": action, "id": msg_id, "timestamp": int(time.time() * 1000),
           "type": "request"}
    out = bytearray()
    for ftype, obj in ((ENVELOPE, env), (BODY, params)):
        data = json.dumps(obj, separators=(",", ":")).encode("utf-8")
        out += FRAME_HDR.pack(ftype, 0x01, 0x0000, len(data)) + data
    return bytes(out)


async def _read_frame(reader: asyncio.StreamReader) -> tuple[int, dict]:
    hdr = await reader.readexactly(FRAME_HDR.size)
    ftype, _flags, _resv, length = FRAME_HDR.unpack(hdr)
    raw = await reader.readexactly(length) if length else b""
    return ftype, (json.loads(raw) if raw else {})


async def _read_message(reader: asyncio.StreamReader) -> tuple[dict, dict]:
    """Read one envelope+body pair, resyncing on unexpected frame types."""
    ftype, payload = await _read_frame(reader)
    while True:
        while ftype != ENVELOPE:                  # resync if we ever land mid-stream
            ftype, payload = await _read_frame(reader)
        env = payload
        ftype, payload = await _read_frame(reader)
        if ftype == BODY:
            return env, payload
        # Not a body: keep the new frame and retry (an envelope without a body
        # must not swallow the next message's envelope).


AdvCallback = Callable[[Advertisement], None]


class Transport:
    """Supplies a byte stream to an AP's bleconnd. Subclasses implement the how."""

    async def connect(self):  # -> tuple[StreamReader-like, StreamWriter-like]
        raise NotImplementedError

    async def disconnect(self) -> None:
        raise NotImplementedError

    def describe(self) -> str:
        return self.__class__.__name__


class TcpTransport(Transport):
    """Plain TCP, e.g. to a pre-existing SSH `-L` forward. Used by the test tools."""

    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port
        self._writer = None

    async def connect(self):
        reader, writer = await asyncio.open_connection(self.host, self.port)
        self._writer = writer
        return reader, writer

    async def disconnect(self) -> None:
        if self._writer is not None:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except OSError:
                pass
            self._writer = None

    def describe(self) -> str:
        return f"{self.host}:{self.port}"


class BleConnClient:
    """One connection to one AP's bleconnd. Runs a scan and reports advertisements."""

    def __init__(self, transport: Transport, *, scan_phys: tuple[str, ...] = ("1M",),
                 on_advertisement: AdvCallback,
                 on_state: Callable[[str], None] | None = None,
                 reconnect_delay: float = 5.0):
        self._transport = transport
        self.scan_phys = list(scan_phys)
        self._on_adv = on_advertisement
        self._on_state = on_state or (lambda s: None)
        self._reconnect_delay = reconnect_delay
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._pending: dict[str, asyncio.Future] = {}
        self._closing = False
        self._stop_event = asyncio.Event()
        self.iface: str | None = None
        self.adapter_mac: str | None = None

    async def _request(self, action: str, params: dict | None = None,
                       timeout: float = 10.0) -> dict:
        assert self._writer is not None
        msg_id = str(uuid.uuid4())
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = fut
        self._writer.write(_encode_message(action, params or {}, msg_id))
        await self._writer.drain()
        try:
            env, body = await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(msg_id, None)
        if env.get("errorCode"):
            raise RuntimeError(f"{action} failed: {env.get('error')!r} "
                               f"(code {env['errorCode']})")
        return body

    async def _session(self) -> None:
        reader, writer = await self._transport.connect()
        if self._closing:                         # stop() raced with connect()
            await self._transport.disconnect()
            return
        self._reader, self._writer = reader, writer
        # Kick off the reader so _request futures resolve.
        reader_task = asyncio.create_task(self._read_loop(reader))
        try:
            info = await self._request("hdshkStart", {"clientID": str(uuid.uuid4())})
            ifs = info.get("ifs", {})
            self.iface = next(iter(ifs), None)
            if self.iface:
                self.adapter_mac = ifs[self.iface].get("addr", {}).get("mac")
            await self._request("hdshkFinish", {})
            await self._request("scanStart", {"scanPhys": self.scan_phys})
            self._on_state("scanning")
            await reader_task
        finally:
            reader_task.cancel()
            await asyncio.gather(reader_task, return_exceptions=True)
            await self._transport.disconnect()
            self._reader = self._writer = None

    async def _read_loop(self, reader: asyncio.StreamReader) -> None:
        while True:
            env, body = await _read_message(reader)
            msg_id = env.get("id")
            if env.get("type") == "response" and msg_id in self._pending:
                fut = self._pending[msg_id]
                if not fut.done():
                    fut.set_result((env, body))
            elif env.get("action") == "scanResult" or env.get("type") == "event":
                adv = parse_advertisement(body)
                if adv is not None:
                    self._on_adv(adv)

    def _fail_pending(self) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.cancel()
        self._pending.clear()

    async def run(self) -> None:
        """Connect and keep scanning, reconnecting on failure, until stop()
        or task cancellation."""
        try:
            while not self._closing:
                try:
                    await self._session()
                except Exception as exc:  # incl. asyncssh errors; CancelledError is BaseException
                    self._on_state(f"disconnected: {exc}")
                self._fail_pending()
                if self._closing:
                    break
                try:
                    await asyncio.wait_for(self._stop_event.wait(),
                                           self._reconnect_delay)
                except TimeoutError:
                    pass
        finally:
            self._closing = True
            self._fail_pending()
            await self._transport.disconnect()

    async def stop(self) -> None:
        self._closing = True
        self._stop_event.set()
        await self._transport.disconnect()


async def probe_transport(transport: Transport, timeout: float = 15.0) -> dict:
    """One-shot connect + handshake over any transport; return {'iface','mac','caps'}."""
    reader, writer = await asyncio.wait_for(transport.connect(), timeout)
    try:
        async def request(action: str, params: dict) -> dict:
            writer.write(_encode_message(action, params, str(uuid.uuid4())))
            await writer.drain()
            while True:                           # skip events interleaved with the response
                env, body = await asyncio.wait_for(_read_message(reader), timeout)
                if env.get("type") != "response":
                    continue
                if env.get("errorCode"):
                    raise RuntimeError(f"{action} rejected: {env.get('error')!r}")
                return body

        info = await request("hdshkStart", {"clientID": str(uuid.uuid4())})
        ifs = info.get("ifs", {})
        iface = next(iter(ifs), None)
        mac = ifs.get(iface, {}).get("addr", {}).get("mac") if iface else None
        await request("hdshkFinish", {})
        return {"iface": iface, "mac": mac, "caps": ifs.get(iface, {}).get("caps", {})}
    finally:
        await transport.disconnect()


async def probe(host: str, port: int, timeout: float = 10.0) -> dict:
    """TCP convenience wrapper around probe_transport (tests / -L forwards)."""
    return await probe_transport(TcpTransport(host, port), timeout)
