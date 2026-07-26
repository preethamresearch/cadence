"""State-machine tests for the duplex turn recorder.

These drive the recorder with synthetic events and assert on the spans that
come out, so the turn model can be verified without an API key, a microphone,
or a network. Every scenario here is one I actually got wrong at least once
while writing the recorder.
"""

from __future__ import annotations

import time

import pytest
from opentelemetry import trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import cadence
from cadence import semconv
from cadence.events import EventType, VoiceEvent
from cadence.metrics import reset as reset_metrics
from cadence.recorder import ConversationRecorder


@pytest.fixture
def harness(monkeypatch):
    """Fresh providers per test, with in-memory exporters."""
    exporter = InMemorySpanExporter()
    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(SimpleSpanProcessor(exporter))

    reader = InMemoryMetricReader()
    meter_provider = MeterProvider(metric_readers=[reader])

    # Each module does `from .tracing import get_tracer/get_meter`, binding its
    # own reference at import time, so patching cadence.tracing alone is not
    # enough -- every importing module has to be patched too.
    monkeypatch.setattr(cadence.tracing, "get_tracer", lambda: tracer_provider.get_tracer("test"))
    monkeypatch.setattr(cadence.tracing, "get_meter", lambda: meter_provider.get_meter("test"))
    monkeypatch.setattr(cadence.recorder, "get_tracer", lambda: tracer_provider.get_tracer("test"))
    monkeypatch.setattr(cadence.metrics, "get_meter", lambda: meter_provider.get_meter("test"))
    reset_metrics()

    yield exporter, reader
    reset_metrics()


def ev(kind: EventType, t: float, **kw) -> VoiceEvent:
    """Build an event at an explicit monotonic time so latency assertions are
    exact rather than dependent on how fast the test machine is."""
    return VoiceEvent(type=kind, monotonic=t, wall_ns=int(t * 1e9), **kw)


def spans_by_name(exporter):
    out: dict[str, list] = {}
    for span in exporter.get_finished_spans():
        out.setdefault(span.name, []).append(span)
    return out


def test_clean_turn_produces_full_span_tree(harness):
    exporter, _ = harness
    rec = ConversationRecorder(session_id="s1", model="gemini-3.1-flash-live-preview")
    rec.start()

    rec.handle(ev(EventType.USER_SPEECH_START, 0.0))
    rec.handle(ev(EventType.USER_AUDIO_SENT, 0.5, audio_ms=500))
    rec.handle(ev(EventType.USER_SPEECH_END, 1.0))
    rec.handle(ev(EventType.AGENT_AUDIO_CHUNK, 1.4, audio_ms=100))
    rec.handle(ev(EventType.AGENT_AUDIO_CHUNK, 1.5, audio_ms=100))
    rec.handle(ev(EventType.AGENT_GENERATION_COMPLETE, 1.6))
    rec.handle(ev(EventType.TURN_COMPLETE, 2.0, reason="completed"))
    rec.close()

    names = spans_by_name(exporter)
    assert semconv.SPAN_CONVERSATION in names
    assert semconv.SPAN_TURN in names
    assert semconv.SPAN_USER_UTTERANCE in names
    assert semconv.SPAN_AGENT_UTTERANCE in names
    assert semconv.SPAN_CHAT in names

    turn = names[semconv.SPAN_TURN][0]
    assert turn.attributes[semconv.VOICE_TURN_END_REASON] == "completed"
    assert turn.attributes[semconv.VOICE_TURN_INTERRUPTED] is False
    assert turn.attributes[semconv.VOICE_TURN_INDEX] == 0

    # All turn children share the turn's span id as parent, and the whole
    # session shares one trace.
    conversation = names[semconv.SPAN_CONVERSATION][0]
    assert turn.parent.span_id == conversation.context.span_id
    for child_name in (semconv.SPAN_USER_UTTERANCE, semconv.SPAN_AGENT_UTTERANCE,
                       semconv.SPAN_CHAT):
        assert names[child_name][0].parent.span_id == turn.context.span_id
    trace_ids = {s.context.trace_id for s in exporter.get_finished_spans()}
    assert len(trace_ids) == 1


