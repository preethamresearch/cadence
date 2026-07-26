/* Hero visual.
 *
 * Prefers the three.js terrain in hero3d.js, where the silence between turns
 * is carved out as a canyon you can see into. Falls back to a flat 2D ribbon
 * if WebGL is unavailable — locked-down browsers, VMs, driver blocklists —
 * because an empty hero is worse than a simple one.
 *
 * Both tell the same story: user speaks, silence opens, agent replies, user
 * cuts in.
 */

const canvas = document.getElementById("hero-ribbon");

let mounted3d = false;
try {
  const { mountHero } = await import("./hero3d.js");
  mountHero(canvas);
  mounted3d = true;
} catch (err) {
  console.warn("cadence: WebGL hero unavailable, falling back to 2D", err);
}

if (!mounted3d) start2DRibbon(canvas);

/* ── 2D fallback ────────────────────────────────────────────────── */

function start2DRibbon(canvas) {
  const ctx = canvas.getContext("2d");
  if (!ctx) return;

  const SKY = "56,189,248";
  const VIOLET = "167,139,250";
  const AMBER = "251,191,36";
  const ROSE = "251,113,133";

  const CYCLE = 9200;
  const script = [
    { from: 0,    to: 2100, kind: "user" },
    { from: 2100, to: 2760, kind: "silence" },
    { from: 2760, to: 4900, kind: "agent" },
    { from: 4900, to: 4910, kind: "barge" },
    { from: 4910, to: 6300, kind: "user" },
    { from: 6300, to: 6620, kind: "silence" },
    { from: 6620, to: 9200, kind: "agent" },
  ];

  const dpr = window.devicePixelRatio || 1;
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

  // Deterministic so the waveform is stable across frames rather than
  // shimmering — per-frame randomness reads as static, not speech.
  const noise = (seed) => {
    const x = Math.sin(seed * 12.9898) * 43758.5453;
    return x - Math.floor(x);
  };

  const envelope = (kind, t, seed) =>
    kind === "user"
      ? 0.30 + Math.abs(Math.sin(t / 170)) * 0.48 + noise(seed) * 0.14
      : 0.26 + Math.abs(Math.sin(t / 138)) * 0.42 + noise(seed) * 0.11;

  const segmentAt = (ms) => script.find((s) => ms >= s.from && ms < s.to);

  function draw(now) {
    const elapsed = now % CYCLE;
    const mid = h / 2;
    ctx.clearRect(0, 0, w, h);

    const pxPerMs = w / CYCLE;
    const step = 14;
    const barW = Math.max(2, pxPerMs * step * 0.55);

    for (const seg of script) {
      if (seg.kind !== "silence") continue;
      const x0 = seg.from * pxPerMs;
      const x1 = seg.to * pxPerMs;

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

    const barge = script.find((s) => s.kind === "barge");
    const bx = barge.from * pxPerMs;
    ctx.save();
    ctx.strokeStyle = `rgba(${ROSE},0.95)`;
    ctx.lineWidth = 2;
    ctx.shadowColor = `rgba(${ROSE},0.9)`;
    ctx.shadowBlur = 12;
    ctx.beginPath();
    ctx.moveTo(bx, 6); ctx.lineTo(bx, h - 6);
    ctx.stroke();
    ctx.restore();
    ctx.fillStyle = `rgba(${ROSE},1)`;
    ctx.beginPath(); ctx.arc(bx, 6, 3.5, 0, Math.PI * 2); ctx.fill();
    ctx.font = "600 9px 'JetBrains Mono', monospace";
    ctx.textAlign = "left";
    ctx.fillText("interrupted", bx + 7, h - 6);

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
}
