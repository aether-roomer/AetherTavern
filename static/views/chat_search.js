/* In-chat Ctrl+F search bar.
 *
 * Browser Find can only see DOM nodes the page rendered, and the message-list
 * virtualization in chat.js drops everything outside the loaded window from
 * the DOM. So instead of relying on the browser, we run our own search:
 *   • match collection scans the raw message data along the active path,
 *     so unloaded messages still count;
 *   • highlight pass walks ``.bubble`` text nodes in the loaded window and
 *     wraps matches with ``<mark class="search-mark">``;
 *   • prev / next navigate through ALL matches; if the target message is
 *     virtualized out, we ask chat.js to materialise it (via the injected
 *     ``smartScrollToMessageById``) before scrolling the mark into view.
 *
 * Match shape: ``{ msgId, bubbleIdx, charStart, charEnd }`` where the char
 * range is into the raw SubMessage text (``state.chatMessages[*].body[i].text``).
 */

import { state, subscribe } from '../state.js';
import { el } from '../util.js';


let _isOpen = false;
let _bar = null;
let _input = null;
let _counterEl = null;
let _caseSensitive = false;
let _regex = false;
let _matches = [];
let _currentIdx = -1;
let _debounceTimer = null;
let _smartScrollToMessageById = null;   // injected from chat.js


export function setSmartScrollFn(fn) {
  _smartScrollToMessageById = fn;
}

export function isSearchOpen() { return _isOpen; }

export function openSearch() {
  if (_isOpen) {
    _input.focus();
    _input.select();
    return;
  }
  _isOpen = true;
  _buildBar();
  // Anchor the pill inside ``chat-messages-wrap`` — that's the closest
  // ``position: relative`` container, so the pill's ``position: absolute``
  // top-right placement lands on the chat surface, not the whole page.
  const host = document.getElementById('chat-messages-wrap') || document.body;
  host.appendChild(_bar);
  // Focus + select so the user can immediately type or replace the prior
  // query (which we keep in the input across re-opens within the session).
  _input.focus();
  _input.select();
  if (_input.value) _recompute();
}

export function closeSearch() {
  if (!_isOpen) return;
  _isOpen = false;
  _clearHighlights();
  if (_bar && _bar.parentNode) _bar.parentNode.removeChild(_bar);
  _matches = [];
  _currentIdx = -1;
}

/* Called by chat.js after every refreshMessages — the wholesale DOM swap
 * wipes our ``<mark>`` wrappers, so we re-walk the loaded bubbles. The
 * underlying match list is unchanged unless the caller also bumped state,
 * so this is just a paint refresh. */
export function reapplyHighlightsAfterRefresh() {
  if (!_isOpen) return;
  requestAnimationFrame(() => { _applyHighlights(); });
}


function _buildBar() {
  _input = el('input', {
    type: 'text',
    class: 'search-input',
    placeholder: 'Search…',
    value: _input ? _input.value : '',
    onInput: () => _scheduleRecompute(),
    onKeyDown: (e) => {
      // Capture before the document-level Ctrl+F handler sees Enter / Esc.
      if (e.key === 'Escape') {
        e.preventDefault(); e.stopPropagation();
        closeSearch();
      } else if (e.key === 'Enter') {
        e.preventDefault(); e.stopPropagation();
        _navigate(e.shiftKey ? -1 : 1);
      }
    },
  });
  const caseBtn = el('button', {
    type: 'button',
    class: 'search-toggle' + (_caseSensitive ? ' active' : ''),
    title: 'Case sensitive',
    onClick: (e) => {
      _caseSensitive = !_caseSensitive;
      e.currentTarget.classList.toggle('active', _caseSensitive);
      _recompute();
    },
  }, 'aB');
  const regexBtn = el('button', {
    type: 'button',
    class: 'search-toggle' + (_regex ? ' active' : ''),
    title: 'Regex',
    onClick: (e) => {
      _regex = !_regex;
      e.currentTarget.classList.toggle('active', _regex);
      _recompute();
    },
  }, '.*');
  _counterEl = el('span', { class: 'search-counter' }, '0/0');
  const prevBtn = el('button', {
    type: 'button', class: 'search-nav', title: 'Previous (Shift+Enter)',
    onClick: () => _navigate(-1),
  }, '▴');
  const nextBtn = el('button', {
    type: 'button', class: 'search-nav', title: 'Next (Enter)',
    onClick: () => _navigate(1),
  }, '▾');
  const closeBtn = el('button', {
    type: 'button', class: 'search-close', title: 'Close (Esc)',
    onClick: closeSearch,
  }, '✕');
  _bar = el('div', { class: 'chat-search-pill' },
    _input, caseBtn, regexBtn, _counterEl, prevBtn, nextBtn, closeBtn,
  );
}


function _scheduleRecompute() {
  clearTimeout(_debounceTimer);
  _debounceTimer = setTimeout(_recompute, 80);
}

