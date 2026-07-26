"""Instrumented wrapper around a Gemini Live session.

Wraps the SDK session rather than monkey-patching it. Monkey-patching a
library whose API is still moving is how instrumentation ends up broken by a
minor release; an explicit wrapper fails loudly and obviously instead.

Usage mirrors the raw SDK closely enough that adopting it is a two-line change:

    async with client.aio.live.connect(model=MODEL, config=config) as raw:
        async with CadenceSession(raw, model=MODEL) as session:
            await session.send_realtime_input(audio=chunk)
            async for message in session.receive():
                ...
"""

from __future__ import annotations

import logging
import time
from typing import Any, AsyncIterator

from .events import EventType, VoiceEvent
from .providers import gemini
from .recorder import ConversationRecorder, EventHook

logger = logging.getLogger(__name__)


class CadenceSession:
    """Adds tracing to a live session while staying out of the audio path.

    Every method delegates to the wrapped session. Telemetry is derived from
    the messages flowing past; nothing is buffered, and no audio is copied.
    """

    def __init__(
        self,
        session: Any,
        *,
        model: str | None = None,
        session_id: str | None = None,
        capture_content: bool = False,
        on_event: EventHook | None = None,
        recorder: ConversationRecorder | None = None,
    ) -> None:
        self._session = session
        self.recorder = recorder or ConversationRecorder(
            session_id=session_id,
            provider=gemini.PROVIDER,
            model=model,
            capture_content=capture_content,
            input_sample_rate=gemini.INPUT_SAMPLE_RATE,
            output_sample_rate=gemini.OUTPUT_SAMPLE_RATE,
            on_event=on_event,
        )
        self._started = False

    # -- lifecycle ------------------------------------------------------

    async def __aenter__(self) -> CadenceSession:
        self.start()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self.close(exc)

    def start(self) -> None:
        if not self._started:
            self.recorder.start()
            self._started = True

    def close(self, error: BaseException | None = None) -> None:
        self.recorder.close(error)

    # -- receive --------------------------------------------------------

    async def receive(self) -> AsyncIterator[Any]:
        """Iterate server messages, recording telemetry as they pass through.

        The original message is always yielded unchanged, so application code
        is unaffected by what cadence does or does not understand.
        """
        self.start()
        try:
            async for message in self._session.receive():
                for event in gemini.translate(message):
                    self.recorder.handle(event)
                yield message
        except Exception as exc:
            self.recorder.handle(
                VoiceEvent(
                    type=EventType.ERROR,
                    monotonic=time.monotonic(),
                    wall_ns=time.time_ns(),
                    text=str(exc),
                )
            )
            raise

    # -- send -----------------------------------------------------------

    async def send_realtime_input(self, **kwargs: Any) -> Any:
        """Forward realtime input, accounting for audio seconds and frames.

        Input cost in a realtime session is measured in seconds of open
        microphone, not in prompt tokens, so it has to be counted here on the
        way out -- the server never tells us how much we sent.
        """
        audio = kwargs.get("audio")
        if audio is not None:
            data = getattr(audio, "data", None) or (audio if isinstance(audio, bytes) else None)
            if data:
                self.recorder.handle(gemini.user_audio_event(len(data)))

        if kwargs.get("video") is not None:
            self.recorder.handle(gemini.video_frame_event())

        for chunk in kwargs.get("media_chunks") or []:
            mime = getattr(chunk, "mime_type", "") or ""
            data = getattr(chunk, "data", None)
            if not data:
                continue
            if mime.startswith("audio"):
                self.recorder.handle(gemini.user_audio_event(len(data)))
            elif mime.startswith("image") or mime.startswith("video"):
                self.recorder.handle(gemini.video_frame_event())

        return await self._session.send_realtime_input(**kwargs)

    async def send_tool_response(self, **kwargs: Any) -> Any:
        """Forward a tool response and close the matching ``execute_tool`` span."""
        for response in kwargs.get("function_responses") or []:
            self.recorder.handle(
                gemini.tool_result_event(
                    call_id=getattr(response, "id", None),
                    name=getattr(response, "name", None),
                )
            )
        return await self._session.send_tool_response(**kwargs)

    async def send_client_content(self, **kwargs: Any) -> Any:
        return await self._session.send_client_content(**kwargs)

    def __getattr__(self, item: str) -> Any:
        # Anything cadence does not wrap passes straight through, so the
        # wrapper never becomes a bottleneck on SDK features.
        return getattr(self._session, item)

    # -- convenience ----------------------------------------------------

    @property
    def trace_id(self) -> str | None:
        return self.recorder.trace_id

    @property
    def stats(self) -> dict[str, Any]:
        return self.recorder.stats
