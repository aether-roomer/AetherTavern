/* UI helpers: icons, modal, toast. */

import { el } from './util.js';

/* ====== Icons (Lucide-style SVG) ====== */

const ICONS = {
  help: 'M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20zM9.5 9a2.5 2.5 0 1 1 4 2c-.8.5-1.5 1-1.5 2v.5M12 17h.01',
  send: 'M3 11.5 21 3l-3.5 18-5-7.5L21 3M12.5 13.5 3 11.5',
  stop: 'M6 6h12v12H6z',
  reroll: 'M4 4v6h6M20 20v-6h-6M4 10a8 8 0 0 1 14.5-4M20 14a8 8 0 0 1-14.5 4',
  edit: 'M4 20h4l10-10-4-4L4 16v4z M14 6l4 4',
  trash: 'M6 6h12M9 6V4h6v2M7 6l1 14h8l1-14',
  brain: 'M12 7a2.5 2.5 0 0 0-5.6.7A2 2 0 0 0 5 11a2 2 0 0 0 1.4 3.5A2.5 2.5 0 0 0 12 17z M12 7a2.5 2.5 0 0 1 5.6.7A2 2 0 0 1 19 11a2 2 0 0 1-1.4 3.5A2.5 2.5 0 0 1 12 17z',
  bookmark: 'M5 3h14v18l-7-4-7 4z',
  star: 'M12 2l3 7 7 .5-5.5 4.5L18 21l-6-3.5L6 21l1.5-7L2 9.5 9 9z',
  copy: 'M9 9h11v11H9zM5 5h11v3M5 5v11h3',
  undo: 'M4 7v6h6M20 17a8 8 0 0 0-7-12.5 8 8 0 0 0-9 5.5',
  plus: 'M12 5v14M5 12h14',
  minus: 'M5 12h14',
  x: 'M6 6l12 12M18 6 6 18',
  check: 'M5 12l4 4L19 6',
  back: 'M19 12H5M11 18l-6-6 6-6',
  next: 'M5 12h14M13 6l6 6-6 6',
  prev: 'M19 12H5M11 18l-6-6 6-6',
  search: 'M11 19a8 8 0 1 0 0-16 8 8 0 0 0 0 16zM21 21l-4.5-4.5',
  upload: 'M12 19V5M5 12l7-7 7 7M3 21h18',
  download: 'M12 5v14M5 12l7 7 7-7M3 21h18',
  refresh: 'M4 4v6h6M20 20v-6h-6M4 10a8 8 0 0 1 14.5-4M20 14a8 8 0 0 1-14.5 4',
  more: 'M5 12h.5M12 12h.5M19 12h.5',
  menu: 'M4 6h16M4 12h16M4 18h16',
  paperclip: 'M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48',
  filter: 'M3 5h18l-7 9v6l-4-2v-4z',
  cancel: 'M18 6 6 18M6 6l12 12',
  crop: 'M6 2v4M6 6H2M6 6h12a2 2 0 0 1 2 2v12M18 22v-4M18 18h4M18 18H6a2 2 0 0 1-2-2V4',
  info: 'M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20M12 12v5M12 8h.01',
  settings: 'M12 9a3 3 0 1 0 0 6 3 3 0 0 0 0-6z M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z',
  eye: 'M2 12s3-7 10-7 10 7 10 7-3 7-10 7-10-7-10-7z M12 9a3 3 0 1 0 0 6 3 3 0 0 0 0-6z',
  'eye-off': 'M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-10-8-10-8a18.45 18.45 0 0 1 5.06-5.94 M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 10 8 10 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24 M1 1l22 22',
  speaker: 'M11 5L6 9H2v6h4l5 4V5z M15.54 8.46a5 5 0 0 1 0 7.07 M19.07 4.93a10 10 0 0 1 0 14.14',
  pause: 'M6 4h4v16H6V4z M14 4h4v16h-4V4z',
  play: 'M5 3l14 9-14 9V3z',
  dice: 'M5 3h14a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2z M8 8h.01 M16 8h.01 M8 16h.01 M16 16h.01 M12 12h.01',
  grip: 'M9 6h.01 M9 12h.01 M9 18h.01 M15 6h.01 M15 12h.01 M15 18h.01',
  wave: 'M3 9c2-2 4-2 6 0s4 2 6 0 4-2 6 0 M3 17c2-2 4-2 6 0s4 2 6 0 4-2 6 0',
};

