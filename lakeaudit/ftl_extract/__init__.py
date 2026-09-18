"""Recovering surveyed FTL vertex tables from scanned HMDA notification drawings.
The drawings are non-georeferenced PDFs with a coordinate table. OCR of that table
is the entry point for every official boundary — and OCR lies, so every table
passes three validity gates before it is allowed to become a polygon."""