def test_ttfa_measured_from_end_of_user_speech(harness):
    """TTFA is the silence the user sat through: speech end -> first audio out."""
    exporter, _ = harness
    rec = ConversationRecorder(session_id="s2")
    rec.start()

    rec.handle(ev(EventType.USER_SPEECH_START, 10.0))
    rec.handle(ev(EventType.USER_SPEECH_END, 12.0))       # spoke for 2s
    rec.handle(ev(EventType.AGENT_AUDIO_CHUNK, 12.42, audio_ms=60))  # replied 420ms later
    rec.handle(ev(EventType.TURN_COMPLETE, 14.0))
    rec.close()

    turn = spans_by_name(exporter)[semconv.SPAN_TURN][0]
    # 420ms, not 2420ms -- the 2s of user speech must not be counted.
    assert turn.attributes[semconv.VOICE_TURN_TTFA_MS] == pytest.approx(420.0, abs=1.0)


def test_agent_initiated_turn_has_no_ttfa(harness):
    """A proactive greeting has no user speech to measure from. A fabricated
    TTFA here would silently corrupt the p95, so it must be absent."""
    exporter, _ = harness
    rec = ConversationRecorder(session_id="s3")
    rec.start()

    rec.handle(ev(EventType.AGENT_AUDIO_CHUNK, 0.2, audio_ms=100))
    rec.handle(ev(EventType.TURN_COMPLETE, 1.0))
    rec.close()

    turn = spans_by_name(exporter)[semconv.SPAN_TURN][0]
    assert semconv.VOICE_TURN_TTFA_MS not in turn.attributes
    assert turn.attributes["voice.turn.agent_initiated"] is True


def test_barge_in_records_offset_and_opens_new_turn(harness):
    """Interrupting mid-reply ends the turn and hands the floor to the user."""
    exporter, _ = harness
    rec = ConversationRecorder(session_id="s4")
    rec.start()

    rec.handle(ev(EventType.USER_SPEECH_START, 0.0))
    rec.handle(ev(EventType.USER_SPEECH_END, 1.0))
    rec.handle(ev(EventType.AGENT_AUDIO_CHUNK, 1.3, audio_ms=100))   # agent starts
    rec.handle(ev(EventType.USER_SPEECH_START, 2.1))                 # cut in 800ms later
    rec.handle(ev(EventType.USER_SPEECH_END, 2.6))
    rec.handle(ev(EventType.AGENT_AUDIO_CHUNK, 2.9, audio_ms=100))
    rec.handle(ev(EventType.TURN_COMPLETE, 3.5))
    rec.close()

    names = spans_by_name(exporter)
    turns = sorted(names[semconv.SPAN_TURN], key=lambda s: s.attributes[semconv.VOICE_TURN_INDEX])
    assert len(turns) == 2, "barge-in should close the interrupted turn and start a new one"

    first, second = turns
    assert first.attributes[semconv.VOICE_TURN_INTERRUPTED] is True
    assert first.attributes[semconv.VOICE_TURN_END_REASON] == semconv.EndReason.INTERRUPTED
    assert second.attributes[semconv.VOICE_TURN_INTERRUPTED] is False

    # The barge-in event lands on the utterance it cut off, 800ms in.
    agent_utt = names[semconv.SPAN_AGENT_UTTERANCE][0]
    barge_events = [e for e in agent_utt.events if e.name == semconv.EVENT_BARGE_IN]
    assert len(barge_events) == 1
    assert barge_events[0].attributes[semconv.VOICE_BARGE_IN_OFFSET_MS] == pytest.approx(800.0, abs=1.0)
    assert agent_utt.attributes[semconv.VOICE_UTTERANCE_TRUNCATED] is True


def test_server_interrupt_after_client_detection_is_not_double_counted(harness):
    """Gemini echoes an `interrupted` flag after its own VAD fires. We may have
    already recorded the barge-in from inbound audio; counting both would
    double the barge-in rate on every dashboard."""
    exporter, reader = harness
    rec = ConversationRecorder(session_id="s5")
    rec.start()

    rec.handle(ev(EventType.USER_SPEECH_START, 0.0))
    rec.handle(ev(EventType.USER_SPEECH_END, 1.0))
    rec.handle(ev(EventType.AGENT_AUDIO_CHUNK, 1.2, audio_ms=100))
    rec.handle(ev(EventType.USER_SPEECH_START, 1.9))   # client-side detection
    rec.handle(ev(EventType.INTERRUPTED, 2.0))         # server confirmation
    rec.handle(ev(EventType.TURN_COMPLETE, 2.5))
    rec.close()

    agent_utts = spans_by_name(exporter)[semconv.SPAN_AGENT_UTTERANCE]
    total_barge_events = sum(
        len([e for e in s.events if e.name == semconv.EVENT_BARGE_IN]) for s in agent_utts
    )
    assert total_barge_events == 1


