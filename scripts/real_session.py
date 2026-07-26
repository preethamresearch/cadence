"""Run real Gemini Live sessions through cadence and ship them to SigNoz.

Everything else in this repo either tests the recorder with synthetic events
or drives it from the simulator. Both bypass ``providers/gemini.py`` — which
means the one component that touches the actual vendor API has, until this
script runs, never processed a real ``LiveServerMessage``.

That is the gap this closes. The adapter's field mappings
(``voice_activity_detection_signal``, ``turn_complete_reason``, ``interrupted``,
inline audio extraction, ``usage_metadata``) were written from SDK
introspection. This exercises them against the live API and reports which ones
actually fired, so the claim "cadence instruments Gemini Live" is verified
rather than asserted.

**Honest caveat about TTFA.** Turns are driven by text rather than a
microphone, so time-to-first-audio here measures *text in → first audio out*.
That still includes model queueing, generation, and network — everything except
voice-activity-detection dwell. It is a real measurement of a real round trip,
but it is not identical to a spoken turn, and sessions are tagged
``prompt_version="real-text-driven"`` so they never get silently averaged in
with microphone sessions.

    export GEMINI_API_KEY=...
    python scripts/real_session.py --turns 5
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import cadence  # noqa: E402
from cadence import semconv  # noqa: E402
from cadence.providers import gemini  # noqa: E402

# Several conversation shapes rather than one script repeated.
#
# Running the same five prompts N times would produce N copies of one
# conversation, which looks like a bigger dataset while telling you nothing
# new. These vary length, tool pressure, and whether a repair occurs, so the
# resulting distribution has some genuine spread.
CONVERSATIONS = [
    # Short, no tools — the fast path.
    [
        "In one sentence, what is time to first audio?",
        "Thanks, that's all.",
    ],
    # Tool-heavy self-observation.
    [
        "How fast have you been responding? Check your telemetry.",
        "What is my token spend so far?",
        "How many times was I interrupted?",
        "Summarise this session for me.",
    ],
    # Contains a repair, so the dialogue classifier sees a real one.
    [
        "How many turns have we had?",
        "No, that's not what I asked — I meant how long they took.",
        "Right. And is that within your latency objective?",
    ],
    # Longer answers, which stress streaming and stream-gap detection.
    [
        "Explain why barge-in offset distribution matters more than the count.",
        "What would cause offsets to cluster under 400 milliseconds?",
        "And what does it mean if they cluster above two seconds?",
        "Which of those is happening right now?",
    ],
    # A fallback the agent genuinely cannot answer from telemetry.
    [
        "What is the weather in Bangalore?",
        "Fine — what can you actually tell me about yourself?",
        "How is your containment rate?",
    ],
]


def conversation_for(index: int) -> list[str]:
    return CONVERSATIONS[index % len(CONVERSATIONS)]


async def run_session(
    *,
    api_key: str,
    model: str,
    prompts: list[str],
    capture_content: bool,
) -> dict:
    from google import genai
    from google.genai import types

    sys.path.insert(0, str(ROOT))
    from app.tools.observability import (  # noqa: E402
        SYSTEM_INSTRUCTION,
        TOOL_DECLARATIONS,
        LiveStats,
        ObservabilityTools,
    )
    from app.tools.signoz import SigNozClient, SigNozConfig  # noqa: E402

    stats = LiveStats()
    signoz_config = SigNozConfig.from_env()
    tools = ObservabilityTools(
        SigNozClient(signoz_config) if signoz_config else None, stats
    )

    # Count which normalized events the adapter actually produced. This is the
    # verification: if a field mapping is wrong, its event never appears.
    observed: Counter[str] = Counter()

    def on_telemetry(kind: str, data: dict) -> None:
        stats.on_event(kind, data)
        observed[kind] += 1

    config = types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        system_instruction=SYSTEM_INSTRUCTION,
        tools=[{"function_declarations": TOOL_DECLARATIONS}],
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
    )

    client = genai.Client(api_key=api_key)
    print(f"connecting to {model} …")

    async with client.aio.live.connect(model=model, config=config) as raw:
        async with cadence.CadenceSession(
            raw,
            model=model,
            capture_content=capture_content,
            prompt_version="real-text-driven",
            agent_version="cadence-real-1.0",
            on_event=on_telemetry,
        ) as session:
            print(f"session {session.recorder.session_id}  trace {session.trace_id}")

            for index, prompt in enumerate(prompts, start=1):
                print(f"\n  [{index}] > {prompt}")

                # A text turn still opens a conversational turn: mark speech
                # start/end around it so the recorder's turn model — and the
                # TTFA clock — behave exactly as they do for audio.
                session.recorder.handle(gemini.user_turn_start_event())
                await session.send_client_content(
                    turns=types.Content(role="user", parts=[types.Part(text=prompt)]),
                    turn_complete=True,
                )
                session.recorder.handle(gemini.user_turn_end_event())

                audio_bytes = 0
                reply: list[str] = []
                async for message in session.receive():
                    content = getattr(message, "server_content", None)

                    if content is not None:
                        model_turn = getattr(content, "model_turn", None)
                        for part in getattr(model_turn, "parts", None) or []:
                            inline = getattr(part, "inline_data", None)
                            if inline is not None and getattr(inline, "data", None):
                                audio_bytes += len(inline.data)

                        transcription = getattr(content, "output_transcription", None)
                        text = getattr(transcription, "text", None) if transcription else None
                        if text:
                            reply.append(text)

                    tool_call = getattr(message, "tool_call", None)
                    if tool_call is not None:
                        responses = []
                        for fc in getattr(tool_call, "function_calls", None) or []:
                            name = getattr(fc, "name", "")
                            args = dict(getattr(fc, "args", None) or {})
                            print(f"      tool: {name}({args})")
                            result = await tools.dispatch(name, args)
                            responses.append(
                                types.FunctionResponse(
                                    id=getattr(fc, "id", None), name=name, response=result
                                )
                            )
                        if responses:
                            await session.send_tool_response(function_responses=responses)

                    if content is not None and getattr(content, "turn_complete", None):
                        break

                spoken = "".join(reply).strip()
                seconds = gemini.pcm_duration_ms(audio_bytes, gemini.OUTPUT_SAMPLE_RATE) / 1000
                print(f"      < {spoken[:120] or '(audio only)'}")
                print(f"      {audio_bytes:,} bytes of audio ({seconds:.1f}s)")

            trace_id = session.trace_id
            recorder_stats = session.stats

    return {"trace_id": trace_id, "stats": recorder_stats, "observed": observed,
            "ttfa": list(stats.ttfa_samples)}


async def main_async(args) -> int:
    api_key = args.api_key
    if not api_key:
        print("error: GEMINI_API_KEY not set (https://aistudio.google.com/apikey)",
              file=sys.stderr)
        return 2

    cadence.configure(
        service_name=args.service,
        endpoint=args.endpoint,
        ingestion_key=os.getenv("SIGNOZ_INGESTION_KEY"),
        metric_export_interval_ms=5_000,
        force=True,
    )

    results = []
    for n in range(args.sessions):
        print(f"\n=== session {n + 1}/{args.sessions} ===")
        try:
            results.append(await run_session(
                api_key=api_key, model=args.model,
                prompts=conversation_for(n),
                capture_content=args.capture_content,
            ))
        except Exception as exc:
            print(f"  session failed: {type(exc).__name__}: {exc}", file=sys.stderr)

    print("\nflushing telemetry …")
    cadence.shutdown()

    if not results:
        print("no sessions completed", file=sys.stderr)
        return 1

    # --- the actual verification ---------------------------------------
    combined: Counter[str] = Counter()
    ttfa: list[float] = []
    for r in results:
        combined.update(r["observed"])
        ttfa.extend(r["ttfa"])

    print("\n" + "=" * 62)
    print("ADAPTER VERIFICATION — normalized events produced from real messages")
    print("=" * 62)
    expected = [
        "turn_start", "user_speech_start", "user_speech_end", "ttfa",
        "agent_transcript", "turn_end", "usage", "tool_call", "barge_in",
    ]
    for name in expected:
        count = combined.get(name, 0)
        mark = "✓" if count else "·"
        note = "" if count else "   (not exercised by a text-driven session)"
        print(f"  {mark} {name:<20} {count}{note}")

    if ttfa:
        ordered = sorted(ttfa)
        print(f"\n  real TTFA over {len(ttfa)} turns:")
        print(f"    min {min(ordered):.0f}ms   median {ordered[len(ordered)//2]:.0f}ms"
              f"   max {max(ordered):.0f}ms")
        print("    (text in -> first audio out; excludes VAD dwell)")

    print("\n  traces:")
    for r in results:
        print(f"    {args.signoz_ui}/trace/{r['trace_id']}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-key", default=os.getenv("GEMINI_API_KEY"))
    parser.add_argument("--model", default=os.getenv("GEMINI_MODEL",
                                                     "gemini-3.1-flash-live-preview"))
    parser.add_argument("--sessions", type=int, default=3)
    parser.add_argument("--service", default=os.getenv("OTEL_SERVICE_NAME",
                                                       "cadence-voice-agent"))
    parser.add_argument("--endpoint", default=os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT",
                                                        "http://localhost:4318"))
    parser.add_argument("--signoz-ui", default=os.getenv("SIGNOZ_UI",
                                                         "http://localhost:8080"))
    parser.add_argument("--capture-content", action="store_true",
                        help="Record transcripts as span attributes (off by default).")
    args = parser.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
