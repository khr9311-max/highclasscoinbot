"""Bundle the spot-only update: the bot keeps BTC/alt rotation and leaves COIN-M to the user.

Contains only the changed sources and the deploy script, LF-normalized. The deploy
script checks that the server's copies are the reviewed base before touching anything.
"""
import hashlib
import json
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[2]
FILES = ("btc_portfolio/config.py", "btc_portfolio/engine.py", "btc_portfolio/venues.py", "btc_portfolio/aggressive.py",
         "btc_portfolio/runtime.py", "btc_portfolio/code_update.py", "btc_portfolio/notify.py", "btc_lab/ledger.py",
         "btc_portfolio/deploy/deploy-spot-only.sh")
OUTPUT = ROOT / "btc_portfolio/deploy/btc-spot-only-bundle.zip"


def build(output=OUTPUT):
    manifest = {}
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in FILES:
            data = (ROOT / name).read_bytes().replace(b"\r\n", b"\n")
            manifest[name] = hashlib.sha256(data).hexdigest()
            archive.writestr(name, data)
        archive.writestr("spot-only-bundle-manifest.json", json.dumps(manifest, indent=1))
    print(json.dumps({"bundle": str(output), "files": len(manifest), "bytes": output.stat().st_size,
                      "sha256": hashlib.sha256(output.read_bytes()).hexdigest()}))


if __name__ == "__main__":
    build()
