/**
 * Message-info popover. Surfaces per-message generation metadata for
 * any origin (AER, Generic, Manual). Anchored at the avatar element or
 * a dedicated ``(i)`` badge in the meta-row when the bubble has no
 * avatar.
 *
 * Single instance lives on ``document.body``; opening on a new trigger
 * closes any existing instance. Outside-click and ``Escape`` dismiss.
 * Lighter than the modal system — no backdrop, no focus trap.
 */
import { el, formatRelative } from '../util.js';
import { state } from '../state.js';
import { icon } from '../ui.js';
import { PROVIDER_LABELS } from '../model_sources.js';


// One global popover instance + dismiss handlers. ``_active`` holds the
// currently-mounted popover element; opening another closes this one.
let _active = null;
let _outsideClickHandler = null;
let _escapeHandler = null;
let _scrollHandler = null;


function closePopover() {
  if (_active) {
    _active.remove();
    _active = null;
  }
  if (_outsideClickHandler) {
    document.removeEventListener('mousedown', _outsideClickHandler, true);
    _outsideClickHandler = null;
  }
  if (_escapeHandler) {
    document.removeEventListener('keydown', _escapeHandler, true);
    _escapeHandler = null;
  }
  if (_scrollHandler) {
    window.removeEventListener('scroll', _scrollHandler, true);
    _scrollHandler = null;
  }
}


const MODE_LABELS = {
  aer: 'AER',
  generic: 'Generic',
  manual: 'Manual',
};


function formatDuration(seconds) {
  if (seconds == null) return null;
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  const minutes = Math.floor(seconds / 60);
  const rest = Math.round(seconds - minutes * 60);
  return `${minutes}m ${rest}s`;
}


function resolveGenerationPresetName(id) {
  if (!id) return null;
  const presets = state.presets || [];
  const found = presets.find(p => p.id === id);
  return found ? found.name : '(deleted)';
}


function resolveContextPresetName(id) {
  if (!id) return null;
  const presets = state.contextPresets || [];
  const found = presets.find(p => p.id === id);
  return found ? found.name : '(deleted)';
}


function appendRow(dl, label, value, { titleAttr } = {}) {
  const dt = el('dt', {}, label);
  const dd = titleAttr
    ? el('dd', { title: titleAttr }, value)
    : el('dd', {}, value);
  dl.append(dt, dd);
}


function buildPopoverContent(msg) {
  const dl = el('dl', {});
  const origin = msg.origin || 'aer';
  // Manual-only flag — distinct from "user-side". Impersonate-generated
  // user messages have ``sender === 'user'`` AND non-manual origin, so
  // they get the full Emotion + Generation rows treatment.
  const isManual = origin === 'manual';

  // Emotion — skip for manually-typed messages (not meaningful). Contact
  // and impersonate-generated user messages with multiple bubbles show
  // the first bubble's emotion; the edit-message modal remains the
  // per-bubble surface.
  if (!isManual) {
    const firstEmotion = (msg.body && msg.body[0] && msg.body[0].emotion) || null;
    appendRow(dl, 'Emotion', firstEmotion == null ? '—' : firstEmotion);
  }

  // Chronological order: Generation start → Completed (= msg.timestamp,
  // which is the persist time, *after* the model finishes for AI msgs).
  // For manually-typed messages there's no generation, so just label the
  // single row as "Sent".
  if (!isManual) {
    if (msg.generation_started_at) {
      const iso = new Date(msg.generation_started_at * 1000).toISOString();
      appendRow(dl, 'Generation start',
        formatRelative(msg.generation_started_at),
        { titleAttr: iso });
    } else {
      appendRow(dl, 'Generation start', '—');
    }
    if (msg.timestamp) {
      const iso = new Date(msg.timestamp * 1000).toISOString();
      appendRow(dl, 'Completed', formatRelative(msg.timestamp), { titleAttr: iso });
    } else {
      appendRow(dl, 'Completed', '—');
    }
    appendRow(dl, 'Duration',
      formatDuration(msg.generation_duration_seconds) || '—');
  } else if (msg.timestamp) {
    const iso = new Date(msg.timestamp * 1000).toISOString();
    appendRow(dl, 'Sent', formatRelative(msg.timestamp), { titleAttr: iso });
  } else {
    appendRow(dl, 'Sent', '—');
  }

  appendRow(dl, 'Origin', MODE_LABELS[origin] || origin);

  appendRow(dl, 'Provider',
    msg.provider ? (PROVIDER_LABELS[msg.provider] || msg.provider) : '—');

  appendRow(dl, 'Model', msg.model || '—');

  appendRow(dl, 'Generation preset',
    resolveGenerationPresetName(msg.generation_preset_id) || '—');

  // Context-preset row is hidden when the message is AER (the concept
  // doesn't apply). For Generic / Manual / legacy with the field
  // unpopulated, show "—".
  if (origin !== 'aer') {
    appendRow(dl, 'Context preset',
      resolveContextPresetName(msg.context_preset_id) || '—');
  }

  return dl;
}


