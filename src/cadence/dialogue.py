"""Heuristic detection of conversational repair, fallback, and handoff.

Every other signal cadence emits is a timing measurement, and timing is easy
to be certain about. These three are attempts at *semantic* quality — whether
the conversation actually worked — and they are inherently fuzzier.

The approach is deliberately simple: phrase matching over transcripts. That
choice is worth defending, because the obvious objection is "why not use a
model?"

* It is **free and instant**. Running a classifier over every utterance of
  every call would cost more than the agent itself and add latency to the path
  cadence exists to measure.
* It is **inspectable**. When repair rate spikes, you can read the pattern
  that fired and judge it yourself. A model's judgement is not auditable at
  3am.
* It is **honest about being approximate**. See the limitations below.

**Limitations, stated plainly.** This will miss politely-worded repairs ("I
think there may have been a misunderstanding"), miss every repair in a
language it has no patterns for, and occasionally fire on someone quoting a
phrase rather than using it. It is therefore a **trend instrument, not a
verdict on any single turn**. A repair rate moving from 6% to 14% after a
deploy is real signal; one turn flagged as a repair is a hint.

Patterns are overridable so a deployment can tune them to its own domain and
language without forking.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import semconv

# --------------------------------------------------------------------------
# Repair: the user having to fix a failed exchange
# --------------------------------------------------------------------------

_REPETITION = [
    r"\bi (?:already |just )?said\b",
    r"\blike i said\b",
    r"\bi told you\b",
    r"\bagain,? i\b",
    r"\bfor the (?:second|third|last) time\b",
]

_CORRECTION = [
    r"\bno,? (?:i|that|it)\b",
    r"\bthat'?s not (?:what|right|correct)\b",
    r"\bthat'?s wrong\b",
    r"\bi (?:didn'?t|did not) say\b",
    r"\bi meant\b",
    r"\bnot (?:that|what) i\b",
    r"\byou (?:misunderstood|got that wrong)\b",
    r"\bwrong\b.{0,20}\b(?:one|thing|answer)\b",
]

_CLARIFICATION = [
    r"\b(?:can|could) you (?:repeat|say that again)\b",
    r"\bsay (?:that )?again\b",
    r"\bwhat did you say\b",
    r"\bi (?:didn'?t|did not) (?:catch|hear|understand) (?:that|you)\b",
    r"^\s*(?:what|sorry|pardon|huh)\s*\?*\s*$",
    r"\bcome again\b",
]

# --------------------------------------------------------------------------
# Fallback: the agent giving up
# --------------------------------------------------------------------------

_NOT_UNDERSTOOD = [
    r"\bi (?:didn'?t|did not) (?:quite )?(?:catch|understand|get) that\b",
    r"\bi'?m not sure (?:i understand|what you mean)\b",
    r"\bcould you (?:rephrase|repeat) that\b",
    r"\bsorry,? i (?:didn'?t|don'?t) understand\b",
]

_NO_CAPABILITY = [
    r"\bi (?:can'?t|cannot|am not able to) help with\b",
    r"\bi (?:don'?t|do not) have (?:access to|that information)\b",
    r"\bthat'?s (?:outside|beyond) (?:my|what i)\b",
    r"\bi'?m (?:not able|unable) to (?:do|assist)\b",
]

_HANDOFF = [
    r"\b(?:let me |i'?ll )?(?:transfer|connect) you\b",
    r"\bspeak (?:to|with) (?:a|an) (?:human|agent|representative|person)\b",
    r"\bhanding (?:you )?(?:off|over)\b",
    r"\bput you through to\b",
    r"\bone of (?:my|our) colleagues\b",
]


def _compile(patterns: list[str]) -> list[re.Pattern[str]]:
    return [re.compile(p, re.IGNORECASE) for p in patterns]


@dataclass(slots=True)
class DialogueClassifier:
    """Classifies utterances. Instantiate once per session and reuse."""

    repetition: list[re.Pattern[str]] = field(
        default_factory=lambda: _compile(_REPETITION)
    )
    correction: list[re.Pattern[str]] = field(
        default_factory=lambda: _compile(_CORRECTION)
    )
    clarification: list[re.Pattern[str]] = field(
        default_factory=lambda: _compile(_CLARIFICATION)
    )
    not_understood: list[re.Pattern[str]] = field(
        default_factory=lambda: _compile(_NOT_UNDERSTOOD)
    )
    no_capability: list[re.Pattern[str]] = field(
        default_factory=lambda: _compile(_NO_CAPABILITY)
    )
    handoff: list[re.Pattern[str]] = field(default_factory=lambda: _compile(_HANDOFF))

    def classify_user(self, text: str) -> str | None:
        """Return a repair type, or None.

        Ordered most-specific first: a clarification request is also often a
        correction, and reporting the more precise one is more useful.
        """
        if not text or not text.strip():
            return None
        for patterns, label in (
            (self.clarification, semconv.RepairType.CLARIFICATION_REQUEST),
            (self.correction, semconv.RepairType.CORRECTION),
            (self.repetition, semconv.RepairType.REPETITION),
        ):
            if any(p.search(text) for p in patterns):
                return label
        return None

    def classify_agent(self, text: str) -> str | None:
        """Return a fallback reason, or None.

        Handoff is checked first: "I can't help with that, let me transfer you"
        is a handoff, and counting it as a capability gap would understate the
        transfer rate, which is the one an SLO is written against.
        """
        if not text or not text.strip():
            return None
        for patterns, label in (
            (self.handoff, semconv.FallbackReason.HANDOFF),
            (self.no_capability, semconv.FallbackReason.NO_CAPABILITY),
            (self.not_understood, semconv.FallbackReason.NOT_UNDERSTOOD),
        ):
            if any(p.search(text) for p in patterns):
                return label
        return None


DEFAULT_CLASSIFIER = DialogueClassifier()
