#!/usr/bin/env python3
"""Client / probe for the UniFi AP `bleconnd` JSON API (TCP :8383, loopback).

Wire format (reverse-engineered from ble/*.pcap):

    frame = type(1) | 0x01 | 0x00 0x00 | length(4, big-endian) | json-utf8[length]

A logical message is two consecutive frames sent back-to-back:
    type 1 = envelope  {"action","id","timestamp","type":"request|response|event",...}
    type 2 = body      the action's params / result / event payload

Usage:
    # offline: validate the codec against the captured handshakes
    python3 bleconn.py --selftest ../ble/bleconnd.pcap ../ble/bleconnd/bleconn.pcap

    # live: through an SSH tunnel to an AP's loopback bleconnd
    #   ssh -N -J root@192.168.10.1 root@192.168.10.10 -L 8391:127.0.0.1:8383
    python3 bleconn.py --host 127.0.0.1 --port 8391 --scan-phys 1M --duration 20
"""
from __future__ import annotations

import argparse
import json
import socket
import struct
import sys
import time
import uuid

FRAME_HDR = struct.Struct(">B B H I")  # type, flags(=1), reserved(=0), length
ENVELOPE, BODY = 1, 2


def now_ms() -> int:
    return int(time.time() * 1000)


def encode_frame(ftype: int, obj: dict) -> bytes:
    data = json.dumps(obj, separators=(",", ":")).encode("utf-8")
    return FRAME_HDR.pack(ftype, 0x01, 0x0000, len(data)) + data


def encode_message(action: str, params: dict, msg_type: str = "request",
                   msg_id: str | None = None) -> bytes:
    envelope = {
        "action": action,
        "id": msg_id or str(uuid.uuid4()),
        "timestamp": now_ms(),
        "type": msg_type,
    }
    return encode_frame(ENVELOPE, envelope) + encode_frame(BODY, params)


def iter_frames(buf: bytes):
    """Yield (ftype, json_obj, consumed_end) for every complete frame in buf."""
    i, n = 0, len(buf)
    while i + FRAME_HDR.size <= n:
        ftype, _flags, _resv, length = FRAME_HDR.unpack_from(buf, i)
        end = i + FRAME_HDR.size + length
        if end > n:
            break
        raw = buf[i + FRAME_HDR.size:end]
        try:
            obj = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            obj = {"__raw__": raw.hex()}
        yield ftype, obj, end
        i = end


class BleConnClient:
    def __init__(self, host: str, port: int, timeout: float = 5.0):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.buf = b""
        self.client_id = str(uuid.uuid4())

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass

    def send(self, action: str, params: dict | None = None) -> str:
        msg_id = str(uuid.uuid4())
        self.sock.sendall(encode_message(action, params or {}, msg_id=msg_id))
        return msg_id

    def _fill(self) -> bool:
        try:
            chunk = self.sock.recv(65536)
        except socket.timeout:
            return False
        if not chunk:
            raise ConnectionError("bleconnd closed the connection")
        self.buf += chunk
        return True

    def messages(self):
        """Yield (envelope, body) pairs as they arrive, forever."""
        pending_env = None
        while True:
            consumed = 0
            for ftype, obj, end in iter_frames(self.buf):
                consumed = end
                if ftype == ENVELOPE:
                    pending_env = obj
                elif ftype == BODY:
                    yield pending_env, obj
                    pending_env = None
            if consumed:
                self.buf = self.buf[consumed:]
            if not self._fill():
                yield None, None  # timeout tick, lets caller check the clock

    def request(self, action: str, params: dict | None = None, timeout: float = 5.0):
        want = self.send(action, params)
        deadline = time.time() + timeout
        for env, body in self.messages():
            if env is None:
                if time.time() > deadline:
                    raise TimeoutError(f"no response to {action!r}")
                continue
            if env.get("id") == want and env.get("type") == "response":
                return env, body
        raise ConnectionError("stream ended")


