/* Scripted session replay.
 *
 * Visit `/?replay=1` to watch a recorded conversation drive the console with
 * no microphone, no API key, and no network. Two reasons this exists:
 *
 *  - The deployed demo is public. Without a key of their own, a visitor would
 *    otherwise land on a dead page.
 *  - It makes the console's own rendering testable in isolation from Gemini.
 *
 * The events below are the same shapes the server emits from the cadence
 * recorder's event hook, so this exercises the real render path. The numbers
 * are taken from an actual traced session rather than invented, so the
 * latencies and the barge-in offset are representative.
 */

const script = [
  { at: 300,  level: "user" },
  { at: 320,  event: "user_speech_start", data: {} },
  { at: 340,  transcript: ["user", "How fast have you been responding?"] },
  { at: 2100, event: "user_speech_end", data: {} },
  { at: 2100, level: "silence" },

  { at: 2442, event: "ttfa", data: { turn: 0, ttfa_ms: 342 } },
  { at: 2442, level: "agent" },
  { at: 2500, tool: ["get_response_latency", { source: "signoz", p95_ms: 455, p50_ms: 342 }] },
  { at: 2600, transcript: ["agent", "About 340 milliseconds — fast enough to feel immediate."] },
  { at: 5200, event: "turn_end", data: {
      turn: 0, ttfa_ms: 342, duration_ms: 4900, agent_audio_ms: 2600,
      interrupted: false, tool_calls: 1 } },
  { at: 5200, level: "idle" },

  { at: 6000, level: "user" },
  { at: 6000, event: "user_speech_start", data: {} },
  { at: 6020, transcript: ["user", "And how often do I cut you off?"] },
  { at: 7600, event: "user_speech_end", data: {} },
  { at: 7600, level: "silence" },

  { at: 8210, event: "ttfa", data: { turn: 1, ttfa_ms: 610 } },
  { at: 8210, level: "agent" },
  { at: 8300, tool: ["get_interruption_stats", {
      source: "live_session", barge_ins: 1, median_offset_ms: 700 }] },
  { at: 8400, transcript: ["agent", "Once so far, about seven hundred milliseconds into my reply, which usually means"] },

  // the interruption
  { at: 9100, event: "barge_in", data: { turn: 1, offset_ms: 700, source: "client_vad" } },
  { at: 9100, event: "turn_end", data: {
      turn: 1, ttfa_ms: 610, duration_ms: 3100, agent_audio_ms: 890,
      interrupted: true, tool_calls: 1 } },
  { at: 9100, level: "user" },
  { at: 9120, event: "user_speech_start", data: {} },
  { at: 9140, transcript: ["user", "Sorry — just show me the trace."] },
  { at: 10400, event: "user_speech_end", data: {} },
  { at: 10400, level: "silence" },

  { at: 10688, event: "ttfa", data: { turn: 2, ttfa_ms: 288 } },
  { at: 10688, level: "agent" },
  { at: 10800, transcript: ["agent", "Opening it in SigNoz now — every turn you just took is in there."] },
  { at: 13200, event: "turn_end", data: {
      turn: 2, ttfa_ms: 288, duration_ms: 2800, agent_audio_ms: 2100,
      interrupted: false, tool_calls: 0 } },
  { at: 13200, level: "idle" },

  { at: 14600, event: "usage", data: {
      prompt_tokens_details: [{ modality: "AUDIO", token_count: 14208 },
                              { modality: "TEXT", token_count: 1284 }],
      response_tokens_details: [{ modality: "AUDIO", token_count: 9640 }] } },
];

/* Envelope shapes per floor state, so the ribbon shows plausible speech
 * rather than a flat line. */
/* Amplitudes sit in the range a real microphone RMS actually produces
 * (roughly 0.03–0.20), so replayed speech looks like the live signal rather
 * than a louder caricature of it. */
const ENVELOPE = {
  user: (t) =>
    0.05 + Math.abs(Math.sin(t / 210)) * 0.13 + Math.abs(Math.sin(t / 71)) * 0.04 + Math.random() * 0.02,
  agent: (t) =>
    0.04 + Math.abs(Math.sin(t / 165)) * 0.11 + Math.abs(Math.sin(t / 58)) * 0.035 + Math.random() * 0.015,
  silence: () => 0.003,
  idle: () => 0.002,
};

export function startReplay({ ribbon, onEvent, onTranscript, onTool, onReset, setFloor, Floor }) {
  let level = "idle";
  const started = performance.now();
  const timers = [];

  for (const step of script) {
    timers.push(
      setTimeout(() => {
        if (step.level) {
          level = step.level;
          setFloor(Floor[step.level.toUpperCase()] ?? Floor.IDLE);
        }
        if (step.event) onEvent(step.event, step.data);
        if (step.transcript) onTranscript(...step.transcript);
        if (step.tool) {
          const [name, result] = step.tool;
          onTool(name, null, true);
          setTimeout(() => onTool(name, result, false), 420);
        }
      }, step.at)
    );
  }

  const pump = setInterval(() => {
    ribbon.push(ENVELOPE[level](performance.now() - started));
  }, 16);

  // Loop so an unattended demo screen keeps playing. Reset first, or each
  // pass stacks another copy of the same three turns onto the history.
  const loop = setTimeout(() => {
    clearInterval(pump);
    timers.forEach(clearTimeout);
    onReset?.();
    startReplay({ ribbon, onEvent, onTranscript, onTool, onReset, setFloor, Floor });
  }, 17500);

  return () => {
    clearInterval(pump);
    clearTimeout(loop);
    timers.forEach(clearTimeout);
  };
}
