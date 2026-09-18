"""Nalla Cheruvu (Kukatpally), April 2024 -> April 2026: six pinned Sentinel-2 scenes around HYDRAA's
restoration, as one slide. Evidence for the lake-identity check and REVIEW_DEMO beat 5, not a pipeline output.

    python scripts/restoration_figure.py        # -> docs/figures/nalla_restoration_2024_2026.png + .json

Each panel: true colour, our max observed extent (red, from outputs/<lake>/boundary.geojson), the OSM
seed outline (yellow), and open water in that scene (cyan) = MNDWI > 0 on a 10 m grid, connected to the
OSM outline. That cyan is a quick check, not the pipeline's per-lake Otsu threshold, so its acres differ
a little from water_by_year.csv. Scenes are pinned by id so the figure is reproducible; the JSON beside
the PNG records every scene id, the measurements, and the run id / commit / params hash of this run."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import rasterio
from pyproj import Transformer
from pystac_client import Client
from rasterio.enums import Resampling
from rasterio.features import rasterize
from rasterio.transform import from_origin
from rasterio.vrt import WarpedVRT
from scipy import ndimage
from shapely.geometry import shape
from shapely.ops import transform as stransform

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))                       # run as a plain script from anywhere
from lakeaudit.provenance import new_run  # noqa: E402

LAKE = "nalla_cheruvu_kukatpally"
OUT = ROOT / "docs" / "figures" / "nalla_restoration_2024_2026"
STAC = "https://earth-search.aws.element84.com/v1"
HALF_M, RES_M, UTM = 420, 10, "EPSG:32644"
ACRE_M2 = 4046.86
# (scene id, what the record says about that date). Dates only: no reading of how far the works had got.
SCENES = [
    ("S2A_43QHV_20240427_0_L2A", "dry season, before the September 2024 demolitions"),
    ("S2B_43QHV_20241218_0_L2A", "post-monsoon, after the 22 Sep 2024 demolitions"),
    ("S2B_43QHV_20250328_0_L2A", "lake drained (HYDRAA reported desilting, Apr 2025)"),
    ("S2B_43QHV_20251213_1_L2A", "post-monsoon; latest scene in our 2019–2025 record"),
    ("S2A_43QHV_20260310_0_L2A", "the day after the 9 Mar 2026 reopening"),
    ("S2A_43QHV_20260429_0_L2A", "dry season"),
]


def main() -> None:
    prov = new_run(ROOT, [ROOT / "configs" / "thresholds.yaml", ROOT / "configs" / "lakes.yaml"])
    fwd = Transformer.from_crs(4326, UTM, always_xy=True).transform
    seed = shape(json.loads((ROOT / "cache" / "seeds" / f"{LAKE}.geojson").read_text())["geometry"])
    bfeats = json.loads((ROOT / "outputs" / LAKE / "boundary.geojson").read_text())["features"]
    ext_f = next(f for f in bfeats if f["properties"].get("layer") == "max_observed_extent")
    ext_u, seed_u = stransform(fwd, shape(ext_f["geometry"])), stransform(fwd, seed)
    cx, cy = seed_u.centroid.x, seed_u.centroid.y
    n = int(2 * HALF_M / RES_M)
    grid = from_origin(cx - HALF_M, cy + HALF_M, RES_M, RES_M)
    seed_mask = rasterize([seed_u], out_shape=(n, n), transform=grid).astype(bool)

    def read(href, rs):
        with rasterio.open(href) as src, WarpedVRT(src, crs=UTM, transform=grid, width=n, height=n, resampling=rs) as v:
            return v.read()

    items = {it.id: it for it in Client.open(STAC).search(collections=["sentinel-2-l2a"], ids=[s for s, _ in SCENES]).items()}
    missing = [s for s, _ in SCENES if s not in items]
    assert not missing, f"scenes not found on {STAC}: {missing}"

    panels, record = [], []
    for sid, note in SCENES:
        it = items[sid]
        g = read(it.assets["green"].href, Resampling.bilinear)[0].astype("float32")
        s = read(it.assets["swir16"].href, Resampling.bilinear)[0].astype("float32")
        if it.properties.get("earthsearch:boa_offset_applied") is False:    # same rule as imagery.read_scene
            g, s = g - 1000, s - 1000
        mndwi = (g - s) / np.maximum(g + s, 1)
        wet = mndwi > 0
        lab, _ = ndimage.label(wet)
        touching = np.unique(lab[seed_mask & wet])
        water = np.isin(lab, touching[touching > 0])
        rec = {"scene_id": sid, "date": it.properties["datetime"][:10], "note": note,
               "scene_cloud_pct": round(float(it.properties.get("eo:cloud_cover", float("nan"))), 2),
               "open_water_acres_mndwi_gt0_10m": round(float(water.sum()) * RES_M * RES_M / ACRE_M2, 1),
               "median_mndwi_in_osm_outline": round(float(np.median(mndwi[seed_mask])), 2)}
        record.append(rec)
        panels.append((rec, read(it.assets["visual"].href, Resampling.bilinear).transpose(1, 2, 0), water))
        print(rec)

    fig, axs = plt.subplots(2, 3, figsize=(15, 11.2))
    to_px = lambda xs, ys: ([(x - (cx - HALF_M)) / RES_M for x in xs], [((cy + HALF_M) - y) / RES_M for y in ys])
    for ax, (rec, rgb, water) in zip(axs.flat, panels):
        ax.imshow(np.clip(rgb.astype(float) / 200, 0, 1), interpolation="nearest")
        ax.contour(water, levels=[0.5], colors="#00e5ff", linewidths=1.6)
        for geom, color, ls in ((ext_u, "#e0103f", "-"), (seed_u, "#ffd400", "--")):
            for poly in getattr(geom, "geoms", [geom]):
                ax.plot(*to_px(*poly.exterior.xy), color=color, lw=1.8, ls=ls)
        ax.set_xticks([]); ax.set_yticks([])
        ax.set_title(f"{rec['date']} · {rec['note']}\n{rec['open_water_acres_mndwi_gt0_10m']} ac open water · {rec['scene_id']}",
                     fontsize=10, loc="left")
    fig.suptitle("Nalla Cheruvu (Kukatpally): six Sentinel-2 scenes, April 2024 → April 2026", fontsize=15, x=0.01, ha="left", y=0.985)
    fig.text(0.01, 0.012,
             f"Red: maximum post-monsoon water extent observed by Sentinel-2 since 2019 (run {ext_f['properties'].get('run_id')}) — "
             "one witness to the FTL, not the notified FTL and not a legal determination.\n"
             "Yellow dashed: OSM way 129532522.   Cyan: open water in that scene, MNDWI > 0 on a 10 m grid, connected to the OSM outline — "
             "a quick check, not the pipeline threshold.   Window 840 m.\n"
             f"Figure run {prov.run_id} · commit {prov.code_commit} · params {prov.params_hash}.",
             fontsize=8.5, color="#333")
    fig.tight_layout(rect=[0, 0.06, 1, 0.965], h_pad=3.5)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT.with_suffix(".png"), dpi=110)
    OUT.with_suffix(".json").write_text(json.dumps({
        "figure": OUT.with_suffix(".png").name, "lake_id": LAKE, **prov.as_dict(),
        "boundary_run_id": ext_f["properties"].get("run_id"), "boundary_code_commit": ext_f["properties"].get("code_commit"),
        "stac": STAC, "window_m": 2 * HALF_M, "grid_m": RES_M,
        "water_is": "MNDWI > 0 on a 10 m grid, connected to the OSM seed outline; a quick check, not the pipeline's Otsu threshold",
        "scenes": record}, indent=1, ensure_ascii=False) + "\n")
    print("wrote", OUT.with_suffix(".png"), "and", OUT.with_suffix(".json"))


if __name__ == "__main__":
    main()
