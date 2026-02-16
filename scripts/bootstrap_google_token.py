from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.calendar import CALENDAR_SCOPES
from tools.email import GMAIL_SCOPES
from tools.google_auth import get_google_credentials


PACKAGE_ROOT = REPO_ROOT
ENV_FILE = PACKAGE_ROOT / ".env"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Bootstrap Google OAuth token for Calendar + Gmail.")
    parser.add_argument(
        "--env-file",
        default=str(ENV_FILE),
        help="Path to .env file.",
    )
    return parser.parse_args()


def _load_env(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        parsed = os.path.expandvars(value.strip())
        if (parsed.startswith('"') and parsed.endswith('"')) or (parsed.startswith("'") and parsed.endswith("'")):
            parsed = parsed[1:-1]
        os.environ[key] = parsed


def main() -> int:
    args = parse_args()
    _load_env(Path(args.env_file))
    client_secret_file = os.getenv("GOOGLE_CLIENT_SECRET_FILE", "").strip()
    token_file = os.getenv("GOOGLE_TOKEN_FILE", "").strip()
    if not client_secret_file:
        raise RuntimeError("GOOGLE_CLIENT_SECRET_FILE is required")
    if not token_file:
        raise RuntimeError("GOOGLE_TOKEN_FILE is required")

    scopes = sorted(set(list(CALENDAR_SCOPES) + list(GMAIL_SCOPES)))
    creds = get_google_credentials(
        scopes=scopes,
        client_secret_file=client_secret_file,
        token_file=token_file,
    )
    token_path = Path(token_file)
    if not token_path.exists():
        raise RuntimeError(f"token file was not created: {token_path}")

    print(f"google token ready: {token_path}")
    print(f"scopes: {', '.join(scopes)}")
    print(f"valid: {getattr(creds, 'valid', False)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
