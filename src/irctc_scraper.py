"""
irctc_scraper.py — DEPRECATED SHIM
====================================
This file is kept for backward compatibility only.
All logic has been moved to:
  irctc_api.py      — API client (correct endpoints + keys)
  vacancy_filter.py — Pure-Python filtering (real bsd structure)
  main_tool.py      — Orchestrator (single entry point)

New code should import from main_tool:
  from main_tool import find_vacant_berths
"""

import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

# Re-export the new entry point under the old name
from main_tool import find_vacant_berths  # noqa: F401

__all__ = ["find_vacant_berths"]
