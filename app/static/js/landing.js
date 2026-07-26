/* Hero ribbon.
 *
 * A looping, synthetic version of what the console draws live: the user
 * speaking, the silence, the agent replying, and an interruption. The point of
 * putting it above the fold is that the argument the project makes is visual —
 * the amber void in the middle is the thing nobody measures.
 *
 * Deliberately not the real Ribbon class: this one loops a fixed script and
 * needs no audio context, so the page stays static-hostable.
 */

const canvas = document.getElementById("hero-ribbon");
const ctx = canvas.getContext("2d");

const SKY = "56,189,248";
const VIOLET = "167,139,250";
const AMBER = "251,191,36";
const ROSE = "251,113,133";

/* One loop of the story, in milliseconds. */
const CYCLE = 9200;
const script = [
  { from: 0,    to: 2100, kind: "user" },
  { from: 2100, to: 2760, kind: "silence" },   // 660ms of nothing
  { from: 2760, to: 4900, kind: "agent" },
  { from: 4900, to: 4910, kind: "barge" },
  { from: 4910, to: 6300, kind: "user" },
  { from: 6300, to: 6620, kind: "silence" },   // 320ms
  { from: 6620, to: 9200, kind: "agent" },
];

let dpr = window.devicePixelRatio || 1;
let w = 0;
let h = 0;

function resize() {
  const rect = canvas.getBoundingClientRect();
  w = rect.width;
  h = rect.height;
  canvas.width = w * dpr;
  canvas.height = h * dpr;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
}
resize();
window.addEventListener("resize", resize);

/* Deterministic pseudo-noise so the waveform is stable across frames rather
 * than shimmering — Math.random() per frame reads as static, not speech. */
function noise(seed) {
  const x = Math.sin(seed * 12.9898) * 43758.5453;
  return x - Math.floor(x);
}

function envelope(kind, t, seed) {
  if (kind === "user") {
    return 0.30 + Math.abs(Math.sin(t / 170)) * 0.48 + noise(seed) * 0.14;
  }
  return 0.26 + Math.abs(Math.sin(t / 138)) * 0.42 + noise(seed) * 0.11;
}

function segmentAt(ms) {
  return script.find((s) => ms >= s.from && ms < s.to);
}

function draw(now) {
  const elapsed = now % CYCLE;
  const mid = h / 2;
  ctx.clearRect(0, 0, w, h);

  const pxPerMs = w / CYCLE;
  const step = 14; // ms between bars
  const barW = Math.max(2, pxPerMs * step * 0.55);

  // Silence bands behind everything.
  // The whole cycle is drawn every frame rather than revealed progressively:
  // as a hero this has to communicate the entire story at a glance, and a
  // panel that is empty for most of its loop communicates nothing.
  for (const seg of script) {
    if (seg.kind !== "silence") continue;
    const x0 = seg.from * pxPerMs;
    const x1 = seg.to * pxPerMs;
    if (x1 <= x0) continue;

    ctx.save();
    ctx.beginPath();
    ctx.rect(x0, 8, x1 - x0, h - 16);
    ctx.clip();
    ctx.fillStyle = `rgba(${AMBER},0.06)`;
    ctx.fillRect(x0, 8, x1 - x0, h - 16);
    ctx.strokeStyle = `rgba(${AMBER},0.22)`;
    ctx.lineWidth = 1;
    for (let x = x0 - h; x < x1 + h; x += 8) {
      ctx.beginPath();
      ctx.moveTo(x, h);
      ctx.lineTo(x + h, 0);
      ctx.stroke();
    }
    ctx.restore();

    ctx.setLineDash([3, 3]);
    ctx.strokeStyle = `rgba(${AMBER},0.5)`;
    ctx.beginPath();
    ctx.moveTo(x0, 8); ctx.lineTo(x0, h - 8);
    ctx.moveTo(x1, 8); ctx.lineTo(x1, h - 8);
    ctx.stroke();
    ctx.setLineDash([]);

    if (x1 - x0 > 34) {
      ctx.font = "700 10px 'JetBrains Mono', monospace";
      ctx.fillStyle = `rgba(${AMBER},0.95)`;
      ctx.textAlign = "center";
      ctx.fillText(`${seg.to - seg.from}ms`, (x0 + x1) / 2, mid + 3.5);
    }
  }

  // Full waveform. Bars ahead of the playhead are dimmed rather than hidden,
  // so the shape of the conversation is always readable.
  for (let t = 0; t < CYCLE; t += step) {
    const seg = segmentAt(t);
    if (!seg || seg.kind === "silence" || seg.kind === "barge") continue;
    const colour = seg.kind === "user" ? SKY : VIOLET;
    const amp = Math.min(1, envelope(seg.kind, t, t)) * (mid - 20);
    const x = t * pxPerMs;
    const played = t <= elapsed ? 1 : 0.26;
    const grad = ctx.createLinearGradient(0, mid - amp, 0, mid + amp);
    grad.addColorStop(0, `rgba(${colour},${0.92 * played})`);
    grad.addColorStop(0.5, `rgba(${colour},${0.5 * played})`);
    grad.addColorStop(1, `rgba(${colour},${0.92 * played})`);
    ctx.fillStyle = grad;
    ctx.beginPath();
    ctx.roundRect(x - barW / 2, mid - amp, barW, amp * 2, barW / 2);
    ctx.fill();
  }

  // Interruption marker.
  const barge = script.find((s) => s.kind === "barge");
  {
    const x = barge.from * pxPerMs;
    ctx.save();
    ctx.strokeStyle = `rgba(${ROSE},0.95)`;
    ctx.lineWidth = 2;
    ctx.shadowColor = `rgba(${ROSE},0.9)`;
    ctx.shadowBlur = 12;
    ctx.beginPath();
    ctx.moveTo(x, 6); ctx.lineTo(x, h - 6);
    ctx.stroke();
    ctx.restore();
    ctx.fillStyle = `rgba(${ROSE},1)`;
    ctx.beginPath(); ctx.arc(x, 6, 3.5, 0, Math.PI * 2); ctx.fill();
    ctx.font = "600 9px 'JetBrains Mono', monospace";
    ctx.textAlign = "left";
    ctx.fillText("interrupted", x + 7, h - 6);
  }

  // Playhead.
  const px = elapsed * pxPerMs;
  const seg = segmentAt(elapsed);
  const edge = seg?.kind === "agent" ? VIOLET : seg?.kind === "silence" ? AMBER : SKY;
  ctx.save();
  ctx.shadowColor = `rgba(${edge},0.9)`;
  ctx.shadowBlur = 14;
  ctx.fillStyle = `rgba(${edge},0.85)`;
  ctx.fillRect(px - 1, 4, 2, h - 8);
  ctx.restore();

  requestAnimationFrame(draw);
}

requestAnimationFrame(draw);
