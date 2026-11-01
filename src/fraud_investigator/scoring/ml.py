"""Optional gradient boosting model blended with the rule score (needs the `ml` extra).

Features are the fired flags of every rule signal; the label is "should block".
It trains on a database generated from a different seed than the evaluation
set, so the comparison in ADR 0005 is not measured on training data.

    uv run --extra ml python -m fraud_investigator.scoring.ml --train-seed 7
"""

from __future__ import annotations

import argparse
import json
import pickle
import tempfile
from pathlib import Path
from typing import Any

from ..schemas import Alert, SignalResult


class BlendModel:
    def __init__(self, classifier: Any, features: list[str]) -> None:
        self.classifier = classifier
        self.features = features

    @staticmethod
    def vector(signals: list[SignalResult], features: list[str]) -> list[int]:
        fired = {s.name for s in signals if s.fired}
        return [int(f in fired) for f in features]

    def probability(self, signals: list[SignalResult]) -> float:
        return float(self.classifier.predict_proba([self.vector(signals, self.features)])[0][1])

    def importances(self) -> dict[str, float]:
        return dict(
            sorted(
                zip(self.features, (round(float(v), 4) for v in self.classifier.feature_importances_), strict=True),
                key=lambda kv: -kv[1],
            )
        )

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(pickle.dumps({"classifier": self.classifier, "features": self.features}))

    @classmethod
    def load(cls, path: Path) -> BlendModel:
        if not path.exists():
            raise FileNotFoundError(f"{path} missing; train it with python -m fraud_investigator.scoring.ml")
        data = pickle.loads(path.read_bytes())
        return cls(data["classifier"], data["features"])


def labelled_signals(db_path: Path, cases_path: Path) -> list[tuple[list[SignalResult], str]]:
    from ..tools.backend import DataBackend
    from ..tools.collect import collect_all
    from .engine import ScoringConfig, ScoringEngine

    config = ScoringConfig.load()
    config.ml_blend = {"enabled": False}
    engine = ScoringEngine(config)
    backend = DataBackend(db_path)
    rows = []
    for line in cases_path.read_text(encoding="utf-8").splitlines():
        case = json.loads(line)
        alert = Alert.model_validate(backend.load_alert(case["alert_id"]))
        assessment = engine.score(alert, collect_all(backend, alert))
        rows.append((assessment.signals, case["expected_decision"]))
    return rows


def train(train_seed: int, out: Path) -> BlendModel:
    from sklearn.ensemble import GradientBoostingClassifier

    from ..datagen import generate
    from .engine import ScoringConfig

    config = ScoringConfig.load()
    features = [*config.signals, *config.combinations, *config.mitigations]
    with tempfile.TemporaryDirectory() as tmp:
        db, cases = Path(tmp) / "train.db", Path(tmp) / "cases"
        generate(db, cases, seed=train_seed)
        rows = labelled_signals(db, cases / "golden.jsonl")
    x = [BlendModel.vector(signals, features) for signals, _ in rows]
    y = [int(label == "block") for _, label in rows]
    clf = GradientBoostingClassifier(n_estimators=60, max_depth=2, random_state=train_seed).fit(x, y)
    model = BlendModel(clf, features)
    model.save(out)
    return model


def main() -> None:
    from ..settings import project_root

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--train-seed", type=int, default=7)
    args = parser.parse_args()
    out = project_root() / "data" / "ml_model.pkl"
    model = train(args.train_seed, out)
    print(f"saved {out}")
    print(json.dumps(model.importances(), indent=1))


if __name__ == "__main__":
    main()
