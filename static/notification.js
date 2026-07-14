/* Generation-complete notification chime.
 *
 * Default: a two-note synthetic ping generated at runtime via Web Audio.
 * Custom: a user-uploaded audio file served from ``/api/files/notification-sound``
 * — set via the Settings tab and persisted on the Settings model.
 *
 * Reuses ``primeAudioSession`` from ``tts_player.js`` so the first chime
 * during a session unblocks iOS Safari's programmatic-playback gate. */

import { state } from './state.js';
import { primeAudioSession } from './tts_player.js';

let _ctx = null;
function getCtx() {
  if (!_ctx) {
    const AC = window.AudioContext || window.webkitAudioContext;
    if (!AC) return null;
    _ctx = new AC();
  }
  return _ctx;
}

function playSyntheticPing() {
  const ctx = getCtx();
  if (!ctx) return;
  // Some browsers suspend the context on creation until a gesture has
  // touched it. ``resume()`` is safe to call repeatedly.
  if (ctx.state === 'suspended') ctx.resume().catch(() => {});
  const now = ctx.currentTime;
  for (const [t, freq] of [[0, 880], [0.12, 1175]]) {
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.type = 'sine';
    osc.frequency.value = freq;
    osc.connect(gain).connect(ctx.destination);
    gain.gain.setValueAtTime(0.0001, now + t);
    gain.gain.exponentialRampToValueAtTime(0.18, now + t + 0.015);
    gain.gain.exponentialRampToValueAtTime(0.0001, now + t + 0.12);
    osc.start(now + t);
    osc.stop(now + t + 0.13);
  }
}

let _audioEl = null;
function playCustom() {
  if (!_audioEl) _audioEl = new Audio();
  // Cache-buster on the URL so a fresh upload doesn't keep playing the
  // previous file from the browser cache.
  _audioEl.src = `/api/files/notification-sound?t=${Date.now()}`;
  _audioEl.play().catch(() => playSyntheticPing());
}

/** Play the user's notification chime (custom file or synthetic). */
export function playNotification() {
  try {
    primeAudioSession();
    const sound = state.settings && state.settings.notification_sound;
    if (sound) {
      playCustom();
    } else {
      playSyntheticPing();
    }
  } catch {
    // Swallow — a missing AudioContext or autoplay block shouldn't break
    // the chat-completion flow.
  }
}
