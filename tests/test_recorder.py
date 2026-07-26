"""State-machine tests for the duplex turn recorder.

These drive the recorder with synthetic events and assert on the spans and
metrics that come out, so the turn model is verifiable without an API key, a
microphone, or a network.

Nearly every scenario here is one I got wrong at least once while writing the
recorder, which is why the assertions are specific about *values* rather than
just presence.
"""

from __future__ import annotations

import pytest
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
    # enough — every importing module has to be patched too.
    monkeypatch.setattr(cadence.tracing, "get_tracer", lambda: tracer_provider.get_tracer("t"))
    monkeypatch.setattr(cadence.tracing, "get_meter", lambda: meter_provider.get_meter("t"))
    monkeypatch.setattr(cadence.recorder, "get_tracer", lambda: tracer_provider.get_tracer("t"))
    monkeypatch.setattr(cadence.metrics, "get_meter", lambda: meter_provider.get_meter("t"))
    reset_metrics()

    yield exporter, reader
    reset_metrics()


def ev(kind: EventType, t: float, **kw) -> VoiceEvent:
    """Event at an explicit monotonic time, so latency assertions are exact
    rather than dependent on how fast the test machine is."""
    return VoiceEvent(type=kind, monotonic=t, wall_ns=int(t * 1e9), **kw)


def by_name(exporter):
    out: dict[str, list] = {}
    for span in exporter.get_finished_spans():
        out.setdefault(span.name, []).append(span)
    return out


def metric_names(reader) -> set[str]:
    data = reader.get_metrics_data()
    names = set()
    if data is None:
        return names
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for metric in sm.metrics:
                names.add(metric.name)
    return names


# ---------------------------------------------------------------- structure


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

    names = by_name(exporter)
    for expected in (
        semconv.SPAN_SESSION,
        semconv.SPAN_TURN,
        semconv.SPAN_USER_UTTERANCE,
        semconv.SPAN_AGENT_UTTERANCE,
        semconv.SPAN_CHAT,
    ):
        assert expected in names, f"missing {expected}"

    turn = names[semconv.SPAN_TURN][0]
    assert turn.attributes[semconv.TURN_END_REASON] == "completed"
    assert turn.attributes[semconv.TURN_INTERRUPTED] is False
    assert turn.attributes[semconv.TURN_INDEX] == 0

    session = names[semconv.SPAN_SESSION][0]
    assert session.attributes[semconv.ATTR_SCHEMA_VERSION] == semconv.SCHEMA_VERSION
    assert turn.parent.span_id == session.context.span_id
    for child in (semconv.SPAN_USER_UTTERANCE, semconv.SPAN_AGENT_UTTERANCE, semconv.SPAN_CHAT):
        assert names[child][0].parent.span_id == turn.context.span_id

    assert len({s.context.trace_id for s in exporter.get_finished_spans()}) == 1


def test_prompt_version_propagates_for_deploy_correlation(harness):
    """Without this you cannot attribute a regression to the deploy that
    caused it, which is the whole point of carrying it."""
    exporter, _ = harness
    rec = ConversationRecorder(session_id="s1b", prompt_version="v17")
    rec.start()
    rec.handle(ev(EventType.USER_SPEECH_START, 0.0))
    rec.handle(ev(EventType.USER_SPEECH_END, 1.0))
    rec.handle(ev(EventType.AGENT_AUDIO_CHUNK, 1.3, audio_ms=80))
    rec.handle(ev(EventType.TURN_COMPLETE, 2.0))
    rec.close()

    names = by_name(exporter)
    assert names[semconv.SPAN_SESSION][0].attributes[semconv.PROMPT_VERSION] == "v17"
    assert names[semconv.SPAN_TURN][0].attributes[semconv.PROMPT_VERSION] == "v17"


# ---------------------------------------------------------------- latency


