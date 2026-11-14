"""Reset ONLY the synthetic demonstration data (var/ by default) and optionally reload the dataset.

Usage:  python scripts/reset_env.py [--load] [--var-dir DIR]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from stratacaster.environment import Environment  # noqa: E402

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--load", action="store_true", help="load the synthetic MSR cases after reset")
    ap.add_argument("--var-dir", default=None)
    args = ap.parse_args()
    env = Environment(var_dir=Path(args.var_dir) if args.var_dir else None, reset=True)
    print(f"Reset synthetic stores in {env.var_dir}")
    if args.load:
        for r in env.load_all_cases():
            print(f"  {'OK ' if r['ok'] else 'NO '} {r['case_id']}: {r['reason']}")
    env.close()
