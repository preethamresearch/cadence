/* Cadence console.
 *
 * Renders the same turn structure cadence writes to SigNoz, live, from the
 * recorder's event hook. Every number shown is a value that went onto a span —
 * the console is a view of the trace, not a parallel measurement.
 */

import { AudioEngine } from "./audio.js";
import { Ribbon, Floor } from "./ribbon.js";
import { renderTurn } from "./waterfall.js";

const $ = (id) => document.getElementById(id);
const root = document.documentElement;

const els = {
  stateLabel: $("state-label"),
  modelChip: $("model-chip"),
  micButton: $("mic-button"),
  micLabel: $("mic-label"),
  traceLink: $("trace-link"),
  ribbon: $("ribbon"),
  gapReadout: $("gap-readout"),
  gapValue: $("gap-value"),
  gapCaption: $("gap-caption"),
  caption: $("caption"),
  captionHint: $("caption-hint"),
  statTurns: $("stat-turns"),
  statBarge: $("stat-barge"),
  statP50: $("stat-p50"),
  statTools: $("stat-tools"),
  statTokens: $("stat-tokens"),
  statTokensLabel: $("stat-tokens-label"),
  waterfall: $("waterfall"),
  toolLog: $("tool-log"),
  toast: $("toast"),
};

const state = {
  socket: null,
  engine: null,
  live: false,
  config: {},
  turns: 0,
  bargeIns: 0,
  toolCalls: 0,
  ttfaSamples: [],
  pendingBargeIn: null,
  lines: { user: null, agent: null },
};

const ribbon = new Ribbon(els.ribbon);

/* Thresholds match the server's spoken verdicts, so the screen and the agent
 * never disagree about whether latency was good. */
const gradeFor = (ms) => (ms < 500 ? "good" : ms < 1500 ? "ok" : "bad");

/* ── chrome ───────────────────────────────────────────────── */

function toast(message, isError = false) {
  els.toast.textContent = message;
  els.toast.classList.toggle("error", isError);
  els.toast.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => (els.toast.hidden = true), 5200);
}

function setState(next) {
  root.dataset.state = next;
  els.stateLabel.textContent = next;
}

function setFloor(floor) {
  root.dataset.floor = floor;
  ribbon.setFloor(floor);
}

/* ── render loop ──────────────────────────────────────────── */
/* One rAF drives the ribbon and the live gap counter. Running the counter
 * here rather than on a timer keeps the number in step with the void it is
 * measuring. */

function frame() {
  ribbon.draw();

  const gap = ribbon.currentGapMs();
  if (gap != null && ribbon.frozenTtfa == null) {
    els.gapValue.textContent = Math.round(gap);
    els.gapReadout.dataset.grade = "counting";
    els.gapCaption.textContent = "waiting for first audio…";
  }
  requestAnimationFrame(frame);
}
requestAnimationFrame(frame);

/* ── connection ───────────────────────────────────────────── */

async function connect() {
  setState("connecting");
  els.micLabel.textContent = "connecting";

  try {
    state.config = await fetch("/api/config").then((r) => r.json());
    els.modelChip.textContent = state.config.model ?? "—";
  } catch {
    state.config = {};
  }

  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const socket = new WebSocket(`${proto}//${location.host}/ws`);
  socket.binaryType = "arraybuffer";
  state.socket = socket;

  socket.onopen = async () => {
    try {
      state.engine = new AudioEngine({
        onChunk: (buf) => {
          if (socket.readyState === WebSocket.OPEN) socket.send(buf);
        },
        onLevel: ({ input, output, playing }) => {
          const agentActive = playing || output > 0.012;
          ribbon.push(agentActive ? output : input);
        },
      });
      await state.engine.start();
      state.live = true;
      setState("live");
      setFloor(Floor.IDLE);
      els.micLabel.textContent = "stop";
    } catch (err) {
      toast(
        err.name === "NotAllowedError"
          ? "Microphone permission denied — cadence needs it to trace a voice session."
          : `Could not start audio: ${err.message}`,
        true
      );
      disconnect();
    }
  };

  socket.onmessage = (event) => {
    if (event.data instanceof ArrayBuffer) state.engine?.enqueue(event.data);
    else handleMessage(JSON.parse(event.data));
  };

  socket.onerror = () => toast("Connection error.", true);
  socket.onclose = () => {
    if (state.live) toast("Session ended.");
    disconnect();
  };
}

function disconnect() {
  state.live = false;
  setState("idle");
  setFloor(Floor.IDLE);
  els.micLabel.textContent = "start";
  state.engine?.stop();
  state.engine = null;
  if (state.socket && state.socket.readyState <= WebSocket.OPEN) state.socket.close();
  state.socket = null;
}

/* ── messages ─────────────────────────────────────────────── */

function handleMessage(msg) {
  switch (msg.type) {
    case "ready":
      els.modelChip.textContent = msg.model ?? els.modelChip.textContent;
      if (msg.trace_id && state.config.signoz_base_url) {
        els.traceLink.href = `${state.config.signoz_base_url}/trace/${msg.trace_id}`;
        els.traceLink.hidden = false;
      }
      break;

    case "interrupted":
      // Flush queued playback the instant the model reports it was cut off,
      // or the user keeps hearing a reply they already talked over.
      state.engine?.flush();
      break;

    case "transcript":
      appendUtterance(msg.role, msg.text);
      break;

    case "tool_start":
      logTool(msg.name, null, true);
      break;

    case "tool_done":
      state.toolCalls += 1;
      els.statTools.textContent = state.toolCalls;
      logTool(msg.name, msg.result, false);
      break;

    case "telemetry":
      handleTelemetry(msg.event, msg.data);
      break;

    case "fatal":
      toast(msg.message, true);
      disconnect();
      break;
  }
}

