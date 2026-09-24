"""Optional JSON artifact cache (debug/export only). SQLite is the durable store; this is a convenience
dump of each resolution under SPEC_CACHE_DIR. Fail-soft — never breaks a resolution."""
from __future__ import annotations

import json
import os

CACHE_DIR = os.environ.get(
    "SPEC_CACHE_DIR", os.path.join(os.environ.get("CK_WORK_DIR", "/tmp/constraint-kit"), "cadkit/output/spec_cache"))


def write_json_artifact(result: dict, cache_dir: str | None = None) -> str | None:
    cache_dir = cache_dir or CACHE_DIR
    try:
        os.makedirs(cache_dir, exist_ok=True)
        rid = result.get("id") or result.get("query", "result").strip().lower().replace(" ", "_")[:40]
        path = os.path.join(cache_dir, f"{rid}.json")
        with open(path, "w") as fh:
            json.dump(result, fh, indent=2)
        return path
    except Exception:  # noqa: BLE001 -- debug artifact only
        return None
