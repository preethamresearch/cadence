"""The agent's self-observation tools.

These are what make the demo a closed loop: cadence writes the agent's turns
into SigNoz, and these tools read them back so the agent can answer questions
about its own performance out loud.

Two sources, deliberately:

* **SigNoz** is the real answer -- aggregated across every session, surviving
  restarts, and queried the same way a human SRE would.
* **Live in-process stats** cover the first minute of a session, before the
  metric reader has flushed and the backend has ingested.

The tool always reports which source it used. An agent that says "p95 is 380ms"
when it actually has no data is worse than one that says "SigNoz has nothing
yet; from this session it's about 380ms."
"""

from __future__ import annotations

import logging
import statistics
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from .signoz import SigNozClient, latest_value, total_over_window

logger = logging.getLogger(__name__)


@dataclass
class LiveStats:
    """Rolling in-process view of the current session.

    Fed from the recorder's event hook, so it is derived from exactly the same
    signals as the exported telemetry rather than being a parallel accounting.
    """

    ttfa_samples: deque[float] = field(default_factory=lambda: deque(maxlen=200))
    barge_in_offsets: deque[float] = field(default_factory=lambda: deque(maxlen=200))
    turn_count: int = 0
    interrupted_turns: int = 0
    tool_calls: int = 0
    agent_audio_ms: float = 0.0
    tokens: dict[str, int] = field(default_factory=dict)

    def on_event(self, kind: str, data: dict[str, Any]) -> None:
        if kind == "ttfa" and data.get("ttfa_ms") is not None:
            self.ttfa_samples.append(float(data["ttfa_ms"]))
        elif kind == "barge_in":
            self.barge_in_offsets.append(float(data.get("offset_ms") or 0.0))
        elif kind == "turn_end":
            self.turn_count += 1
            if data.get("interrupted"):
                self.interrupted_turns += 1
            self.tool_calls += int(data.get("tool_calls") or 0)
            self.agent_audio_ms += float(data.get("agent_audio_ms") or 0.0)
        elif kind == "usage":
            for direction, key in (("input", "prompt_tokens_details"),
                                   ("output", "response_tokens_details")):
                for entry in data.get(key) or []:
                    modality = (entry.get("modality") or "unspecified").lower()
                    count = entry.get("token_count") or 0
                    label = f"{direction}/{modality}"
                    self.tokens[label] = self.tokens.get(label, 0) + int(count)

    def percentile(self, samples: list[float], pct: float) -> float | None:
        if not samples:
            return None
        if len(samples) == 1:
            return samples[0]
        ordered = sorted(samples)
        # Nearest-rank; with demo-sized samples an interpolated percentile
        # implies more precision than the data supports.
        index = min(len(ordered) - 1, max(0, round(pct / 100 * len(ordered)) - 1))
        return ordered[index]


