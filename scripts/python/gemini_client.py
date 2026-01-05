"""
Gemini API client helpers.

Existing behavior is preserved by importing the current logic from main.py/test_gemini.
Over time, migrate direct Gemini calls here to keep main.py orchestration-only.
"""

import json
import time
from pathlib import Path

import google.generativeai as genai

__all__ = [
    "genai",
]



