"""Import cadence dashboards into SigNoz over the API.

Doing this through the API rather than the UI means the dashboards are
reproducible: a fresh SigNoz plus this script gives an identical result, which
matters when the alternative is a README paragraph telling someone to click
through an import wizard and hope they picked the same file.

Idempotent — a dashboard with the same title is updated rather than duplicated.

    python scripts/import_dashboards.py --api-key "$SIGNOZ_API_KEY"
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

DASHBOARD_DIR = Path(__file__).resolve().parent.parent / "dashboards"


def request(method: str, url: str, key: str, payload: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        headers={"SIGNOZ-API-KEY": key, "Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            body = response.read().decode()
            return response.status, json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode()
        try:
            return exc.code, json.loads(body)
        except json.JSONDecodeError:
            return exc.code, {"raw": body[:400]}


def existing_by_title(base: str, key: str) -> dict[str, str]:
    status, body = request("GET", f"{base}/api/v1/dashboards", key)
    if status != 200:
        return {}
    out: dict[str, str] = {}
    for entry in body.get("data") or []:
        # The uuid lives at the top level, the title inside `data`.
        title = (entry.get("data") or {}).get("title") or entry.get("title")
        uuid = entry.get("uuid") or entry.get("id")
        if title and uuid:
            out[title] = uuid
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default=os.getenv("SIGNOZ_UI", "http://localhost:8080"))
    parser.add_argument("--api-key", default=os.getenv("SIGNOZ_API_KEY"))
    args = parser.parse_args()

    if not args.api_key:
        print("error: --api-key or SIGNOZ_API_KEY required", file=sys.stderr)
        return 2

    files = sorted(DASHBOARD_DIR.glob("*.json"))
    if not files:
        print("no dashboard files found", file=sys.stderr)
        return 1

    existing = existing_by_title(args.base, args.api_key)
    failures = 0

    for path in files:
        payload = json.loads(path.read_text())
        title = payload.get("title", path.stem)

        if title in existing:
            status, body = request(
                "PUT", f"{args.base}/api/v1/dashboards/{existing[title]}",
                args.api_key, payload,
            )
            verb = "updated"
        else:
            status, body = request(
                "POST", f"{args.base}/api/v1/dashboards", args.api_key, payload
            )
            verb = "created"

        if status in (200, 201):
            uuid = (body.get("data") or {}).get("uuid") or existing.get(title, "")
            print(f"  ✓ {verb}: {title}")
            if uuid:
                print(f"      {args.base}/dashboard/{uuid}")
        else:
            failures += 1
            err = body.get("error") if isinstance(body, dict) else body
            message = err.get("message") if isinstance(err, dict) else (err or body)
            print(f"  ✗ failed ({status}): {title}\n      {str(message)[:300]}")

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
