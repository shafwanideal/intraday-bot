import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.shadow import run_shadow

if __name__ == "__main__":
    run_shadow()
