"""Bundle the execution-timing update for the live aggressive portfolio.

Contains only the changed/new sources and the deploy script. The deploy script
checks that the server's aggressive.py and config.py are the reviewed base
(LF-normalized SHA-256 prefixes below) before it touches anything.
"""
import hashlib
import json
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[2]
FILES = ("btc_portfolio/timing.py", "btc_portfolio/aggressive.py", "btc_portfolio/config.py",
         "btc_portfolio/code_update.py", "btc_lab/ledger.py", "btc_portfolio/deploy/deploy-timing.sh")
OUTPUT = ROOT / "btc_portfolio/deploy/btc-timing-bundle.zip"


def build(output=OUTPUT):
    manifest = {}
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in FILES:
            data = (ROOT / name).read_bytes()
            if name.endswith(".sh"):
                data = data.replace(b"\r\n", b"\n")
            manifest[name] = hashlib.sha256(data).hexdigest()
            archive.writestr(name, data)
        archive.writestr("timing-bundle-manifest.json", json.dumps(manifest, indent=1))
    print(json.dumps({"bundle": str(output), "files": len(manifest), "bytes": output.stat().st_size,
                      "sha256": hashlib.sha256(output.read_bytes()).hexdigest()}))


if __name__ == "__main__":
    build()
