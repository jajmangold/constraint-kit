"""Live source DISCOVERY via SearXNG (fail-soft). Search only finds candidate sources — snippets are
NEVER executable facts; facts must come from fetched content or the preseed.

Source-type ranking (prefer authoritative): official standard > manufacturer > distributor >
handbook > generic webpage.
"""
from __future__ import annotations

import os

SEARCH_URL = os.environ.get("SPEC_SEARCH_URL", "http://localhost:8080")

# domain hints -> source_type (rule-based, inspectable)
_OFFICIAL = ("iso.org", "din.de", "ansi.org", "asme.org", "bsigroup.com", "astm.org", "nist.gov")
_MANUFACTURER = ("boltdepot", "mcmaster", "fastenal", "wurth", "bossard", "misumi", "skf.com",
                 "thkstore", "trafalgar", "accu.co.uk", "8020.net")
_DISTRIBUTOR = ("grainger", "rs-online", "rs-components", "digikey", "newark", "farnell")
_HANDBOOK = ("engineeringtoolbox", "machineryshandbook", "engineersedge", "amesweb", "tribology-abc")
_FORUM = ("reddit", "quora", "stackexchange", "forum", "blogspot", "wordpress", "medium.com")

_RANK = {"official_standard": 1, "manufacturer_catalog": 2, "distributor_catalog": 3,
         "handbook": 4, "webpage": 5, "unknown": 6}


def guess_source_type(url: str | None, title: str = "") -> str:
    u = (url or "").lower()
    t = (title or "").lower()
    if any(d in u for d in _OFFICIAL):
        return "official_standard"
    if any(d in u for d in _MANUFACTURER):
        return "manufacturer_catalog"
    if any(d in u for d in _DISTRIBUTOR):
        return "distributor_catalog"
    if any(d in u for d in _HANDBOOK):
        return "handbook"
    if any(d in u for d in _FORUM):
        return "forum"
    if "iso" in t and "metric" in t:
        return "handbook"
    return "webpage"


def normalize_result(r: dict) -> dict:
    url = r.get("url")
    title = r.get("title", "")
    return {
        "title": title, "url": url, "content": r.get("content", ""),
        "engine": r.get("engine", ""), "score": r.get("score", 0.0),
        "category": r.get("category", ""),
        "source_type_guess": guess_source_type(url, title),
    }


def search_searxng(query: str, categories: list[str] | None = None,
                   url: str | None = None, timeout: float = 12.0) -> list[dict]:
    """Query SearXNG JSON API; return normalized candidate sources. FAIL-SOFT: [] on any error
    (no internet, non-200, bad JSON) — never raises."""
    base = (url or SEARCH_URL).rstrip("/")
    params = {"q": query, "format": "json"}
    if categories:
        params["categories"] = ",".join(categories)
    try:
        import httpx
        resp = httpx.get(f"{base}/search", params=params, timeout=timeout,
                         headers={"User-Agent": "constraint-kit-spec-compiler"})
        resp.raise_for_status()
        data = resp.json()
    except Exception:  # noqa: BLE001 -- discovery is best-effort
        return []
    return [normalize_result(r) for r in data.get("results", []) if r.get("url")]


def rank_sources(candidates: list[dict]) -> list[dict]:
    """Order candidates by authority (source_type_guess), then by engine score desc."""
    return sorted(candidates,
                  key=lambda c: (_RANK.get(c.get("source_type_guess", "unknown"), 6),
                                 -float(c.get("score") or 0.0)))
