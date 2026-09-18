"""Best-effort table extraction from an HMDA lake notification PDF.

HMDA sheets vary. This finds any table whose header mentions latitude/longitude
(or 'northing'/'easting') and returns rows as {n, lat, lon, dist_next_m}. It is
deliberately conservative: anything it cannot parse cleanly is returned in
`unparsed` for a human, not guessed."""
from __future__ import annotations

import re
from pathlib import Path

import pdfplumber

_NUM = re.compile(r"[-+]?\d+(?:\.\d+)?")


def _find_cols(header: list[str]) -> dict | None:
    h = [(c or "").strip().lower() for c in header]
    def idx(*keys):
        for i, c in enumerate(h):
            if any(k in c for k in keys):
                return i
        return None
    lat = idx("lat", "northing", "north")
    lon = idx("lon", "long", "easting", "east")
    n = idx("s.no", "sno", "sl", "point", "vertex", "no")
    dist = idx("dist", "length", "chainage")
    if lat is None or lon is None:
        return None
    return {"lat": lat, "lon": lon, "n": n, "dist": dist}


def _dms_or_float(s: str) -> float | None:
    """Accept 17.4321, or 17°25'55.6\"N style."""
    if s is None:
        return None
    s = s.strip()
    m = re.match(r"(\d+)[°\s]+(\d+)['\s]+([\d.]+)\"?\s*([NSEW])?", s)
    if m:
        d, mi, se, hemi = m.groups()
        v = float(d) + float(mi) / 60 + float(se) / 3600
        if hemi in ("S", "W"):
            v = -v
        return v
    m = _NUM.search(s)
    return float(m.group()) if m else None


def extract_vertex_rows(pdf_path: Path) -> tuple[list[dict], list[list[str]], dict]:
    rows, unparsed = [], []
    meta = {"pages_scanned": 0, "tables_seen": 0, "declared_perimeter_m": None, "declared_area_text": None}
    with pdfplumber.open(str(pdf_path)) as pdf:
        for page in pdf.pages:
            meta["pages_scanned"] += 1
            text = page.extract_text() or ""
            m = re.search(r"perimeter[^0-9]{0,20}([\d,]+(?:\.\d+)?)\s*(m|mtr|mts|km)?", text, re.I)
            if m and meta["declared_perimeter_m"] is None:
                val = float(m.group(1).replace(",", ""))
                if (m.group(2) or "").lower().startswith("km"):
                    val *= 1000
                meta["declared_perimeter_m"] = val
            m2 = re.search(r"(FTL|Full Tank Level)[^0-9]{0,30}([\d.]+)\s*(ac|acre|acres|ha|hect)", text, re.I)
            if m2 and meta["declared_area_text"] is None:
                meta["declared_area_text"] = m2.group(0)
            for table in page.extract_tables() or []:
                meta["tables_seen"] += 1
                if not table or len(table) < 2:
                    continue
                cols = _find_cols(table[0])
                if cols is None:
                    continue
                auto_n = 0
                for r in table[1:]:
                    try:
                        lat = _dms_or_float(r[cols["lat"]])
                        lon = _dms_or_float(r[cols["lon"]])
                        if lat is None or lon is None:
                            unparsed.append(r); continue
                        auto_n += 1
                        n = int(_NUM.search(r[cols["n"]]).group()) if cols["n"] is not None and r[cols["n"]] and _NUM.search(r[cols["n"]]) else auto_n
                        dist = _dms_or_float(r[cols["dist"]]) if cols["dist"] is not None and r[cols["dist"]] else None
                        rows.append({"n": n, "lat": lat, "lon": lon, "dist_next_m": dist})
                    except Exception:
                        unparsed.append(r)
    return rows, unparsed, meta