export function icon(name, size = 16, strokeWidth = 2) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('width', size);
  svg.setAttribute('height', size);
  svg.setAttribute('fill', 'none');
  svg.setAttribute('stroke', 'currentColor');
  svg.setAttribute('stroke-width', strokeWidth);
  svg.setAttribute('stroke-linecap', 'round');
  svg.setAttribute('stroke-linejoin', 'round');
  if (ICONS[name]) {
    const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    path.setAttribute('d', ICONS[name]);
    svg.appendChild(path);
  }
  return svg;
}


/* ====== Inline help ======
 *
 * ``helpDetails(content, opts)`` returns a small ``(?)`` icon button.
 * Clicking it reveals a popover with the explanatory ``content`` floating
 * just below — designed for verbose-but-rarely-read prose (activation
 * key reference, cascade-flag semantics, advanced-conditions OR-ing) so
 * the surrounding form stays compact.
 *
 * The popover is portalled to ``<body>`` so it never gets clipped by an
 * ancestor's ``overflow: hidden``. Clicking outside the popover or
 * pressing Escape closes it. No layout shift on toggle — the panel is
 * absolutely positioned and doesn't take part in the surrounding flow.
 *
 * ``content`` may be a string, an Element, or an array of Elements.
 * ``opts.label`` overrides the button's ``aria-label`` + ``title``
 * (default: "Help").
 */
export function helpDetails(content, opts = {}) {
  const label = opts.label || 'Help';
  const btn = el('button', {
    type: 'button',
    class: 'help-btn',
    title: label,
    'aria-label': label,
    'aria-expanded': 'false',
  }, icon('help', 14));

  const panel = el('div', { class: 'help-popover hidden' });
  if (typeof content === 'string') {
    panel.append(el('div', {}, content));
  } else if (Array.isArray(content)) {
    for (const c of content) panel.append(c);
  } else if (content) {
    panel.append(content);
  }

  let isOpen = false;
  let outsideHandler = null;
  let escHandler = null;
  let scrollHandler = null;
  let resizeHandler = null;

  function position() {
    const r = btn.getBoundingClientRect();
    panel.style.maxHeight = '';   // reset before measuring
    // Match the surrounding font so nested code blocks etc. look right.
    const cs = window.getComputedStyle(btn);
    panel.style.fontFamily = cs.fontFamily;
    // Width: cap at a comfortable reading measure and the viewport.
    const maxAvail = Math.min(420, window.innerWidth - 16);
    panel.style.maxWidth = `${maxAvail}px`;
    panel.style.width = 'max-content';

    const natural = panel.offsetHeight;
    const spaceBelow = window.innerHeight - r.bottom - 12;
    const spaceAbove = r.top - 12;
    const placeAbove = natural > spaceBelow && spaceAbove > spaceBelow;
    if (placeAbove) {
      const cap = Math.min(natural, spaceAbove);
      panel.style.maxHeight = `${cap}px`;
      panel.style.top = `${r.top - cap - 6}px`;
    } else {
      const cap = Math.min(natural, spaceBelow);
      panel.style.maxHeight = `${cap}px`;
      panel.style.top = `${r.bottom + 6}px`;
    }
    // Horizontal: anchor to the button's left, but keep the panel inside
    // the viewport.
    const measured = panel.offsetWidth;
    let left = r.left;
    if (left + measured > window.innerWidth - 8) {
      left = Math.max(8, window.innerWidth - measured - 8);
    }
    panel.style.left = `${left}px`;
  }

  function open() {
    if (isOpen) return;
    isOpen = true;
    document.body.append(panel);
    panel.classList.remove('hidden');
    btn.setAttribute('aria-expanded', 'true');
    btn.classList.add('open');
    position();
    outsideHandler = (e) => {
      if (panel.contains(e.target) || btn.contains(e.target)) return;
      close();
    };
    escHandler = (e) => { if (e.key === 'Escape') close(); };
    scrollHandler = () => position();
    resizeHandler = () => position();
    // Capture-phase so opens of sibling pickers don't sneak past.
    document.addEventListener('mousedown', outsideHandler, true);
    document.addEventListener('keydown', escHandler);
    window.addEventListener('scroll', scrollHandler, true);
    window.addEventListener('resize', resizeHandler);
  }
  function close() {
    if (!isOpen) return;
    isOpen = false;
    panel.classList.add('hidden');
    btn.setAttribute('aria-expanded', 'false');
    btn.classList.remove('open');
    if (panel.parentNode) panel.parentNode.removeChild(panel);
    document.removeEventListener('mousedown', outsideHandler, true);
    document.removeEventListener('keydown', escHandler);
    window.removeEventListener('scroll', scrollHandler, true);
    window.removeEventListener('resize', resizeHandler);
  }

  btn.addEventListener('click', (e) => {
    e.preventDefault();
    e.stopPropagation();
    if (isOpen) close(); else open();
  });

  return btn;
}


