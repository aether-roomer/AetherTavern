/* Chat list pane: virtualized + server-paged with filter chips + search.
 *
 * Backed by ``createPagedList`` against ``/api/chats``; rows mount via
 * ``createVirtList``. ``state.chats`` mirrors the loaded slice (sparse —
 * indices the user hasn't scrolled to yet are ``undefined``) so other
 * consumers (the new-chat wizard's "most recent" hint, the chat info
 * modal's chip joins) still have a fresh view of the user's recently-
 * touched chats without each having to call the API themselves.
 */

import { api } from '../api.js';
import { state, setState, subscribe } from '../state.js';
import {
  el, formatRelative, avatarEl, sortControls, favoriteStar, debounce,
} from '../util.js';
import { icon, toast } from '../ui.js';
import { openNewChatWizard } from './new_chat_wizard.js';
import { runImport } from './import_progress.js';
import { createVirtList } from '../virt_list.js';
import { createPagedList } from '../list_source.js';


export function renderChatList(container) {
  container.replaceChildren();
  const pane = el('div', { class: 'list-pane' });
  const header = renderHeader();
  const body = el('div', { class: 'list-body', id: 'chat-list-body' });
  pane.append(header, body);
  container.append(pane);
  mountList(body);
  // Subscribe to state changes that affect the list.
  return subscribe((prev) => onStateChange(prev));
}


const CHAT_SORT_OPTIONS = [
  ['edited', 'Activity'],
  ['added', 'Added'],
  ['name', 'Name'],
];


/* Map the front-end sort mode to the server's ``sort`` query param. */
function _serverSort(mode) {
  if (mode === 'added') return 'created_at';
  if (mode === 'name') return 'title';
  return 'updated_at';
}


function renderHeader() {
  const search = el('input', {
    class: 'list-search',
    type: 'text',
    placeholder: 'Search chats…',
    oninput: (e) => {
      _searchTerm = e.target.value || '';
      _searchDebounced();
    }
  });
  const sort = sortControls(
    state.chatSort || { mode: 'edited', direction: 'desc' },
    (v) => {
      setState({ chatSort: v });
      try { localStorage.setItem('chatSort', JSON.stringify(v)); } catch {}
    },
    CHAT_SORT_OPTIONS,
  );

  const chips = el('div', { class: 'list-chips', id: 'chat-list-chips' });

  const importInput = el('input', {
    id: 'chat-import-input', type: 'file',
    accept: '.json,.png,.zip,application/json,image/png,application/zip',
    class: 'hidden',
    onChange: async (e) => {
      const f = e.target.files[0];
      if (!f) return;
      const isZip = f.name.toLowerCase().endsWith('.zip');
      await runImport(f, { title: isZip ? 'Bulk import' : 'Import' });
      e.target.value = '';
    },
  });
  const importBtn = el('label', {
    class: 'btn',
    for: 'chat-import-input',
    title: 'Import chat',
    style: { cursor: 'pointer' },
  }, icon('upload', 14));

  const newBtn = el('button', {
    class: 'btn primary',
    title: 'New chat',
    onClick: () => openNewChatWizard(),
  }, icon('plus', 14));

  const head = el('div', { class: 'list-header' },
    el('div', { class: 'list-actions', style: { justifyContent: 'space-between', alignItems: 'center' } },
      el('h2', {}, 'Chats'),
      el('div', { style: { display: 'flex', gap: '6px' } }, importInput, importBtn, newBtn),
    ),
    el('div', { style: { display: 'flex', gap: '6px', alignItems: 'center' } },
      search,
      sort,
    ),
    chips,
  );
  return head;
}


// Module-level so the subscribe handler can talk to them.
let _virt = null;
let _paged = null;
let _searchTerm = '';
let _lastChatSort = null;
let _lastFilterKey = '';
let _lastChatsRef = null;

const _searchDebounced = debounce(() => {
  if (_paged) _paged.setParams({ q: _searchTerm.trim() || undefined });
}, 200);


function _currentParams() {
  const cs = state.chatSort || { mode: 'edited', direction: 'desc' };
  return {
    q: (_searchTerm || '').trim() || undefined,
    contact_id: state.filterContactId || undefined,
    user_id: state.filterUserId || undefined,
    scenario_id: state.filterScenarioId || undefined,
    favorites_first: !!cs.favoritesFirst,
    sort: _serverSort(cs.mode),
    direction: cs.direction || 'desc',
  };
}


