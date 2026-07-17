# src/visual_report/__init__.py
"""Generate a self-contained, styled HTML page from deep research results.

Re-exports the public API: generate_visual_report and json_dumps_str.
"""

from src.visual_report.report import generate_visual_report, json_dumps_str

__all__ = ["generate_visual_report", "json_dumps_str"]
