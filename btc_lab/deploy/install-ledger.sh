#!/bin/sh
# Stage the situation ledger from a bundle built by build_ledger_bundle.py.
# Public market data only. It uses its own venv, does not stop, restart or
# reconfigure trading services, and does not start the ledger service itself.
set -eu
test "$(id -u)" = 0
BUNDLE=${1:?usage: install-ledger.sh /tmp/btc-ledger-bundle.zip}
VENV=/opt/btc-ledger/.venv
PY=$VENV/bin/python
test -d /opt/btc-spot/btc_lab
test -f /etc/btc-spot/binance.env
cd /opt/btc-spot                                   # python -m resolves btc_lab from here

# Extract only the files listed in the bundle manifest and verify their hashes.
python3 - "$BUNDLE" <<'EOF'
import hashlib, json, sys, zipfile
from pathlib import Path
root = Path("/opt/btc-spot")
with zipfile.ZipFile(sys.argv[1]) as z:
    manifest = json.loads(z.read("btc_lab/deploy/ledger-bundle-manifest.json"))
    for name, digest in manifest.items():
        data = z.read(name)
        if hashlib.sha256(data).hexdigest() != digest:
            raise SystemExit("hash mismatch: " + name)
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        target.chmod(0o644)
print("extracted", len(manifest), "files")
EOF

if ! test -x "$PY"; then
    install -d -o root -g root -m 0755 /opt/btc-ledger
    python3.12 -m venv "$VENV"
fi
"$PY" -m pip install --quiet --disable-pip-version-check -r /opt/btc-spot/btc_lab/deploy/requirements-ledger.txt

# Telegram settings only; the ledger never reads Binance keys.
umask 027
grep -E '^(TELEGRAM_BOT_TOKEN|TELEGRAM_CHAT_ID|USD_KRW_RATE)=' /etc/btc-spot/binance.env > /etc/btc-spot/ledger-telegram.env
chown root:btcspot /etc/btc-spot/ledger-telegram.env
chmod 0640 /etc/btc-spot/ledger-telegram.env
umask 022

# One snapshot as the service user, printed only (no Telegram, throwaway state).
# The unit is registered only after this succeeds.
SMOKE=$(mktemp -d)
chown btcspot:btcspot "$SMOKE"
sudo -u btcspot "$PY" -m btc_lab.ledger once --state-dir "$SMOKE"
rm -rf "$SMOKE"

install -o root -g root -m 0644 /opt/btc-spot/btc_lab/deploy/btc-ledger.service /etc/systemd/system/btc-ledger.service
systemctl daemon-reload
printf '%s\n' 'Ledger staged. Trading services were not touched. Start it with: systemctl enable --now btc-ledger'
