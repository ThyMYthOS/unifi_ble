# AGENTS.md

Guidance for AI agents (and humans) working in this repository. Read this before
running commands or editing code — the environment has sharp edges.

## What this project is

A Home Assistant custom integration that exposes UniFi access-point BLE radios as
remote Bluetooth scanners. It talks to each AP's local `bleconnd` JSON API
(`127.0.0.1:8383`, loopback) over an SSH tunnel and feeds advertisements into
`habluetooth`. See [`README.md`](README.md) for the user-facing overview.

Deliverable code lives in [`custom_components/unifi_ble/`](custom_components/unifi_ble/).
Helper scripts live in [`tools/`](tools/).

## Environment: read this first

This repo is typically worked on from a Claude Code snap sandbox with unusual
constraints. **First check `ls /usr/bin/python3.14`** — the constraints below only
apply when it is absent.

- **Which Python to use.** If `/usr/bin/python3.14` exists, call it directly (or
  the project's `.venv/bin/python`) — it can run the real venv/HA, so the
  [`tools/py`](tools/py) wrapper is **not** needed. `tools/py` is only required in
  the snap sandbox, which ships **only Python 3.12** and injects `PYTHONPATH`,
  `PYTHONHOME`, and `PIP_PREFIX` that hijack a normal venv and send installs into
  snap paths; the wrapper strips those vars (installs then land in `.venv`). Never
  call bare `python`/`pip` from a polluted snap shell.
- **When you're limited to the snap's Python 3.12** (no `/usr/bin/python3.14`):
  it **cannot execute the project's real venv/HA** — Home Assistant 2026.x needs
  Python 3.14, whose interpreter isn't visible to the sandbox, and the user's home
  is permission-denied. Therefore:
  - Anything importing `homeassistant`/`habluetooth` must be run by the **user**
    in their own terminal, not by the agent.
  - Use [`tools/validate_ha.py`](tools/validate_ha.py) for HA-side checks — hand it to the user to run
    (`.venv/bin/python tools/validate_ha.py`) and work from the pasted output.
  - You *can* still verify the transport/parser logic yourself with the snap's
    Python 3.12 (see below), since that code has no HA dependency.
- **Not available:** `sudo`, `curl`, `gh`, and there is no `GH_TOKEN`/`GITHUB_TOKEN`.
  `git` is available. `api.github.com` and PyPI are reachable. Creating a GitHub
  repo requires the user to provide a token or run `gh` themselves.

## How to test

Transport + parser (no Home Assistant needed), against a live AP:

```bash
# user forwards an AP's loopback bleconnd to a local port first, e.g.
#   ssh -N -L 8383:127.0.0.1:8383 <ap>            (or -J <gateway> for a jump)
tools/py tools/scan_ha.py --targets 127.0.0.1:8383 --duration 20
```

Protocol codec, offline against captured pcaps:

```bash
tools/py tools/bleconn.py --selftest ble/bleconnd.pcap ble/bleconnd/bleconn.pcap
```

GATT layer, live against a connectable device (stdlib-only, via a forward):

```bash
tools/py tools/gatt_probe.py       --host 127.0.0.1 --port 8383 --mac <mac>  # reverse schemas
tools/py tools/gatt_client_test.py --host 127.0.0.1 --port 8383 --mac <mac>  # exercise BleConnClient
```

Interactive bluetoothctl/gatttool-style CLI (scan / connect / gatt read/write/notify):

```bash
tools/py tools/blectl.py --host 127.0.0.1 --port 8383      # via a forward
# or over SSH (needs asyncssh, HA venv):
.venv/bin/python tools/blectl.py --ap <ap> --key <keyfile> [--jump-host <gw>]
```

HA API surface (must run in the user's real HA venv, not the agent):

```bash
tools/validate_ha.py
```

The SSH transport ([`ssh.py`](custom_components/unifi_ble/ssh.py)) is HA-free at import (the HA storage import is lazy),
so it can be unit-tested with only `asyncssh` — e.g. against an in-process
`asyncssh` server plus a fake `bleconnd`.

## Architecture notes and rationale

- **We attach to `bleconnd` on `:8383`, not the network-facing bridges.**
  `:8383` is plaintext, unauthenticated, and multi-client by design (UniFi's own
  bridges are already connected). We join as an additional client; scanning coexists
  with UniFi Protect.
- **Do not use `:8381` (`blebr2d`) or `:8080` (`blebrd`).** `:8381` is UniFi
  Protect's TLS+DH bridge (mutual TLS is defeatable with the on-disk cert, but the
  `BleAuthProto` DH + pre-shared-secret layer is not, and Protect holds the
  session). `:8080` (`blebrd` v1) is an opaque binary protocol we don't have the
  binary for. `:8383` over SSH is the chosen, proven path.
- **[`bleconn.py`](custom_components/unifi_ble/bleconn.py) is transport-agnostic.** `BleConnClient` takes a `Transport`
  (`TcpTransport` for tools/tests, `SshTunnelTransport` for the integration).
  Keep new transport work behind that abstraction.
- **Connectable GATT status.** The connection + GATT-client protocol is fully
  reversed ([`docs/unifi-ble-and-bleconnd.md`](docs/unifi-ble-and-bleconnd.md)).
  `BleConnClient` has the GATT layer
  (`gatt_connect`/`discover`/`read_char`/`write_char`/`start_notify`/…),
  [`client.py`](custom_components/unifi_ble/client.py) provides `UnifiBleakClient`
  (a bleak backend), and [`__init__.py`](custom_components/unifi_ble/__init__.py)
  registers the scanner `connectable=True` with a `HaBluetoothConnector`
  (`client=UnifiBleakClient`, `source`, `can_connect`) + `connection_slots`
  (`DEFAULT_MAX_CONNECTIONS`) and `register_client(source, client)`. Remaining:
  verify a real GATT connection through HA; tune slots/`can_connect` against the
  radio's shared reservation pool.
- **Read [`docs/architecture.md`](docs/architecture.md)** for the whole-system picture and file map.

## bleconnd protocol (quick reference)

Frame: `type(1) | 0x01 | 0x00 0x00 | length(4, big-endian) | json-utf8`. A logical
message is two frames: envelope (type 1: `action`/`id`/`timestamp`/`type`) then
body (type 2). Startup: `hdshkStart {clientID}` → `hdshkFinish` → `scanStart
{scanPhys:["1M"]}`, then a stream of `scanResult` events. A `scanResult` body has
`addr{mac,type}`, `connectable`, `signal{strength=rssi}`, and `data` as an
already-parsed AD list (`[{type:<AD type>, value:<hex>}]`). `parse_advertisement()`
maps that AD list to bleak/habluetooth fields.

## Conventions

- Match the surrounding style; keep comments to load-bearing constraints only.
- Verify changes to [`bleconn.py`](custom_components/unifi_ble/bleconn.py)/[`ssh.py`](custom_components/unifi_ble/ssh.py)
  with `tools/py -m py_compile` and, where possible, the scan/self-test tools before handing off.
- Any change touching the `habluetooth` API surface (scanner construction,
  `push()`, registration) must be re-validated via [`tools/validate_ha.py`](tools/validate_ha.py) in the
  user's HA — it is the one surface that shifts between HA versions.

## Do not

- Do not connect to or disturb the AP's `:8381`/`:8080` bridges or UniFi Protect.
- Do not commit any `*.key` or `*.cert` to any remote.
- Do not assume you can run Home Assistant or the 3.14 venv from the sandbox —
  delegate those runs to the user.
