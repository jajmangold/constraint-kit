"""Fetch + extraction. Deterministic first (HTML tables / PDF text); the vision model ONLY for visual tables
or when deterministic confidence is low. Everything is fail-soft (no internet/PDF/VLM -> structured error,
never a crash). The VLM result is NOT ground truth — it must still pass schema/unit/sanity/provenance
downstream.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
from datetime import datetime, timezone



def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def fetch_source(url: str, timeout: float = 15.0, save_dir: str | None = None) -> dict:
    """Fetch a URL. Returns {ok, status, content_type, text|pdf_path, source_hash, retrieved_at}.
    FAIL-SOFT: {ok:false, error} on any failure."""
    try:
        import httpx
        r = httpx.get(url, timeout=timeout, follow_redirects=True,
                      headers={"User-Agent": "constraint-kit-spec-compiler"})
        r.raise_for_status()
        body = r.content
        ctype = r.headers.get("content-type", "").split(";")[0].strip()
        h = hashlib.sha256(body).hexdigest()
        out = {"ok": True, "status": r.status_code, "content_type": ctype,
               "source_hash": f"sha256:{h}", "retrieved_at": _now(), "url": url}
        if "pdf" in ctype:
            save_dir = save_dir or os.environ.get("SPEC_CACHE_DIR", "/tmp/spec_cache")
            os.makedirs(save_dir, exist_ok=True)
            p = os.path.join(save_dir, f"{h[:16]}.pdf")
            with open(p, "wb") as fh:
                fh.write(body)
            out["pdf_path"] = p
        else:
            out["text"] = r.text
        return out
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "url": url}


_NUM_RE = re.compile(r"[-+]?\d+(?:\.\d+)?")
_MAX_ROWS = 5000          # guard against a pathological page; surfaced via `truncated`


def parse_quantity(text: str):
    """Best-effort (value, unit) from a cell like '8 mm', 'Ø22', '0.75 in', '1,25' → (8.0,'mm') /
    (22.0,None) / (0.75,'in') / (1.25,None). Returns the FIRST number + the unit token following it, or
    None. Deterministic; candidate evidence only (never a final fact on its own)."""
    if text is None:
        return None
    s = str(text).strip().replace("×", "x").replace(",", ".")
    m = _NUM_RE.search(s)
    if not m:
        return None
    rest = s[m.end():].strip()
    um = re.match(r"[A-Za-zµμ°\"']+", rest)
    unit = um.group() if um and um.group().lower() not in ("x",) else None
    return (float(m.group()), unit)


def numbers_in(content) -> set:
    """The set of distinct numeric TOKENS in text/tables — full numbers, so 6 is NOT found inside 16 or
    0.6 (the fix for substring false-positives in confirmation). Commas treated as decimal points."""
    return {round(float(x), 6) for x in _NUM_RE.findall(str(content).replace(",", "."))}


def value_confirmed(content, parsed: dict, tol: float = 1e-6) -> bool:
    """A source CONFIRMS a parsed fact only if EVERY numeric value appears as a standalone numeric token
    in its content (within `tol`) — a robust confidence gate, not a substring match. No numeric values to
    confirm → False (can't confirm nothing)."""
    toks = numbers_in(content)
    nums = [v["value"] for v in parsed.get("values", {}).values() if isinstance(v["value"], (int, float))]
    return bool(nums) and all(any(abs(n - t) <= tol for t in toks) for n in nums)


def extract_text_tables(html_or_text: str) -> dict:
    """Deterministic, HARDENED extraction of HTML <table> rows. Expands colspan (repeats the cell so
    columns stay aligned), collapses whitespace, drops empty rows/tables, and caps total rows (`truncated`
    flag, no silent loss). Returns {ok, tables, n_tables, truncated}. Rows are CANDIDATE evidence, not facts."""
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html_or_text, "html.parser")
        tables, total, truncated = [], 0, False
        for t in soup.find_all("table"):
            rows = []
            for tr in t.find_all("tr"):
                cells = []
                for c in tr.find_all(["td", "th"]):
                    txt = " ".join(c.get_text(strip=True).split())   # collapse whitespace/newlines
                    try:
                        span = max(1, int(c.get("colspan", 1) or 1))
                    except (TypeError, ValueError):
                        span = 1
                    cells.extend([txt] * span)                       # keep columns aligned across colspan
                if any(cell for cell in cells):                      # skip fully-empty rows
                    rows.append(cells)
                    total += 1
                    if total >= _MAX_ROWS:
                        truncated = True
                        break
            if rows:
                tables.append(rows)
            if truncated:
                break
        return {"ok": True, "tables": tables, "n_tables": len(tables), "truncated": truncated}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "tables": []}