function _filterKey() {
  // String summary of every filter / sort / search dim — used by
  // the subscribe handler to decide whether to push a paged.setParams.
  const p = _currentParams();
  return JSON.stringify(p);
}


function mountList(body) {
  const chipsEl = document.getElementById('chat-list-chips');
  if (chipsEl) chipsEl.replaceChildren(...buildChips());

  // Empty-state notice + fiction disclaimer go below the virtualized
  // rows. They're appended AFTER ``createVirtList`` since the
  // virtualizer's first action is ``replaceChildren(inner)``, which
  // would wipe anything already in the body. The empty-state text
  // swaps based on whether any filter / search is active —
  // "no chats yet" only applies in the truly-empty case.
  const emptyEl = el('div', { class: 'list-empty', style: { display: 'none' } });
  const disclaimerEl = el('div', { class: 'mobile-disclaimer' },
    'Remember, everything here is strictly fictional roleplay created by ' +
    'a hallucination generator. Only an utter fool would believe anything ' +
    'written in these chats.'
  );

  _paged = createPagedList({
    endpoint: (qs, opts) => api.listChatsPage(qs, opts),
    pageSize: 50,
    params: _currentParams(),
    onChange: ({ offset, length }) => {
      const total = _paged.getTotal() ?? 0;
      // Mirror loaded items into state.chats so cross-view consumers
      // (new-chat wizard, info modal joins) keep seeing recent rows.
      // Sparse fill: only the indices the paged adapter has loaded
      // get real values; the rest stay ``undefined``.
      const next = state.chats ? state.chats.slice() : [];
      next.length = total;
      for (let i = offset; i < offset + length; i += 1) {
        const item = _paged.getItem(i);
        if (item) next[i] = item;
      }
      // Direct mutation: setState would re-trigger subscribe → mountList
      // → loop. We manually invalidate the affected range below.
      state.chats = next;
      _lastChatsRef = next;
      // Empty-state visibility. The copy depends on whether any
      // filter / search is active — confusingly saying "no chats yet"
      // when the user is staring at a non-empty global store but a
      // 0-result filter is a real-world bug report waiting to happen.
      if (_emptyEl) {
        if (total === 0) {
          _emptyEl.replaceChildren();
          const hasFilter = !!(
            (state.filterContactId || state.filterUserId || state.filterScenarioId)
            || (_searchTerm && _searchTerm.trim())
          );
          if (hasFilter) {
            _emptyEl.append('No chats match your filters.');
          } else {
            _emptyEl.append('No chats yet. Click ', el('strong', {}, 'New chat'), ' to start.');
          }
          _emptyEl.style.display = '';
        } else {
          _emptyEl.style.display = 'none';
        }
      }
      _virt.setCount(total, { preserveScroll: true });
      _virt.invalidate([offset, offset + length]);
    },
  });

  _virt = createVirtList(body, {
    count: 0,
    getItem: (i) => {
      _paged.ensureLoaded(i);
      // Prefer the locally-mirrored slot — favourite-star toggles patch
      // ``state.chats[i]`` in place, but the paged adapter's per-page
      // cache still holds the pre-toggle value. The mirror is filled
      // from the adapter's onChange, so they're identical until a
      // local mutation overrides.
      if (state.chats && state.chats[i] != null) return state.chats[i];
      return _paged.getItem(i);
    },
    renderItem: (chat, i) => chat ? renderRow(chat) : renderSkeleton(),
    estimatedHeight: 64,
    // Chat rows are fixed-shape — skip per-row ResizeObservers.
    observeRows: false,
  });
  // Append the empty-state + disclaimer AFTER ``createVirtList`` so its
  // ``replaceChildren`` doesn't wipe them. ``virt-list-inner`` has its
  // own height (cumulative row tops) and pushes these below.
  body.appendChild(emptyEl);
  body.appendChild(disclaimerEl);
  // Stash a reference for the onChange handler.
  _emptyEl = emptyEl;

  _lastChatSort = state.chatSort;
  _lastFilterKey = _filterKey();
  // Kick off the first fetch.
  _paged.ensureLoaded(0);
}

let _emptyEl = null;


