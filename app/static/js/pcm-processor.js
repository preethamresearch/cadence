/* Microphone capture worklet.
 *
 * Runs on the audio thread, converts float samples to the 16-bit PCM Gemini
 * Live expects, and batches them before posting.
 *
 * The batching matters: a worklet is called with 128 frames at a time, which
 * at 16 kHz is an 8 ms chunk. Posting every chunk straight to a WebSocket would
 * mean ~125 messages a second and measurable overhead on the very latency
 * path cadence exists to measure. 1024 samples (64 ms) is small enough to
 * keep barge-in responsive and large enough to stay cheap.
 */

const BATCH_SAMPLES = 1024;

class PCMProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this._buffer = new Int16Array(BATCH_SAMPLES);
    this._offset = 0;
  }

  process(inputs) {
    const channel = inputs[0] && inputs[0][0];
    if (!channel) return true;

    for (let i = 0; i < channel.length; i++) {
      // Clamp before scaling; values outside [-1, 1] wrap to the opposite
      // sign once cast, which sounds like a loud click.
      const sample = Math.max(-1, Math.min(1, channel[i]));
      this._buffer[this._offset++] = sample < 0 ? sample * 0x8000 : sample * 0x7fff;

      if (this._offset === BATCH_SAMPLES) {
        const copy = this._buffer.slice();
        this.port.postMessage(copy.buffer, [copy.buffer]);
        this._offset = 0;
      }
    }
    return true;
  }
}

registerProcessor("pcm-processor", PCMProcessor);
