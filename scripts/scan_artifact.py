"""Validate public artifact paths, confidential-content signatures and file checksums."""

import hashlib
import json
import re
import zipfile
from pathlib import Path

path = Path("dist/ConanGrayBot-Core-v2.0.0-source.zip")
with zipfile.ZipFile(path) as archive:
    manifest = json.loads(archive.read("BUILD_MANIFEST.json"))
    assert set(archive.namelist()) == set(manifest["files"]) | {"BUILD_MANIFEST.json"}
    for name in archive.namelist():
        parts = Path(name).parts
        assert not Path(name).is_absolute() and ".." not in parts
        assert not any(
            part in {"secrets", "private", "node_modules", ".git"} for part in parts
        )
        assert not Path(name).name.startswith(".env") or name == ".env.example"
        content = archive.read(name)
        assert not re.search(
            rb"-----BEGIN (?:RSA |EC )?PRIVATE KEY-----|AIza[A-Za-z0-9_-]{35}|gh[pousr]_[A-Za-z0-9]{30,}",
            content,
        ), "Potential confidential artifact content"
        if name in manifest["files"]:
            assert hashlib.sha256(content).hexdigest() == manifest["files"][name]
print("Artifact boundaries and manifest checksums pass")
