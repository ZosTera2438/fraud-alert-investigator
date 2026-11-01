"""RedactionHook: masks card numbers, emails and phone numbers before the model sees them.

Tools already return masked data; this hook is defence in depth for anything
that slips through (alert free text, tool errors, investigator notes). It
rewrites each message in place as it is added to the conversation, so the next
model call only ever receives the masked text.
"""

from __future__ import annotations

import re
from typing import Any

from strands.hooks import HookProvider, HookRegistry, MessageAddedEvent

PAN = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")
EMAIL = re.compile(r"\b([A-Za-z0-9])[A-Za-z0-9._%+-]*@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b")
PHONE = re.compile(r"(?<![\w-])(?:\+61[ -]?|\(0\d\)[ -]?|0)[2-478](?:[ -]?\d){7,8}(?![\w-])")


def _mask_pan(m: re.Match[str]) -> str:
    digits = re.sub(r"\D", "", m.group(0))
    return m.group(0) if len(digits) < 13 else f"[CARD ****{digits[-4:]}]"


def redact(text: str) -> str:
    text = PAN.sub(_mask_pan, text)
    text = EMAIL.sub(lambda m: f"{m.group(1)}***@{m.group(2)}", text)
    return PHONE.sub("[PHONE]", text)


def redact_message(message: dict[str, Any]) -> int:
    changed = 0
    for block in message.get("content", []):
        if "text" in block:
            new = redact(block["text"])
            changed += new != block["text"]
            block["text"] = new
        for inner in block.get("toolResult", {}).get("content", []):
            if "text" in inner:
                new = redact(inner["text"])
                changed += new != inner["text"]
                inner["text"] = new
    return changed


class RedactionHook(HookProvider):
    def __init__(self) -> None:
        self.redactions = 0

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(MessageAddedEvent, self.on_message)

    def on_message(self, event: MessageAddedEvent) -> None:
        self.redactions += redact_message(event.message)  # type: ignore[arg-type]