def test_ttfa_measured_from_end_of_user_speech(harness):
    """TTFA is the silence the user sat through: speech end -> first audio."""
    exporter, _ = harness
    rec = ConversationRecorder(session_id="s2")
    rec.start()

    rec.handle(ev(EventType.USER_SPEECH_START, 10.0))
    rec.handle(ev(EventType.USER_SPEECH_END, 12.0))                  # spoke 2s
    rec.handle(ev(EventType.AGENT_AUDIO_CHUNK, 12.42, audio_ms=60))  # replied 420ms later
    rec.handle(ev(EventType.TURN_COMPLETE, 14.0))
    rec.close()

    turn = by_name(exporter)[semconv.SPAN_TURN][0]
    # 420ms, not 2420ms -- the user's own speech must not be counted.
    assert turn.attributes[semconv.TURN_TTFA_MS] == pytest.approx(420.0, abs=1.0)


def test_agent_initiated_turn_has_no_ttfa(harness):
    """A proactive greeting has no user speech to measure from. A fabricated
    value here silently corrupts the p95."""
    exporter, _ = harness
    rec = ConversationRecorder(session_id="s3")
    rec.start()
    rec.handle(ev(EventType.AGENT_AUDIO_CHUNK, 0.2, audio_ms=100))
    rec.handle(ev(EventType.TURN_COMPLETE, 1.0))
    rec.close()

    turn = by_name(exporter)[semconv.SPAN_TURN][0]
    assert semconv.TURN_TTFA_MS not in turn.attributes
    assert turn.attributes[semconv.TURN_AGENT_INITIATED] is True


def test_stream_gap_detects_mid_utterance_stutter(harness):
    """A stall *during* speech is a different fault from a slow start."""
    exporter, _ = harness
    rec = ConversationRecorder(session_id="s3b")
    rec.start()
    rec.handle(ev(EventType.USER_SPEECH_START, 0.0))
    rec.handle(ev(EventType.USER_SPEECH_END, 1.0))
    rec.handle(ev(EventType.AGENT_AUDIO_CHUNK, 1.2, audio_ms=100))
    rec.handle(ev(EventType.AGENT_AUDIO_CHUNK, 1.3, audio_ms=100))   # normal pacing
    rec.handle(ev(EventType.AGENT_AUDIO_CHUNK, 2.1, audio_ms=100))   # 800ms stall
    rec.handle(ev(EventType.TURN_COMPLETE, 2.5))
    rec.close()

    turn = by_name(exporter)[semconv.SPAN_TURN][0]
    assert turn.attributes[semconv.TURN_MAX_STREAM_GAP_MS] == pytest.approx(800.0, abs=2.0)


# ---------------------------------------------------------------- barge-in


def test_barge_in_records_offset_and_opens_new_turn(harness):
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

    names = by_name(exporter)
    turns = sorted(names[semconv.SPAN_TURN], key=lambda s: s.attributes[semconv.TURN_INDEX])
    assert len(turns) == 2, "barge-in should close the interrupted turn and start a new one"

    first, second = turns
    assert first.attributes[semconv.TURN_INTERRUPTED] is True
    assert first.attributes[semconv.TURN_END_REASON] == semconv.EndReason.INTERRUPTED
    assert second.attributes[semconv.TURN_INTERRUPTED] is False

    agent_utt = names[semconv.SPAN_AGENT_UTTERANCE][0]
    events = [e for e in agent_utt.events if e.name == semconv.EVENT_BARGE_IN]
    assert len(events) == 1
    assert events[0].attributes[semconv.TURN_BARGE_IN_OFFSET_MS] == pytest.approx(800.0, abs=1.0)
    assert agent_utt.attributes[semconv.AUDIO_UTTERANCE_TRUNCATED] is True


