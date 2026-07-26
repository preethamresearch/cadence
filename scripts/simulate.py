"""Traffic simulator.

Drives realistic conversations through the **real** ``ConversationRecorder``,
the real OpenTelemetry SDK, and a real OTLP exporter into a real SigNoz. The
only synthetic part is the audio timing — every span, metric, attribute and
export path is production code.

Why this exists: dashboards, SLOs and regression detection cannot be built or
demonstrated against an empty backend, and a single hand-held demo call
produces three turns. This produces the hundreds of sessions those features
are designed for.

It deliberately encodes a **story**, not noise:

* ``prompt v16`` is healthy — TTFA around 300ms, few interruptions.
* ``prompt v17`` is a regression — a prompt change made replies longer and
  slower. TTFA rises past the 350ms objective, users interrupt more, and
  repair rate climbs because they are being talked over.
* ``pstn`` sessions carry more interruptions than ``websocket`` ones
  regardless of prompt, because line noise trips voice activity detection.

That gives the analysis layer two genuinely different root causes to
distinguish — a deploy regression and a channel-specific fault — which is
exactly the discrimination an operator needs and a single aggregate cannot
make.

    python scripts/simulate.py --sessions 120
"""

from __future__ import annotations

import argparse
import os
import random
import sys
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import cadence  # noqa: E402
from cadence import semconv  # noqa: E402
from cadence.events import EventType, VoiceEvent  # noqa: E402
from cadence.recorder import ConversationRecorder  # noqa: E402


@dataclass(frozen=True)
class Profile:
    """A behavioural cohort. The differences between profiles are the signal
    the analysis layer is meant to surface."""

    prompt_version: str
    transport: str
    ttfa_mean: float
    ttfa_spread: float
    barge_in_rate: float
    repair_rate: float
    handoff_rate: float
    reply_seconds: tuple[float, float]
    weight: float


PROFILES = [
    # Healthy baseline.
    Profile("v16", semconv.Transport.WEBSOCKET, 295, 70, 0.18, 0.05, 0.07, (2.0, 4.5), 0.34),
    Profile("v16", semconv.Transport.PSTN, 330, 95, 0.34, 0.07, 0.09, (2.0, 4.5), 0.16),
    # The regression: slower to start, longer replies, so more interruptions
    # and more repairs.
    Profile("v17", semconv.Transport.WEBSOCKET, 470, 150, 0.42, 0.12, 0.13, (4.0, 8.0), 0.34),
    Profile("v17", semconv.Transport.PSTN, 520, 180, 0.61, 0.16, 0.15, (4.0, 8.0), 0.16),
]

USER_LINES = [
    "I need to change my delivery address",
    "What's the status of my order",
    "Can you check if this is still under warranty",
    "I want to speak about a refund",
    "When does my subscription renew",
]
REPAIR_LINES = [
    "No, that's not what I asked for",
    "I already said the address is wrong",
    "Sorry, can you say that again",
    "That's wrong, I meant the other one",
]
AGENT_LINES = [
    "I can help with that. Let me pull up your account.",
    "Your order shipped yesterday and arrives Thursday.",
    "That item is covered until March next year.",
]
HANDOFF_LINES = [
    "Let me transfer you to an agent who can help.",
    "I'll connect you with a representative now.",
]
FALLBACK_LINES = [
    "I'm not sure I understand what you mean.",
    "I don't have access to that information.",
]


def pick(rng: random.Random, profiles: list[Profile]) -> Profile:
    return rng.choices(profiles, weights=[p.weight for p in profiles], k=1)[0]


