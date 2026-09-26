#!/bin/sh
# Add the monthly retrain to the installed situation ledger (run as root).
# Public data only. Trading services, their venv and system packages are untouched:
# LightGBM's libgomp is unpacked privately for the retrain service.
set -eu
test "$(id -u)" = 0
BUNDLE=${1:?usage: install-retrain.sh /tmp/btc-ledger-bundle.zip}
VENV=/opt/btc-ledger/.venv
PY=$VENV/bin/python
GOMP=/opt/btc-ledger/gomp
MODEL_DIR=/var/lib/btc-ledger/model
test -x "$PY"
test -d /var/lib/btc-ledger
cd /opt/btc-spot

# 1. Hash-verified sources and deploy files.
python3 - "$BUNDLE" <<'EOF'
import hashlib, json, sys, zipfile
from pathlib import Path
root = Path("/opt/btc-spot")
with zipfile.ZipFile(sys.argv[1]) as z:
    manifest = json.loads(z.read("btc_lab/deploy/ledger-bundle-manifest.json"))
    data = {n: z.read(n) for n in manifest}
for n, digest in manifest.items():
    if hashlib.sha256(data[n]).hexdigest() != digest:
        raise SystemExit("hash mismatch: " + n)
for n, b in data.items():
    target = root / n
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b)
    target.chmod(0o644)
print("extracted", len(data), "files")
EOF

# 2. Training packages in the ledger venv only.
"$PY" -m pip install --quiet --disable-pip-version-check -r /opt/btc-spot/btc_lab/deploy/requirements-retrain.txt

# 3. libgomp: apt's candidate .deb over HTTPS, checked against apt's signed index, unpacked privately.
if ! test -f "$GOMP/usr/lib/x86_64-linux-gnu/libgomp.so.1"; then
    VER=$(apt-cache policy libgomp1 | awk '/Candidate:/{print $2}')
    FILE=$(apt-cache show "libgomp1=$VER" | awk '/^Filename:/{print $2; exit}')
    SHA=$(apt-cache show "libgomp1=$VER" | awk '/^SHA256:/{print $2; exit}')
    test -n "$FILE"
    test -n "$SHA"
    TMP=$(mktemp -d)
    python3 -c "import sys, urllib.request; open(sys.argv[2], 'wb').write(urllib.request.urlopen(sys.argv[1], timeout=60).read())" \
        "https://ap-northeast-2.ec2.archive.ubuntu.com/ubuntu/$FILE" "$TMP/libgomp1.deb"
    echo "$SHA  $TMP/libgomp1.deb" | sha256sum -c -
    install -d -m 0755 "$GOMP"
    dpkg-deb -x "$TMP/libgomp1.deb" "$GOMP"
    rm -rf "$TMP"
fi
LD_LIBRARY_PATH=$GOMP/usr/lib/x86_64-linux-gnu OMP_NUM_THREADS=1 "$PY" -c "import lightgbm; print('lightgbm', lightgbm.__version__)"

# 4. The live model moves to the writable state directory (an existing one is kept).
install -d -o btcspot -g btcspot -m 0700 "$MODEL_DIR" /var/lib/btc-ledger/retrain
if ! test -f "$MODEL_DIR/ledger_model.json"; then
    install -o btcspot -g btcspot -m 0600 /opt/btc-spot/btc_lab/state/ledger_model/ledger_model.json "$MODEL_DIR/ledger_model.json"
fi

# 5. Units: the ledger reads the moved model and reloads it when the retrain replaces it.
install -o root -g root -m 0644 btc_lab/deploy/btc-ledger.service /etc/systemd/system/btc-ledger.service
install -o root -g root -m 0644 btc_lab/deploy/btc-ledger-retrain.service /etc/systemd/system/btc-ledger-retrain.service
install -o root -g root -m 0644 btc_lab/deploy/btc-ledger-retrain.timer /etc/systemd/system/btc-ledger-retrain.timer
systemctl daemon-reload
systemctl restart btc-ledger
sleep 45
printf 'btc-ledger=%s ' "$(systemctl is-active btc-ledger)"
cat /var/lib/btc-ledger/status.json
echo
printf '%s\n' 'Retrain staged; the timer is not enabled yet.'
