"""Keep the directive's overall and critical coverage gates explicit."""

import json
from pathlib import Path

coverage = json.loads(Path("coverage.json").read_text())
failed = []
if coverage["totals"]["percent_covered"] < 80:
    failed.append("Overall backend coverage must be >=80%")
for name in ["config", "models", "migrations", "security", "media_stream"]:
    row = coverage["files"].get(f"backend/{name}.py")
    if not row or row["summary"]["percent_covered"] < 90:
        failed.append(f"Critical {name} coverage must be >=90%")
if failed:
    raise SystemExit("\n".join(failed))
print("Coverage gates pass")
