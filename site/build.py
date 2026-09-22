#!/usr/bin/env python3
"""
Embeds data/hospitals.json into site/template.html -> site/index.html,
and also copies the result to docs/index.html.

Why docs/: GitHub Pages can serve straight from a "/docs" folder on the
main branch with a single one-time toggle in repo Settings -> Pages
("Deploy from a branch" -> main -> /docs) — no separate deploy workflow
needed, which keeps the whole setup free and simple.
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
data = json.loads((ROOT / "data" / "hospitals.json").read_text(encoding="utf-8"))
template = (ROOT / "site" / "template.html").read_text(encoding="utf-8")

out = template.replace("__HOSPITAL_DATA__", json.dumps(data, ensure_ascii=False))

(ROOT / "site" / "index.html").write_text(out, encoding="utf-8")

docs_dir = ROOT / "docs"
docs_dir.mkdir(exist_ok=True)
(docs_dir / "index.html").write_text(out, encoding="utf-8")

print("Wrote site/index.html and docs/index.html —", len(data["hospitals"]), "hospitals, last_updated", data["last_updated"])
