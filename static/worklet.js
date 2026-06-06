/**
 * AudioWorkletProcessor — マイク PCM のリサンプリング + エコー抑制ゲーティング
 *
 * ブラウザの AudioContext は通常 48kHz で動作する。
 * Gemini Live は 16kHz の PCM を要求するため、ここでダウンサンプリングする。
 *
 * アルゴリズム: 単純な間引き (48000/16000 = 3 サンプルに 1 つを使用)
 * Float32Array → Int16Array (PCM 16-bit LE) に変換して postMessage する。
 *
 * チャンクサイズ: 960 サンプル = 60ms @ 16kHz → 1920 bytes / chunk
 *
 * ## エコー抑制ゲーティング (issue #12)
 *
 * メインスレッドから { type: 'setMute', value: bool } を受け取る。
 * ミュート中はゼロ PCM を送信し、AI 発話音声がエコーとして
 * Gemini VAD に届くのを防ぐ。
 * ミュート中でも RMS が ACTIVITY_THRESHOLD を超えた場合は
 * ユーザーが話し始めたと判断し、ミュート解除 + user_activity 通知を送る。
 */
class Resampler16kProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this._ratio = 3;        // 48kHz ÷ 16kHz
    this._buffer = [];
    this._chunkSize = 960;  // 60ms @ 16kHz
    this._muted = false;

    // ミュート中にユーザー発話を検知する RMS 閾値 (Float32: 0.0〜1.0)
    // 会話音声は通常 0.05〜0.2、エアコン等の環境ノイズは 0.01 未満
    this._ACTIVITY_THRESHOLD = 0.03;

    this.port.onmessage = (e) => {
      if (e.data?.type === 'setMute') {
        this._muted = e.data.value;
      }
    };
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

      if (this._muted) {
        // ミュート中: ユーザー活性チェック (エコーか発話かを RMS で判断)
        const rms = this._rms(chunk);
        if (rms > this._ACTIVITY_THRESHOLD) {
          // ユーザーが話し始めた → ミュート解除 + メインスレッドに通知
          // 今のチャンクはユーザー音声としてそのまま送信する
          this._muted = false;
          this.port.postMessage({ type: 'user_activity' });
          // fall-through: 以下の通常 PCM 変換・送信へ
        } else {
          // AI エコーと判断 → ゼロ PCM を送信
          const silent = new Int16Array(this._chunkSize);
          this.port.postMessage(silent.buffer, [silent.buffer]);
          continue;
        }
      }

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

  /**
   * Float32 チャンクの RMS (二乗平均平方根) を計算する。
   * @param {number[]} chunk - Float32 サンプル配列
   * @returns {number} RMS 値 (0.0〜1.0)
   */
  _rms(chunk) {
    let sumSq = 0;
    for (let i = 0; i < chunk.length; i++) {
      sumSq += chunk[i] * chunk[i];
    }
    return Math.sqrt(sumSq / chunk.length);
  }
}

registerProcessor('resampler-16k', Resampler16kProcessor);
