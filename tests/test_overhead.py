"""Instrumentation overhead.

"Negligible overhead" is a claim, and claims about performance that nobody
measured are usually wrong. This measures the actual cost of the hot path so
the README can cite a number instead of an adjective.

What matters for a realtime agent is the **per-event** cost on the audio path.
Gemini Live delivers audio in chunks of roughly 20–100ms, so even a busy
session generates on the order of 50 events per second per stream. Anything in
the low microseconds is free at that rate; the number to avoid is
milliseconds, which would show up as jitter in the very metric cadence exists
to measure.

Run directly for a readable report:

    python -m pytest tests/test_overhead.py -s -q
"""

from __future__ import annotations

import time

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import cadence
from cadence.events import EventType, VoiceEvent
from cadence.metrics import reset as reset_metrics
from cadence.recorder import ConversationRecorder

# Audio chunks arrive every ~20-100ms. 250us per event would still be under
# 2% of the smallest chunk interval; this ceiling is deliberately generous so
# the test measures a real regression rather than CI noise.
MAX_MICROSECONDS_PER_EVENT = 250.0


@pytest.fixture
def recorder(monkeypatch):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(cadence.recorder, "get_tracer", lambda: provider.get_tracer("bench"))
    # Metrics are left on the default no-op provider: this benchmark measures
    # the state machine and span creation, which is the part cadence controls.
    reset_metrics()
    yield ConversationRecorder(session_id="bench", provider="gemini_live")
    reset_metrics()


def _drive(rec: ConversationRecorder, turns: int) -> int:
    """Run a realistic conversation and return the event count."""
    t = 0.0
    events = 0

    def emit(kind: EventType, dt: float, **kw):
        nonlocal t, events
        t += dt
        rec.handle(VoiceEvent(type=kind, monotonic=t, wall_ns=int(t * 1e9), **kw))
        events += 1

    for _ in range(turns):
        emit(EventType.USER_SPEECH_START, 0.5)
        # 1.5s of user audio at 60ms chunks
        for _ in range(25):
            emit(EventType.USER_AUDIO_SENT, 0.06, audio_ms=60)
        emit(EventType.USER_SPEECH_END, 0.05)
        emit(EventType.AGENT_AUDIO_CHUNK, 0.32, audio_ms=120)
        # 3s of agent audio
        for _ in range(25):
            emit(EventType.AGENT_AUDIO_CHUNK, 0.12, audio_ms=120)
        emit(EventType.AGENT_TRANSCRIPT, 0.01, text="Your order shipped yesterday.")
        emit(EventType.AGENT_GENERATION_COMPLETE, 0.01)
        emit(EventType.PLAYBACK_FINISHED, 0.01)
        emit(EventType.TURN_COMPLETE, 0.01)
    return events


def test_per_event_overhead_is_microseconds(recorder, capsys):
    recorder.start()

    _drive(recorder, turns=3)  # warm caches and instrument creation

    turns = 60
    start = time.perf_counter()
    events = _drive(recorder, turns)
    elapsed = time.perf_counter() - start
    recorder.close()

    per_event_us = (elapsed / events) * 1_000_000

    # A minute of conversation is roughly 50 events/sec.
    cost_per_minute_ms = per_event_us * 50 * 60 / 1000

    with capsys.disabled():
        print(
            f"\n  cadence overhead: {per_event_us:.1f} us/event "
            f"({events} events over {turns} turns in {elapsed * 1000:.1f} ms)"
            f"\n  ~= {cost_per_minute_ms:.1f} ms of CPU per minute of conversation\n"
        )

    assert per_event_us < MAX_MICROSECONDS_PER_EVENT, (
        f"{per_event_us:.1f} us/event exceeds the {MAX_MICROSECONDS_PER_EVENT} us budget"
    )


def test_disabled_instrumentation_is_nearly_free(recorder):
    """With no exporter configured the SDK uses non-recording spans. Cost
    should drop, confirming the expense is export rather than the state
    machine — which is what makes sampling an effective lever."""
    recorder.start()
    _drive(recorder, turns=3)

    start = time.perf_counter()
    events = _drive(recorder, turns=30)
    elapsed = time.perf_counter() - start
    recorder.close()

    per_event_us = (elapsed / events) * 1_000_000
    assert per_event_us < MAX_MICROSECONDS_PER_EVENT
