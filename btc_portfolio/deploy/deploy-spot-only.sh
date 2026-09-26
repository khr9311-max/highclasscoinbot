#!/bin/sh
# Switch the live aggressive portfolio to spot-only (run as root). The bot keeps the
# BTC/alt rotation and stops reading or trading COIN-M, which the user now trades by hand.
# Requires the ledger to own nothing on COIN-M (the bot already closed its position and
# cancelled its stop). No transfer: the COIN-M BTC stays where it is.
# Any failure after the trading service stops restores the previous files, settings and
# ledger binding, then starts the services again.
set -u
BUNDLE=${1:?usage: deploy-spot-only.sh /tmp/btc-spot-only-bundle.zip}
PY=/opt/btc-spot/.venv/bin/python
CONFIG=/etc/btc-spot/portfolio.json
CREDS=/etc/btc-spot/binance.env
STATE=/var/lib/btc-portfolio/live
BK=/var/backups/btc-portfolio-spotonly/code-$(date -u +%Y%m%dT%H%M%SZ)
FILES="btc_portfolio/config.py btc_portfolio/engine.py btc_portfolio/venues.py btc_portfolio/aggressive.py
btc_portfolio/runtime.py btc_portfolio/code_update.py btc_portfolio/notify.py btc_lab/ledger.py"
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
        for f in $FILES; do cp -p "$BK/$f" "$f"; done
        cp -p "$BK/portfolio.json" "$CONFIG"
        systemctl restart btc-ledger btc-portfolio-notify
    fi
    if [ "$STOPPED" = 1 ]; then
        systemctl start btc-portfolio
        sleep 20
        echo "btc-portfolio=$(systemctl is-active btc-portfolio) after rollback"
    fi
    exit 1
}

# 0. Reviewed base, and a ledger that owns nothing on COIN-M; nothing touched yet.
for pair in btc_portfolio/config.py:62519c8ac1e5fd31 btc_portfolio/engine.py:6d992bf345fbf44d \
            btc_portfolio/venues.py:cdd07cb798588f02 btc_portfolio/aggressive.py:f57ccec6b1297cfa \
            btc_portfolio/runtime.py:288175ca139f105f btc_portfolio/code_update.py:a9f6f7c69bfb2272 \
            btc_portfolio/notify.py:b7e24c59b605405e btc_lab/ledger.py:f4b58ffc414c1294; do
    f=${pair%%:*}
    [ "$(lfhash "$f")" = "${pair##*:}" ] || { echo "$f is not the reviewed base"; exit 1; }
done
python3 - "$STATE" "$CONFIG" <<'EOF' || { echo "precondition failed; nothing changed"; exit 1; }
import json, sys, time
s = json.load(open(sys.argv[1] + "/status.json"))
c = json.load(open(sys.argv[2]))
print("before: status", s["result"].get("status"), "coin_qty", s["coin_qty"], "stop", s.get("stop"),
      "halt", s["halt"], "pending", s["pending"], "spot_fraction", c.get("spot_fraction"),
      "timing", c.get("timing_prediction_path"))
ok = (float(s["coin_qty"] or 0) == 0 and not s.get("stop") and not s["halt"] and s["pending"] == 0
      and c.get("spot_fraction") == "1" and not c.get("timing_prediction_path"))
sys.exit(0 if ok else 1)
EOF
install -d -m 0700 "$BK"
for f in $FILES; do install -D -p -m 0644 "$f" "$BK/$f" || exit 1; done
cp -p "$CONFIG" "$BK/" || exit 1
echo "backup $BK"

# 1. Stop trading.
systemctl stop btc-portfolio
STOPPED=1
systemctl is-active --quiet btc-portfolio && rollback "service did not stop"

# 2. Hash-verified sources, then the setting.
python3 - "$BUNDLE" <<'EOF' || rollback "bundle extraction failed"
import hashlib, json, sys, zipfile
from pathlib import Path
with zipfile.ZipFile(sys.argv[1]) as z:
    manifest = json.loads(z.read("spot-only-bundle-manifest.json"))
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
c["coinm_managed"] = False
st = os.stat(p)
with open(p + ".new", "w") as f:
    f.write(json.dumps(c, indent=2) + "\n")
os.chown(p + ".new", st.st_uid, st.st_gid)
os.chmod(p + ".new", st.st_mode & 0o777)
os.replace(p + ".new", p)
print("settings updated: coinm_managed false")
EOF

# 3. Rebind: the binding change must be the only blocker and the ledger must own nothing on COIN-M.
OUT=$(sudo -u btcspot "$PY" -m btc_portfolio.code_update prepare --config "$CONFIG" --credentials-file "$CREDS" --state-dir "$STATE")
echo "$OUT"
echo "$OUT" | python3 -c "import json,sys; d=json.load(sys.stdin); sys.exit(0 if d.get('ready') and d.get('status')!='ERROR' else 1)" \
    || rollback "rebind preflight not ready"
OLD=$(echo "$OUT" | python3 -c "import json,sys; print(json.load(sys.stdin)['old_identity'])")
OUT=$(sudo -u btcspot "$PY" -m btc_portfolio.code_update apply --config "$CONFIG" --credentials-file "$CREDS" \
      --state-dir "$STATE" --backup-path "$STATE/ledger-before-spot-only-$(date -u +%Y%m%dT%H%M%SZ).sqlite3" \
      --expected-old-identity "$OLD" --confirm I_UNDERSTAND_CODE_REBIND)
echo "$OUT"
echo "$OUT" | python3 -c "import json,sys; sys.exit(0 if json.load(sys.stdin).get('status')=='REBOUND' else 1)" \
    || rollback "rebind failed"
OLD_ID=$OLD
NEW_ID=$(echo "$OUT" | python3 -c "import json,sys; print(json.load(sys.stdin)['new_identity'])")

OUT=$(sudo -u btcspot "$PY" -m btc_portfolio prepare --config "$CONFIG" --credentials-file "$CREDS" --state-dir "$STATE")
echo "$OUT" | python3 -c "import json,sys; d=json.load(sys.stdin); print('prepare ready', d.get('ready'), d.get('blockers'), 'coinm', d.get('coinm')); sys.exit(0 if d.get('ready') and d.get('coinm') == 'manual' else 1)" \
    || rollback "runtime prepare not ready"

# 4. Start and confirm a fresh healthy spot-only heartbeat; reports pick up the new wording.
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
base = db.execute("SELECT value FROM state WHERE key='spot_only_initial_equity'").fetchone()
r = s.get("result", {})
print("heartbeat_age_s", round(age), "status", r.get("status"), "action", r.get("action"), "halt", halt,
      "coinm_managed", s.get("coinm_managed"), "equity_btc", r.get("equity_btc"), "spot_only_baseline", base)
sys.exit(0 if age < 90 and not (halt and json.loads(halt[0])) and r.get("status") in ("READY", "PENDING")
         and s.get("coinm_managed") is False else 1)
EOF
systemctl restart btc-ledger btc-portfolio-notify
sleep 5
for s in btc-portfolio btc-portfolio-notify btc-ledger; do printf '%s=%s ' $s "$(systemctl is-active $s)"; done; echo
echo "DEPLOYED spot-only; backup $BK"
