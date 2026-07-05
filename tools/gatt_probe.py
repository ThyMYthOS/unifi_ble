#!/usr/bin/env python3
"""Phase-A GATT protocol probe for bleconnd.

Drives connection + GATT-client actions against a live AP (through an SSH-forwarded
local port) and prints every frame verbatim, so we can reverse the request/response
/event JSON schemas. Iterate the SEQUENCE below based on what bleconnd returns
(its errors are descriptive). Always attempts to close the connection at the end.

    python3 tools/gatt_probe.py --host 127.0.0.1 --port 8383 --mac cd2a06463422
"""
from __future__ import annotations

import argparse
import json
import socket
import struct
import time
import uuid

FRAME_HDR = struct.Struct(">B B H I")
ENVELOPE, BODY = 1, 2


def enc(action, params, msg_id):
    out = bytearray()
    for ftype, obj in ((ENVELOPE, {"action": action, "id": msg_id,
                                   "timestamp": int(time.time() * 1000),
                                   "type": "request"}), (BODY, params)):
        data = json.dumps(obj, separators=(",", ":")).encode()
        out += FRAME_HDR.pack(ftype, 0x01, 0x0000, len(data)) + data
    return bytes(out)


class Probe:
    def __init__(self, host, port):
        self.s = socket.create_connection((host, port), timeout=8)
        self.buf = b""

    def _read_frame(self):
        while len(self.buf) < FRAME_HDR.size:
            self._fill()
        ftype, _f, _r, ln = FRAME_HDR.unpack(self.buf[:FRAME_HDR.size])
        need = FRAME_HDR.size + ln
        while len(self.buf) < need:
            self._fill()
        raw = self.buf[FRAME_HDR.size:need]
        self.buf = self.buf[need:]
        return ftype, (json.loads(raw) if raw else {})

    def _fill(self):
        chunk = self.s.recv(65536)
        if not chunk:
            raise ConnectionError("closed")
        self.buf += chunk

    def _read_message(self):
        ft, env = self._read_frame()
        while ft != ENVELOPE:
            ft, env = self._read_frame()
        _bt, body = self._read_frame()
        return env, body

    def call(self, action, params=None, timeout=8.0, quiet=False):
        """Send a request; print/return the matching response, printing events too."""
        mid = str(uuid.uuid4())
        self.s.sendall(enc(action, params or {}, mid))
        deadline = time.time() + timeout
        self.s.settimeout(timeout)
        while time.time() < deadline:
            try:
                env, body = self._read_message()
            except socket.timeout:
                break
            tag = env.get("type", "?")
            if env.get("id") == mid and tag == "response":
                ec = env.get("errorCode")
                print(f"  <= {action} resp err={ec!r} {env.get('error','')!r}  {json.dumps(body)}")
                return env, body
            else:
                print(f"  ** EVENT env={json.dumps(env)} body={json.dumps(body)}")
        if not quiet:
            print(f"  <= {action}: (no response within {timeout}s)")
        return None, None

    def listen(self, seconds, label=""):
        print(f"  .. listening {seconds}s {label}")
        self.s.settimeout(seconds)
        end = time.time() + seconds
        while time.time() < end:
            try:
                env, body = self._read_message()
            except socket.timeout:
                break
            print(f"  ** EVENT env={json.dumps(env)} body={json.dumps(body)}")

    def wait_event(self, name, timeout=15.0):
        """Read frames until an event with env['name']==name; print everything."""
        self.s.settimeout(timeout)
        end = time.time() + timeout
        while time.time() < end:
            try:
                env, body = self._read_message()
            except socket.timeout:
                break
            print(f"  ** EVENT env={json.dumps(env)} body={json.dumps(body)}")
            if env.get("name") == name:
                return env, body
        print(f"  !! event {name!r} not seen within {timeout}s")
        return None, None

    def close(self):
        try:
            self.s.close()
        except OSError:
            pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8383)
    ap.add_argument("--mac", default="cd2a06463422", help="target device mac (no colons)")
    ap.add_argument("--type", default="public", help="address type public|random")
    args = ap.parse_args()

    p = Probe(args.host, args.port)
    try:
        print("# handshake")
        p.call("hdshkStart", {"clientID": str(uuid.uuid4())})
        p.call("hdshkFinish", {})
        print("# reservation / connection state")
        p.call("freeResvsGet", {})
        _, conns = p.call("connsGet", {})

        # Clean up any dangling connections from earlier runs.
        for h in list((conns or {}).get("connections", {})):
            print(f"# closing stale connHandle {h}")
            p.call("connClose", {"connHandle": int(h)}, timeout=6)
            p.listen(2, "(post stale close)")

        addr = {"mac": args.mac, "type": args.type}
        print(f"# connOpen to {addr}")
        env, body = p.call("connOpen", {"addr": addr})
        ch = (body or {}).get("connHandle")
        if ch is None:
            return
        oenv, obody = p.wait_event("connOpened", 15)
        if oenv is None:
            print("  !! never opened; aborting")
            return

        print("# gattcDbUpdate then primaryServicesGet")
        p.call("gattcDbUpdate", {"connHandle": ch})
        time.sleep(1.5)
        _, svcs = p.call("gattcPrimaryServicesGet", {"connHandle": ch})
        print(f"# service handles = {svcs}")

        # Build DB and subscribe to every notify/indicate characteristic to catch
        # the notification event schema.
        subscribed = []
        for sh in (svcs or []):
            _, sinfo = p.call("gattcServiceGet", {"connHandle": ch, "handle": sh}, timeout=6, quiet=True)
            for chh in (sinfo or {}).get("includedChars", []):
                _, cinfo = p.call("gattcCharGet", {"connHandle": ch, "handle": chh}, timeout=6, quiet=True)
                props = (cinfo or {}).get("properties", [])
                if "notify" not in props and "indicate" not in props:
                    continue
                cccd = None
                for dh in (cinfo or {}).get("includedDescriptors", []):
                    _, dinfo = p.call("gattcDescGet", {"connHandle": ch, "handle": dh}, timeout=6, quiet=True)
                    if str((dinfo or {}).get("uuid", "")).lower() == "2902":
                        cccd = dh
                if cccd is None:
                    continue
                val = "0200" if "indicate" in props and "notify" not in props else "0100"
                _, r = p.call("gattcDescValueWrite",
                              {"connHandle": ch, "handle": cccd, "data": val}, timeout=6, quiet=True)
                print(f"# subscribed char {chh} ({(cinfo or {}).get('uuid')}) props={props} "
                      f"cccd={cccd} val={val} -> {json.dumps(r and 'ok' or 'fail')}")
                subscribed.append(chh)

        print(f"# subscribed to {len(subscribed)} char(s); listening for notifications")
        print("# >>> trigger notifications in nRF Connect now (or let the sample service run) <<<")
        p.listen(30, "(awaiting notify events)")

        print("# cleanup")
        p.call("connClose", {"connHandle": ch}, timeout=6)
        p.wait_event("connClosed", 5)
        return
        _unused_svcs = svcs  # (old walk below retained but unreachable)

        # Focus on the custom AAA1 (read+notify) char 167 and AAA2 (write) char 173.
        NOTIFY_CH, WRITE_CH = 167, 173
        _, cinfo = p.call("gattcCharGet", {"connHandle": ch, "handle": NOTIFY_CH}, timeout=6)
        print(f"# char {NOTIFY_CH} = {json.dumps(cinfo)}")
        cccd = None
        for dh in (cinfo or {}).get("includedDescriptors", []):
            _, dinfo = p.call("gattcDescGet", {"connHandle": ch, "handle": dh}, timeout=6)
            print(f"#   desc {dh} = {json.dumps(dinfo)}")
            if isinstance(dinfo, dict) and str(dinfo.get("uuid", "")).lower() in ("2902",):
                cccd = dh
        print(f"# CCCD handle = {cccd}")
        if cccd is not None:
            p.call("gattcDescValueRead", {"connHandle": ch, "handle": cccd}, timeout=6)
            print("# enable notify: descValueWrite CCCD=0100 (trying payload shapes)")
            for params in (
                {"connHandle": ch, "handle": cccd, "data": "0100"},
                {"connHandle": ch, "handle": cccd, "data": "0100", "offset": 0},
                {"connHandle": ch, "handle": cccd, "value": "0100", "offset": 0},
            ):
                _, r = p.call("gattcDescValueWrite", params, timeout=6)
                _, chk = p.call("gattcDescValueRead", {"connHandle": ch, "handle": cccd}, timeout=6)
                print(f"#   after write {params}: CCCD now {json.dumps(chk)}")
                if (chk or {}).get("data") == "0100":
                    print("#   ^ this shape worked"); break

        print("# write test: charValueWrite AAA2 (trying payload shapes)")
        for params in (
            {"connHandle": ch, "handle": WRITE_CH, "data": "aabbcc"},
            {"connHandle": ch, "handle": WRITE_CH, "data": "aabbcc", "withResponse": True},
            {"connHandle": ch, "handle": WRITE_CH, "data": "aabbcc", "offset": 0},
        ):
            _, r = p.call("gattcCharValueWrite", params, timeout=6)
            print(f"#   charValueWrite {params} -> ok" if r is not None else "#   (no resp)")

        print("# >>> TAP 'Notify' on the AAA1 (handle 167) characteristic in nRF Connect now <<<")
        p.listen(25, "(awaiting notification event)")

        if cccd is not None:
            p.call("gattcDescValueWrite", {"connHandle": ch, "handle": cccd, "value": "0000"}, timeout=6)

        print("# cleanup")
        p.call("connClose", {"connHandle": ch}, timeout=6)
        p.wait_event("connClosed", 5)
    finally:
        p.close()


if __name__ == "__main__":
    main()
