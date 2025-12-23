from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any


def code_search(repo_root: Path, query: str, max_hits: int = 5) -> list[dict[str, Any]]:
    if not query.strip():
        return []

    cmd = [
        "rg",
        "-n",
        "--max-count",
        str(max_hits),
        "--no-heading",
        query,
        str(repo_root),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except FileNotFoundError:
        return []

    if proc.returncode not in (0, 1):
        return []

    hits: list[dict[str, Any]] = []
    for line in proc.stdout.splitlines():
        # Expected format: path:line:snippet
        parts = line.split(":", 2)
        if len(parts) < 3:
            continue
        file_path, line_no, snippet = parts
        normalized_file = file_path
        if Path(file_path).is_absolute():
            try:
                normalized_file = str(Path(file_path).resolve().relative_to(repo_root.resolve()))
            except ValueError:
                normalized_file = file_path

        hits.append(
            {
                "id": f"CODE-{len(hits) + 1}",
                "file": normalized_file,
                "line": int(line_no) if line_no.isdigit() else 0,
                "snippet": snippet.strip(),
                "title": f"{file_path}:{line_no}",
            }
        )
        if len(hits) >= max_hits:
            break

    return hits
