"""cadence -- OpenTelemetry instrumentation for real-time voice agents.

The GenAI semantic conventions describe request/response model calls. Real-time
voice agents speak over a persistent duplex socket where neither a request nor
a response exists, so the conventions have nothing to say about the signals
that actually determine whether such an agent works: how long the user waited
in silence, how often they had to talk over it, and how much the open
microphone cost.

cadence adds a ``voice.*`` namespace covering exactly that, reconstructs
conversational turn structure from the raw signal stream, and exports it over
OTLP to any OpenTelemetry backend.

    import cadence

    cadence.configure(service_name="my-voice-agent")

    async with client.aio.live.connect(model=MODEL, config=config) as raw:
        async with cadence.CadenceSession(raw, model=MODEL) as session:
            async for message in session.receive():
                ...

See ``docs/SEMCONV.md`` for the full attribute specification.
"""

from __future__ import annotations

from . import semconv
from .events import EventType, VoiceEvent
from .metrics import voice_metrics
from .recorder import ConversationRecorder
from .session import CadenceSession
from .tracing import configure, get_meter, get_tracer, shutdown

__version__ = "0.1.0"

__all__ = [
    "CadenceSession",
    "ConversationRecorder",
    "EventType",
    "VoiceEvent",
    "configure",
    "get_meter",
    "get_tracer",
    "semconv",
    "shutdown",
    "voice_metrics",
    "__version__",
]
