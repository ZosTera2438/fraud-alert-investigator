"""Investigator and decision agents."""

from __future__ import annotations

from pathlib import Path

PROMPTS = Path(__file__).parent / "prompts"
PROMPT_VERSION = "v1"


def load_prompt(name: str, version: str = PROMPT_VERSION) -> str:
    return (PROMPTS / f"{name}_{version}.md").read_text(encoding="utf-8")
