"""Provider adapters.

Each adapter translates one vendor's realtime wire format into cadence's
neutral event vocabulary. The turn state machine lives in ``cadence.recorder``
and is shared by all of them.
"""

from . import gemini

__all__ = ["gemini"]
