from __future__ import annotations

import json
import os
import shutil
from collections import defaultdict
from pathlib import Path

import pytest

os.environ.setdefault("FRAUD_MODEL_PROVIDER", "scripted")
os.environ.pop("FRAUD_TRACING", None)

from fraud_investigator.datagen import generate
from fraud_investigator.settings import Settings, load_settings


@pytest.fixture(scope="session")
def generated(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("data")
    generate(root / "fraud.db", root / "cases", seed=42)
    return root


@pytest.fixture(scope="session")
def cases(generated: Path) -> dict[str, list[dict]]:
    by_scenario: dict[str, list[dict]] = defaultdict(list)
    for name in ("golden", "red_team"):
        for line in (generated / "cases" / f"{name}.jsonl").read_text(encoding="utf-8").splitlines():
            case = json.loads(line)
            by_scenario[case["scenario"]].append(case)
    return dict(by_scenario)


@pytest.fixture
def workdir(generated: Path, tmp_path: Path) -> Path:
    """A fresh copy of the database per test, so actions never leak between tests."""
    shutil.copy(generated / "fraud.db", tmp_path / "fraud.db")
    return tmp_path


@pytest.fixture
def settings(workdir: Path) -> Settings:
    s = load_settings()
    s.model.provider = "scripted"
    s.observability.enabled = False
    s.tools.transport = "local"
    s.paths.database = str(workdir / "fraud.db")
    s.paths.runs = str(workdir / "runs")
    s.paths.sessions = str(workdir / "sessions")
    return s


@pytest.fixture
def db_path(settings: Settings) -> Path:
    return settings.database_path
