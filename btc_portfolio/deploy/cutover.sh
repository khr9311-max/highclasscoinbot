#!/bin/sh
# One-shot migration of an already staged BTC portfolio on the AWS host.
# This command can move real BTC and start live trading. Run only deliberately.
set -eu

test "$(id -u)" -eq 0
cd /opt/btc-spot
exec 9>/run/lock/btc-portfolio-cutover.lock
flock -n 9

PY=/opt/btc-spot/.venv/bin/python
CONFIG=/etc/btc-spot/portfolio.json
CREDS=/etc/btc-spot/binance.env
OLD=/var/lib/btc-spot/live/ledger.sqlite3
NEW=/var/lib/btc-portfolio/live

test -x "$PY"
test -f "$CONFIG"
test -f "$CREDS"
test -f "$OLD"
test ! -e "$NEW/transfer-intent.json"
test ! -e "$NEW/ledger.sqlite3"
systemctl is-active --quiet spotlive
! systemctl is-active --quiet btc-portfolio

stamp=$(date -u +%Y%m%dT%H%M%SZ)
backup=/var/backups/btc-portfolio-cutover/$stamp
test ! -e "$backup"
install -d -m 0700 "$backup"
phase=before_transfer
recover() {
    code=$?
    trap - 0
    if [ "$code" -ne 0 ]; then
        echo "Cutover stopped in phase=$phase; backup=$backup" >&2
        if [ "$phase" = before_transfer ]; then
            systemctl enable --now spotlive spotnotify || true
            echo 'No transfer was attempted; old spot services were restored.' >&2
        else
            echo 'Transfer may have occurred. Do not restart the old strategy or retry the transfer.' >&2
        fi
    fi
    exit "$code"
}
trap recover 0

systemctl disable --now spotlive spotnotify
"$PY" - "$OLD" "$backup/ledger.sqlite3" <<'PYCODE'
import sqlite3, sys
source = sqlite3.connect('file:' + sys.argv[1] + '?mode=ro', uri=True)
target = sqlite3.connect(sys.argv[2])
try:
    if source.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
        raise RuntimeError('Old ledger integrity failed')
    if source.execute("SELECT COUNT(*) FROM decisions WHERE phase='PENDING'").fetchone()[0]:
        raise RuntimeError('Old ledger has a pending order')
    source.backup(target)
    if target.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
        raise RuntimeError('Backup integrity failed')
finally:
    target.close()
    source.close()
PYCODE
chmod 0600 "$backup/ledger.sqlite3"
cp /opt/btc-spot/btc_spot/state/live-registry.json "$backup/live-registry.json"
chmod 0600 "$backup/live-registry.json"

attempt=0
while :; do
    if sudo -u btcspot "$PY" -m btc_portfolio.transfer prepare \
        --config "$CONFIG" --credentials-file "$CREDS" \
        --old-spot-ledger "$OLD" --state-dir "$NEW" > "$backup/transfer-prepare.json"; then
        break
    fi
    "$PY" - "$backup/transfer-prepare.json" <<'PYCODE'
import json, sys
result = json.load(open(sys.argv[1]))
blockers = result.get('blockers', [])
if not blockers or set(blockers) != {'legacy_spot_has_recent_heartbeat'}:
    raise RuntimeError('Transfer preflight has a non-heartbeat blocker: ' + ','.join(blockers))
PYCODE
    attempt=$((attempt + 1))
    test "$attempt" -le 15
    sleep 10
done
chmod 0600 "$backup/transfer-prepare.json"
"$PY" - "$backup/transfer-prepare.json" <<'PYCODE'
import json, sys
from decimal import Decimal
result = json.load(open(sys.argv[1]))
if (not result.get('ready') or result.get('type') != 'MAIN_CMFUTURE' or
        result.get('asset') != 'BTC' or Decimal(result.get('amount', '0')) != Decimal('0.0018')):
    raise RuntimeError('Unexpected transfer plan; no transfer attempted')
print('Verified transfer plan: 0.0018 BTC Spot -> COIN-M')
PYCODE

# Once the request may have reached Binance, never restore the old bot automatically.
phase=transfer_attempted
sudo -u btcspot "$PY" -m btc_portfolio.transfer transfer \
    --config "$CONFIG" --credentials-file "$CREDS" \
    --old-spot-ledger "$OLD" --state-dir "$NEW" \
    --confirm I_UNDERSTAND_BTC_COINM_TRANSFER > "$backup/transfer-result.json"
chmod 0600 "$backup/transfer-result.json"
"$PY" - "$backup/transfer-result.json" <<'PYCODE'
import json, sys
from decimal import Decimal
result = json.load(open(sys.argv[1]))
if (result.get('status') != 'ACKNOWLEDGED' or
        Decimal(result.get('amount_btc', '0')) != Decimal('0.0018')):
    raise RuntimeError('Transfer response requires reconciliation')
print('Transfer acknowledged by Binance; verifying both wallets')
PYCODE

sudo -u btcspot "$PY" -m btc_portfolio prepare \
    --config "$CONFIG" --credentials-file "$CREDS" --state-dir "$NEW" \
    > "$backup/portfolio-prepare.json"
chmod 0600 "$backup/portfolio-prepare.json"
"$PY" - "$backup/portfolio-prepare.json" <<'PYCODE'
import json, sys
from decimal import Decimal
result = json.load(open(sys.argv[1]))
if (not result.get('ready') or result.get('orders_submitted') != 0 or
        Decimal(result.get('coinm_btc_available', '0')) != Decimal('0.0018')):
    raise RuntimeError('Portfolio preflight failed after transfer')
print('Portfolio funded and ready; starting live services')
PYCODE

systemctl enable --now btc-portfolio btc-portfolio-notify
attempt=0
while :; do
    systemctl is-active --quiet btc-portfolio
    systemctl is-active --quiet btc-portfolio-notify
    if test -f "$NEW/status.json" && "$PY" - "$NEW/status.json" <<'PYCODE'
import json, sys, time
result = json.load(open(sys.argv[1]))
assert (result.get('mode') == 'live' and result.get('orders_enabled') is True and
        time.time()*1000-result.get('updated_at_ms', 0) < 90000 and
        result.get('result', {}).get('status') == 'READY' and
        result.get('pending') == 0 and not result.get('halt'))
PYCODE
    then
        break
    fi
    attempt=$((attempt + 1))
    test "$attempt" -le 12
    sleep 5
done
echo 'Live portfolio heartbeat: READY; pending: 0'
phase=completed
echo "Live portfolio cutover complete; backup=$backup"
