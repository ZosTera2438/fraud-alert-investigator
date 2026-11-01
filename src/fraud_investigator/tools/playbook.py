"""Fraud playbook passages and a small lexical retriever.

The plan allows Chroma or LanceDB; a dependency-free BM25 keeps the demo and CI
light. The interface (query -> ranked passages) is the same, so swapping in a
vector store later only changes this module.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass

from ..schemas import PlaybookPassage


@dataclass(frozen=True)
class Passage:
    passage_id: str
    title: str
    text: str


PASSAGES: list[Passage] = [
    Passage(
        "PB-001",
        "Card testing",
        "Fraudsters test stolen card numbers with many small card-not-present charges, often under 5 dollars, "
        "at a newly registered online merchant within minutes. Velocity of small amounts is the key indicator. "
        "Block the card and reissue.",
    ),
    Passage(
        "PB-002",
        "Account takeover",
        "Account takeover follows a change of email, phone or password, then a login from a new device and "
        "often a new country, followed by a high value purchase. A recent profile change combined with a new "
        "device is a strong indicator. Block and contact the customer through a verified channel.",
    ),
    Passage(
        "PB-003",
        "Impossible travel and skimming",
        "Card-present transactions far apart in a short time imply travel faster than an aircraft. This "
        "suggests a counterfeit or skimmed card. Check travel notices before acting.",
    ),
    Passage(
        "PB-004",
        "High risk merchants",
        "Merchants registered in the last 30 days or with chargeback rates above 1 percent carry elevated risk. "
        "A single purchase at such a merchant is not proof of fraud; escalate for review.",
    ),
    Passage(
        "PB-005",
        "Emulators, rooted devices and proxies",
        "Device emulators, rooted phones and proxy or VPN connections hide the real device and location. "
        "Combined with a device first seen in the last day they indicate automated or remote fraud.",
    ),
    Passage(
        "PB-006",
        "Conflicting signals",
        "When a long-standing trusted device shows unusual velocity or location, signals conflict. Do not "
        "auto-decide; escalate to an analyst with the evidence on both sides.",
    ),
    Passage(
        "PB-007",
        "First-party fraud",
        "Customers who repeatedly dispute delivered goods may be committing first-party fraud. These cases "
        "need analyst review and evidence of delivery; do not block automatically.",
    ),
    Passage(
        "PB-008",
        "Legitimate travel",
        "A travel notice on file that covers the transaction country and dates, with a known device, "
        "strongly mitigates foreign transaction alerts.",
    ),
    Passage(
        "PB-009",
        "Large legitimate purchases",
        "Large purchases at merchants the customer already uses, from a long-standing device, are usually "
        "legitimate even when the amount is above the customer's normal range.",
    ),
    Passage(
        "PB-010",
        "Missing or failed evidence",
        "If a data source times out or returns invalid data, do not guess. Escalate with the reason so an "
        "analyst can complete the investigation.",
    ),
]

_TOKEN = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> list[str]:
    return _TOKEN.findall(text.lower())


class PlaybookIndex:
    def __init__(self, passages: list[Passage] | None = None, k1: float = 1.5, b: float = 0.75) -> None:
        self.passages = passages or PASSAGES
        self.k1, self.b = k1, b
        self.docs = [Counter(_tokens(f"{p.title} {p.text}")) for p in self.passages]
        self.lengths = [sum(d.values()) for d in self.docs]
        self.avg_len = sum(self.lengths) / len(self.lengths)
        df: Counter[str] = Counter()
        for d in self.docs:
            df.update(d.keys())
        n = len(self.docs)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    def search(self, query: str, top_k: int = 3) -> list[PlaybookPassage]:
        terms = _tokens(query.replace("_", " "))
        scored: list[tuple[float, int]] = []
        for i, doc in enumerate(self.docs):
            score = 0.0
            for t in terms:
                if t not in doc:
                    continue
                tf = doc[t]
                norm = tf + self.k1 * (1 - self.b + self.b * self.lengths[i] / self.avg_len)
                score += self.idf[t] * tf * (self.k1 + 1) / norm
            scored.append((score, i))
        scored.sort(key=lambda s: (-s[0], s[1]))
        return [
            PlaybookPassage(
                passage_id=self.passages[i].passage_id,
                title=self.passages[i].title,
                text=self.passages[i].text,
                score=round(s, 3),
            )
            for s, i in scored[: max(1, min(top_k, 5))]
            if s > 0
        ]
