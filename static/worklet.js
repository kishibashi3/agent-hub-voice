/**
 * AudioWorkletProcessor — マイク PCM のリサンプリング
 *
 * ブラウザの AudioContext は通常 48kHz で動作する。
 * Gemini Live は 16kHz の PCM を要求するため、ここでダウンサンプリングする。
 *
 * アルゴリズム: 単純な間引き (48000/16000 = 3 サンプルに 1 つを使用)
 * Float32Array → Int16Array (PCM 16-bit LE) に変換して postMessage する。
 *
 * チャンクサイズ: 960 サンプル = 60ms @ 16kHz → 1920 bytes / chunk
 */
class Resampler16kProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this._ratio = 3;        // 48kHz ÷ 16kHz
    this._buffer = [];
    this._chunkSize = 960;  // 60ms @ 16kHz
  }

  process(inputs, outputs, parameters) {
    const input = inputs[0];
    if (!input || !input[0]) return true;

    const inputChannel = input[0]; // Float32Array, 128 サンプル @ 48kHz

    // ダウンサンプリング: 3 サンプルに 1 つを間引き
    for (let i = 0; i < inputChannel.length; i += this._ratio) {
      this._buffer.push(inputChannel[i]);
    }

    // チャンク単位で送信
    while (this._buffer.length >= this._chunkSize) {
      const chunk = this._buffer.splice(0, this._chunkSize);

      // Float32 [-1, 1] → Int16 [-32768, 32767]
      const pcm = new Int16Array(chunk.length);
      for (let i = 0; i < chunk.length; i++) {
        const s = Math.max(-1, Math.min(1, chunk[i]));
        pcm[i] = s < 0 ? s * 0x8000 : s * 0x7FFF;
      }

      // ArrayBuffer をメインスレッドに転送 (zero-copy)
      this.port.postMessage(pcm.buffer, [pcm.buffer]);
    }

    return true; // プロセッサを継続
  }
}

registerProcessor('resampler-16k', Resampler16kProcessor);
