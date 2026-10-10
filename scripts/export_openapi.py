"""Write the API's OpenAPI document to docs/design/openapi.json (no server or database needed)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

from relay_api.deps import Container
from relay_api.main import create_app
from relay_core.config import Settings

OUT = Path(__file__).resolve().parents[1] / "docs" / "design" / "openapi.json"


def main() -> None:
    # Only route metadata is read, so the collaborators can be placeholders.
    container = Container(
        settings=Settings(),
        session_factory=cast(Any, None), registry=cast(Any, None), secrets=cast(Any, None),
        publisher=cast(Any, None), dispatcher=cast(Any, None), ingestion=cast(Any, None),
        investigations=cast(Any, None), clock=cast(Any, None),
    )  # fmt: skip
    spec = create_app(container).openapi()
    OUT.write_text(json.dumps(spec, indent=2, sort_keys=True) + "\n")
    print(f"wrote {OUT.relative_to(OUT.parents[2])} ({len(spec['paths'])} paths)")


if __name__ == "__main__":
    main()