def extract_pdf_text(pdf_path: str) -> dict:
    """Deterministic PDF text extraction (pypdf, no OCR). Returns {ok, text, n_pages}."""
    try:
        from pypdf import PdfReader
        reader = PdfReader(pdf_path)
        text = "\n".join((pg.extract_text() or "") for pg in reader.pages)
        return {"ok": True, "text": text, "n_pages": len(reader.pages)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "text": ""}


def extract_pdf(pdf_path: str) -> dict:
    """Layout-aware PDF text extraction — the DURABLE deterministic PDF path. Uses pdf_oxide (Rust,
    layout-aware) which recovers per-cell text from dense tabular PDFs that flat pypdf scrambles (the
    ISO 286 lesson, T5.1); falls back to pypdf, then to a structured error. Returns {ok, text, n_pages,
    extractor}. Fail-soft: never raises. This is deterministic-first — the VLM stays a last resort."""
    try:
        import pdf_oxide
        doc = pdf_oxide.PdfDocument(pdf_path)
        n = doc.page_count()
        # join layout-extracted words per page (preserves numeric tokens that pypdf flattens away)
        def page_words(p):
            try:
                ws = doc.extract_words(p)
                return " ".join((w["text"] if isinstance(w, dict) else getattr(w, "text", "")) for w in ws)
            except Exception:  # noqa: BLE001
                return doc.extract_page_text(p) or ""
        text = "\n".join(page_words(p) for p in range(n))
        return {"ok": True, "text": text, "n_pages": n, "extractor": "pdf_oxide"}
    except Exception as exc:  # noqa: BLE001 -- pdf_oxide missing/errors -> deterministic pypdf fallback
        fb = extract_pdf_text(pdf_path)
        if fb.get("ok"):
            return {**fb, "extractor": "pypdf", "pdf_oxide_error": f"{type(exc).__name__}: {exc}"}
        return {"ok": False, "error": f"pdf_oxide+pypdf failed: {exc}", "text": "", "extractor": "none"}


def extract_pdf_tables(pdf_path: str, page: int | None = None) -> dict:
    """Reconstruct PDF tables as rows of cell text via pdf_oxide layout (bbox row/column clustering) — the
    reusable, general engineering-table reader (generalized from the ISO 286-2 extraction, T5.1). `page`
    limits to one page (else all). Returns {ok, tables:[{page,rows:[[cell,...]]}], extractor}. Fail-soft."""
    try:
        import pdf_oxide
        doc = pdf_oxide.PdfDocument(pdf_path)
        pages = [page] if page is not None else range(doc.page_count())
        out = []
        for p in pages:
            ws = doc.extract_words(p)
            cells = sorted(((round((w["bbox"] if isinstance(w, dict) else w.bbox)[1], 1),
                             (w["bbox"] if isinstance(w, dict) else w.bbox)[0],
                             (w["text"] if isinstance(w, dict) else w.text)) for w in ws))
            rows, cur, ly = [], [], None
            for y, x, t in cells:
                if ly is None or abs(y - ly) <= 2.5:
                    cur.append(t)
                else:
                    rows.append(cur); cur = [t]
                ly = y
            if cur:
                rows.append(cur)
            if rows:
                out.append({"page": p, "rows": rows})
        return {"ok": True, "tables": out, "extractor": "pdf_oxide"}
    except Exception as exc:  # noqa: BLE001 -- pdf_oxide missing/errors
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "tables": []}


def extract_with_qwen_vlm(image_or_page_path: str, schema: dict, prompt: str,
                          base_url: str | None = None, model: str | None = None,
                          timeout: float = 120.0) -> dict:
    """Extract structured values from a table/figure IMAGE using the vision model (see llm.py; json output,
    thinking disabled). Returns {ok, data} or {ok:false, error}. FAIL-SOFT. The data is NOT trusted until
    it passes schema/unit/sanity validation upstream."""
    from . import llm
    base = base_url or llm.base_url("vlm")
    model = model or llm.vlm_model()
    try:
        with open(image_or_page_path, "rb") as fh:
            b64 = base64.b64encode(fh.read()).decode()
        body = {
            "model": model,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": prompt + (
                    "\nRules: extract ONLY visible table values; preserve units exactly; return null for "
                    "uncertain/missing cells; never infer from memory; output must match the schema.")},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}]}],
            "temperature": 0.0, "max_tokens": 800,
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": "extraction", "schema": schema, "strict": True}},
            "chat_template_kwargs": {"enable_thinking": False},
        }
        content = llm.chat(body, url=base, timeout=timeout)["choices"][0]["message"].get("content") or "{}"
        return {"ok": True, "data": json.loads(content), "extractor": f"{model}-vlm"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