def test_server_interrupt_after_client_detection_is_not_double_counted(harness):
    """Providers echo an interruption after local detection has already fired.
    Counting both doubles every barge-in dashboard."""
    exporter, _ = harness
    rec = ConversationRecorder(session_id="s5")
    rec.start()

    rec.handle(ev(EventType.USER_SPEECH_START, 0.0))
    rec.handle(ev(EventType.USER_SPEECH_END, 1.0))
    rec.handle(ev(EventType.AGENT_AUDIO_CHUNK, 1.2, audio_ms=100))
    rec.handle(ev(EventType.USER_SPEECH_START, 1.9))   # client-side detection
    rec.handle(ev(EventType.INTERRUPTED, 2.0))         # server confirmation
    rec.handle(ev(EventType.TURN_COMPLETE, 2.5))
    rec.close()

    total = sum(
        len([e for e in s.events if e.name == semconv.EVENT_BARGE_IN])
        for s in by_name(exporter)[semconv.SPAN_AGENT_UTTERANCE]
    )
    assert total == 1


def test_overlap_is_measured_separately_from_barge_in(harness):
    """Overlap is how long both parties continued at once — a different fault
    from the interruption itself."""
    exporter, _ = harness
    rec = ConversationRecorder(session_id="s5b")
    rec.start()

    rec.handle(ev(EventType.USER_SPEECH_START, 0.0))
    rec.handle(ev(EventType.USER_SPEECH_END, 1.0))
    rec.handle(ev(EventType.AGENT_AUDIO_CHUNK, 1.2, audio_ms=100))  # agent speaking
    rec.handle(ev(EventType.USER_SPEECH_START, 2.0))                # both now active
    rec.handle(ev(EventType.PLAYBACK_FINISHED, 2.25))               # agent yields 250ms later
    rec.handle(ev(EventType.USER_SPEECH_END, 3.0))
    rec.handle(ev(EventType.TURN_COMPLETE, 3.2))
    rec.close()

    turns = by_name(exporter)[semconv.SPAN_TURN]
    overlaps = [t.attributes.get(semconv.TURN_OVERLAP_MS, 0) for t in turns]
    assert max(overlaps) == pytest.approx(250.0, abs=5.0)


# ---------------------------------------------------------------- dialogue


def test_user_repair_is_detected_and_flagged(harness):
    exporter, _ = harness
    rec = ConversationRecorder(session_id="s6a")
    rec.start()
    rec.handle(ev(EventType.USER_SPEECH_START, 0.0))
    rec.handle(ev(EventType.USER_TRANSCRIPT, 0.2, text="No, that's not what I asked for"))
    rec.handle(ev(EventType.USER_SPEECH_END, 1.0))
    rec.handle(ev(EventType.TURN_COMPLETE, 2.0))
    rec.close()

    turn = by_name(exporter)[semconv.SPAN_TURN][0]
    assert turn.attributes[semconv.TURN_REPAIR] is True
    events = [e for e in turn.events if e.name == semconv.EVENT_REPAIR]
    assert events[0].attributes[semconv.REPAIR_TYPE] == semconv.RepairType.CORRECTION


def test_agent_handoff_marks_session_transferred(harness):
    """Containment is the outcome the deployment is funded on, so a handoff
    has to change the session verdict, not just add a counter."""
    exporter, _ = harness
    rec = ConversationRecorder(session_id="s6b")
    rec.start()
    rec.handle(ev(EventType.USER_SPEECH_START, 0.0))
    rec.handle(ev(EventType.USER_SPEECH_END, 1.0))
    rec.handle(ev(EventType.AGENT_AUDIO_CHUNK, 1.2, audio_ms=100))
    rec.handle(ev(EventType.AGENT_TRANSCRIPT, 1.3, text="Let me transfer you to an agent."))
    rec.handle(ev(EventType.TURN_COMPLETE, 2.0))
    rec.close()

    session = by_name(exporter)[semconv.SPAN_SESSION][0]
    assert session.attributes[semconv.SESSION_OUTCOME] == semconv.Outcome.TRANSFERRED
    assert session.attributes[semconv.SESSION_CONTAINED] is False