function onStateChange() {
  // Sort, filter, or favourites-first flipped — re-issue against the
  // server. The paged adapter diffs the params and drops cached pages
  // only when something filter-y actually changed; sort + direction
  // count as filter-y here.
  const fk = _filterKey();
  if (fk !== _lastFilterKey) {
    _lastFilterKey = fk;
    const chipsEl = document.getElementById('chat-list-chips');
    if (chipsEl) chipsEl.replaceChildren(...buildChips());
    if (_paged) _paged.setParams(_currentParams());
    return;
  }
  // ``state.chats`` was replaced from outside (import / chat-create /
  // chat-delete / a route's ``setState({chats: await api.listChats()})``).
  // The paged adapter's cache is now stale; drop it and refetch page 0.
  // ``_lastChatsRef`` is updated inside our own ``onChange`` so our
  // own mutations don't trip this branch.
  if (state.chats !== _lastChatsRef) {
    _lastChatsRef = state.chats;
    if (_paged) _paged.refresh();
    return;
  }
  // No filter or external change — just an active-chat highlight
  // switch or similar. Tell the virtualizer to re-render mounted
  // rows so the active-row class flips.
  if (_virt) _virt.invalidate('all');
}


function buildChips() {
  const chips = [];
  if (state.filterContactId) {
    const c = (state.contacts || []).find(x => x.id === state.filterContactId);
    if (c) chips.push(makeChip('Contact: ' + c.name, () => setState({ filterContactId: null })));
  }
  if (state.filterUserId) {
    const u = (state.users || []).find(x => x.id === state.filterUserId);
    if (u) chips.push(makeChip('User: ' + u.name, () => setState({ filterUserId: null })));
  }
  if (state.filterScenarioId) {
    const s = (state.scenarios || []).find(x => x.id === state.filterScenarioId);
    if (s) chips.push(makeChip('Scenario: ' + s.name, () => setState({ filterScenarioId: null })));
  }
  return chips;
}

function makeChip(label, onClose) {
  return el('div', { class: 'chip' },
    label,
    el('button', { onClick: onClose, title: 'Clear filter' }, '×'),
  );
}


/* Skeleton row shown while the covering page is in flight. Matches the
 * estimated row height so the scrollbar stays honest. */
function renderSkeleton() {
  return el('div', { class: 'list-row skeleton', style: { opacity: '0.4' } },
    el('div', { class: 'avatar avatar-placeholder' }),
    el('div', { class: 'row-text' },
      el('div', { class: 'row-title' }, el('span', { class: 'row-title-text' }, ' ')),
      el('div', { class: 'row-sub' }, ' '),
    ),
  );
}


function renderRow(chat) {
  const contact = (state.contacts || []).find(c => c.id === chat.contact_id);
  const user = (state.users || []).find(u => u.id === chat.user_id);
  const scenario = (state.scenarios || []).find(s => s.id === chat.scenario_id);
  const isActive = chat.id === state.activeChatId;

  const subtitle = [
    contact && contact.name,
    user && user.name,
    scenario && scenario.name,
  ].filter(Boolean).join(' · ');

  const count = chat.message_count;
  const meta = el('div', { class: 'row-meta' });
  if (typeof count === 'number') {
    meta.append(el('span', {
      class: 'row-msg-count',
      title: count === 1 ? '1 message' : `${count} messages`,
    }, String(count)));
  }
  meta.append(el('span', { class: 'row-time' }, formatRelative(chat.updated_at)));

  const row = el('div', {
    class: `list-row ${isActive ? 'active' : ''}`,
    onClick: () => setState({ activeChatId: chat.id }),
  },
    // cacheBust on the contact's own ``updated_at`` — bumping on chat
    // updates would force a fresh fetch on every message send (the row
    // re-renders when state.chats changes), which makes the avatar
    // visibly flicker. Only the contact's ``updated_at`` reflects an
    // actual avatar / emotion-sprite change.
    avatarEl(contact, { cacheBust: contact && contact.updated_at }),
    el('div', { class: 'row-text' },
      el('div', { class: 'row-title' },
        el('span', { class: 'row-title-text' }, chat.title || 'Untitled'),
        favoriteStar(chat, async (next) => {
          try {
            const updated = await api.setChatFavorite(chat.id, next);
            // Patch the local mirror in place — virt.getItem prefers
            // ``state.chats[i]`` over the paged adapter's cache, so
            // the next render frame picks up the updated favourite.
            // The favourites-first ordering is server-side; flipping
            // a star doesn't reorder the visible page until the next
            // filter/sort change.
            setState({
              chats: (state.chats || []).map(
                c => c && c.id === updated.id ? updated : c,
              ),
            });
          } catch (err) { toast(err.message, 'error'); }
        }),
      ),
      el('div', { class: 'row-sub' }, subtitle),
    ),
    meta,
  );
  return row;
}