def run_live(args):
    c = BleConnClient(args.host, args.port)
    print(f"connected to {args.host}:{args.port}  client_id={c.client_id}")

    env, body = c.request("hdshkStart", {"clientID": c.client_id})
    ec = env.get("errorCode")
    if ec:
        print(f"!! hdshkStart rejected: errorCode={ec} error={env.get('error')!r}")
        print("   (a second client may not be allowed while Protect is connected)")
        return
    print("handshake OK. server/interface info:")
    print(json.dumps(body, indent=2))

    c.request("hdshkFinish", {})
    _, st = c.request("ifStatusGet", {})
    print("ifStatusGet:", st)

    phys = [p.strip() for p in args.scan_phys.split(",") if p.strip()]
    env, body = c.request("scanStart", {"scanPhys": phys})
    if env.get("errorCode"):
        print(f"!! scanStart failed: {env.get('error')!r}")
        return
    print(f"scanning on PHYs {phys} for {args.duration}s "
          f"(coexisting with blebrd/blebr2d)...\n")

    seen, deadline = {}, time.time() + args.duration
    for env, body in c.messages():
        if env is None:
            if time.time() > deadline:
                break
            continue
        if env.get("action") == "scanResult" or env.get("type") == "event":
            addr = (body or {}).get("addr")
            key = json.dumps(addr, sort_keys=True) if addr is not None else id(body)
            if key not in seen:
                seen[key] = body
                print("scanResult:", json.dumps(body))
            elif args.verbose:
                print("scanResult:", json.dumps(body))

    c.request("scanStop", {})
    print(f"\ndone. {len(seen)} unique device(s) seen.")
    c.close()


def run_selftest(paths):
    def read_pcap_tcp_streams(path):
        with open(path, "rb") as f:
            data = f.read()
        # pcap global header is 24 bytes; link-layer header length depends on DLT:
        #   1 = Ethernet (14 bytes), 113 = Linux cooked / SLL (16 bytes).
        linktype = struct.unpack_from("<I", data, 20)[0]
        link = 16 if linktype == 113 else 14
        off, streams = 24, {}
        while off + 16 <= len(data):
            _s, _u, caplen, _o = struct.unpack_from("<IIII", data, off)
            off += 16
            pkt, off = data[off:off + caplen], off + caplen
            ip = pkt[link:]
            if len(ip) < 20 or ip[9] != 6:  # TCP only
                continue
            ihl = (ip[0] & 0x0f) * 4
            tcp = ip[ihl:]
            if len(tcp) < 20:
                continue
            sport, dport = struct.unpack_from(">HH", tcp, 0)
            seq = struct.unpack_from(">I", tcp, 4)[0]
            payload = tcp[(tcp[12] >> 4) * 4:]
            if payload:
                streams.setdefault((sport, dport), []).append((seq, payload))
        return streams

    total = 0
    for path in paths:
        print(f"== {path} ==")
        for (sp, dp), parts in read_pcap_tcp_streams(path).items():
            parts.sort()
            buf = b"".join(p for _s, p in parts)
            frames = list(iter_frames(buf))
            total += len(frames)
            print(f"  stream {sp}->{dp}: {len(frames)} frames")
            for ftype, obj, _end in frames:
                tag = "ENV " if ftype == ENVELOPE else "BODY"
                s = json.dumps(obj)
                print(f"    {tag} {s[:140]}")
    print(f"\ncodec OK: decoded {total} frames total.")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--selftest", nargs="+", metavar="PCAP",
                    help="decode captured pcap(s) and exit (offline codec check)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8391)
    ap.add_argument("--scan-phys", default="1M",
                    help="comma list: 1M,coded (default 1M)")
    ap.add_argument("--duration", type=int, default=20)
    ap.add_argument("--verbose", action="store_true",
                    help="print every scanResult, not just first-seen per device")
    args = ap.parse_args()

    if args.selftest:
        run_selftest(args.selftest)
    else:
        run_live(args)


if __name__ == "__main__":
    sys.exit(main())
