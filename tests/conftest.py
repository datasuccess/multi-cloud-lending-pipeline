"""Shared test setup.

Powertools wants a few env vars at import time; setting them here keeps each
test file from repeating the boilerplate. Tests run on tmp_path — no AWS.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("POWERTOOLS_SERVICE_NAME", "loan-app-generator")
os.environ.setdefault("POWERTOOLS_METRICS_NAMESPACE", "Lending/Generators")
os.environ.setdefault("LOG_LEVEL", "WARNING")