def test_tool_call_during_playback_is_flagged_mid_stream(harness):
    exporter, _ = harness
    rec = ConversationRecorder(session_id="s6")
    rec.start()

    rec.handle(ev(EventType.USER_SPEECH_START, 0.0))
    rec.handle(ev(EventType.USER_SPEECH_END, 1.0))
    rec.handle(ev(EventType.AGENT_AUDIO_CHUNK, 1.3, audio_ms=100))
    rec.handle(ev(EventType.TOOL_CALL, 1.5, name="get_my_latency", call_id="c1"))
    rec.handle(ev(EventType.TOOL_RESULT, 1.9, call_id="c1"))
    rec.handle(ev(EventType.TURN_COMPLETE, 2.5))
    rec.close()

    tool_spans = spans_by_name(exporter)[semconv.SPAN_EXECUTE_TOOL]
    assert len(tool_spans) == 1
    assert tool_spans[0].attributes[semconv.GEN_AI_TOOL_NAME] == "get_my_latency"
    assert tool_spans[0].attributes["voice.tool.mid_stream"] is True

    turn = spans_by_name(exporter)[semconv.SPAN_TURN][0]
    assert turn.attributes[semconv.VOICE_TURN_TOOL_CALL_COUNT] == 1


def test_unclosed_spans_are_reaped_on_session_close(harness):
    """A dropped socket mid-turn must not leak unended spans, or the trace
    never appears in the backend at all."""
    exporter, _ = harness
    rec = ConversationRecorder(session_id="s7")
    rec.start()

    rec.handle(ev(EventType.USER_SPEECH_START, 0.0))
    rec.handle(ev(EventType.USER_SPEECH_END, 1.0))
    rec.handle(ev(EventType.AGENT_AUDIO_CHUNK, 1.2, audio_ms=100))
    rec.handle(ev(EventType.TOOL_CALL, 1.4, name="stuck_tool", call_id="c9"))
    rec.close()  # socket dies here

    names = spans_by_name(exporter)
    assert semconv.SPAN_TURN in names
    assert semconv.SPAN_AGENT_UTTERANCE in names
    assert semconv.SPAN_EXECUTE_TOOL in names
    turn = names[semconv.SPAN_TURN][0]
    assert turn.attributes[semconv.VOICE_TURN_END_REASON] == semconv.EndReason.SESSION_CLOSED


def test_metrics_recorded(harness):
    exporter, reader = harness
    rec = ConversationRecorder(session_id="s8")
    rec.start()
    rec.handle(ev(EventType.USER_SPEECH_START, 0.0))
    rec.handle(ev(EventType.USER_SPEECH_END, 1.0))
    rec.handle(ev(EventType.AGENT_AUDIO_CHUNK, 1.35, audio_ms=200))
    rec.handle(ev(EventType.TURN_COMPLETE, 2.0))
    rec.close()

    data = reader.get_metrics_data()
    recorded = set()
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for metric in sm.metrics:
                recorded.add(metric.name)

    assert semconv.METRIC_TTFA in recorded
    assert semconv.METRIC_TURN_DURATION in recorded
    assert semconv.METRIC_TURN_COUNT in recorded
    assert semconv.METRIC_AUDIO_OUTPUT_SECONDS in recorded


def test_instrumentation_never_raises_into_the_agent(harness):
    """An exception inside cadence must not propagate into the audio loop."""
    exporter, _ = harness
    rec = ConversationRecorder(session_id="s9")
    rec.start()
    rec.on_event = lambda kind, data: (_ for _ in ()).throw(RuntimeError("ui blew up"))

    rec.handle(ev(EventType.USER_SPEECH_START, 0.0))  # must not raise
    rec.handle(ev(EventType.TURN_COMPLETE, 1.0))
    rec.close()
