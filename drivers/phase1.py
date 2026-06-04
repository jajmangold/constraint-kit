#!/usr/bin/env python3
"""Phase-1 driver (2D layout, host side, stdlib only).

  qwen27b plan_layout -> cadkit solve + DXF + PNG preview -> qwen27b VLM QA -> persist verdict to atlas.

No Blender needed: the 2D preview PNG is rendered by cadkit (matplotlib). Reuses qwen27b + atlas.

    python3 drivers/phase1.py "three rectangular panels in a row with 10mm gaps, plus a disc beside them"
"""
from __future__ import annotations

import base64
import json
import os
import sys
import urllib.request

CADKIT = os.environ.get("CADKIT_URL", "http://127.0.0.1:8195")
VLM = os.environ.get("VLM_URL", "http://localhost:8000/v1")
VLM_MODEL = os.environ.get("MODEL_VLM", "qwen27b")

DEFAULT = ("three 60x40 rectangular panels in a horizontal row with 10mm gaps between them, each with "
           "4 corner mounting holes, and a 40mm disc placed to the right of the last panel")


def _post(url, payload, timeout=240.0):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def qa(png, layout_desc):
    with open(png, "rb") as fh:
        b64 = base64.b64encode(fh.read()).decode()
    payload = {
        "model": VLM_MODEL,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": (
                f"This is a 2D CAD sheet layout (parts to be laser-cut). Intended: {layout_desc}. "
                "Are the parts laid out without overlapping each other? Answer YES or NO first, "
                "then one short sentence.")},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
        ]}],
        "temperature": 0.0, "max_tokens": 400,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    return (_post(f"{VLM}/chat/completions", payload)["choices"][0]["message"].get("content") or "").strip()


def main():
    request = sys.argv[1] if len(sys.argv) > 1 else DEFAULT
    print(f"[1/3] layout via cadkit ({CADKIT}) ...")
    out = _post(f"{CADKIT}/layout2d", {"request": request, "name": "phase1"})
    print("      spec parts:", [(p["id"], p["type"]) for p in out["parts"]])
    for p in out["parts"]:
        print(f"      {p['id']:<10} {p['type']:<11} place={p['place']} rot={p['rotate_deg']}")
    print(f"      sheet size: {out['sheet_size']} mm   stored_in_atlas={out['stored']}")
    print(f"      dxf: {out['dxf']}")
    print(f"      png: {out['png']}")
    print(f"[2/3] VLM QA via {VLM_MODEL} ...")
    verdict = qa(out["png"], request)
    try:
        _post(f"{CADKIT}/layout_qa", {"name": out["name"], "verdict": verdict})
    except Exception as exc:  # noqa: BLE001
        print("      (warning: verdict persist failed:", exc, ")")
    print("[3/3] DONE")
    print("=" * 70)
    print("VLM verdict:", verdict)
    print("artifacts:", out["dxf"], "|", out["png"])


if __name__ == "__main__":
    main()
