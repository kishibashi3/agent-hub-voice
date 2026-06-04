/**
 * voice.js — voice-gateway ブラウザクライアント
 *
 * フロー:
 *   1. WebSocket 接続 (ws:// or wss://)
 *   2. OTP 認証  {"type":"auth","code":"XXXXXX"}
 *   3. マイク取得 (getUserMedia)
 *   4. AudioWorklet でリサンプリング (48kHz → 16kHz) → WS binary 送信
 *   5. WS binary (24kHz PCM) → AudioContext で再生
 *   6. pikon! / transcript 制御メッセージ処理
 */

'use strict';

// Gemini 出力サンプルレート (24kHz)
const PLAYBACK_SAMPLE_RATE = 24000;

// WebSocket エンドポイント (same host/port)
const GATEWAY_WS_URL =
  (location.protocol === 'https:' ? 'wss' : 'ws') + '://' + location.host + '/ws';

class VoiceGatewayClient {
  constructor() {
    this.ws = null;
    this.audioCtx = null;
    this.workletNode = null;
    this.micStream = null;
    this.isConnected = false;

    // 逐次再生キュー (Promise chain で順番を保証)
    this._playbackQueue = Promise.resolve();

    // コールバック
    this._onStatusChange = null; // (status: string) => void
    this._onTranscript = null;   // (speaker: 'user'|'model', text: string) => void
    this._onPikon = null;        // (from: string, preview: string) => void
  }

  // ---- public API ----------------------------------------------------------

  onStatusChange(fn) { this._onStatusChange = fn; }
  onTranscript(fn)   { this._onTranscript = fn; }
  onPikon(fn)        { this._onPikon = fn; }

  /**
   * OTP コードを指定して gateway に接続する。
   * @param {string} otpCode - 6 桁のワンタイムコード
   */
  async connect(otpCode) {
    this._setStatus('connecting');

    this.ws = new WebSocket(GATEWAY_WS_URL);
    this.ws.binaryType = 'arraybuffer';

    // 接続確立を待つ
    await new Promise((resolve, reject) => {
      this.ws.onopen = resolve;
      this.ws.onerror = () => reject(new Error('WebSocket 接続失敗'));
    });

    this.ws.onmessage = (e) => this._onMessage(e);
    this.ws.onclose = () => {
      this.isConnected = false;
      this._setStatus('disconnected');
      this._stopMic();
    };
    this.ws.onerror = (e) => {
      console.error('[voice-gateway] WS error', e);
      this._setStatus('error: ws_error');
    };

    // OTP 認証メッセージを送信
    this.ws.send(JSON.stringify({ type: 'auth', code: otpCode }));
    this._setStatus('authenticating');
  }

  disconnect() {
    this._stopMic();
    if (this.ws) {
      this.ws.close();
      this.ws = null;
    }
  }

  // ---- private: メッセージ処理 --------------------------------------------

  async _onMessage(event) {
    if (event.data instanceof ArrayBuffer) {
      // Gemini 応答音声 (PCM 24kHz)
      this._playAudio(event.data);
      return;
    }

    let msg;
    try {
      msg = JSON.parse(event.data);
    } catch {
      return;
    }

    switch (msg.type) {
      case 'auth_ok':
        this._setStatus('auth_ok');
        // マイクを起動してセッション開始を待つ
        try {
          await this._startMic();
        } catch (e) {
          this._setStatus('error: mic_failed');
          console.error('[voice-gateway] mic error', e);
          this.disconnect();
        }
        break;

      case 'session_ready':
        this.isConnected = true;
        this._setStatus('listening');
        break;

      case 'pikon':
        this._playPikon();
        if (this._onPikon) this._onPikon(msg.from, msg.preview);
        break;

      case 'transcript':
        if (this._onTranscript) this._onTranscript(msg.speaker, msg.text);
        break;

      case 'error':
        console.error('[voice-gateway] error:', msg.code, msg.message);
        this._setStatus('error: ' + msg.code);
        this.disconnect();
        break;
    }
  }

  // ---- private: マイク / AudioWorklet -------------------------------------

  async _startMic() {
    // AudioContext を起動 (ユーザージェスチャー後なので自動再生ポリシーに合致)
    this.audioCtx = new AudioContext({ sampleRate: 48000 });

    // AudioWorklet モジュールをロード
    await this.audioCtx.audioWorklet.addModule('/worklet.js');

    // マイク取得
    this.micStream = await navigator.mediaDevices.getUserMedia({
      audio: {
        sampleRate: 48000,
        channelCount: 1,
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
    });

    const source = this.audioCtx.createMediaStreamSource(this.micStream);
    this.workletNode = new AudioWorkletNode(this.audioCtx, 'resampler-16k');

    // Worklet からリサンプル済み PCM を受け取って WS 送信
    this.workletNode.port.onmessage = (e) => {
      if (this.ws && this.ws.readyState === WebSocket.OPEN) {
        this.ws.send(e.data); // PCM 16kHz binary
      }
    };

    // マイク → Worklet (スピーカーには繋がない)
    source.connect(this.workletNode);
  }

  _stopMic() {
    if (this.workletNode) {
      this.workletNode.disconnect();
      this.workletNode = null;
    }
    if (this.micStream) {
      this.micStream.getTracks().forEach(t => t.stop());
      this.micStream = null;
    }
    if (this.audioCtx) {
      this.audioCtx.close().catch(() => {});
      this.audioCtx = null;
    }
  }

  // ---- private: 音声再生 --------------------------------------------------

  _playAudio(pcmBuffer) {
    // Promise チェーンで順次再生（バッファが詰まらないように）
    this._playbackQueue = this._playbackQueue.then(async () => {
      if (!this.audioCtx) return;

      const pcm = new Int16Array(pcmBuffer);
      const float32 = new Float32Array(pcm.length);
      for (let i = 0; i < pcm.length; i++) {
        float32[i] = pcm[i] / (pcm[i] < 0 ? 0x8000 : 0x7FFF);
      }

      const audioBuffer = this.audioCtx.createBuffer(
        1,
        float32.length,
        PLAYBACK_SAMPLE_RATE,
      );
      audioBuffer.getChannelData(0).set(float32);

      const source = this.audioCtx.createBufferSource();
      source.buffer = audioBuffer;
      source.connect(this.audioCtx.destination);

      return new Promise((resolve) => {
        source.onended = resolve;
        source.start();
      });
    }).catch(() => {}); // エラーは無視してキューを継続
  }

  // ---- private: pikon! チャイム -------------------------------------------

  _playPikon() {
    try {
      const ctx = new AudioContext();
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.connect(gain);
      gain.connect(ctx.destination);
      osc.type = 'sine';
      // A5 (880Hz) → E6 (1320Hz) の上昇音型
      osc.frequency.setValueAtTime(880, ctx.currentTime);
      osc.frequency.setValueAtTime(1320, ctx.currentTime + 0.1);
      gain.gain.setValueAtTime(0.3, ctx.currentTime);
      gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.3);
      osc.start(ctx.currentTime);
      osc.stop(ctx.currentTime + 0.3);
      setTimeout(() => ctx.close().catch(() => {}), 1000);
    } catch (e) {
      console.warn('[voice-gateway] pikon! failed:', e);
    }
  }

  // ---- private: ユーティリティ --------------------------------------------

  _setStatus(s) {
    console.log('[voice-gateway] status:', s);
    if (this._onStatusChange) this._onStatusChange(s);
  }
}

// グローバルに export
window.VoiceGatewayClient = VoiceGatewayClient;
