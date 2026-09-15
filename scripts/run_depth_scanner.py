import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.depth_scanner import run_depth_scanner

if __name__ == "__main__":
    run_depth_scanner()
