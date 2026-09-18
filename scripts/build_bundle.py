"""Build the portable Claude Desktop extension and a local stdio config alternative.

Run with: uv run python scripts/build_bundle.py
"""

import json
import shutil
import tomllib
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

ROOT = Path(__file__).resolve().parents[1]
BUNDLE_FILES = (
    "manifest.json",
    "pyproject.toml",
    "uv.lock",
    ".python-version",
    "server.py",
    "govfacts/__init__.py",
    "govfacts/server.py",
    "govfacts/data.py",
    "icon.png",
)


def main() -> None:
    manifest = json.loads((ROOT / "manifest.json").read_text())
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    if manifest["version"] != project["project"]["version"]:
        raise SystemExit("Bundle and package versions must match.")
    uv = shutil.which("uv")
    if not uv:
        raise SystemExit("Install uv before building the bundle.")
    destination = ROOT / "dist"
    destination.mkdir(exist_ok=True)
    bundle = destination / "GovFacts.mcpb"
    with ZipFile(bundle, "w", compression=ZIP_DEFLATED, compresslevel=9) as archive:
        for name in BUNDLE_FILES:
            entry = ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            entry.compress_type = ZIP_DEFLATED
            entry.external_attr = 0o100644 << 16
            archive.writestr(entry, (ROOT / name).read_bytes())
    config = {
        "mcpServers": {
            "govfacts": {
                "command": str(Path(uv).resolve()),
                "args": ["run", "--frozen", "--no-dev", "--directory", str(ROOT), "server.py"],
                "env": {"GOVFACTS_CONTACT": "REPLACE_WITH_YOUR_EMAIL@example.org"},
            }
        }
    }
    (destination / "claude_desktop_config.json").write_text(json.dumps(config, indent=2) + "\n")
    print(f"Built {bundle} ({bundle.stat().st_size:,} bytes)")
    print(f"Alternative local launch config: {destination / 'claude_desktop_config.json'}")


if __name__ == "__main__":
    main()
