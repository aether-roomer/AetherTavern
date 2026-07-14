/* Streaming audio queue for TTS playback.
 *
 * The browser plays /api/tts/speak?... directly through an <audio>
 * element — we lean on the built-in progressive-download support
 * rather than wiring up MediaSource. Each queued item gets its own
 * <audio>; we kick the next one's HTTP request a couple seconds
 * before the current ends so the gap between bubbles stays short.
 */

import { sliceForApi } from './tts_helpers.js';
import { ttsSpeakUrl } from './api.js';


// Tiny silent MP3 used to "prime" iOS Safari's audio session during a
// real user gesture. Once a single .play() succeeds inside a click
// handler, subsequent .play() calls work without needing a fresh
// gesture. (Generated with `lame -b 8 -q 9 silence.wav silence.mp3`
// for ~80ms of silence.) Base64 inline so we don't take a network
// roundtrip for it.
const _SILENT_MP3 =
  'data:audio/mpeg;base64,' +
  '//uQxAAAAAAAAAAAAAAAAAAAAAAAWGluZwAAAA8AAAACAAAEsACQkJCQkJCQkJCQ' +
  'kJCQkJCQkJCQkJCQkJCQ////////////////////////////////////////////' +
  '/////////////////////8AAAAATGF2YzU3LjEwAAAAAAAAAAAAAAAAJAQAAAAAA' +
  'AAABLDfgsdsAAAAAAAAAAAAAAAAAAAA//uQRAAAAAAAaQAAAAAAAA0gAAAAAAABp' +
  'AAAAAAAADSAAAAATEFNRTMuOTkuM1VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV' +
  'VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV' +
  'VVVVVVVVVVVVVVVVVVVVVV';


/* Call this from a user-gesture click handler (Send button, etc.) to
 * unblock subsequent programmatic <audio>.play() calls on iOS Safari.
 * No-op on browsers that don't gate playback this way; cheap enough to
 * just always call.
 */
let _primed = false;
export function primeAudioSession() {
  if (_primed) return;
  try {
    const a = new Audio(_SILENT_MP3);
    a.muted = true;
    a.volume = 0;
    const p = a.play();
    if (p && typeof p.then === 'function') {
      p.catch(() => { /* still counts as a gesture attempt */ });
    }
    _primed = true;
  } catch {
    // Older browsers without Audio() — TTS won't work anyway.
  }
}


export class TTSPlayer {
  constructor() {
    this.queue = [];        // [{ url, audio?, requested, errored }]
    this.state = 'idle';    // 'idle' | 'playing' | 'paused'
    this._listeners = new Set();
    this._onError = null;   // (err) => void — set by chat.js to surface toasts
  }

  enqueue(text, config) {
    if (!text) return;
    const slices = sliceForApi(text, config.api);
    let added = false;
    for (const slice of slices) {
      const url = ttsSpeakUrl(slice, config);
      this.queue.push({ url, audio: null, requested: false, errored: false });
      added = true;
    }
    if (added && this.state === 'idle') {
      this._advance();
    }
  }

  pause() {
    if (this.state !== 'playing') return;
    const cur = this.queue[0];
    if (cur?.audio) {
      try { cur.audio.pause(); } catch {}
    }
    this._setState('paused');
  }

  resume() {
    if (this.state !== 'paused') return;
    const cur = this.queue[0];
    if (cur?.audio) {
      const p = cur.audio.play();
      if (p && typeof p.catch === 'function') {
        p.catch(err => this._handleError(err, cur));
      }
    }
    this._setState('playing');
  }

  stop() {
    for (const item of this.queue) {
      if (item.audio) {
        try { item.audio.pause(); } catch {}
        try {
          // Detach src + abort the in-flight HTTP fetch.
          item.audio.removeAttribute('src');
          item.audio.load();
        } catch {}
        item.audio = null;
      }
    }
    this.queue = [];
    this._setState('idle');
  }

  onStateChange(fn) {
    this._listeners.add(fn);
    fn(this.state);
    return () => this._listeners.delete(fn);
  }

  onError(fn) {
    this._onError = fn;
  }

  // --- internals -----------------------------------------------------

  _setState(s) {
    if (this.state === s) return;
    this.state = s;
    for (const fn of this._listeners) {
      try { fn(s); } catch (e) { console.error('TTS state listener error:', e); }
    }
  }

  _handleError(err, item) {
    if (item) item.errored = true;
    if (this._onError) {
      try { this._onError(err); } catch {}
    }
  }

  _prepare(item) {
    if (item.audio) return;
    const audio = new Audio();
    audio.preload = 'auto';
    audio.src = item.url;
    item.audio = audio;
    item.requested = true;

    audio.addEventListener('ended', () => this._onItemEnded(item));
    audio.addEventListener('error', () => {
      this._handleError(new Error('Audio load failed'), item);
      this._onItemEnded(item);  // skip past it
    });
    audio.addEventListener('timeupdate', () => this._tick(audio));
  }

  _onItemEnded(item) {
    // Only act if THIS item is at the head of the queue. Out-of-order
    // ended events (e.g. a prefetched item that errors before the
    // current finishes) shouldn't pop the wrong slot.
    if (this.queue[0] !== item) {
      // It's a non-head item that errored during prefetch — just drop it.
      const idx = this.queue.indexOf(item);
      if (idx > 0) this.queue.splice(idx, 1);
      return;
    }
    try { item.audio?.removeAttribute('src'); } catch {}
    try { item.audio?.load(); } catch {}
    item.audio = null;
    this.queue.shift();
    if (this.queue.length === 0) {
      this._setState('idle');
      return;
    }
    // Small gap so the previous audio's tail doesn't bleed into the next.
    setTimeout(() => {
      if (this.state === 'paused') return;
      this._advance();
    }, 120);
  }

  _advance() {
    const item = this.queue[0];
    if (!item) {
      this._setState('idle');
      return;
    }
    if (!item.audio) this._prepare(item);
    const p = item.audio.play();
    if (p && typeof p.catch === 'function') {
      p.catch(err => this._handleError(err, item));
    }
    this._setState('playing');
    // Speculative prefetch of the NEXT item — kicks off the HTTP fetch
    // so by the time current ends, next is buffered.
    this._prefetchNext();
  }

  _prefetchNext() {
    const next = this.queue[1];
    if (next && !next.requested && !next.errored) {
      this._prepare(next);
    }
  }

  _tick(audio) {
    if (this.state !== 'playing') return;
    if (this.queue[0]?.audio !== audio) return;
    const dur = audio.duration;
    if (Number.isFinite(dur) && dur > 0) {
      if (dur - audio.currentTime <= 2.0) this._prefetchNext();
    } else if (audio.currentTime >= 3.0) {
      // Unknown / infinite duration (streaming, no Content-Length).
      // Prefetch after we've been playing 3s — gives a tiny grace
      // period to avoid hitting the upstream rate limit if the first
      // bubble was unusually short.
      this._prefetchNext();
    }
  }
}
