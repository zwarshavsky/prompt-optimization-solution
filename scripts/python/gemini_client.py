"""
Gemini API client helpers.

Existing behavior is preserved by importing the current logic from main.py/test_gemini.
Over time, migrate direct Gemini calls here to keep main.py orchestration-only.
"""

import json
import time
from pathlib import Path

try:
    import google.genai as genai
    USE_NEW_GENAI = True
except ImportError:
    import google.generativeai as genai
    USE_NEW_GENAI = False

__all__ = [
    "genai",
    "USE_NEW_GENAI",
]



