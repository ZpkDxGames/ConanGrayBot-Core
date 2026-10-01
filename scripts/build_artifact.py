"""Reproducible source artifact using an explicit public-source allowlist."""

import hashlib
import json
import subprocess
import zipfile
from pathlib import Path

paths = [
    p
    for root in ["backend", "scripts", "contracts", "tests", "release"]
    for p in Path(root).rglob("*")
    if p.is_file() and "__pycache__" not in p.parts and p.suffix in {".py", ".json"}
]
paths += [
    Path(name)
    for name in [
        "README.md",
        "SECURITY.md",
        "MIGRATION.md",
        "CHANGELOG.md",
        ".env.example",
        "pyproject.toml",
        "requirements.in",
        "requirements.txt",
        "requirements.lock",
        "requirements-dev.txt",
        "main.py",
        "discloud.config",
    ]
    if Path(name).is_file()
]
manifest = {
    "version": "2.0.0",
    "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
    "files": {
        str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(paths)
    },
}
Path("dist").mkdir(exist_ok=True)
archive_path = Path("dist/ConanGrayBot-Core-v2.0.0-source.zip")
with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
    for name, data in [(str(p), p.read_bytes()) for p in sorted(paths)] + [
        (
            "BUILD_MANIFEST.json",
            (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode(),
        )
    ]:
        info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
        info.external_attr = 0o100644 << 16
        info.compress_type = zipfile.ZIP_DEFLATED
        archive.writestr(info, data)
Path("dist/SHA256SUMS").write_text(
    hashlib.sha256(archive_path.read_bytes()).hexdigest()
    + "  "
    + archive_path.name
    + "\n"
)
print(f"Built {archive_path.name} with {len(paths)} source files")
