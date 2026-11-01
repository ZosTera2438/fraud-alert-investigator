from __future__ import annotations

import hashlib
from pathlib import Path

from fraud_investigator.datagen import SCENARIOS, VARIANTS_PER_SCENARIO, generate
from fraud_investigator.db import session


def digest(db: Path) -> str:
    with session(db) as conn:
        rows = conn.execute("SELECT * FROM transactions ORDER BY id").fetchall()
        rows += conn.execute("SELECT * FROM alerts ORDER BY id").fetchall()
    return hashlib.sha256(repr([tuple(r) for r in rows]).encode()).hexdigest()


def test_generation_is_reproducible(tmp_path: Path) -> None:
    a = generate(tmp_path / "a.db", tmp_path / "ca", seed=42)
    generate(tmp_path / "b.db", tmp_path / "cb", seed=42)
    assert a["golden"] == len(SCENARIOS) * VARIANTS_PER_SCENARIO
    assert digest(tmp_path / "a.db") == digest(tmp_path / "b.db")
    assert (tmp_path / "ca" / "golden.jsonl").read_text() == (tmp_path / "cb" / "golden.jsonl").read_text()


def test_different_seed_gives_different_data(tmp_path: Path) -> None:
    generate(tmp_path / "a.db", tmp_path / "ca", seed=1)
    generate(tmp_path / "b.db", tmp_path / "cb", seed=2)
    assert digest(tmp_path / "a.db") != digest(tmp_path / "b.db")
