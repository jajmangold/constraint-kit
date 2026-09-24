#!/usr/bin/env python3
"""Host-side batch renderer for S7: render every GLB in s7_worklist.jsonl -> PNG (CAD-QA renderer),
writing png/mutant_png paths back into the worklist. Parallel Blender containers; idempotent (skips done).

  python3 drivers/render_batch.py [n_parallel]
"""
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

ROOT = os.environ.get("CK_WORK_DIR", "/tmp/constraint-kit")
WL = f"{ROOT}/cadkit/output/dataset/s7_worklist.jsonl"
BLENDER = os.environ.get("BLENDER_IMAGE", "blender:latest")


def render(glb):
    png = os.path.splitext(glb)[0] + "_qa.png"
    if os.path.exists(png) and os.path.getsize(png) > 0:
        return png
    r = subprocess.run(["docker", "run", "--rm", "-v", f"{ROOT}:/tmp/constraint-kit",
                        "-e", "HOME=/tmp/blender-home", "-e", "XDG_CONFIG_HOME=/tmp/blender-config",
                        BLENDER, "blender", "--background", "--python", f"{ROOT}/tools/render_part.py",
                        "--", glb, png, "768"], capture_output=True, text=True, timeout=300)
    return png if (os.path.exists(png) and os.path.getsize(png) > 0) else None


def main():
    par = int(sys.argv[1]) if len(sys.argv) > 1 else 6
    items = [json.loads(l) for l in open(WL)]
    jobs = []
    for it in items:
        jobs.append((it, "glb", "png"))
        if it.get("mutant_glb"):
            jobs.append((it, "mutant_glb", "mutant_png"))
    done = fail = 0
    with ThreadPoolExecutor(max_workers=par) as ex:
        for (it, src, dst), png in zip(jobs, ex.map(lambda j: render(j[0][j[1]]), jobs)):
            if png:
                it[dst] = png
                done += 1
            else:
                fail += 1
            if (done + fail) % 100 == 0:
                print(f"  {done} rendered / {fail} failed of {len(jobs)}", flush=True)
    with open(WL, "w") as fh:
        for it in items:
            fh.write(json.dumps(it) + "\n")
    print(f"render_batch: {done}/{len(jobs)} rendered ({fail} failed)")


if __name__ == "__main__":
    main()