function _recompute() {
  _matches = _collectMatches();
  _currentIdx = _matches.length > 0 ? 0 : -1;
  _updateCounter();
  _applyHighlights();
  if (_currentIdx >= 0) _scrollToCurrent();
}

function _escapeRegex(s) {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

function _collectMatches() {
  const query = _input ? _input.value : '';
  if (!query) {
    if (_input) _input.classList.remove('regex-error');
    return [];
  }
  let regex;
  try {
    regex = new RegExp(
      _regex ? query : _escapeRegex(query),
      _caseSensitive ? 'g' : 'gi',
    );
  } catch {
    if (_input) _input.classList.add('regex-error');
    return [];
  }
  if (_input) _input.classList.remove('regex-error');

  const allMessages = state.chatMessages || [];
  const path = (state.activePathIds || [])
    .map(id => allMessages.find(m => m.id === id))
    .filter(Boolean);
  const matches = [];
  for (const msg of path) {
    const body = msg.body || [];
    for (let bi = 0; bi < body.length; bi++) {
      const text = (body[bi] && body[bi].text) || '';
      regex.lastIndex = 0;
      let m;
      while ((m = regex.exec(text)) !== null) {
        matches.push({
          msgId: msg.id,
          bubbleIdx: bi,
          charStart: m.index,
          charEnd: m.index + m[0].length,
        });
        if (m.index === regex.lastIndex) regex.lastIndex++;   // zero-length safety
      }
    }
  }
  return matches;
}

function _updateCounter() {
  if (!_counterEl) return;
  if (_matches.length === 0) _counterEl.textContent = '0/0';
  else _counterEl.textContent = `${_currentIdx + 1}/${_matches.length}`;
}

function _navigate(dir) {
  if (_matches.length === 0) return;
  _currentIdx = (_currentIdx + dir + _matches.length) % _matches.length;
  _updateCounter();
  _applyHighlights();
  _scrollToCurrent();
}

function _scrollToCurrent() {
  const m = _matches[_currentIdx];
  if (!m) return;
  const root = document.getElementById('chat-messages');
  if (!root) return;
  // Materialise the target message into the loaded window if it's been
  // virtualized away. The smart-scroll wrapper handles both cases.
  if (_smartScrollToMessageById) _smartScrollToMessageById(m.msgId, root);
  // Wait a frame for the slide-driven re-render (if any) and the
  // post-refresh highlight re-apply, then scroll the current mark
  // into the centre of the viewport.
  requestAnimationFrame(() => {
    const mark = root.querySelector('mark.search-mark.current');
    if (mark) mark.scrollIntoView({ block: 'center', behavior: 'smooth' });
  });
}


function _clearHighlights() {
  const root = document.getElementById('chat-messages');
  if (!root) return;
  for (const mark of root.querySelectorAll('mark.search-mark')) {
    const text = document.createTextNode(mark.textContent);
    if (mark.parentNode) mark.parentNode.replaceChild(text, mark);
  }
  // Merge adjacent text nodes split by previous wrap operations so
  // subsequent character-position math is accurate.
  for (const bubble of root.querySelectorAll('.bubble')) bubble.normalize();
}

function _applyHighlights() {
  _clearHighlights();
  if (_matches.length === 0) return;
  const root = document.getElementById('chat-messages');
  if (!root) return;

  // Group by msgId, then by bubbleIdx.
  const byMsg = new Map();
  for (let i = 0; i < _matches.length; i++) {
    const m = _matches[i];
    let buckets = byMsg.get(m.msgId);
    if (!buckets) { buckets = new Map(); byMsg.set(m.msgId, buckets); }
    let arr = buckets.get(m.bubbleIdx);
    if (!arr) { arr = []; buckets.set(m.bubbleIdx, arr); }
    arr.push({ ...m, _idx: i });
  }

  for (const [msgId, buckets] of byMsg) {
    const msgEl = root.querySelector(`.msg[data-msg-id="${CSS.escape(msgId)}"]`);
    if (!msgEl) continue;   // virtualized out — skip; will rehighlight on slide
    const bubbles = msgEl.querySelectorAll('.bubble');
    for (const [bi, ms] of buckets) {
      const bubble = bubbles[bi];
      if (bubble) _highlightBubble(bubble, ms);
    }
  }
}

function _highlightBubble(bubble, matches) {
  // Walk text nodes in document order. Track character offset against
  // bubble.textContent — that's what the user sees, and it differs from
  // the raw SubMessage text only by formatting markers (** / * / _) and
  // whitespace inside <pre><code>. For matches that line up cleanly with a
  // single text node we wrap; matches that span elements we drop silently.
  const walker = document.createTreeWalker(bubble, NodeFilter.SHOW_TEXT, {
    acceptNode: (node) => {
      const parent = node.parentNode;
      if (parent && parent.nodeName === 'MARK') return NodeFilter.FILTER_REJECT;
      return NodeFilter.FILTER_ACCEPT;
    },
  });
  const nodes = [];
  let n;
  while ((n = walker.nextNode())) nodes.push(n);

  // Sort matches by charStart so we walk both arrays linearly.
  const sorted = [...matches].sort((a, b) => a.charStart - b.charStart);

  let pos = 0;
  // Map each text node to {node, start, end} based on bubble textContent
  // walk order.
  const slots = nodes.map(node => {
    const start = pos;
    const end = pos + node.nodeValue.length;
    pos = end;
    return { node, start, end };
  });
  // Walk text content also via textContent to find raw-rendered offsets.
  // The match charStart/charEnd are against the SUBMESSAGE TEXT, not the
  // rendered text — so translate by computing the bubble's raw text.
  // Since renderBubble strips paired markers, we can't trivially align.
  // For a first cut, fall back to "search rendered text" by walking nodes
  // and matching against textContent.
  const renderedText = nodes.map(n => n.nodeValue).join('');

  // Build a fresh match list from rendered text + the same query. This
  // gives us highlight positions that exist in the DOM. The counter still
  // uses the raw-text match list (for unloaded messages), so the counter
  // and the on-screen highlight count may differ when formatting markers
  // change content length — acceptable.
  const query = _input ? _input.value : '';
  if (!query) return;
  let regex;
  try {
    regex = new RegExp(
      _regex ? query : _escapeRegex(query),
      _caseSensitive ? 'g' : 'gi',
    );
  } catch { return; }
  regex.lastIndex = 0;
  const renderedMatches = [];
  let rm;
  while ((rm = regex.exec(renderedText)) !== null) {
    renderedMatches.push({ start: rm.index, end: rm.index + rm[0].length });
    if (rm.index === regex.lastIndex) regex.lastIndex++;
  }

  // Apply each rendered match — find slot, surroundContents.
  // Process right-to-left within each slot so earlier positions stay valid.
  // Group renderedMatches by which slot they fall in.
  const bySlot = new Map();
  for (const rmt of renderedMatches) {
    for (const slot of slots) {
      if (rmt.start >= slot.start && rmt.end <= slot.end) {
        if (!bySlot.has(slot.node)) bySlot.set(slot.node, { slot, ms: [] });
        bySlot.get(slot.node).ms.push(rmt);
        break;
      }
    }
  }

  // The "current" highlight goes to the (k-th) rendered match where k is the
  // currentIdx among the bubble-local matches that align with raw matches.
  // For simplicity, pick the rendered match whose absolute order in the
  // bubble matches the raw match's order. Degenerate cases will mis-mark
  // but won't error.
  const sortedRaw = [...matches].sort((a, b) => a.charStart - b.charStart);
  const renderedToCurrent = renderedMatches.map((_, i) => {
    const raw = sortedRaw[i];
    return raw && raw._idx === _currentIdx;
  });

  for (const [, { slot, ms }] of bySlot) {
    ms.sort((a, b) => b.start - a.start);
    for (const rmt of ms) {
      const localStart = rmt.start - slot.start;
      const localEnd = rmt.end - slot.start;
      const range = document.createRange();
      try {
        range.setStart(slot.node, localStart);
        range.setEnd(slot.node, localEnd);
      } catch { continue; }
      const mark = document.createElement('mark');
      const isCurrent = renderedToCurrent[renderedMatches.indexOf(rmt)];
      mark.className = 'search-mark' + (isCurrent ? ' current' : '');
      try { range.surroundContents(mark); } catch { /* range crosses elements */ }
    }
  }
}


/* ---------- Document-level open trigger + lifecycle ---------- */

document.addEventListener('keydown', (e) => {
  // Same gating regardless of which key is being checked.
  const inChat = state.activeTab === 'chats'
    && !!document.getElementById('chat-messages');
  if (!inChat) return;
  const modalRoot = document.getElementById('modal-root');
  const modalOpen = modalRoot && !modalRoot.classList.contains('hidden');
  if (modalOpen) return;

  // Ctrl/Cmd+F — open the search pill (or refocus + reselect if already open).
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'f') {
    e.preventDefault();
    openSearch();
    return;
  }
  // F3 / Shift+F3 — next / previous match (mirrors browser Find UX). Only
  // active while the pill is open and has accumulated some matches.
  if (e.key === 'F3') {
    if (!_isOpen || _matches.length === 0) return;
    e.preventDefault();
    _navigate(e.shiftKey ? -1 : 1);
  }
});

let _lastTab = state.activeTab;
let _lastChatId = state.activeChatId;
subscribe(() => {
  if (_isOpen
      && (state.activeTab !== _lastTab || state.activeChatId !== _lastChatId)) {
    closeSearch();
  }
  _lastTab = state.activeTab;
  _lastChatId = state.activeChatId;
});
