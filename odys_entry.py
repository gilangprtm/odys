"""odys — CLI entry point.

Thin wrapper agar `pip install -e .` gak perlu import dependensi berat.
Actual logic di odys.py.
"""
import sys
import os

# Pastikan root project ada di PATH
_root = os.path.dirname(os.path.abspath(__file__))
if _root not in sys.path:
    sys.path.insert(0, _root)

from odys import main

if __name__ == "__main__":
    main()
