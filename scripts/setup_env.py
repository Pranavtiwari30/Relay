"""Create .env from .env.example with freshly generated local secrets.

Idempotent: an existing .env is left untouched. Values are written to the file
only — never printed — so they do not end up in terminal scrollback or logs.
"""

from __future__ import annotations

import os
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MARKER = "__GENERATE__"


def main() -> int:
    example, target = ROOT / ".env.example", ROOT / ".env"
    if target.exists():
        print(".env already exists; leaving it unchanged.")
        return 0
    lines = []
    generated = 0
    for line in example.read_text().splitlines():
        if not line.startswith("#") and line.endswith(f"={MARKER}"):
            line = f"{line[: -len(MARKER)]}{secrets.token_urlsafe(24)}"
            generated += 1
        lines.append(line)
    # Owner-only from the moment it exists.
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write("\n".join(lines) + "\n")
    print(f"Created .env with {generated} generated secrets (file mode 600).")
    print("Sign-in passwords for the seeded accounts are the RELAY_SEED_*_PASSWORD values in .env.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
