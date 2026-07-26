/* Audio capture and playback.
 *
 * Two independent AudioContexts, because Gemini Live speaks 16 kHz in and
 * 24 kHz out, and resampling either direction by hand costs latency and
 * fidelity for no benefit — the browser will do it natively if you just ask
 * for the right rate.
 */

const INPUT_RATE = 16000;
const OUTPUT_RATE = 24000;

export class AudioEngine {
  constructor({ onChunk, onLevel }) {
    this.onChunk = onChunk;
    this.onLevel = onLevel;

    this.captureCtx = null;
    this.playbackCtx = null;
    this.stream = null;
    this.workletNode = null;
    this.analyser = null;
    this.outAnalyser = null;

    /* When the next queued buffer should start. Scheduling against this rather
     * than currentTime is what keeps playback gapless. */
    this._cursor = 0;
    this._sources = new Set();
    this._levelTimer = null;
  }

  async start() {
    this.stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: 1,
        echoCancellation: true,
        noiseSuppression: true,
        // Without this the agent's own voice comes back through the mic and
        // trips barge-in detection against itself.
        autoGainControl: true,
      },
    });

    this.captureCtx = new AudioContext({ sampleRate: INPUT_RATE });
    await this.captureCtx.audioWorklet.addModule("/static/js/pcm-processor.js");

    const source = this.captureCtx.createMediaStreamSource(this.stream);
    this.analyser = this.captureCtx.createAnalyser();
    this.analyser.fftSize = 512;
    this.analyser.smoothingTimeConstant = 0.75;

    this.workletNode = new AudioWorkletNode(this.captureCtx, "pcm-processor");
    this.workletNode.port.onmessage = (event) => this.onChunk?.(event.data);

    source.connect(this.analyser);
    source.connect(this.workletNode);
    // The worklet has no output; connecting it to the destination would echo
    // the microphone back at the user.

    this.playbackCtx = new AudioContext({ sampleRate: OUTPUT_RATE });
    this.outAnalyser = this.playbackCtx.createAnalyser();
    this.outAnalyser.fftSize = 512;
    this.outAnalyser.smoothingTimeConstant = 0.75;
    this.outAnalyser.connect(this.playbackCtx.destination);
    this._cursor = this.playbackCtx.currentTime;

    this._startLevelLoop();
  }

  /** Queue a chunk of 16-bit PCM from the model. */
  enqueue(arrayBuffer) {
    if (!this.playbackCtx) return;

    const pcm = new Int16Array(arrayBuffer);
    if (!pcm.length) return;

    const buffer = this.playbackCtx.createBuffer(1, pcm.length, OUTPUT_RATE);
    const channel = buffer.getChannelData(0);
    for (let i = 0; i < pcm.length; i++) channel[i] = pcm[i] / 32768;

    const node = this.playbackCtx.createBufferSource();
    node.buffer = buffer;
    node.connect(this.outAnalyser);

    // If playback has fallen behind, restart from now rather than trying to
    // catch up — chasing a stale cursor stacks buffers and sounds robotic.
    const startAt = Math.max(this.playbackCtx.currentTime, this._cursor);
    node.start(startAt);
    this._cursor = startAt + buffer.duration;

    this._sources.add(node);
    node.onended = () => this._sources.delete(node);
  }

  /** Barge-in: drop everything already queued, immediately. */
  flush() {
    for (const node of this._sources) {
      try {
        node.stop();
      } catch {
        /* already finished */
      }
    }
    this._sources.clear();
    if (this.playbackCtx) this._cursor = this.playbackCtx.currentTime;
  }

  get isPlaying() {
    return this._sources.size > 0;
  }

  _startLevelLoop() {
    const inData = new Uint8Array(this.analyser.frequencyBinCount);
    const outData = new Uint8Array(this.outAnalyser.frequencyBinCount);

    const rms = (analyser, data) => {
      analyser.getByteTimeDomainData(data);
      let sum = 0;
      for (let i = 0; i < data.length; i++) {
        const v = (data[i] - 128) / 128;
        sum += v * v;
      }
      return Math.sqrt(sum / data.length);
    };

    const tick = () => {
      this.onLevel?.({
        input: rms(this.analyser, inData),
        output: rms(this.outAnalyser, outData),
        playing: this.isPlaying,
      });
      this._levelTimer = requestAnimationFrame(tick);
    };
    tick();
  }

  async stop() {
    if (this._levelTimer) cancelAnimationFrame(this._levelTimer);
    this.flush();
    this.stream?.getTracks().forEach((t) => t.stop());
    await this.captureCtx?.close().catch(() => {});
    await this.playbackCtx?.close().catch(() => {});
    this.captureCtx = this.playbackCtx = this.stream = null;
  }
}
