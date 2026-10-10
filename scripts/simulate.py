"""Send simulated source events to a running Relay API over real HTTP.

    uv run --env-file .env python scripts/simulate.py --connection <uuid> valid_order
    uv run --env-file .env python scripts/simulate.py --connection <uuid> valid_order --count 5

Acts exactly like an external source system: no session, only the HMAC
signature. The signing secret is read from RELAY_SECRET_DEMO_WEBHOOK.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import UTC, datetime

import httpx

from relay_core.simulator import SCENARIOS, build_request


def main() -> int:
    parser = argparse.ArgumentParser(description="Send simulated webhooks to Relay.")
    parser.add_argument("scenario", choices=sorted(SCENARIOS))
    parser.add_argument(
        "--connection", required=True, help="source connection id (printed by `make seed`)"
    )
    parser.add_argument("--api", default="http://127.0.0.1:8000")
    parser.add_argument("--count", type=int, default=1)
    parser.add_argument("--secret-env", default="RELAY_SECRET_DEMO_WEBHOOK")
    args = parser.parse_args()
    secret = os.environ.get(args.secret_env)
    if not secret:
        print(
            f"{args.secret_env} is not set; run via `uv run --env-file .env ...`", file=sys.stderr
        )
        return 1

    previous = None
    with httpx.Client(timeout=15) as client:
        for index in range(args.count):
            now = datetime.now(UTC)
            sequence = (int(now.timestamp() * 1000) + index) % 100_000
            scenario = (
                "valid_order"
                if args.scenario == "duplicate" and previous is None
                else args.scenario
            )
            built = build_request(
                scenario, secret=secret, now=now, sequence=sequence, previous_event_id=previous
            )
            sends = 2 if args.scenario == "duplicate" else 1
            for _ in range(sends):
                response = client.post(
                    f"{args.api}/webhooks/{args.connection}",
                    content=built.body,
                    headers=built.headers,
                )
                print(f"{response.status_code}  {built.event_id}  {response.text[:300]}")
            previous = built.event_id
    return 0


if __name__ == "__main__":
    sys.exit(main())