def test_ordinary_session_is_contained(harness):
    exporter, _ = harness
    rec = ConversationRecorder(session_id="s6c")
    rec.start()
    rec.handle(ev(EventType.USER_SPEECH_START, 0.0))
    rec.handle(ev(EventType.USER_SPEECH_END, 1.0))
    rec.handle(ev(EventType.AGENT_AUDIO_CHUNK, 1.2, audio_ms=100))
    rec.handle(ev(EventType.AGENT_TRANSCRIPT, 1.3, text="Done — your order is confirmed."))
    rec.handle(ev(EventType.TURN_COMPLETE, 2.0))
    rec.close()

    session = by_name(exporter)[semconv.SPAN_SESSION][0]
    assert session.attributes[semconv.SESSION_OUTCOME] == semconv.Outcome.CONTAINED


# ---------------------------------------------------------------- tools


def test_tool_call_during_playback_is_flagged_and_timed(harness):
    exporter, _ = harness
    rec = ConversationRecorder(session_id="s7")
    rec.start()

    rec.handle(ev(EventType.USER_SPEECH_START, 0.0))
    rec.handle(ev(EventType.USER_SPEECH_END, 1.0))
    rec.handle(ev(EventType.AGENT_AUDIO_CHUNK, 1.3, audio_ms=100))
    rec.handle(ev(EventType.TOOL_CALL, 1.5, name="get_latency", call_id="c1"))
    rec.handle(ev(EventType.TOOL_RESULT, 1.9, call_id="c1"))
    rec.handle(ev(EventType.TURN_COMPLETE, 2.5))
    rec.close()

    tools = by_name(exporter)[semconv.SPAN_EXECUTE_TOOL]
    assert len(tools) == 1
    assert tools[0].attributes[semconv.GEN_AI_TOOL_NAME] == "get_latency"
    assert tools[0].attributes[semconv.TOOL_MID_STREAM] is True
    assert tools[0].attributes[semconv.TOOL_DURATION_MS] == pytest.approx(400.0, abs=5.0)
    assert by_name(exporter)[semconv.SPAN_TURN][0].attributes[semconv.TURN_TOOL_CALL_COUNT] == 1


def test_unclosed_spans_are_reaped_on_session_close(harness):
    """A dropped socket mid-turn must not leak unended spans, or the trace
    never reaches the backend at all."""
    exporter, _ = harness
    rec = ConversationRecorder(session_id="s8")
    rec.start()

    rec.handle(ev(EventType.USER_SPEECH_START, 0.0))
    rec.handle(ev(EventType.USER_SPEECH_END, 1.0))
    rec.handle(ev(EventType.AGENT_AUDIO_CHUNK, 1.2, audio_ms=100))
    rec.handle(ev(EventType.TOOL_CALL, 1.4, name="stuck", call_id="c9"))
    rec.close()  # socket dies here

    names = by_name(exporter)
    assert semconv.SPAN_TURN in names
    assert semconv.SPAN_EXECUTE_TOOL in names
    assert names[semconv.SPAN_EXECUTE_TOOL][0].attributes[semconv.TOOL_INTERRUPTED] is True
    assert names[semconv.SPAN_TURN][0].attributes[semconv.TURN_END_REASON] == (
        semconv.EndReason.SESSION_CLOSED
    )


# ---------------------------------------------------------------- metrics


def test_core_metrics_recorded(harness):
    _, reader = harness
    rec = ConversationRecorder(session_id="s9")
    rec.start()
    rec.handle(ev(EventType.USER_SPEECH_START, 0.0))
    rec.handle(ev(EventType.USER_AUDIO_SENT, 0.5, audio_ms=500))
    rec.handle(ev(EventType.USER_SPEECH_END, 1.0))
    rec.handle(ev(EventType.AGENT_AUDIO_CHUNK, 1.35, audio_ms=200))
    rec.handle(ev(EventType.TURN_COMPLETE, 2.0))
    rec.close()

    names = metric_names(reader)
    for expected in (
        semconv.METRIC_TTFA,
        semconv.METRIC_TURN_DURATION,
        semconv.METRIC_TURN_COUNT,
        semconv.METRIC_AUDIO_OUTPUT_SECONDS,
        semconv.METRIC_AUDIO_INPUT_SECONDS,
        semconv.METRIC_SESSION_COUNT,
        semconv.METRIC_SESSION_OUTCOME,
    ):
        assert expected in names, f"missing metric {expected}"