function handleTelemetry(kind, data) {
  switch (kind) {
    case "user_speech_start":
      setFloor(Floor.USER);
      state.lines.user = null;
      break;

    case "user_speech_end":
      // The gap opens here and the counter starts climbing.
      setFloor(Floor.SILENCE);
      break;

    case "ttfa":
      setFloor(Floor.AGENT);
      state.lines.agent = null;
      freezeGap(data.ttfa_ms);
      break;

    case "barge_in":
      state.bargeIns += 1;
      els.statBarge.textContent = state.bargeIns;
      state.pendingBargeIn = data.offset_ms;
      ribbon.mark("barge", `cut in @${Math.round(data.offset_ms)}ms`);
      state.lines.agent?.classList.add("cut");
      state.lines.agent = null;
      break;

    case "turn_end":
      setFloor(Floor.IDLE);
      state.turns += 1;
      els.statTurns.textContent = state.turns;
      renderTurn(els.waterfall, data, state.pendingBargeIn);
      state.pendingBargeIn = null;
      break;

    case "usage":
      updateTokens(data);
      break;

    case "session_close":
      setFloor(Floor.IDLE);
      break;
  }
}

/* ── the gap readout ──────────────────────────────────────── */

function freezeGap(ms) {
  if (ms == null) return;
  ribbon.freezeTtfa(ms);
  state.ttfaSamples.push(ms);

  els.gapValue.textContent = Math.round(ms);
  els.gapReadout.dataset.grade = gradeFor(ms);
  els.gapCaption.textContent = "time to first audio";

  const sorted = [...state.ttfaSamples].sort((a, b) => a - b);
  els.statP50.textContent = `${Math.round(sorted[Math.floor(sorted.length / 2)])}ms`;
}

/* ── captions ─────────────────────────────────────────────── */

function appendUtterance(role, text) {
  els.captionHint?.remove();

  // Transcription streams in fragments; append into the speaker's active line
  // rather than making a new one per fragment.
  let line = state.lines[role];
  if (!line) {
    els.caption.innerHTML = "";
    line = document.createElement("div");
    line.className = `utterance ${role}`;
    line.innerHTML = `<span class="who">${role}</span><span class="said"></span>`;
    els.caption.appendChild(line);
    state.lines[role] = line;
  }
  line.querySelector(".said").textContent += text;
}

/* ── tool log ─────────────────────────────────────────────── */

function logTool(name, result, pending) {
  els.toolLog.querySelector(".empty")?.remove();

  if (pending) {
    const li = document.createElement("li");
    li.className = "tool-entry pending";
    li.dataset.tool = name;
    li.innerHTML = `<span class="tool-name">${name}</span>
                    <span class="tool-result">querying SigNoz…</span>`;
    els.toolLog.prepend(li);
    return;
  }

  const li = els.toolLog.querySelector(`.tool-entry.pending[data-tool="${name}"]`);
  if (!li) return;
  li.classList.remove("pending");
  const source = (result?.source ?? "unknown").replace("_", " ");
  li.innerHTML = `<span class="tool-name">${name}</span>
                  <span class="tool-result">${summarise(result)}</span>
                  <span class="tool-source">${source}</span>`;
}

function summarise(result) {
  if (!result) return "no result";
  if (result.error) return `error: ${result.error}`;
  if (result.p95_ms != null) return `p95 ${Math.round(result.p95_ms)}ms`;
  if (result.barge_ins != null) return `${result.barge_ins} interruptions`;
  if (result.total != null) return `${Math.round(result.total)} tokens`;
  if (result.turns != null) return `${result.turns} turns`;
  return result.note ?? "ok";
}

/* ── tokens ───────────────────────────────────────────────── */

function updateTokens(usage) {
  const totals = {};
  for (const key of ["prompt_tokens_details", "response_tokens_details"]) {
    for (const entry of usage[key] ?? []) {
      const modality = (entry.modality ?? "unspecified").toLowerCase();
      totals[modality] = (totals[modality] ?? 0) + (entry.token_count ?? 0);
    }
  }
  const [top] = Object.entries(totals).sort((a, b) => b[1] - a[1]);
  if (!top) return;
  els.statTokens.textContent = top[1].toLocaleString();
  els.statTokensLabel.textContent = `${top[0]} tokens`;
}

/* ── wiring ───────────────────────────────────────────────── */

els.micButton.addEventListener("click", () => (state.live ? disconnect() : connect()));
window.addEventListener("beforeunload", () => state.engine?.stop());

/* Replay mode: `/?replay=1` drives the console from a recorded session so the
 * public demo is not a dead page for visitors without a microphone or key. */
if (new URLSearchParams(location.search).has("replay")) {
  const { startReplay } = await import("./replay.js");
  setState("live");
  els.micButton.disabled = true;
  els.micLabel.textContent = "replay";
  els.modelChip.textContent = "recorded session";

  startReplay({
    ribbon,
    Floor,
    setFloor,
    onEvent: handleTelemetry,
    onTranscript: appendUtterance,
    // Route through the same counter the live path uses, so the HUD reads
    // consistently in both modes.
    onTool: (name, result, pending) => {
      if (!pending) {
        state.toolCalls += 1;
        els.statTools.textContent = state.toolCalls;
      }
      logTool(name, result, pending);
    },
    onReset: () => {
      state.turns = state.bargeIns = state.toolCalls = 0;
      state.ttfaSamples = [];
      state.lines = { user: null, agent: null };
      els.statTurns.textContent = els.statBarge.textContent = els.statTools.textContent = "0";
      els.statP50.textContent = "—";
      els.waterfall.innerHTML = "";
      els.toolLog.innerHTML = "";
    },
  });
}
