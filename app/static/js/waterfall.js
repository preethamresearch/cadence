/* Turn waterfall.
 *
 * Each row is one closed `voice.turn` span, drawn from the values that went
 * onto the span itself — no client-side timing, so what you see matches what
 * SigNoz stores.
 *
 * The layout is the argument the whole project makes: a turn is
 *
 *     [ you speaking ][ silence ][ agent speaking ]
 *
 * and the middle segment — time to first audio — is drawn as literal empty
 * space. A conventional request/response view collapses that segment away
 * entirely, which is precisely why it cannot tell you why a voice agent feels
 * broken.
 */

/* Bars share one scale so rows are comparable down the column. The scale only
 * ever grows; rescaling downward would make earlier turns appear to stretch. */
let scaleMs = 3000;

export function resetWaterfall(container) {
  scaleMs = 3000;
  container.innerHTML = "";
}

export function renderTurn(container, turn, bargeInOffsetMs) {
  container.querySelector(".empty")?.remove();

  const total = Math.max(turn.duration_ms ?? 0, 1);
  const gap = Math.max(0, turn.ttfa_ms ?? 0);

  // Decoded agent audio can exceed the turn's wall clock when playback was
  // buffered ahead of an interruption, so clamp it to what actually fit.
  const agent = Math.min(Math.max(0, turn.agent_audio_ms ?? 0), Math.max(0, total - gap));
  const user = Math.max(0, total - gap - agent);

  if (total > scaleMs) scaleMs = Math.ceil(total / 1000) * 1000;
  const pct = (ms) => (ms / scaleMs) * 100;

  const row = document.createElement("div");
  row.className = "turn-row";

  const segments = [];
  let cursor = 0;

  if (user > 0) {
    segments.push(
      `<div class="seg user" style="left:${pct(cursor)}%;width:${pct(user)}%"></div>`
    );
    cursor += user;
  }
  if (gap > 0) {
    // Only label the gap when the segment is wide enough to hold the text.
    const label = pct(gap) > 7 ? `${Math.round(gap)}ms` : "";
    segments.push(
      `<div class="seg gap" data-ms="${label}" style="left:${pct(cursor)}%;width:${pct(gap)}%"></div>`
    );
    cursor += gap;
  }
  if (agent > 0) {
    segments.push(
      `<div class="seg agent" style="left:${pct(cursor)}%;width:${pct(agent)}%"></div>`
    );
  }

  if (bargeInOffsetMs != null) {
    // Positioned relative to where the agent started speaking, which is where
    // the offset is measured from.
    const at = user + gap + Math.max(0, bargeInOffsetMs);
    segments.push(
      `<div class="barge-marker" style="left:${Math.min(99.4, pct(at))}%"
            title="You interrupted ${Math.round(bargeInOffsetMs)}ms into the reply"></div>`
    );
  }

  const ttfaClass = gap === 0 ? "" : gap < 500 ? "good" : gap < 1500 ? "" : "bad";
  const ttfaText = turn.ttfa_ms != null ? `${Math.round(turn.ttfa_ms)}ms` : "—";

  const badges = [];
  if (turn.interrupted) badges.push(`<span class="turn-badge interrupted">cut off</span>`);
  if (turn.tool_calls > 0)
    badges.push(
      `<span class="turn-badge tool">${turn.tool_calls} tool${turn.tool_calls > 1 ? "s" : ""}</span>`
    );

  row.innerHTML = `
    <span class="turn-index">#${turn.turn}</span>
    <div class="turn-track">${segments.join("")}</div>
    <div class="turn-meta">
      ${badges.join("")}
      <span class="turn-ttfa ${ttfaClass}">${ttfaText}</span>
    </div>`;

  container.prepend(row);

  // Keep the column bounded; the full history lives in SigNoz.
  while (container.children.length > 40) container.lastElementChild.remove();
}
