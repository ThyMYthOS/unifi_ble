# UniFi AP BLE Proxy for Home Assistant

Turn the Bluetooth Low Energy radios built into UniFi access points into remote
Bluetooth scanners for Home Assistant — the same role an ESPHome Bluetooth proxy
plays, but using hardware you already have. Each adopted AP becomes a coverage
point; Home Assistant aggregates them and picks the closest one per device by RSSI.

> **Status: early / experimental.** Passive advertisement scanning is implemented
> and validated end-to-end. Connectable (GATT) proxying is not implemented yet.

## How it works

UniFi APs run a local BLE service, `bleconnd`, that exposes a line-oriented JSON
API on `127.0.0.1:8383` (loopback only). It already serves multiple clients
(UniFi's own `blebrd`/`blebr2d` bridges), so this integration attaches as an
additional client, starts a scan, and forwards every advertisement to Home
Assistant. UniFi Protect keeps using its own path (the TLS bridge on `:8381`) and
is not disturbed.

```
MT7915 radio ─ btservice(:8873) ─ bleconnd(:8383, loopback)
                                        │  (SSH direct-tcpip channel)
                                        ▼
                         Home Assistant ── custom_components/unifi_ble
                                        │
                          habluetooth remote scanner (one per AP)
```

Because `:8383` is loopback-only, the integration reaches it over SSH. It
generates its own SSH keypair, shows you the public key at setup, and opens the
tunnel itself using [`asyncssh`](https://asyncssh.readthedocs.io/) — no external
`autossh` or manually managed port forwards. If Home Assistant cannot reach the
AP directly, it can hop through your UDM/gateway as an SSH jump host.

## Requirements

- Home Assistant 2026.7+ (validated against `habluetooth` 6.26.4; the integration
  depends on the `bluetooth_adapters` integration).
- UniFi APs with a BLE radio, adopted by a controller where you can enable
  **Device SSH Authentication** and add an SSH public key.
- Network reachability from Home Assistant to each AP's SSH port, directly or via
  a jump host (e.g. the UDM/gateway).

## Installation

1. Copy `custom_components/unifi_ble/` into your Home Assistant `config/custom_components/`
   directory and restart Home Assistant.
2. Go to **Settings → Devices & Services → Add Integration → "UniFi AP BLE Proxy"**.
   The first screen shows a generated **public SSH key**.
3. In UniFi, go to **Settings → System → Device SSH Authentication**, enable it,
   and add that public key. It is pushed to your adopted devices (the APs and the
   gateway), so one key covers everything.
4. Back in Home Assistant, enter each AP's connection details. Add the integration
   once per AP.

## Configuration

Per AP:

| Field         | Meaning                                                        | Default         |
|---------------|----------------------------------------------------------------|-----------------|
| Host          | AP IP/hostname                                                 | —               |
| Username      | SSH username (UniFi device SSH user)                           | `admin`         |
| Port          | `bleconnd` port on the AP                                      | `8383`          |
| Jump host     | UDM/gateway to hop through, if the AP isn't directly reachable | *(blank)*       |
| Jump username | SSH username on the jump host                                  | same as AP user |

Each AP is identified by its BLE MAC (discovered during setup) so re-adding is idempotent.

## Repository layout

```
custom_components/unifi_ble/   The Home Assistant integration
  bleconn.py    async bleconnd client + advertisement parser (transport-agnostic)
  ssh.py        SSH tunnel transport + shared Ed25519 keypair management
  scanner.py    UnifiBleScanner (habluetooth BaseHaRemoteScanner)
  __init__.py   entry setup: one scanner per AP, register + background task
  config_flow.py, const.py, manifest.json, strings.json, translations/
tools/
  py            run the project venv's Python with the snap sandbox env stripped
  scan_ha.py    exercise the async client against forwarded ports; print adverts
  bleconn.py    pcap decoder + one-shot probe for the bleconnd protocol
  validate_ha.py  validate the habluetooth API surface inside a real HA venv
```

## Development / testing

The client and parser are transport-agnostic, so you can test them against a
plain TCP forward without Home Assistant:

```bash
# forward an AP's loopback bleconnd to localhost (direct, or -J via the gateway)
ssh -N -L 8383:127.0.0.1:8383 <ap-host>

# scan and print parsed advertisements (one or more comma-separated targets)
tools/py tools/scan_ha.py --targets 127.0.0.1:8383 --duration 20
```

To validate the Home Assistant API surface, run the checker **inside your real HA
Python environment** (it imports `habluetooth`):

```bash
.venv/bin/python tools/validate_ha.py
```

See `AGENTS.md` for environment specifics (the venv wrapper, dependency pinning,
and what can and cannot be run where).

## Security notes

- The generated SSH private key is stored in Home Assistant's storage. Only its
  public key leaves Home Assistant (for you to provision in UniFi).
- Host-key verification is currently disabled (`known_hosts=None`). For APs on a
  trusted management network this is a deliberate simplification; trust-on-first-use
  pinning is a planned option.

## Roadmap

- Optional trust-on-first-use SSH host-key pinning.
- Connectable (GATT) proxying via `bleconnd`'s `gattc*` and connection-slot
  reservation API (the radio reports `maxConnections: 8`).
- Investigate the network-facing `:8381` bridge to remove SSH entirely (requires
  reversing its `BleAuthProto` DH + pre-shared-secret handshake).
