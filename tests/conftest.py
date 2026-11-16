import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tests" / "e2e"))
sys.path.insert(0, str(REPO / "bridge"))
