/* The live conversation ribbon.
 *
 * A scrolling, real-time view of who holds the floor. This is the centrepiece
 * of the console and the argument the project makes, animated:
 *
 *   you speaking  ->  SILENCE  ->  agent speaking
 *
 * The silence is drawn as a widening void with the elapsed milliseconds
 * ticking up inside it. In a request/response trace view that interval is
 * invisible — it is the space between two spans. Here it is the largest thing
 * on screen, because to the person talking to the agent it is the only part
 * of the turn they actually experience.
 */

const WINDOW_MS = 11000; // visible history
/* Sample spacing. Pushing at full 60fps packs bars ~1.5px apart at typical
 * widths, which renders as a solid slab rather than a waveform — the gaps
 * between bars are what make it legible as sound. */
const FRAME_MS = 30;

export const Floor = {
  IDLE: "idle",
  USER: "user",
  SILENCE: "silence",
  AGENT: "agent",
};

const COLOURS = {
  [Floor.USER]: { core: "56,189,248", glow: "56,189,248" },    // sky
  [Floor.AGENT]: { core: "167,139,250", glow: "167,139,250" }, // violet
  [Floor.SILENCE]: { core: "251,191,36", glow: "251,191,36" }, // amber
  [Floor.IDLE]: { core: "58,68,84", glow: "58,68,84" },
};

export class Ribbon {
  constructor(canvas) {
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d");
    this.samples = [];     // { t, level, floor }
    this.markers = [];     // { t, kind, label }
    this.floor = Floor.IDLE;
    this.silenceStart = null;
    this.frozenTtfa = null;
    this._dpr = window.devicePixelRatio || 1;
    this._resize();
    window.addEventListener("resize", () => this._resize());
  }

  _resize() {
    const rect = this.canvas.getBoundingClientRect();
    this.w = rect.width;
    this.h = rect.height;
    this.canvas.width = this.w * this._dpr;
    this.canvas.height = this.h * this._dpr;
    this.ctx.setTransform(this._dpr, 0, 0, this._dpr, 0, 0);
  }

  setFloor(floor) {
    if (floor === this.floor) return;
    if (floor === Floor.SILENCE) {
      this.silenceStart = performance.now();
      this.frozenTtfa = null;
    }
    if (floor === Floor.AGENT) this.silenceStart = null;
    this.floor = floor;
  }

  /** Freeze the displayed gap at the server's authoritative TTFA. */
  freezeTtfa(ms) {
    this.frozenTtfa = ms;
    this.silenceStart = null;
  }

  mark(kind, label) {
    this.markers.push({ t: performance.now(), kind, label });
  }

  push(level) {
    const t = performance.now();
    // Throttle to the display spacing regardless of caller frame rate.
    if (this._lastPush && t - this._lastPush < FRAME_MS * 0.9) return;
    this._lastPush = t;
    this.samples.push({ t, level, floor: this.floor });
    const cutoff = t - WINDOW_MS - 500;
    while (this.samples.length && this.samples[0].t < cutoff) this.samples.shift();
    while (this.markers.length && this.markers[0].t < cutoff) this.markers.shift();
  }

  /** Milliseconds of silence elapsed right now, for the live counter. */
  currentGapMs() {
    if (this.frozenTtfa != null) return this.frozenTtfa;
    if (this.silenceStart == null) return null;
    return performance.now() - this.silenceStart;
  }

