#!/bin/sh
# Apply the execution-timing update to the live aggressive portfolio (run as root).
# Any failure after the trading service stops restores the previous files, settings
# and ledger binding, then starts the service again. Positions and the exchange-held
# protective stop are never touched; the stop keeps protecting while the bot is down.
set -u
BUNDLE=${1:?usage: deploy-timing.sh /tmp/btc-timing-bundle.zip}
BASE_AGGRESSIVE=bf24c655843f1f21
BASE_CONFIG=00a130b121af0808
PY=/opt/btc-spot/.venv/bin/python
CONFIG=/etc/btc-spot/portfolio.json
CREDS=/etc/btc-spot/binance.env
STATE=/var/lib/btc-portfolio/live
BK=/var/backups/btc-portfolio-timing/$(date -u +%Y%m%dT%H%M%SZ)
STOPPED=0
PLACED=0
OLD_ID=
NEW_ID=
cd /opt/btc-spot || exit 1

lfhash() { tr -d '\r' < "$1" | sha256sum | cut -c1-16; }

rollback() {
    echo "ROLLBACK: $1"
    if [ -n "$NEW_ID" ] && [ -n "$OLD_ID" ]; then
        python3 - "$STATE/ledger.sqlite3" "$OLD_ID" "$NEW_ID" <<'EOF'
import json, sqlite3, sys, time
path, old, new = sys.argv[1:4]
db = sqlite3.connect(path, isolation_level=None)
db.execute("BEGIN IMMEDIATE")
row = json.loads(db.execute("SELECT value FROM state WHERE key='binding'").fetchone()[0])
if row == {"identity": new, "mode": "live"}:
    db.execute("UPDATE state SET value=? WHERE key='binding'", (json.dumps({"identity": old, "mode": "live"}),))
    db.execute("INSERT INTO events(kind,payload,created_ms) VALUES(?,?,?)",
               ("code_rebind_rollback", json.dumps({"from": new, "to": old}), time.time_ns()//1_000_000))
    print("binding restored")
db.execute("COMMIT")
EOF
    fi
    if [ "$PLACED" = 1 ]; then
        cp -p "$BK/aggressive.py" "$BK/config.py" btc_portfolio/
        cp -p "$BK/ledger.py" btc_lab/
        cp -p "$BK/portfolio.json" "$CONFIG"
        rm -f btc_portfolio/timing.py btc_portfolio/code_update.py
        systemctl restart btc-ledger
    fi
    if [ "$STOPPED" = 1 ]; then
        systemctl start btc-portfolio
        sleep 20
        echo "btc-portfolio=$(systemctl is-active btc-portfolio) after rollback"
    fi
    exit 1
}

# 0. Reviewed base only; nothing has been touched yet.
[ "$(lfhash btc_portfolio/aggressive.py)" = "$BASE_AGGRESSIVE" ] || { echo "aggressive.py is not the reviewed base"; exit 1; }
[ "$(lfhash btc_portfolio/config.py)" = "$BASE_CONFIG" ] || { echo "config.py is not the reviewed base"; exit 1; }
install -d -m 0700 "$BK"
cp -p btc_portfolio/aggressive.py btc_portfolio/config.py btc_lab/ledger.py "$CONFIG" "$BK/" || exit 1
echo "backup $BK"

# 1. Stop trading (the exchange stop order stays in place).
systemctl stop btc-portfolio
STOPPED=1
systemctl is-active --quiet btc-portfolio && rollback "service did not stop"

# 2. Place hash-verified sources and enable timing in the settings.
python3 - "$BUNDLE" <<'EOF' || rollback "bundle extraction failed"
import hashlib, json, sys, zipfile
from pathlib import Path
with zipfile.ZipFile(sys.argv[1]) as z:
    manifest = json.loads(z.read("timing-bundle-manifest.json"))
    data = {n: z.read(n) for n in manifest}
for n, digest in manifest.items():
    if hashlib.sha256(data[n]).hexdigest() != digest:
        raise SystemExit("hash mismatch " + n)
for n, b in data.items():
    if n.startswith("btc_portfolio/deploy/"):
        continue
    target = Path("/opt/btc-spot") / n
    target.write_bytes(b)
    target.chmod(0o644)
print("placed", len(data) - 1, "sources")
EOF
PLACED=1
python3 - "$CONFIG" <<'EOF' || rollback "settings update failed"
import json, os, sys
p = sys.argv[1]
c = json.load(open(p))
c["timing_prediction_path"] = "/var/lib/btc-ledger/prediction.json"
c["timing_max_wait_seconds"] = 14400
st = os.stat(p)
with open(p + ".new", "w") as f:
    f.write(json.dumps(c, indent=2) + "\n")
os.chown(p + ".new", st.st_uid, st.st_gid)
os.chmod(p + ".new", st.st_mode & 0o777)
os.replace(p + ".new", p)
print("settings updated")
EOF

# 3. The ledger now also writes prediction.json (the bot trades normally without it).
systemctl restart btc-ledger
for i in $(seq 1 12); do [ -f /var/lib/btc-ledger/prediction.json ] && break; sleep 5; done
ls -l /var/lib/btc-ledger/prediction.json 2>&1

# 4. Rebind: the binding change must be the only blocker.
OUT=$(sudo -u btcspot "$PY" -m btc_portfolio.code_update prepare --config "$CONFIG" --credentials-file "$CREDS" --state-dir "$STATE")
echo "$OUT"
echo "$OUT" | python3 -c "import json,sys; d=json.load(sys.stdin); sys.exit(0 if d.get('ready') and d.get('status')!='ERROR' else 1)" \
    || rollback "rebind preflight not ready"
OLD=$(echo "$OUT" | python3 -c "import json,sys; print(json.load(sys.stdin)['old_identity'])")
OUT=$(sudo -u btcspot "$PY" -m btc_portfolio.code_update apply --config "$CONFIG" --credentials-file "$CREDS" \
      --state-dir "$STATE" --backup-path "$STATE/ledger-before-timing-$(date -u +%Y%m%dT%H%M%SZ).sqlite3" \
      --expected-old-identity "$OLD" --confirm I_UNDERSTAND_CODE_REBIND)
echo "$OUT"
echo "$OUT" | python3 -c "import json,sys; sys.exit(0 if json.load(sys.stdin).get('status')=='REBOUND' else 1)" \
    || rollback "rebind failed"
OLD_ID=$OLD
NEW_ID=$(echo "$OUT" | python3 -c "import json,sys; print(json.load(sys.stdin)['new_identity'])")

# 5. The runtime's own read-only preparation must pass.
OUT=$(sudo -u btcspot "$PY" -m btc_portfolio prepare --config "$CONFIG" --credentials-file "$CREDS" --state-dir "$STATE")
echo "$OUT" | python3 -c "import json,sys; d=json.load(sys.stdin); print('prepare ready', d.get('ready'), d.get('blockers')); sys.exit(0 if d.get('ready') else 1)" \
    || rollback "runtime prepare not ready"

# 6. Start trading and confirm a fresh heartbeat without a halt.
systemctl start btc-portfolio
STOPPED=0
sleep 60
systemctl is-active --quiet btc-portfolio || { STOPPED=1; rollback "service not active after start"; }
python3 - "$STATE" <<'EOF' || { systemctl stop btc-portfolio; STOPPED=1; rollback "no fresh healthy heartbeat"; }
import json, sqlite3, sys, time
d = sys.argv[1]
s = json.load(open(d + "/status.json"))
age = time.time() - s.get("updated_at_ms", 0)/1000
db = sqlite3.connect(f"file:{d}/ledger.sqlite3?mode=ro", uri=True)
halt = db.execute("SELECT value FROM state WHERE key='halt'").fetchone()
r = s.get("result", {})
print("heartbeat_age_s", round(age), "status", r.get("status"), "action", r.get("action"), "halt", halt,
      "timing_wait", r.get("coinm_timing_wait"))
sys.exit(0 if age < 90 and not (halt and json.loads(halt[0])) and r.get("status") in ("READY", "PENDING") else 1)
EOF
echo "DEPLOYED timing overlay; backup $BK"
