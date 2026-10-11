"""Shared test fixtures for wechat-exp."""
import os
import sys

_BASE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(_BASE, 'src')
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