  draw() {
    const { ctx, w, h } = this;
    const now = performance.now();
    const mid = h / 2;

    ctx.clearRect(0, 0, w, h);

    // Centre rule — the zero line the waveform pivots around.
    ctx.strokeStyle = "rgba(255,255,255,0.05)";
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(0, mid + 0.5);
    ctx.lineTo(w, mid + 0.5);
    ctx.stroke();

    const xFor = (t) => w - ((now - t) / WINDOW_MS) * w;

    // --- silence bands, drawn behind the waveform -------------------
    let runStart = null;
    for (let i = 0; i < this.samples.length; i++) {
      const s = this.samples[i];
      const isSilence = s.floor === Floor.SILENCE;
      if (isSilence && runStart === null) runStart = s.t;
      if ((!isSilence || i === this.samples.length - 1) && runStart !== null) {
        const x0 = xFor(runStart);
        const x1 = xFor(s.t);
        this._drawSilence(x0, x1, h, mid);
        runStart = null;
      }
    }

    // --- waveform ---------------------------------------------------
    const spacing = (w / WINDOW_MS) * FRAME_MS;
    const barW = Math.max(2, spacing * 0.58); // leave air between bars
    for (const s of this.samples) {
      if (s.floor === Floor.SILENCE || s.floor === Floor.IDLE) continue;
      const x = xFor(s.t);
      if (x < -barW) continue;
      // Scaled so ordinary speech peaks around three quarters of the height:
      // enough to fill the frame, with headroom left so loud passages still
      // read as louder instead of clipping flat.
      const amp = Math.min(1, s.level * 4.2) * (mid - 26);
      const c = COLOURS[s.floor];
      const grad = ctx.createLinearGradient(0, mid - amp, 0, mid + amp);
      grad.addColorStop(0, `rgba(${c.core},0.95)`);
      grad.addColorStop(0.5, `rgba(${c.core},0.55)`);
      grad.addColorStop(1, `rgba(${c.core},0.95)`);
      ctx.fillStyle = grad;
      const barH = Math.max(2, amp * 2);
      ctx.beginPath();
      ctx.roundRect(x - barW / 2, mid - barH / 2, barW, barH, barW / 2);
      ctx.fill();
    }

    // --- barge-in markers -------------------------------------------
    for (const m of this.markers) {
      const x = xFor(m.t);
      if (x < 0 || x > w) continue;
      ctx.save();
      ctx.strokeStyle = "rgba(251,111,132,0.95)";
      ctx.lineWidth = 2;
      ctx.shadowColor = "rgba(251,111,132,0.85)";
      ctx.shadowBlur = 12;
      ctx.beginPath();
      ctx.moveTo(x, 8);
      ctx.lineTo(x, h - 8);
      ctx.stroke();
      ctx.restore();

      ctx.fillStyle = "rgba(251,111,132,1)";
      ctx.beginPath();
      ctx.arc(x, 8, 3.5, 0, Math.PI * 2);
      ctx.fill();

      ctx.font = "600 9px 'JetBrains Mono', monospace";
      ctx.fillStyle = "rgba(251,111,132,0.92)";
      ctx.textAlign = "center";
      ctx.fillText(m.label ?? "cut in", x, h - 14);
    }

    // --- the leading edge -------------------------------------------
    const edge = COLOURS[this.floor];
    const pulse = 0.55 + Math.sin(now / 260) * 0.2;
    ctx.save();
    ctx.shadowColor = `rgba(${edge.glow},0.9)`;
    ctx.shadowBlur = 16;
    ctx.fillStyle = `rgba(${edge.core},${pulse})`;
    ctx.fillRect(w - 2, 6, 2, h - 12);
    ctx.restore();
  }

  _drawSilence(x0, x1, h, mid) {
    const { ctx } = this;
    const width = Math.max(0, x1 - x0);
    if (width < 1) return;

    // A hatched void rather than a filled block: the point is that there is
    // nothing here.
    ctx.save();
    ctx.beginPath();
    ctx.rect(x0, 10, width, h - 20);
    ctx.clip();

    ctx.fillStyle = "rgba(245,177,61,0.05)";
    ctx.fillRect(x0, 10, width, h - 20);

    ctx.strokeStyle = "rgba(245,177,61,0.20)";
    ctx.lineWidth = 1;
    for (let x = x0 - h; x < x1 + h; x += 9) {
      ctx.beginPath();
      ctx.moveTo(x, h);
      ctx.lineTo(x + h, 0);
      ctx.stroke();
    }
    ctx.restore();

    // Bounding rules make the interval read as a measured span.
    ctx.setLineDash([3, 3]);
    ctx.strokeStyle = "rgba(245,177,61,0.5)";
    ctx.beginPath();
    ctx.moveTo(x0, 10); ctx.lineTo(x0, h - 10);
    ctx.moveTo(x1, 10); ctx.lineTo(x1, h - 10);
    ctx.stroke();
    ctx.setLineDash([]);

    if (width > 46) {
      ctx.font = "700 10px 'JetBrains Mono', monospace";
      ctx.fillStyle = "rgba(245,177,61,0.95)";
      ctx.textAlign = "center";
      ctx.fillText("SILENCE", (x0 + x1) / 2, mid + 3);
    }
  }
}
