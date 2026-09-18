Synthetic FTL vertex tables for demonstrating the three validity gates.
Declared perimeter for all three sheets: 1858.4 m

  ftl_vertices_clean.csv               -> should PASS
  ftl_vertices_ocr_dropped_digit.csv   -> vertex 5 longitude '78.4005' read as '7.84005' -> FAILS gate 1 (plausibility)
  ftl_vertices_ocr_shifted_point.csv   -> vertex 8 latitude off by ~100 m -> FAILS gate 3 (sheet distances disagree)

  python -m lakeaudit gates --csv sample_data/ftl_vertices_clean.csv --perimeter 1858.4
