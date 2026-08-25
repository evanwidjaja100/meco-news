from __future__ import annotations

from pathlib import Path


root = Path(__file__).resolve().parents[1]

dockerignore = (root / ".dockerignore").read_text(encoding="utf-8")
for required in (".env", "data/", "logs/", ".git", "__pycache__/"):
    if required not in dockerignore:
        raise SystemExit(f".dockerignore is missing required rule: {required}")

dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
if "COPY ." in dockerfile:
    raise SystemExit("Dockerfile uses a broad COPY")
if "COPY config ./config" not in dockerfile or "COPY meco_news ./meco_news" not in dockerfile:
    raise SystemExit("Dockerfile does not copy required source/config explicitly")

print("build-context sentinel passed")
