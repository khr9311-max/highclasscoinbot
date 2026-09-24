"""Build a source-only overlay for the existing /opt/btc-spot checkout."""
import hashlib
import json
from pathlib import Path
import zipfile


def build():
    root = Path(__file__).resolve().parents[2]
    package = root/"btc_portfolio"
    output = package/"deploy/btc-portfolio-source.zip"
    paths = sorted(path for path in package.rglob("*") if path.is_file()
                   and not {"state", "__pycache__", ".pytest_cache"}.intersection(path.relative_to(package).parts)
                   and path.suffix in {".py", ".json", ".md", ".sh", ".service"}
                   and path.name != "bundle-manifest.json")
    manifest = {str(path.relative_to(root)).replace("\\", "/"): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in paths:
            archive.write(path, path.relative_to(root).as_posix())
        archive.writestr("btc_portfolio/deploy/bundle-manifest.json", json.dumps(manifest, indent=2))
    print(json.dumps({"bundle": str(output), "source_files": len(paths),
                      "sha256": hashlib.sha256(output.read_bytes()).hexdigest()}))


if __name__ == "__main__":
    build()
