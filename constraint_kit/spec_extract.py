"""Fetch + extraction. Deterministic first (HTML tables / PDF text); qwen27b VLM ONLY for visual tables
or when deterministic confidence is low. Everything is fail-soft (no internet/PDF/VLM -> structured error,
never a crash). The VLM result is NOT ground truth — it must still pass schema/unit/sanity/provenance
downstream.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from datetime import datetime, timezone

QWEN_BASE_URL = os.environ.get("QWEN_BASE_URL", "http://localhost:8000/v1")
QWEN_MODEL = os.environ.get("QWEN_MODEL", "qwen27b")


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


def extract_text_tables(html_or_text: str) -> dict:
    """Deterministic extraction of HTML <table> rows (best-effort). Returns {ok, tables, n_tables}.
    Snippets/rows are CANDIDATE evidence, not final facts."""
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html_or_text, "html.parser")
        tables = []
        for t in soup.find_all("table"):
            rows = []
            for tr in t.find_all("tr"):
                cells = [c.get_text(strip=True) for c in tr.find_all(["td", "th"])]
                if cells:
                    rows.append(cells)
            if rows:
                tables.append(rows)
        return {"ok": True, "tables": tables, "n_tables": len(tables)}
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


def extract_with_qwen_vlm(image_or_page_path: str, schema: dict, prompt: str,
                          base_url: str | None = None, model: str | None = None,
                          timeout: float = 120.0) -> dict:
    """Extract structured values from a table/figure IMAGE using qwen27b (OpenAI-compatible, json_schema,
    thinking disabled). Returns {ok, data} or {ok:false, error}. FAIL-SOFT. The data is NOT trusted until
    it passes schema/unit/sanity validation upstream."""
    base = (base_url or QWEN_BASE_URL).rstrip("/")
    model = model or QWEN_MODEL
    try:
        with open(image_or_page_path, "rb") as fh:
            b64 = base64.b64encode(fh.read()).decode()
        import httpx
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
        r = httpx.post(f"{base}/chat/completions", json=body, timeout=timeout)
        r.raise_for_status()
        content = r.json()["choices"][0]["message"].get("content") or "{}"
        return {"ok": True, "data": json.loads(content), "extractor": "qwen27b-vlm"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
