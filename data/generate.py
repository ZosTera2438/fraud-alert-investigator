"""Generate the synthetic database and evaluation cases.

uv run python data/generate.py [--seed 42]
"""

from __future__ import annotations

import argparse

from fraud_investigator.datagen import generate
from fraud_investigator.settings import get_settings, project_root


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    settings = get_settings()
    counts = generate(settings.database_path, project_root() / "evals" / "cases", seed=args.seed)
    print(f"database: {settings.database_path}")
    print(f"cases: {counts}")


if __name__ == "__main__":
    main()
