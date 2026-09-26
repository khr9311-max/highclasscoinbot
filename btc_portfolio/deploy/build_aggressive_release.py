"""Build a source-only, separately staged release; never installs or starts it."""
import hashlib
import json
from pathlib import Path
import zipfile


def build():
    root = Path(__file__).resolve().parents[2]
    files = []
    for package in ("btc_portfolio", "btc_spot", "btc_lab", "binance_coinm_v1"):
        for path in (root/package).rglob("*.py"):
            if not {"state", "__pycache__", ".venv", ".pytest_cache"}.intersection(path.relative_to(root).parts):
                files.append(path)
    files += [root/"btc_portfolio/config.aggressive.example.json",
              root/"btc_portfolio/AGGRESSIVE.md", root/"btc_spot/deploy/requirements.txt"]
    manifest = {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(files)}
    release = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()[:16]
    output = root/"btc_portfolio/deploy"/f"aggressive-{release}.zip"
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for relative in manifest:
            archive.write(root/relative, relative)
        archive.writestr("release-manifest.json", json.dumps(manifest, indent=2))
    result = {"archive": str(output), "release": release, "files": len(manifest),
              "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
              "note": "Stage in a separate directory. Do not extract over the running checkout."}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


if __name__ == "__main__":
    build()
