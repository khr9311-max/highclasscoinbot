#!/bin/sh
# Stage only. Starting the trading service is a separate operator action.
set -eu
test "$(id -u)" = 0
test -x /opt/btc-spot/.venv/bin/python
test -f /etc/btc-spot/binance.env
install -d -o btcspot -g btcspot -m 0700 /var/lib/btc-portfolio/live
install -d -o btcspot -g btcspot -m 0700 /opt/btc-spot/btc_portfolio/state
install -d -o btcspot -g btcspot -m 0700 /opt/btc-spot/btc_spot/state
install -d -o btcspot -g btcspot -m 0700 /opt/btc-spot/binance_coinm_v1/state
if ! test -f /etc/btc-spot/portfolio.json; then
    install -o root -g btcspot -m 0640 /opt/btc-spot/btc_portfolio/config.example.json /etc/btc-spot/portfolio.json
fi
install -o root -g root -m 0644 /opt/btc-spot/btc_portfolio/deploy/btc-portfolio.service /etc/systemd/system/btc-portfolio.service
install -o root -g root -m 0644 /opt/btc-spot/btc_portfolio/deploy/btc-portfolio-notify.service /etc/systemd/system/btc-portfolio-notify.service
systemctl daemon-reload
printf '%s\n' 'Portfolio services staged. Existing services were not stopped; live trading was not started.'
