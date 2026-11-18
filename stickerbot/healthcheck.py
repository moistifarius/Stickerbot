"""Docker HEALTHCHECK: the worker writes a heartbeat at least every ~20s."""
import os
import sys
import time
from pathlib import Path

hb = Path(os.getenv("DATA_DIR", "/data")) / "heartbeat"
try:
    age = time.time() - int(hb.read_text().strip())
except (OSError, ValueError):
    sys.exit(1)
sys.exit(0 if age < 120 else 1)
