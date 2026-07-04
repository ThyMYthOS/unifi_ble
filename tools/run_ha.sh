#!/usr/bin/env bash
#
# Start Home Assistant from the project venv with a dedicated dev config that has
# the unifi_ble custom integration linked in, for testing in the browser.
#
#   tools/run_ha.sh
#
# The config dir defaults to ./dev-ha-config (override with HA_CONFIG_DIR). The
# integration is symlinked, so code edits take effect on the next HA restart.
#
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CONFIG_DIR="${HA_CONFIG_DIR:-$ROOT/dev-ha-config}"
VENV_PY="$ROOT/.venv/bin/python"
HASS="$ROOT/.venv/bin/hass"

# The Claude Code snap sandbox exports these and they hijack a venv; clearing them
# is a no-op in a normal shell and protects against a polluted environment.
unset PYTHONPATH PYTHONHOME PIP_PREFIX 2>/dev/null || true

if [ ! -x "$VENV_PY" ]; then
  echo "error: no venv Python at $VENV_PY — create the venv first." >&2
  exit 1
fi
if ! "$VENV_PY" -c "import homeassistant" 2>/dev/null; then
  echo "error: homeassistant is not installed in $ROOT/.venv" >&2
  exit 1
fi

mkdir -p "$CONFIG_DIR/custom_components"

# Link the integration so edits in the repo are picked up on restart.
LINK="$CONFIG_DIR/custom_components/unifi_ble"
if [ -L "$LINK" ] || [ -e "$LINK" ]; then
  rm -rf "$LINK"
fi
ln -s "$ROOT/custom_components/unifi_ble" "$LINK"

# Minimal but complete dev config (only created if absent, so your edits persist).
CFG="$CONFIG_DIR/configuration.yaml"
if [ ! -f "$CFG" ]; then
  cat > "$CFG" <<'YAML'
# Dev config for testing the unifi_ble custom integration.
# default_config pulls in the frontend, config UI, onboarding, and the
# bluetooth stack that unifi_ble registers its remote scanners with.
default_config:

logger:
  default: info
  logs:
    custom_components.unifi_ble: debug
    habluetooth: info
    homeassistant.components.bluetooth: info
YAML
fi

cat <<EOF

Starting Home Assistant
  config dir : $CONFIG_DIR
  integration: $LINK
             -> $ROOT/custom_components/unifi_ble
  URL        : http://localhost:8123

First run: complete onboarding (create a user), then
Settings -> Devices & Services -> Add Integration -> "UniFi AP BLE Proxy".
Press Ctrl+C to stop.

EOF

exec "$HASS" -c "$CONFIG_DIR"