def test_instrumentation_never_raises_into_the_agent(harness):
    """A bug in cadence must not take down the agent it is watching."""
    _, _ = harness
    rec = ConversationRecorder(session_id="s10")
    rec.start()
    rec.on_event = lambda kind, data: (_ for _ in ()).throw(RuntimeError("ui blew up"))

    rec.handle(ev(EventType.USER_SPEECH_START, 0.0))  # must not raise
    rec.handle(ev(EventType.TURN_COMPLETE, 1.0))
    rec.close()


def test_barge_in_is_not_recorded_against_a_finished_turn(harness):
    """Found by real Gemini sessions, not by any synthetic test.

    A completed turn leaves the agent no longer holding the floor. If the
    recorder does not clear that state, the next turn's speech-start looks
    like an interruption, and the barge-in is attributed to a span that was
    exported seconds ago — so the counter climbs while `interrupted` stays
    false on every turn. Twenty-two events, zero interrupted turns.
    """
    exporter, _ = harness
    rec = ConversationRecorder(session_id="s11")
    rec.start()

    # Turn 0: a clean exchange that completes on its own.
    rec.handle(ev(EventType.USER_SPEECH_START, 0.0))
    rec.handle(ev(EventType.USER_SPEECH_END, 1.0))
    rec.handle(ev(EventType.AGENT_AUDIO_CHUNK, 1.3, audio_ms=200))
    rec.handle(ev(EventType.AGENT_GENERATION_COMPLETE, 2.0))
    rec.handle(ev(EventType.TURN_COMPLETE, 2.1, reason="completed"))

    # Turn 1: the user speaks again. No PLAYBACK_FINISHED was ever sent.
    rec.handle(ev(EventType.USER_SPEECH_START, 3.0))
    rec.handle(ev(EventType.USER_SPEECH_END, 4.0))
    rec.handle(ev(EventType.AGENT_AUDIO_CHUNK, 4.3, audio_ms=200))
    rec.handle(ev(EventType.TURN_COMPLETE, 5.0, reason="completed"))
    rec.close()

    names = by_name(exporter)
    turns = sorted(names[semconv.SPAN_TURN], key=lambda s: s.attributes[semconv.TURN_INDEX])
    assert len(turns) == 2, "a normal second turn must not be treated as an interruption"
    for turn in turns:
        assert turn.attributes[semconv.TURN_END_REASON] == "completed"
        assert turn.attributes[semconv.TURN_INTERRUPTED] is False

    barge_events = sum(
        len([e for e in s.events if e.name == semconv.EVENT_BARGE_IN])
        for s in exporter.get_finished_spans()
    )
    assert barge_events == 0, "no interruption occurred, so none should be recorded"


def test_genuine_barge_in_still_marks_the_turn(harness):
    """The guard above must not suppress a real interruption."""
    exporter, _ = harness
    rec = ConversationRecorder(session_id="s12")
    rec.start()

    rec.handle(ev(EventType.USER_SPEECH_START, 0.0))
    rec.handle(ev(EventType.USER_SPEECH_END, 1.0))
    rec.handle(ev(EventType.AGENT_AUDIO_CHUNK, 1.2, audio_ms=200))
    rec.handle(ev(EventType.USER_SPEECH_START, 1.9))   # cuts in mid-reply
    rec.handle(ev(EventType.USER_SPEECH_END, 2.4))
    rec.handle(ev(EventType.TURN_COMPLETE, 3.0))
    rec.close()

    turns = by_name(exporter)[semconv.SPAN_TURN]
    interrupted = [t for t in turns if t.attributes[semconv.TURN_INTERRUPTED]]
    assert len(interrupted) == 1
    assert interrupted[0].attributes[semconv.TURN_END_REASON] == semconv.EndReason.INTERRUPTED
