#!/usr/bin/env python
"""Register the current Termux tunnel URL with the permanent cloud control plane."""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from pathlib import Path


def main() -> int:
    control_url = os.environ.get("GCP_CONTROL_URL", "").strip().rstrip("/")
    token = os.environ.get("GCP_REGISTER_TOKEN", "").strip()
    if not control_url or not token:
        print("registration_skipped: missing GCP_CONTROL_URL or GCP_REGISTER_TOKEN")
        return 0

    if len(sys.argv) > 1:
        worker_url = sys.argv[1].strip().rstrip("/")
    else:
        worker_url = (Path.home() / "irctc-vacancy-mcp" / "public_url.txt").read_text().strip().rstrip("/")

    if not worker_url.startswith("https://"):
        print("registration_skipped: no https worker URL")
        return 0

    payload = json.dumps({"worker_url": worker_url, "status": "online"}).encode("utf-8")
    request = urllib.request.Request(
        f"{control_url}/register",
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json", "X-Register-Token": token},
    )
    response = urllib.request.urlopen(request, timeout=20)
    print(response.read().decode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