function positionPopover(popoverEl, anchorRect) {
  // Place above the anchor when there's room (bubbles usually sit low
  // in the viewport), below otherwise. Constrain to viewport.
  const margin = 8;
  const popHeight = popoverEl.offsetHeight;
  const popWidth = popoverEl.offsetWidth;
  const vpW = window.innerWidth;
  const vpH = window.innerHeight;

  // Horizontal: align left edge with anchor, then clamp.
  let left = anchorRect.left;
  if (left + popWidth + margin > vpW) {
    left = vpW - popWidth - margin;
  }
  if (left < margin) left = margin;

  // Vertical: prefer above.
  let top = anchorRect.top - popHeight - 6;
  if (top < margin) {
    top = anchorRect.bottom + 6;
  }
  if (top + popHeight + margin > vpH) {
    top = vpH - popHeight - margin;
  }
  if (top < margin) top = margin;

  popoverEl.style.left = `${Math.round(left)}px`;
  popoverEl.style.top = `${Math.round(top)}px`;
}


/**
 * Open the message-info popover anchored at ``anchorEl``.
 *
 * @param {HTMLElement} anchorEl - the trigger (avatar or info badge).
 * @param {ChatMessage} msg - the message object to read fields from.
 */
export function openMessageInfoPopover(anchorEl, msg) {
  closePopover();
  if (!anchorEl || !msg) return;

  const popoverEl = el('div', {
    class: 'msg-info-popover',
    role: 'dialog',
    'aria-label': 'Message info',
  }, buildPopoverContent(msg));
  // Mount off-screen first so we can measure dimensions before positioning.
  popoverEl.style.left = '-9999px';
  popoverEl.style.top = '-9999px';
  document.body.appendChild(popoverEl);
  _active = popoverEl;

  const rect = anchorEl.getBoundingClientRect();
  positionPopover(popoverEl, rect);

  _outsideClickHandler = (e) => {
    if (!popoverEl.contains(e.target) && e.target !== anchorEl && !anchorEl.contains(e.target)) {
      closePopover();
    }
  };
  document.addEventListener('mousedown', _outsideClickHandler, true);

  _escapeHandler = (e) => {
    if (e.key === 'Escape') {
      e.stopPropagation();
      closePopover();
    }
  };
  document.addEventListener('keydown', _escapeHandler, true);

  // Close on scroll — the anchor moves with the page, so the popover's
  // fixed-position location would drift away.
  _scrollHandler = () => closePopover();
  window.addEventListener('scroll', _scrollHandler, true);
}


/**
 * Build an ``(i)`` badge button for bubbles that have no avatar slot.
 * Mounted next to the sender-name in the meta-row by the caller.
 *
 * @param {ChatMessage} msg - the message the popover should describe.
 * @returns {HTMLElement}
 */
export function makeMessageInfoBadge(msg) {
  const btn = el('button', {
    class: 'msg-info-badge',
    type: 'button',
    'aria-label': 'Message info',
    title: 'Message info',
  }, icon('info', 16));
  btn.addEventListener('click', (e) => {
    e.stopPropagation();
    openMessageInfoPopover(btn, msg);
  });
  return btn;
}


/**
 * Attach an avatar trigger that opens the popover on click. Used by
 * ``renderMessage`` when an avatar (emotion sprite / contact avatar /
 * user avatar) is present.
 */
export function attachAvatarTrigger(avatarEl, msg) {
  if (!avatarEl || !msg) return;
  avatarEl.style.cursor = 'pointer';
  avatarEl.setAttribute('aria-label', 'Message info');
  avatarEl.addEventListener('click', (e) => {
    e.stopPropagation();
    openMessageInfoPopover(avatarEl, msg);
  });
}