def simulate_session(
    rng: random.Random,
    profile: Profile,
    index: int,
    *,
    start_wall_ns: int,
    spread_seconds: float,
) -> dict:
    """Run one conversation through the real recorder.

    Uses a synthetic monotonic clock so a hundred sessions of conversation
    take a second of wall time — the recorder derives every duration from the
    event timestamps, so the spans carry realistic values regardless.
    """
    rec = ConversationRecorder(
        session_id=f"sim-{index:05d}",
        provider="gemini_live",
        model="gemini-3.1-flash-live-preview",
        transport=profile.transport,
        prompt_version=profile.prompt_version,
        agent_version="cadence-demo-1.0",
    )
    # Deliberately not calling rec.start() here: handle() opens the session
    # span lazily on the first event, which means it is stamped with that
    # event's timestamp. Starting eagerly would open the session span at
    # "now" and close it in the simulated past, producing a negative
    # duration that wraps to nonsense.

    # Two clocks, deliberately.
    #
    # `monotonic` drives duration arithmetic and only differences matter, so a
    # synthetic origin is fine. `wall_ns` becomes the span timestamp and must
    # be real epoch nanoseconds — passing monotonic here puts every span near
    # 1970 and yields durations in the billions of milliseconds.
    #
    # Sessions are also spread backwards across the window so the dashboards
    # show a time series with shape rather than one vertical spike.
    mono = 0.0
    wall_start_ns = start_wall_ns - int(spread_seconds * 1e9)

    def emit(kind: EventType, dt: float, **kw) -> None:
        nonlocal mono
        mono += dt
        rec.handle(
            VoiceEvent(
                type=kind,
                monotonic=mono,
                wall_ns=wall_start_ns + int(mono * 1e9),
                **kw,
            )
        )

    turns = rng.randint(3, 8)
    handed_off = False

    for turn_index in range(turns):
        # -- user speaks ------------------------------------------------
        speaking = rng.uniform(1.1, 3.0)
        is_repair = turn_index > 0 and rng.random() < profile.repair_rate
        text = rng.choice(REPAIR_LINES if is_repair else USER_LINES)

        emit(EventType.USER_SPEECH_START, rng.uniform(0.4, 1.6))
        emit(EventType.USER_TRANSCRIPT, 0.15, text=text)
        emit(EventType.USER_AUDIO_SENT, 0.0, audio_ms=speaking * 1000)
        emit(EventType.USER_SPEECH_END, speaking)

        # -- the silence ------------------------------------------------
        ttfa = max(90.0, rng.gauss(profile.ttfa_mean, profile.ttfa_spread))
        emit(EventType.AGENT_AUDIO_CHUNK, ttfa / 1000.0, audio_ms=120)

        # -- agent replies ----------------------------------------------
        reply = rng.uniform(*profile.reply_seconds)
        spoken = 0.12
        interrupted = rng.random() < profile.barge_in_rate

        # Occasional mid-utterance stall — the stutter signal.
        stall_at = reply * rng.uniform(0.3, 0.7) if rng.random() < 0.08 else None

        while spoken < reply:
            step = min(0.24, reply - spoken)
            gap = 0.9 if (stall_at and spoken < stall_at <= spoken + step) else step
            emit(EventType.AGENT_AUDIO_CHUNK, gap, audio_ms=step * 1000)
            spoken += step
            if interrupted and spoken > reply * rng.uniform(0.25, 0.7):
                break

        # Agent utterance text drives fallback/handoff classification.
        if not handed_off and rng.random() < profile.handoff_rate:
            emit(EventType.AGENT_TRANSCRIPT, 0.05, text=rng.choice(HANDOFF_LINES))
            handed_off = True
        elif rng.random() < 0.08:
            emit(EventType.AGENT_TRANSCRIPT, 0.05, text=rng.choice(FALLBACK_LINES))
        else:
            emit(EventType.AGENT_TRANSCRIPT, 0.05, text=rng.choice(AGENT_LINES))

        if rng.random() < 0.35:
            emit(EventType.TOOL_CALL, 0.1, name=rng.choice(
                ["lookup_order", "check_warranty", "get_account"]), call_id=f"c{turn_index}")
            emit(EventType.TOOL_RESULT, rng.uniform(0.08, 0.9), call_id=f"c{turn_index}")

        emit(EventType.USAGE, 0.02, payload={
            "prompt_token_count": int(rng.uniform(400, 1400)),
            "response_token_count": int(rng.uniform(200, 900)),
            "prompt_tokens_details": [
                {"modality": "AUDIO", "token_count": int(speaking * 25)},
                {"modality": "TEXT", "token_count": int(rng.uniform(40, 160))},
            ],
            "response_tokens_details": [
                {"modality": "AUDIO", "token_count": int(spoken * 25)},
            ],
        })

        if interrupted:
            # The next USER_SPEECH_START is what the recorder turns into a
            # barge-in; it closes this turn on its own.
            emit(EventType.USER_SPEECH_START, rng.uniform(0.05, 0.25))
            emit(EventType.PLAYBACK_FINISHED, rng.uniform(0.05, 0.3))
            emit(EventType.USER_SPEECH_END, rng.uniform(0.6, 1.4))
            emit(EventType.TURN_COMPLETE, 0.05, reason=semconv.EndReason.INTERRUPTED)
        else:
            emit(EventType.AGENT_GENERATION_COMPLETE, 0.05)
            emit(EventType.PLAYBACK_FINISHED, 0.05)
            emit(EventType.TURN_COMPLETE, 0.05, reason=semconv.EndReason.COMPLETED)

    rec.close()
    return rec.stats


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sessions", type=int, default=120)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument(
        "--window-minutes", type=float, default=25.0,
        help="Spread sessions back over this many minutes so dashboards "
             "show a trend rather than a single spike.",
    )
    parser.add_argument("--service", default=os.getenv("OTEL_SERVICE_NAME", "cadence-voice-agent"))
    parser.add_argument(
        "--endpoint",
        default=os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318"),
        help="OTLP HTTP endpoint. Local Foundry SigNoz listens on :4318.",
    )
    parser.add_argument("--ingestion-key", default=os.getenv("SIGNOZ_INGESTION_KEY"))
    args = parser.parse_args()

    cadence.configure(
        service_name=args.service,
        endpoint=args.endpoint,
        ingestion_key=args.ingestion_key,
        metric_export_interval_ms=5_000,
        force=True,
    )

    rng = random.Random(args.seed)
    now_ns = time.time_ns()
    window_seconds = args.window_minutes * 60.0
    totals = {"turns": 0, "barge_ins": 0, "repairs": 0, "fallbacks": 0, "handoffs": 0}

    print(f"simulating {args.sessions} sessions -> {args.endpoint}")
    for i in range(args.sessions):
        profile = pick(rng, PROFILES)
        # Oldest session furthest back, so the series reads left to right.
        spread = window_seconds * (1.0 - i / max(1, args.sessions - 1))
        stats = simulate_session(
            rng, profile, i, start_wall_ns=now_ns, spread_seconds=spread
        )
        totals["turns"] += stats["turns"]
        totals["barge_ins"] += stats["barge_ins"]
        totals["repairs"] += stats["repairs"]
        totals["fallbacks"] += stats["fallbacks"]
        totals["handoffs"] += int(stats["handoff"])
        if (i + 1) % 25 == 0:
            print(f"  {i + 1}/{args.sessions} sessions, {totals['turns']} turns")

    print("\nflushing telemetry…")
    cadence.shutdown()

    print(
        f"\ndone: {args.sessions} sessions, {totals['turns']} turns, "
        f"{totals['barge_ins']} barge-ins, {totals['repairs']} repairs, "
        f"{totals['fallbacks']} fallbacks, {totals['handoffs']} handoffs"
    )
    print("open SigNoz at http://localhost:8080")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
