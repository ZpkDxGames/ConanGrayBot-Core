"""A stable release requires quality gates and explicit production certification evidence."""

import json
import re
from pathlib import Path

record = json.loads(Path("release/certification.json").read_text())
required = [
    "ciPassed",
    "integrationPassed",
    "credentialRevocationVerified",
    "historyRemediationVerified",
    "discordCertified",
    "firebaseCertified",
    "driveCertified",
    "aiCertified",
    "weatherCertified",
    "pageCertified",
]
missing = [key for key in required if record.get(key) is not True]
for key in ["coreCommit", "pageCommit"]:
    if not re.fullmatch(r"[a-f0-9]{40}", record.get(key, "")):
        missing.append(key)
if not record.get("runtimeEvidence"):
    missing.append("runtimeEvidence")
if missing:
    raise SystemExit("Stable release blocked: " + ", ".join(missing))
print("Stable certification gates pass")
