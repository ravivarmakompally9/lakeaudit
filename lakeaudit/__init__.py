"""lakeaudit — satellite-derived lake boundary reconstruction with a published error budget.

Pipeline (one lake, one run):
  register  -> resolve the lake's seed polygon (OSM) and buffer rule
  imagery   -> pick one post-monsoon Sentinel-2 scene per year (public AWS archive)
  water     -> MNDWI, Otsu inside the ROI, mask -> polygon, per year
  boundary  -> maximum observed extent + statutory buffer ring
  structures-> building footprints -> inside FTL / in buffer / outside, distance to line
  store     -> GeoJSON/CSV with run_id, scene ids, code commit and params hash on every row
  api       -> /lookup with the abstention rule
"""
__version__ = "0.2.0"
