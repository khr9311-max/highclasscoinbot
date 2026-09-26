"""Build the source overlay for the situation ledger on the existing /opt/btc-spot checkout.

Only new or research-only files are included; modules the live trading services
import (btc_lab.market_fit, btc_spot, btc_portfolio, binance_coinm_v1) are left alone.
"""
import hashlib
import json
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[2]
FILES = ("btc_lab/ledger.py", "btc_lab/regime_switch.py", "btc_lab/strategy_search.py", "btc_lab/retrain.py",
         "btc_lab/flow_data.py", "btc_lab/intraday_data.py",
         "btc_lab/state/ledger_model/ledger_model.json", "btc_lab/deploy/btc-ledger.service",
         "btc_lab/deploy/btc-ledger-retrain.service", "btc_lab/deploy/btc-ledger-retrain.timer",
         "btc_lab/deploy/requirements-ledger.txt", "btc_lab/deploy/requirements-retrain.txt",
         "btc_lab/deploy/install-ledger.sh", "btc_lab/deploy/install-retrain.sh")
OUTPUT = ROOT / "btc_lab/deploy/btc-ledger-bundle.zip"


def build(output=OUTPUT):
    manifest = {}
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in FILES:
            data = (ROOT / name).read_bytes()
            if name.endswith((".sh", ".service", ".timer", ".txt")):
                data = data.replace(b"\r\n", b"\n")          # Linux line endings on the server
            manifest[name] = hashlib.sha256(data).hexdigest()
            archive.writestr(name, data)
        archive.writestr("btc_lab/deploy/ledger-bundle-manifest.json", json.dumps(manifest, indent=1))
    model = json.loads((ROOT / "btc_lab/state/ledger_model/ledger_model.json").read_text())
    print(json.dumps({"bundle": str(output), "files": len(manifest), "bytes": output.stat().st_size,
                      "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
                      "model_train_end_ms": model["train_end_ms"]}))


if __name__ == "__main__":
    build()