/* ====== Modal ======
 *
 * Modal-root is single by default — opening a modal replaces whatever was in
 * the modal-root. Callers that need to *layer* a confirmation (or any other
 * follow-up modal) over an existing one pass ``opts.stack = true``: the new
 * modal appends instead of replacing, and ``closeModal`` pops just the
 * topmost. Esc / focus-trap / backdrop click only affect the topmost layer.
 *
 * The default replace behavior is what importers (swapping progress ↔
 * conflict prompts) and wizard transitions rely on.
 */

const _modalStack = [];  // [{ modalEl, close }, …] topmost is last.

export function openModal(content, opts = {}) {
  const root = document.getElementById('modal-root');
  root.classList.remove('hidden');
  const modal = el('div', { class: `modal ${opts.size === 'large' ? 'large' : ''}` });
  modal.appendChild(content);

  if (opts.stack) {
    // Layer on top of any existing modal — keep the underlying ones in DOM.
    if (_modalStack.length > 0) {
      _modalStack[_modalStack.length - 1].modalEl.classList.add('modal-behind');
    }
    root.appendChild(modal);
  } else {
    // Replace whatever's there. Close out the existing stack (without
    // running their onClose hooks — they're being replaced, not dismissed).
    for (const entry of _modalStack) {
      entry.modalEl.remove();
    }
    _modalStack.length = 0;
    root.replaceChildren(modal);
  }

  const isTopmost = () =>
    _modalStack.length > 0 && _modalStack[_modalStack.length - 1].modalEl === modal;

  const onBackdrop = (e) => {
    if (e.target === root && isTopmost()) close();
  };
  const onEsc = (e) => { if (e.key === 'Escape' && isTopmost()) close(); };
  // Tab focus-trap: cycle Tab/Shift+Tab within the modal so focus can't leak
  // into the page behind. Picker popovers are portaled to ``document.body``
  // (outside the modal) so we explicitly skip the trap when focus is inside
  // one — the picker's own keyboard handlers run instead.
  const onTabKeydown = (e) => {
    if (e.key !== 'Tab') return;
    if (!isTopmost()) return;
    if (_isInsidePickerPopover(document.activeElement)) return;
    const focusables = _modalFocusables(modal);
    if (focusables.length === 0) {
      e.preventDefault();
      return;
    }
    const first = focusables[0];
    const last = focusables[focusables.length - 1];
    const focused = document.activeElement;
    if (e.shiftKey) {
      if (focused === first || !modal.contains(focused)) {
        e.preventDefault();
        last.focus();
      }
    } else {
      if (focused === last || !modal.contains(focused)) {
        e.preventDefault();
        first.focus();
      }
    }
  };
  // Safety net for non-Tab focus shifts (mouse clicks outside the modal,
  // programmatic focus moves, browser-internal redirects). Does NOT pull
  // focus back when it's inside a picker popover.
  const onFocusIn = (e) => {
    if (!isTopmost()) return;
    if (modal.contains(e.target)) return;
    if (_isInsidePickerPopover(e.target)) return;
    const focusables = _modalFocusables(modal);
    if (focusables.length) focusables[0].focus();
  };
  root.addEventListener('click', onBackdrop);
  document.addEventListener('keydown', onEsc);
  document.addEventListener('keydown', onTabKeydown, true);
  document.addEventListener('focusin', onFocusIn);

  function close() {
    modal.remove();
    root.removeEventListener('click', onBackdrop);
    document.removeEventListener('keydown', onEsc);
    document.removeEventListener('keydown', onTabKeydown, true);
    document.removeEventListener('focusin', onFocusIn);
    const idx = _modalStack.findIndex(s => s.modalEl === modal);
    if (idx >= 0) _modalStack.splice(idx, 1);
    if (_modalStack.length === 0) {
      root.classList.add('hidden');
    } else {
      _modalStack[_modalStack.length - 1].modalEl.classList.remove('modal-behind');
    }
    if (opts.onClose) opts.onClose();
  }
  _modalStack.push({ modalEl: modal, close });
  return close;
}