class ObservabilityTools:
    """Dispatches the agent's tool calls against SigNoz and live stats."""

    def __init__(self, client: SigNozClient | None, stats: LiveStats) -> None:
        self.client = client
        self.stats = stats

    async def dispatch(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        handler = getattr(self, f"_{name}", None)
        if handler is None:
            return {"error": f"unknown tool {name}"}
        try:
            return await handler(**(args or {}))
        except TypeError as exc:
            return {"error": f"bad arguments for {name}: {exc}"}
        except Exception as exc:  # a failing tool must not kill the turn
            logger.exception("tool %s failed", name)
            return {"error": str(exc)}

    # ------------------------------------------------------------------

    async def _get_response_latency(self, window_minutes: int = 15) -> dict[str, Any]:
        """Time to first audio -- how long the user waited in silence."""
        if self.client:
            p95 = await self.client.query_histogram_percentile(
                "voice.turn.time_to_first_audio", "p95", window_minutes=window_minutes
            )
            p50 = await self.client.query_histogram_percentile(
                "voice.turn.time_to_first_audio", "p50", window_minutes=window_minutes
            )
            v95, v50 = latest_value(p95), latest_value(p50)
            if v95 is not None:
                return {
                    "source": "signoz",
                    "window_minutes": window_minutes,
                    "p95_ms": round(v95, 1),
                    "p50_ms": round(v50, 1) if v50 is not None else None,
                    "verdict": _latency_verdict(v95),
                }

        samples = list(self.stats.ttfa_samples)
        if not samples:
            return {
                "source": "none",
                "note": "No turns recorded yet -- say something first.",
            }
        v95 = self.stats.percentile(samples, 95)
        return {
            "source": "live_session",
            "note": "SigNoz has not ingested this session yet; these are live in-process numbers.",
            "samples": len(samples),
            "p95_ms": round(v95, 1) if v95 else None,
            "p50_ms": round(statistics.median(samples), 1),
            "fastest_ms": round(min(samples), 1),
            "slowest_ms": round(max(samples), 1),
            "verdict": _latency_verdict(v95 or 0),
        }

    async def _get_interruption_stats(self, window_minutes: int = 15) -> dict[str, Any]:
        """How often the user talks over the agent, and how far in."""
        if self.client:
            count = await self.client.query_metric(
                "voice.barge_in.count", window_minutes=window_minutes,
                time_aggregation="rate", space_aggregation="sum",
            )
            if count:
                total = total_over_window(count)
                offset = await self.client.query_histogram_percentile(
                    "voice.barge_in.offset", "p50", window_minutes=window_minutes
                )
                median_offset = latest_value(offset)
                return {
                    "source": "signoz",
                    "window_minutes": window_minutes,
                    "barge_ins": round(total, 1),
                    "median_offset_ms": round(median_offset, 1) if median_offset else None,
                    "interpretation": _barge_in_interpretation(median_offset),
                }

        offsets = list(self.stats.barge_in_offsets)
        rate = (
            self.stats.interrupted_turns / self.stats.turn_count
            if self.stats.turn_count else 0.0
        )
        median_offset = statistics.median(offsets) if offsets else None
        return {
            "source": "live_session",
            "barge_ins": len(offsets),
            "turns": self.stats.turn_count,
            "interruption_rate": round(rate, 3),
            "median_offset_ms": round(median_offset, 1) if median_offset else None,
            "interpretation": _barge_in_interpretation(median_offset),
        }

    async def _get_token_spend(self, window_minutes: int = 15) -> dict[str, Any]:
        """Token consumption split by modality -- audio usually dominates."""
        if self.client:
            series = await self.client.query_metric(
                "gen_ai.client.token.usage", window_minutes=window_minutes,
                time_aggregation="rate", space_aggregation="sum",
                group_by=["gen_ai.token.type", "gen_ai.token.modality"],
            )
            if series:
                breakdown = {}
                for entry in series:
                    labels = entry["labels"]
                    key = "/".join(
                        str(labels.get(k, "?"))
                        for k in ("gen_ai.token.type", "gen_ai.token.modality")
                    )
                    breakdown[key] = round(sum(v for _, v in entry["points"]), 1)
                return {"source": "signoz", "window_minutes": window_minutes,
                        "tokens_by_modality": breakdown,
                        "total": round(sum(breakdown.values()), 1)}

        return {
            "source": "live_session",
            "tokens_by_modality": dict(self.stats.tokens),
            "total": sum(self.stats.tokens.values()),
            "agent_speaking_seconds": round(self.stats.agent_audio_ms / 1000, 1),
        }

    async def _get_session_summary(self) -> dict[str, Any]:
        """Everything about the conversation happening right now."""
        samples = list(self.stats.ttfa_samples)
        return {
            "source": "live_session",
            "turns": self.stats.turn_count,
            "interrupted_turns": self.stats.interrupted_turns,
            "tool_calls": self.stats.tool_calls,
            "median_ttfa_ms": round(statistics.median(samples), 1) if samples else None,
            "agent_speaking_seconds": round(self.stats.agent_audio_ms / 1000, 1),
        }


def _latency_verdict(p95_ms: float) -> str:
    # Thresholds from conversational-turn research: humans read a gap beyond
    # roughly half a second as hesitation, and beyond a second as a fault.
    if p95_ms <= 0:
        return "no data"
    if p95_ms < 500:
        return "healthy -- feels immediate"
    if p95_ms < 800:
        return "acceptable, but the pause is noticeable"
    if p95_ms < 1500:
        return "degraded -- users will think it did not hear them"
    return "unhealthy -- long enough that users start repeating themselves"


def _barge_in_interpretation(median_offset_ms: float | None) -> str:
    if median_offset_ms is None:
        return "no interruptions recorded"
    if median_offset_ms < 400:
        return (
            "interruptions land almost immediately, which usually means voice "
            "activity detection is firing on background noise rather than real speech"
        )
    if median_offset_ms < 2000:
        return "normal conversational interruption"
    return (
        "users are cutting in late, which usually means replies are too long "
        "and should be shortened"
    )


# --------------------------------------------------------------------------
# Gemini function declarations
# --------------------------------------------------------------------------

TOOL_DECLARATIONS: list[dict[str, Any]] = [
    {
        "name": "get_response_latency",
        "description": (
            "Look up your own response latency (time to first audio) from the "
            "observability backend. Use whenever asked how fast, how slow, or "
            "how responsive you have been."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "window_minutes": {
                    "type": "INTEGER",
                    "description": "How far back to look. Defaults to 15.",
                }
            },
        },
    },
    {
        "name": "get_interruption_stats",
        "description": (
            "Look up how often users have interrupted you mid-sentence, and how "
            "far into your replies. Use when asked about interruptions, barge-in, "
            "or whether you talk too much."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "window_minutes": {"type": "INTEGER", "description": "Defaults to 15."}
            },
        },
    },
    {
        "name": "get_token_spend",
        "description": (
            "Look up your token consumption broken down by modality (audio, "
            "video, text). Use when asked about cost, spend, or tokens."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "window_minutes": {"type": "INTEGER", "description": "Defaults to 15."}
            },
        },
    },
    {
        "name": "get_session_summary",
        "description": (
            "Summarise the conversation currently in progress: turns taken, "
            "interruptions, tool calls, and typical latency."
        ),
        "parameters": {"type": "OBJECT", "properties": {}},
    },
]


SYSTEM_INSTRUCTION = """\
You are Cadence, a voice agent that can see its own telemetry.

Every turn of this conversation is being traced by the cadence OpenTelemetry \
instrumentation and shipped to SigNoz. You have tools that read that telemetry \
back, so when someone asks how you are performing, you look it up rather than \
guessing.

How to behave:
- You are speaking out loud. Keep answers to one or two sentences. Never read \
out raw JSON, field names, or lists of numbers.
- When you report a measurement, give the number and what it means. "About \
340 milliseconds, which is fast enough to feel immediate" -- not "p95 equals 340".
- If a tool says the data came from the live session rather than SigNoz, say so \
briefly and naturally: "SigNoz hasn't picked this session up yet, but right now \
it's around 340 milliseconds."
- If someone interrupts you, stop immediately and respond to what they said.
- If asked what you are or how you work, explain in one or two sentences that \
you are a demo of cadence, which traces real-time voice agents -- turn \
boundaries, time to first audio, and interruptions -- and sends it all to SigNoz.
- Be warm and direct. No filler openers like "Certainly" or "Great question".
"""
