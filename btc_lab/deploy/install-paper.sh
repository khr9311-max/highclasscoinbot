#!/usr/bin/env bash
# Install files only. Does not start/enable the bot or contact Binance/AWS APIs.
set -euo pipefail
umask 027

fail() { printf '%s\n' "$1" >&2; exit 1; }
[[ $# -eq 5 ]] || fail 'Usage: install-paper.sh /opt/btc-lab/releases/RELEASE EQUITY_BTC TAKER_FEE MAINT_MARGIN_RATE CANDIDATE'
[[ $(id -u) -eq 0 ]] || fail 'Run this reviewed installer as root.'
release=$1
equity=$2
fee=$3
maintenance=$4
candidate=$5
[[ "$release" =~ ^/opt/btc-lab/releases/[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || fail 'Release path must be one named directory under /opt/btc-lab/releases.'
[[ -d "$release" && ! -L "$release" ]] || fail 'Release directory must exist and must not be a symbolic link.'
[[ $(readlink -f -- "$release") == "$release" ]] || fail 'Release parent path must not contain symbolic links.'
[[ $(stat -c %u -- "$release") -eq 0 ]] || fail 'Release directory must be owned by root.'
[[ "$equity" =~ ^[0-9]+([.][0-9]+)?$ && "$fee" =~ ^[0-9]+([.][0-9]+)?$ && "$maintenance" =~ ^[0-9]+([.][0-9]+)?$ ]] || fail 'Equity, fee and maintenance rate must be plain decimal numbers.'
case "$candidate" in momentum60_stop20|momentum40_stop20) ;; *) fail 'Unknown paper candidate.' ;; esac
/usr/bin/python3 - "$equity" "$fee" "$maintenance" <<'PY'
import math, sys
equity, fee, maintenance = map(float, sys.argv[1:])
if not math.isfinite(equity) or equity <= 0 or not math.isfinite(fee) or not 0 <= fee < .01 or not math.isfinite(maintenance) or not 0 < maintenance < .5:
    raise SystemExit('Invalid paper equity, fee or maintenance margin rate.')
PY
[[ -f /etc/os-release ]] || fail 'Ubuntu 24.04 is required.'
# This root-owned OS file is the only sourced file; no user configuration is executed.
. /etc/os-release
[[ "${ID:-}" == ubuntu && "${VERSION_ID:-}" == 24.04 ]] || fail 'This installer targets Ubuntu 24.04 only.'
[[ $(uname -m) == x86_64 ]] || fail 'This deployment targets x86_64.'
systemctl is-active --quiet btc-lab-paper.service && fail 'Stop the current service and complete the hand-off before installing.'
conf=/etc/btc-lab/paper.conf
[[ ! -e "$conf" ]] || fail 'paper.conf already exists. This first-install script does not overwrite an existing deployment.'
deploy="$release/btc_lab/deploy"
for required in btc_lab/__init__.py btc_lab/forward.py btc_lab/engine.py btc_lab/research.py btc_lab/growth_metrics.py binance_coinm_v1/__init__.py binance_coinm_v1/runtime/__init__.py binance_coinm_v1/runtime/instance_lock.py btc_lab/deploy/requirements-paper.txt btc_lab/deploy/btc-lab-paper.service btc_lab/deploy/journald-btc-lab.conf; do
    [[ -f "$release/$required" && ! -L "$release/$required" ]] || fail 'Release package is incomplete or contains linked deployment files.'
done
if [[ -e /opt/btc-lab/current && ! -L /opt/btc-lab/current ]]; then
    fail '/opt/btc-lab/current already exists as a non-link; refusing to replace it.'
fi
if [[ -L /opt/btc-lab/current ]]; then
    previous=$(readlink -f -- /opt/btc-lab/current)
    [[ "$previous" =~ ^/opt/btc-lab/releases/[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || fail 'Current release link points outside the intended release directory.'
fi
# The package is trusted, reviewed application code. Reject writable source before root pip.
if [[ -n $(find "$release" -xdev \( -type f -o -type d \) \( ! -user root -o -perm /022 \) -print -quit) ]]; then
    fail 'Release contents must be root-owned and not group/world writable.'
fi
if ! getent passwd btc-lab >/dev/null; then
    useradd --system --user-group --home-dir /var/lib/btc-lab-paper --no-create-home --shell /usr/sbin/nologin btc-lab
fi
[[ $(id -u btc-lab) -ne 0 ]] || fail 'Invalid service account.'
[[ $(id -gn btc-lab) == btc-lab ]] || fail 'The service account must use its dedicated btc-lab group.'
install -d -o root -g btc-lab -m 0750 /etc/btc-lab
install -d -o btc-lab -g btc-lab -m 0700 /var/lib/btc-lab-paper
(
    # Public application/dependency files must be readable by the service user;
    # private state and configuration keep their separately specified modes.
    umask 022
    /usr/bin/python3 -m venv "$release/.venv"
    "$release/.venv/bin/python" -m pip install --disable-pip-version-check --requirement "$deploy/requirements-paper.txt"
)
(
    cd -- "$release"
    runuser -u btc-lab -- env PYTHONDONTWRITEBYTECODE=1 "$release/.venv/bin/python" -c 'import numpy; import btc_lab.forward'
)
printf 'BTC_PAPER_EQUITY=%s\nBTC_PAPER_FEE=%s\nBTC_PAPER_MAINT_MARGIN_RATE=%s\nBTC_PAPER_CANDIDATE=%s\n' "$equity" "$fee" "$maintenance" "$candidate" > "$conf"
chown root:btc-lab "$conf"
chmod 0640 "$conf"
install -o root -g root -m 0644 "$deploy/btc-lab-paper.service" /etc/systemd/system/btc-lab-paper.service
install -o root -g root -m 0644 "$deploy/journald-btc-lab.conf" /etc/systemd/journald@btc-lab.conf
ln -sfn -- "$release" /opt/btc-lab/current
systemctl daemon-reload
systemd-analyze verify /etc/systemd/system/btc-lab-paper.service
printf '%s\n' 'Paper service installed but NOT enabled or started. Complete the state hand-off in AWS.md first.'
