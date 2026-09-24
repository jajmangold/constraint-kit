#!/usr/bin/env python3
"""Visual QA census: sample diverse corpus entries -> build -> render (CAD-QA renderer) -> qwen27b verdict.

The hinge bug was caught VISUALLY (signature matched a wrong reference); this sweeps a diverse sample of the
corpus through the legible renderer + the VLM to surface more look-wrong parts. Doctrine: the VLM is a
categorical second opinion — every NO goes to a human eyeball + geometry arbitration, it is not a verdict.

Host-side (talks to cadkit HTTP, the blender container, and qwen directly):
  python3 drivers/visual_qa.py [n_samples]
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import urllib.request

ROOT = os.environ.get("CK_WORK_DIR", "/tmp/constraint-kit")
CORPUS = f"{ROOT}/cadkit/output/dsl_corpus.jsonl"
CADKIT = os.environ.get("CADKIT_URL", "http://127.0.0.1:8195")
QWEN = os.environ.get("VLM_URL", os.environ.get("QWEN_BASE_URL", "http://localhost:8000/v1"))
BLENDER = os.environ.get("BLENDER_IMAGE", "blender:latest")


def _post(url, payload, timeout=240):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read())


def sample(n):
    """One entry per distinct part-type combo (favoring assemblies), up to n — diversity over volume."""
    seen, picks = set(), []
    rows = [json.loads(l) for l in open(CORPUS)]
    rows.sort(key=lambda r: -len((r.get("program") or {}).get("parts", [])))   # assemblies first
    for r in rows:
        parts = (r.get("program") or {}).get("parts", [])
        key = tuple(sorted(p.get("type", "?") for p in parts))
        if key and key not in seen:
            seen.add(key)
            picks.append(r)
        if len(picks) >= n:
            break
    return picks


def render(glb, png):
    r = subprocess.run(["docker", "run", "--rm", "-v", f"{ROOT}:/tmp/constraint-kit",
                        "-e", "HOME=/tmp/blender-home", "-e", "XDG_CONFIG_HOME=/tmp/blender-config",
                        BLENDER, "blender", "--background", "--python", f"{ROOT}/tools/render_part.py",
                        "--", glb, png, "900"], capture_output=True, text=True, timeout=300)
    return os.path.exists(png) and os.path.getsize(png) > 0


def ask_qwen(png, desc):
    b64 = base64.b64encode(open(png, "rb").read()).decode()
    body = {"model": os.environ.get("MODEL_VLM", "qwen27b"), "temperature": 0.0, "max_tokens": 200,
            "chat_template_kwargs": {"enable_thinking": False},
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": f"Does this CAD render plausibly show: \"{desc}\"? "
                                          "Answer YES or NO first, then one short sentence."},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}]}]}
    out = _post(f"{QWEN}/chat/completions", body)
    return (out["choices"][0]["message"].get("content") or "").strip().replace("\n", " ")


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 12
    picks = sample(n)
    print(f"visual QA over {len(picks)} diverse corpus entries\n")
    yes = no = fail = 0
    for i, r in enumerate(picks):
        desc = (r.get("description") or "")[:90]
        name = f"vqa_{i}"
        try:
            out = _post(f"{CADKIT}/assemble", {"spec": r["program"], "name": name})
            glb = out.get("glb")
            png = f"{ROOT}/cadkit/output/{name}.png"
            if not glb or not render(glb, png):
                fail += 1
                print(f"[{i:2}] RENDER-FAIL :: {desc}")
                continue
            v = ask_qwen(png, r.get("description", ""))
            verdict = "YES" if v.upper().startswith("YES") else "NO "
            yes += verdict == "YES"; no += verdict == "NO "
            print(f"[{i:2}] {verdict} :: {desc}")
            if verdict == "NO ":
                print(f"      vlm: {v[:130]}")
                print(f"      png: cadkit/output/{name}.png")
        except Exception as exc:  # noqa: BLE001
            fail += 1
            print(f"[{i:2}] ERROR {type(exc).__name__}: {str(exc)[:80]} :: {desc}")
    print(f"\n=== visual QA: {yes} YES / {no} NO / {fail} fail — every NO needs eyeball+geometry arbitration ===")


if __name__ == "__main__":
    main()