function _modalFocusables(root) {
  const sel = 'input:not([disabled]), textarea:not([disabled]), button:not([disabled]),'
    + ' select:not([disabled]), a[href], [tabindex]:not([tabindex="-1"])';
  return [...root.querySelectorAll(sel)].filter(el => el.offsetParent !== null);
}

function _isInsidePickerPopover(node) {
  for (let el = node; el && el !== document.body; el = el.parentNode) {
    if (el.classList && el.classList.contains('avatar-picker-popover')) return true;
  }
  return false;
}

export function closeModal() {
  if (_modalStack.length === 0) return;
  // Pop just the topmost — leaves any underlying modal intact.
  _modalStack[_modalStack.length - 1].close();
}


/* ====== Confirm dialog ====== */

export function confirmModal(title, message, opts = {}) {
  return new Promise(resolve => {
    let settled = false;
    const settle = (v) => { if (!settled) { settled = true; resolve(v); } };
    const body = el('div', {},
      el('h3', {}, title),
      typeof message === 'string' ? el('p', { style: { color: 'var(--text-dim)', margin: '4px 0 12px' } }, message) : message,
      el('div', { class: 'modal-actions' },
        el('button', { class: 'btn ghost', onClick: () => { settle(false); close(); } }, 'Cancel'),
        el('button', {
          class: `btn ${opts.danger ? 'danger' : 'primary'}`,
          onClick: () => { settle(true); close(); }
        }, opts.confirmLabel || (opts.danger ? 'Delete' : 'Confirm')),
      ),
    );
    // Pass through ``opts.stack`` so callers inside an existing modal can
    // layer the confirmation on top instead of replacing the outer modal.
    const close = openModal(body, { stack: !!opts.stack, onClose: () => settle(false) });
  });
}


/* ====== Toast ====== */

export function toast(message, kind = 'info', { duration, dismissible } = {}) {
  const root = document.getElementById('toast-root');
  const t = el('div', { class: `toast ${kind}` }, message);
  if (dismissible) {
    t.classList.add('dismissible');
    t.addEventListener('click', () => dismiss());
    t.title = 'Click to dismiss';
  }
  root.appendChild(t);
  let dismissed = false;
  function dismiss() {
    if (dismissed) return;
    dismissed = true;
    t.style.transition = 'opacity .25s';
    t.style.opacity = '0';
    setTimeout(() => t.remove(), 260);
  }
  const ms = duration ?? (kind === 'error' ? 5000 : 2500);
  setTimeout(dismiss, ms);
}
