/* "Test voice" UI block for the Settings page + entity TTS sections.
 *
 * Owns its own TTSPlayer instance so the test playback doesn't
 * interfere with a chat's auto-TTS queue. A single morphing button
 * toggles between Play (idle) and Stop (active). Textarea is editable.
 *
 * The button is disabled when ``getReady()`` returns false — used by
 * the caller to indicate "no API key configured for this provider".
 * The block subscribes to global state changes (with self-cleanup
 * once the node is removed from the DOM) so save-and-key-typing
 * updates flip the disabled state automatically.
 */

import { subscribe } from '../state.js';
import { el } from '../util.js';
import { icon, toast } from '../ui.js';
import { TTSPlayer, primeAudioSession } from '../tts_player.js';
import { stripForTTS, isTTSConfigComplete } from '../tts_helpers.js';


const DEFAULT_SAMPLE =
  'Hello! This is a quick test of the selected voice. How does it sound?';


/* renderTTSTestBlock(getCfg, opts) returns a DOM element.
 *
 *   getCfg     -> () => effective config object | null
 *   opts.getReady -> () => bool. False disables the Play button. */
export function renderTTSTestBlock(getCfg, opts = {}) {
  const getReady = opts.getReady || (() => true);
  const player = new TTSPlayer();
  player.onError((err) => toast(`TTS test error: ${err.message || err}`, 'error'));

  const textarea = el('textarea', {
    class: 'tts-test-textarea',
    rows: 2,
    placeholder: DEFAULT_SAMPLE,
    value: DEFAULT_SAMPLE,
  });

  const button = el('button', { type: 'button', class: 'btn' });

  function paint() {
    if (player.state === 'idle') {
      const cfg = getCfg();
      const hasCfg = !!cfg && isTTSConfigComplete(cfg);
      const hasKey = getReady();
      const ready = hasCfg && hasKey;
      button.disabled = !ready;
      button.classList.remove('danger');
      button.replaceChildren(icon('play', 14), document.createTextNode('Play'));
      if (ready) {
        button.title = 'Play the sample with the selected voice';
      } else if (!hasKey) {
        // API key takes precedence — without one nothing else matters.
        button.title = 'Configure an API key for this provider first.';
      } else {
        button.title = 'Pick a model + voice (or set NAI voice) first.';
      }
    } else {
      button.disabled = false;
      button.classList.add('danger');
      button.replaceChildren(icon('stop', 14), document.createTextNode('Stop'));
      button.title = 'Stop playback';
    }
  }

  button.addEventListener('click', () => {
    if (player.state !== 'idle') {
      player.stop();
      return;
    }
    primeAudioSession();
    const cfg = getCfg();
    if (!cfg || !isTTSConfigComplete(cfg)) {
      toast('Pick a model + voice (or set NAI voice) first.', 'error');
      return;
    }
    const text = (textarea.value || DEFAULT_SAMPLE).trim();
    if (!text) return;
    setTimeout(() => {
      const clean = stripForTTS(text);
      if (clean) player.enqueue(clean, cfg);
    }, 0);
  });

  player.onStateChange(paint);
  paint();

  const root = el('div', { class: 'tts-test-block' },
    el('label', { class: 'tts-row-label' }, 'Test voice'),
    textarea,
    el('div', { class: 'tts-test-actions' }, button),
  );

  // Re-evaluate `getReady` whenever the global store changes — picks
  // up the new ``api_key_indicator`` right after a settings save.
  // Self-detaches once the block is removed from the DOM so listeners
  // don't pile up across rerenders.
  const unsub = subscribe(() => {
    if (!root.isConnected) { unsub(); return; }
    paint();
  });

  return root;
}
