"""Cadence demo server.

Bridges a browser microphone to Gemini Live, with cadence tracing the duplex
stream in between, and streams the resulting turn structure back to the console
so you can watch spans close in real time.

    browser  --PCM16 16kHz-->  server  --> Gemini Live
             <--PCM16 24kHz--          <--
             <--telemetry JSON--  (from the cadence recorder's event hook)

The telemetry the browser renders is the same data the spans carry, taken from
the same hook -- the console is a view of the trace, not a reimplementation
of it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from google import genai
from google.genai import types

import cadence
from app.tools.observability import (
    SYSTEM_INSTRUCTION,
    TOOL_DECLARATIONS,
    LiveStats,
    ObservabilityTools,
)
from app.tools.signoz import SigNozClient, SigNozConfig

load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
)
logger = logging.getLogger("cadence.app")

STATIC_DIR = Path(__file__).parent / "static"
MODEL = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-live-preview")
SERVICE_NAME = os.getenv("OTEL_SERVICE_NAME", "cadence-voice-agent")

cadence.configure(service_name=SERVICE_NAME)

app = FastAPI(title="Cadence", description="Observability for real-time voice agents")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

_signoz_config = SigNozConfig.from_env()


@app.get("/")
async def landing() -> FileResponse:
    """Explain the project before showing the instrument. A judge landing on a
    bare console has no idea what they are looking at."""
    return FileResponse(STATIC_DIR / "landing.html")


@app.get("/console")
async def console() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/healthz")
async def healthz() -> JSONResponse:
    return JSONResponse(
        {
            "status": "ok",
            "model": MODEL,
            "gemini_configured": bool(os.getenv("GEMINI_API_KEY")),
            "otlp_endpoint": os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT") or None,
            "signoz_query_configured": _signoz_config is not None,
        }
    )


@app.get("/api/config")
async def client_config() -> JSONResponse:
    """Front-end configuration, including the SigNoz base URL used to build
    deep links from a trace id straight into the waterfall view."""
    return JSONResponse(
        {
            "model": MODEL,
            "service_name": SERVICE_NAME,
            "signoz_base_url": _signoz_config.base_url if _signoz_config else None,
        }
    )


def _build_live_config() -> types.LiveConnectConfig:
    return types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        system_instruction=SYSTEM_INSTRUCTION,
        tools=[{"function_declarations": TOOL_DECLARATIONS}],
        # Transcriptions drive the console's caption track. They also let the
        # recorder attach transcript text to utterance spans when content
        # capture is enabled.
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
        realtime_input_config=types.RealtimeInputConfig(
            automatic_activity_detection=types.AutomaticActivityDetection(
                # Leaving VAD sensitivity at defaults on purpose: the barge-in
                # distribution cadence records is only meaningful if it reflects
                # what a real deployment would see.
                disabled=False,
            ),
            activity_handling=types.ActivityHandling.START_OF_ACTIVITY_INTERRUPTS,
        ),
    )


@app.websocket("/ws")
async def voice_socket(websocket: WebSocket) -> None:
    await websocket.accept()

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        await websocket.send_json(
            {"type": "fatal", "message": "GEMINI_API_KEY is not set on the server."}
        )
        await websocket.close()
        return

    stats = LiveStats()
    outbound: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    def on_telemetry(kind: str, data: dict[str, Any]) -> None:
        """Recorder hook. Runs on the audio path, so it must not block --
        it only feeds the local stats and queues a message for the UI task."""
        stats.on_event(kind, data)
        outbound.put_nowait({"type": "telemetry", "event": kind, "data": data})

    signoz_client = SigNozClient(_signoz_config) if _signoz_config else None
    tools = ObservabilityTools(signoz_client, stats)

    client = genai.Client(api_key=api_key)

    try:
        async with client.aio.live.connect(model=MODEL, config=_build_live_config()) as raw:
            async with cadence.CadenceSession(
                raw,
                model=MODEL,
                capture_content=os.getenv("CADENCE_CAPTURE_CONTENT", "").lower() == "true",
                on_event=on_telemetry,
            ) as session:

                await websocket.send_json(
                    {
                        "type": "ready",
                        "model": MODEL,
                        "session_id": session.recorder.session_id,
                        "trace_id": session.trace_id,
                    }
                )

                async def pump_browser_to_model() -> None:
                    while True:
                        message = await websocket.receive()
                        if message.get("type") == "websocket.disconnect":
                            raise WebSocketDisconnect()
                        if (data := message.get("bytes")) is not None:
                            await session.send_realtime_input(
                                audio=types.Blob(data=data, mime_type="audio/pcm;rate=16000")
                            )
                        elif (text := message.get("text")) is not None:
                            await _handle_client_command(session, text)

                async def pump_model_to_browser() -> None:
                    async for msg in session.receive():
                        await _forward_model_message(websocket, session, tools, msg)

                async def pump_telemetry() -> None:
                    while True:
                        payload = await outbound.get()
                        await websocket.send_json(payload)

                tasks = [
                    asyncio.create_task(pump_browser_to_model(), name="browser->model"),
                    asyncio.create_task(pump_model_to_browser(), name="model->browser"),
                    asyncio.create_task(pump_telemetry(), name="telemetry"),
                ]
                done, pending = await asyncio.wait(
                    tasks, return_when=asyncio.FIRST_EXCEPTION
                )
                for task in pending:
                    task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.gather(*pending, return_exceptions=True)
                for task in done:
                    if (exc := task.exception()) and not isinstance(exc, WebSocketDisconnect):
                        raise exc

    except WebSocketDisconnect:
        logger.info("client disconnected")
    except Exception:
        logger.exception("voice session failed")
        with contextlib.suppress(Exception):
            await websocket.send_json(
                {"type": "fatal", "message": "The voice session ended unexpectedly."}
            )
    finally:
        if signoz_client:
            await signoz_client.aclose()
        with contextlib.suppress(Exception):
            await websocket.close()


async def _handle_client_command(session: cadence.CadenceSession, raw: str) -> None:
    try:
        command = json.loads(raw)
    except json.JSONDecodeError:
        return
    if command.get("type") == "text":
        await session.send_client_content(
            turns=types.Content(role="user", parts=[types.Part(text=command["text"])]),
            turn_complete=True,
        )


async def _forward_model_message(
    websocket: WebSocket,
    session: cadence.CadenceSession,
    tools: ObservabilityTools,
    msg: Any,
) -> None:
    """Relay one server message to the browser, executing tools as they arrive."""
    content = getattr(msg, "server_content", None)

    if content is not None:
        model_turn = getattr(content, "model_turn", None)
        if model_turn is not None:
            for part in getattr(model_turn, "parts", None) or []:
                inline = getattr(part, "inline_data", None)
                data = getattr(inline, "data", None) if inline else None
                if data:
                    await websocket.send_bytes(data)

        if getattr(content, "interrupted", None):
            # The browser must flush its playback buffer immediately, or the
            # user hears the agent keep talking after it was cut off.
            await websocket.send_json({"type": "interrupted"})

        for attr, role in (
            ("input_transcription", "user"),
            ("output_transcription", "agent"),
        ):
            transcription = getattr(content, attr, None)
            text = getattr(transcription, "text", None) if transcription else None
            if text:
                await websocket.send_json(
                    {"type": "transcript", "role": role, "text": text}
                )

    tool_call = getattr(msg, "tool_call", None)
    if tool_call is not None:
        responses = []
        for fc in getattr(tool_call, "function_calls", None) or []:
            name = getattr(fc, "name", "")
            args = dict(getattr(fc, "args", None) or {})
            await websocket.send_json({"type": "tool_start", "name": name, "args": args})
            result = await tools.dispatch(name, args)
            await websocket.send_json(
                {"type": "tool_done", "name": name, "result": result}
            )
            responses.append(
                types.FunctionResponse(
                    id=getattr(fc, "id", None), name=name, response=result
                )
            )
        if responses:
            await session.send_tool_response(function_responses=responses)


@app.on_event("shutdown")
async def _flush_telemetry() -> None:
    cadence.shutdown()
