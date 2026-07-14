/* Edit a message: text + emotion (per bubble). */

import { api } from '../api.js';
import { el, autoGrowTextarea } from '../util.js';
import { openModal, closeModal, toast } from '../ui.js';
import { EMOTIONS } from '../constants.js';
import { makeSelect as pickerSelect } from '../avatar_picker.js';
import { loadActiveChat } from './chat.js';
import { markPending, clearPending } from '../save_queue.js';


// "(none)" sentinel for Generic-origin bubbles. We store it as ``null`` on
// the SubMessage (Pydantic widens to ``Emotion | None`` for that origin).
const NONE_EMOTION_VALUE = '__none__';
const NONE_EMOTION_OPTION = { label: '(none)', value: NONE_EMOTION_VALUE };

export function openEditMessageModal(chatId, msg) {
  // Per-bubble emotion picker shows for any LLM-generated message —
  // contact-side AER, contact-side Generic, AND impersonate-generated
  // user-side. Manually-typed user messages skip the picker.
  const isContact = msg.sender === 'contact';
  const hasEmotion = isContact || (msg.sender === 'user' && msg.origin !== 'manual');
  const isGenericOrigin = msg.origin === 'generic';
  // Edit each sub-message (bubble) in turn. We render a stack of textareas.
  const draft = msg.body.map(b => ({ text: b.text, emotion: b.emotion }));

  const stack = el('div', { class: 'bubble-edit-stack' });

  async function save() {
    try {
      const cleaned = draft.filter(b => b.text.trim());
      if (!cleaned.length) { toast('Need at least one non-empty bubble', 'error'); return; }
      // Mirror the patch into localStorage so a reload between this line
      // and the API success replays it via ``drainSaveQueue`` on next boot.
      const patch = { body: cleaned };
      const queueId = `${chatId}:${msg.id}`;
      markPending('chatMessage', queueId, patch);
      try {
        await api.updateMessage(chatId, msg.id, patch);
        clearPending('chatMessage', queueId);
      } catch (e) {
        // Network blip / 5xx — leave it queued for the boot drain. Re-throw
        // so the toast surfaces the failure to the user.
        throw e;
      }
      // Reload the chat so the on-screen bubble picks up the edit without a
      // page reload. ``preserveScroll`` keeps the user where they were.
      await loadActiveChat(chatId, { preserveScroll: true });
      closeModal();
    } catch (e) {
      toast(`Save failed: ${e.message}`, 'error');
    }
  }

  function rerender() {
    stack.replaceChildren();
    for (const [i, b] of draft.entries()) {
      const ta = el('textarea', {
        rows: 2,
        oninput: (e) => { draft[i].text = e.target.value; },
      });
      ta.value = b.text;
      const fitTa = autoGrowTextarea(ta);
      // Enter saves immediately, Ctrl/Cmd+Enter inserts a newline. Inverse
      // of the chat input so quick edits don't need mouse trips.
      ta.addEventListener('keydown', (e) => {
        if (e.key !== 'Enter') return;
        if (e.ctrlKey || e.metaKey) {
          e.preventDefault();
          const start = ta.selectionStart;
          const end = ta.selectionEnd;
          ta.value = ta.value.slice(0, start) + '\n' + ta.value.slice(end);
          ta.selectionStart = ta.selectionEnd = start + 1;
          draft[i].text = ta.value;
          fitTa();
        } else if (!e.shiftKey) {
          e.preventDefault();
          save();
        }
      });

      const row = el('div', { class: 'form-group' },
        el('label', {}, `Bubble ${i + 1}`),
        ta,
      );

      if (hasEmotion) {
        // Generic-origin bubbles gain a ``(none)`` entry at the top and
        // persist ``null`` when selected — distinct from ``"neutral"``
        // so the bubble can render with no emotion sprite (the contact's
        // avatar takes over, or the bubble goes bare).
        const options = isGenericOrigin
          ? [NONE_EMOTION_OPTION, ...EMOTIONS]
          : EMOTIONS;
        const currentValue = b.emotion == null ? NONE_EMOTION_VALUE : b.emotion;
        const emSel = pickerSelect({
          value: currentValue,
          options,
          onChange: (v) => {
            draft[i].emotion = v === NONE_EMOTION_VALUE ? null : v;
          },
        });
        row.append(el('label', { style: { marginTop: '6px' } }, 'Emotion'), emSel);
      }

      const removeBtn = el('button', {
        class: 'btn ghost',
        onClick: () => { draft.splice(i, 1); rerender(); },
        disabled: draft.length === 1,
      }, 'Remove bubble');
      row.append(el('div', { style: { marginTop: '6px' } }, removeBtn));

      stack.append(row);
    }
  }
  rerender();

  const body = el('div', {},
    el('h3', {}, 'Edit message'),
    stack,
    el('button', {
      class: 'btn ghost',
      onClick: () => {
        draft.push({
          text: '',
          // Generic-origin bubbles default to no emotion ("(none)"); AER /
          // legacy bubbles keep the historical ``'neutral'`` default.
          emotion: isGenericOrigin ? null : 'neutral',
        });
        rerender();
      },
    }, '+ Add bubble'),
    el('div', { class: 'modal-actions' },
      el('button', { class: 'btn ghost', onClick: () => closeModal() }, 'Cancel'),
      el('button', { class: 'btn primary', onClick: save }, 'Save'),
    ),
  );

  openModal(body, { size: 'large' });

  // Drop focus into the first bubble so the user can start typing right
  // away. Cursor lands at end of the existing text.
  const first = stack.querySelector('textarea');
  if (first) {
    first.focus();
    first.setSelectionRange(first.value.length, first.value.length);
  }
}
