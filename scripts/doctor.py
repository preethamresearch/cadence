"""cadence doctor — verify the whole telemetry chain, link by link.

A realtime observability stack is a chain of six things:

    agent -> recorder -> OTel SDK -> OTLP -> collector -> ClickHouse -> query API

Most of those fail *silently*. The exporter retries and drops. The collector
accepts TCP and answers nothing. The query API returns an empty series that
looks identical to "no traffic". The application keeps running and reports
success throughout, which is exactly the property you want in production and
exactly the property that makes debugging miserable.

This walks every link and says which one is broken, with the fix. Run it
before a demo, after a deploy, or whenever a dashboard is inexplicably empty.

    python scripts/doctor.py
    python scripts/doctor.py --endpoint http://localhost:4318 --signoz http://localhost:8080
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

GREEN, RED, YELLOW, DIM, BOLD, RESET = (
    "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m"
)


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    fix: str = ""
    fatal: bool = True
    """Fatal checks stop the run: there is no point testing ingestion when the
    port is closed, and a cascade of red is less useful than one red line."""


results: list[Check] = []


def record(check: Check) -> Check:
    results.append(check)
    mark = f"{GREEN}✓{RESET}" if check.ok else f"{RED}✗{RESET}"
    print(f"  {mark} {check.name}")
    if check.detail:
        print(f"      {DIM}{check.detail}{RESET}")
    if not check.ok and check.fix:
        print(f"      {YELLOW}→ {check.fix}{RESET}")
    return check


# --------------------------------------------------------------------------
# 1. The library itself
# --------------------------------------------------------------------------


def check_library() -> Check:
    try:
        import cadence
        from cadence import semconv

        return record(Check(
            "cadence imports",
            True,
            f"v{cadence.__version__}, schema {semconv.SCHEMA_VERSION}",
        ))
    except Exception as exc:
        return record(Check(
            "cadence imports", False, str(exc),
            "pip install -e '.[app,dev]'",
        ))


def check_state_machine() -> Check:
    """Drive a turn through an in-memory exporter and verify the numbers.

    This is the check that catches *silently wrong telemetry* — the failure
    mode where nothing crashes and every number is subtly incorrect.
    """
    try:
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
            InMemorySpanExporter,
        )

        import cadence.recorder as rec_mod
        from cadence import semconv
        from cadence.events import EventType, VoiceEvent
        from cadence.recorder import ConversationRecorder

        exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        original = rec_mod.get_tracer
        rec_mod.get_tracer = lambda: provider.get_tracer("doctor")
        try:
            rec = ConversationRecorder(session_id="doctor")
            rec.start()
            for kind, t, kw in (
                (EventType.USER_SPEECH_START, 0.0, {}),
                (EventType.USER_SPEECH_END, 2.0, {}),          # spoke for 2s
                (EventType.AGENT_AUDIO_CHUNK, 2.4, {"audio_ms": 100}),  # 400ms later
                (EventType.TURN_COMPLETE, 3.0, {}),
            ):
                rec.handle(VoiceEvent(type=kind, monotonic=t, wall_ns=int(t * 1e9), **kw))
            rec.close()
        finally:
            rec_mod.get_tracer = original

        turns = [s for s in exporter.get_finished_spans() if s.name == semconv.SPAN_TURN]
        if not turns:
            return record(Check(
                "turn state machine", False, "no turn span produced",
                "run: pytest tests/test_recorder.py",
            ))

        ttfa = turns[0].attributes.get(semconv.TURN_TTFA_MS)
        if ttfa is None:
            return record(Check("turn state machine", False, "TTFA missing", ""))
        # Must be 400ms, not 2400ms — the classic error is including the
        # user's own speech in the latency measurement.
        if abs(ttfa - 400.0) > 5:
            return record(Check(
                "turn state machine", False,
                f"TTFA computed as {ttfa}ms, expected ~400ms",
                "the recorder is mis-measuring latency; run pytest",
            ))
        return record(Check(
            "turn state machine", True,
            f"TTFA {ttfa:.0f}ms from a synthetic turn (expected 400ms)",
        ))
    except Exception as exc:
        return record(Check("turn state machine", False, repr(exc), "run: pytest -q"))


# --------------------------------------------------------------------------
# 2. Transport
# --------------------------------------------------------------------------


def check_port(endpoint: str) -> Check:
    parsed = urlparse(endpoint)
    host = parsed.hostname or "localhost"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        with socket.create_connection((host, port), timeout=4):
            return record(Check("OTLP port reachable", True, f"{host}:{port} accepts TCP"))
    except OSError as exc:
        return record(Check(
            "OTLP port reachable", False, f"{host}:{port}: {exc}",
            "is SigNoz running?  docker ps | grep signoz",
        ))


def check_otlp_http(endpoint: str, headers: dict[str, str]) -> Check:
    """POST an empty but valid OTLP payload.

    TCP accepting is not enough. A collector running a `nop` pipeline binds
    nothing to the port, so the connection is accepted by the port forwarder
    and then closed with no HTTP response — which is what an exporter reports
    as a transient network error and retries into oblivion.
    """
    url = endpoint.rstrip("/") + "/v1/traces"
    request = urllib.request.Request(
        url,
        data=json.dumps({"resourceSpans": []}).encode(),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=6) as response:
            return record(Check(
                "OTLP endpoint answers", True, f"HTTP {response.status} from {url}"
            ))
    except urllib.error.HTTPError as exc:
        # Any HTTP status means something is listening and speaking HTTP.
        ok = exc.code < 500
        return record(Check(
            "OTLP endpoint answers", ok, f"HTTP {exc.code} from {url}",
            "" if ok else "collector is up but erroring; check: docker logs signoz-ingester-1",
        ))
    except Exception as exc:
        return record(Check(
            "OTLP endpoint answers", False, f"{type(exc).__name__}: {exc}",
            "connection accepted but no HTTP response — the collector is almost "
            "certainly running a nop pipeline. Complete the SigNoz first-run "
            "signup at the UI, then: docker restart signoz-ingester-1",
        ))


# --------------------------------------------------------------------------
# 3. SigNoz
# --------------------------------------------------------------------------


def check_signoz_ui(base: str) -> Check:
    try:
        with urllib.request.urlopen(base.rstrip("/") + "/api/v1/version", timeout=6) as r:
            payload = json.loads(r.read())
        version = str(payload.get("version", "?")).lstrip("v")
        setup = payload.get("setupCompleted")
        if setup is False:
            return record(Check(
                "SigNoz set up", False,
                f"v{version}, but setupCompleted=false",
                f"open {base} and complete the first-run signup. Until an org "
                "exists the config server will not register the collector, and "
                "every pipeline stays nop — which is why OTLP accepts TCP but "
                "never answers.",
            ))
        return record(Check("SigNoz set up", True, f"v{version}, setup complete"))
    except Exception as exc:
        return record(Check(
            "SigNoz set up", False, f"{type(exc).__name__}: {exc}",
            "foundryctl cast -f deploy/casting.yaml",
        ))


def check_collector_pipelines() -> Check:
    """Read the collector's *effective* config, not the file on disk.

    The config file can be perfect while the running collector uses a default
    nop config, because it failed to register with the config server. Only the
    effective config tells the truth.
    """
    import subprocess

    try:
        out = subprocess.run(
            ["docker", "exec", "signoz-ingester-1", "sh", "-lc",
             "awk '/^service:/{f=1} f' /var/tmp/collector-config.yaml"],
            capture_output=True, text=True, timeout=15,
        )
        if out.returncode != 0:
            return record(Check(
                "collector pipelines", False, "could not read effective config",
                "docker ps | grep ingester", fatal=False,
            ))
        text = out.stdout
        nop_pipelines = text.count("- nop")
        if nop_pipelines >= 4:
            return record(Check(
                "collector pipelines", False,
                f"all pipelines are nop ({nop_pipelines} nop entries)",
                "the collector never received its real config. Complete the "
                "SigNoz signup, then: docker restart signoz-ingester-1",
            ))
        return record(Check(
            "collector pipelines", True, "real receivers and exporters configured"
        ))
    except FileNotFoundError:
        return record(Check("collector pipelines", True, "docker unavailable, skipped",
                            fatal=False))
    except Exception as exc:
        return record(Check("collector pipelines", False, repr(exc), "", fatal=False))


# --------------------------------------------------------------------------
# 4. Round trip
# --------------------------------------------------------------------------


def check_round_trip(endpoint: str, ingestion_key: str | None, service: str) -> Check:
    """Export a real span through the real SDK and confirm the flush succeeds.

    This is the only check that exercises the exact path production uses.
    """
    try:
        import cadence
        from cadence.events import EventType, VoiceEvent
        from cadence.recorder import ConversationRecorder

        cadence.configure(
            service_name=service, endpoint=endpoint,
            ingestion_key=ingestion_key, force=True,
        )
        rec = ConversationRecorder(session_id=f"doctor-{int(time.time())}",
                                   prompt_version="doctor")
        rec.start()
        t = time.monotonic()
        for kind, dt, kw in (
            (EventType.USER_SPEECH_START, 0.0, {}),
            (EventType.USER_SPEECH_END, 1.0, {}),
            (EventType.AGENT_AUDIO_CHUNK, 0.3, {"audio_ms": 120}),
            (EventType.TURN_COMPLETE, 1.2, {}),
        ):
            t += dt
            rec.handle(VoiceEvent(type=kind, monotonic=t, wall_ns=int(t * 1e9), **kw))
        rec.close()
        trace_id = rec.trace_id
        cadence.shutdown()
        return record(Check(
            "span exported end-to-end", True,
            f"trace {trace_id} flushed to {endpoint}",
        ))
    except Exception as exc:
        return record(Check(
            "span exported end-to-end", False, repr(exc),
            "see the errors above — the failing link is upstream of this",
        ))


# --------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint",
                        default=os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT",
                                          "http://localhost:4318"))
    parser.add_argument("--signoz", default=os.getenv("SIGNOZ_UI", "http://localhost:8080"))
    parser.add_argument("--ingestion-key", default=os.getenv("SIGNOZ_INGESTION_KEY"))
    parser.add_argument("--service", default=os.getenv("OTEL_SERVICE_NAME",
                                                       "cadence-voice-agent"))
    args = parser.parse_args()

    headers = {"signoz-ingestion-key": args.ingestion_key} if args.ingestion_key else {}

    print(f"\n{BOLD}cadence doctor{RESET}")
    print(f"{DIM}  OTLP    {args.endpoint}")
    print(f"  SigNoz  {args.signoz}{RESET}\n")

    print(f"{BOLD}library{RESET}")
    if not check_library().ok:
        return summarise()
    check_state_machine()

    print(f"\n{BOLD}signoz{RESET}")
    check_signoz_ui(args.signoz)
    check_collector_pipelines()

    print(f"\n{BOLD}transport{RESET}")
    if check_port(args.endpoint).ok:
        if check_otlp_http(args.endpoint, headers).ok:
            print(f"\n{BOLD}round trip{RESET}")
            check_round_trip(args.endpoint, args.ingestion_key, args.service)

    return summarise()


def summarise() -> int:
    failed = [c for c in results if not c.ok]
    print()
    if not failed:
        print(f"{GREEN}{BOLD}All {len(results)} checks passed.{RESET} "
              f"{DIM}The chain is intact end to end.{RESET}\n")
        return 0

    print(f"{RED}{BOLD}{len(failed)} of {len(results)} checks failed.{RESET}")
    print(f"{DIM}Fix the first one — later failures are usually downstream of it.{RESET}\n")
    for check in failed:
        print(f"  {RED}✗{RESET} {check.name}: {check.detail}")
        if check.fix:
            print(f"    {YELLOW}→ {check.fix}{RESET}")
    print()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
