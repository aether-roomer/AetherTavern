/* Bookmarks panel: save/restore active path snapshots. */

import { api } from '../api.js';
import { state, setState } from '../state.js';
import { el, escapeHtml, getChatById } from '../util.js';
import { icon, openModal, closeModal, toast } from '../ui.js';
import { markPending, clearPending } from '../save_queue.js';


export async function openBookmarksModal(chatId) {
  const data = await api.listBookmarks(chatId).catch(() => ({ bookmarks: [], history: [] }));
  const bookmarks = data.bookmarks || [];
  const history = data.history || [];

  const list = el('div', { class: 'section' });
  list.append(el('h3', {}, 'Bookmarks',
    el('span', { class: 'badge' }, String(bookmarks.length))));

  if (bookmarks.length === 0) {
    list.append(el('div', { class: 'list-empty', style: { padding: '12px 0' } }, 'No bookmarks yet.'));
  } else {
    const sorted = [...bookmarks].sort((a, b) => (b.favorite - a.favorite) || (b.created_at - a.created_at));
    for (const b of sorted) {
      list.append(renderBookmark(chatId, b));
    }
  }

  const histBox = el('div', { class: 'section' });
  histBox.append(el('h3', {}, 'Recent history',
    el('span', { class: 'badge' }, String(history.length))));
  if (history.length === 0) {
    histBox.append(el('div', { class: 'list-empty', style: { padding: '12px 0' } }, 'No navigations yet.'));
  } else {
    for (const h of history) {
      histBox.append(el('div', {
        class: 'history-row',
        title: 'Jump to this saved path',
        onClick: async () => {
          try {
            await api.restorePath(chatId, h.selected_child_id || {});
            closeModal();
            const { loadActiveChat } = await import('./chat.js');
            await loadActiveChat(chatId, { flush: true });
          } catch (e) { toast(`Jump failed: ${e.message}`, 'error'); }
        },
      },
        el('div', { class: 'history-time' }, h.time),
        el('div', {}, h.snippet || '(no snippet)'),
      ));
    }
  }

  const header = el('div', {
    style: { display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '14px' }
  },
    el('h3', { style: { margin: 0 } }, 'Bookmarks'),
    el('button', {
      class: 'btn primary',
      onClick: () => openCreateBookmarkModal(chatId),
    }, '+ Bookmark'),
  );

  const body = el('div', {},
    header,
    list,
    histBox,
    el('div', { class: 'modal-actions' },
      el('button', { class: 'btn primary', onClick: () => closeModal() }, 'Close'),
    ),
  );
  openModal(body, { size: 'large' });
}


function renderBookmark(chatId, bookmark) {
  return el('div', { style: { padding: '8px 0', borderTop: '1px solid var(--border)', display: 'flex', gap: '8px', alignItems: 'center' } },
    el('button', {
      class: 'icon-btn',
      title: bookmark.favorite ? 'Unfavorite' : 'Favorite',
      style: { color: bookmark.favorite ? 'var(--warn)' : 'var(--text-mute)' },
      onClick: async () => {
        try {
          const patch = { favorite: !bookmark.favorite };
          const queueId = `${chatId}:${bookmark.id}`;
          markPending('chatBookmark', queueId, patch);
          await api.updateBookmark(chatId, bookmark.id, patch);
          clearPending('chatBookmark', queueId);
          openBookmarksModal(chatId);
        } catch (e) { toast(`Save failed: ${e.message}`, 'error'); }
      },
    }, icon('star', 14)),
    el('div', { style: { flex: 1 } },
      el('div', { style: { fontWeight: '500' } }, bookmark.title || '(untitled)'),
      el('div', { style: { fontSize: '12px', color: 'var(--text-mute)' } }, bookmark.snippet || ''),
    ),
    el('button', {
      class: 'btn',
      title: 'Jump to bookmarked path',
      onClick: async () => {
        try {
          await api.jumpBookmark(chatId, bookmark.id);
          closeModal();
          const { loadActiveChat } = await import('./chat.js');
          await loadActiveChat(chatId, { flush: true });
        } catch (e) { toast(`Jump failed: ${e.message}`, 'error'); }
      },
    }, 'Jump'),
    el('button', {
      class: 'icon-btn danger',
      onClick: async () => {
        await api.deleteBookmark(chatId, bookmark.id);
        openBookmarksModal(chatId);
      },
    }, icon('trash', 14)),
  );
}


function openCreateBookmarkModal(chatId) {
  // Snapshot current path; user provides title.
  const chat = getChatById(state, state.activeChatId);
  const path = state.activePathIds || [];
  const lastMsg = state.chatMessages.find(m => m.id === path[path.length - 1]);
  const snippet = lastMsg ? (lastMsg.body[0]?.text || '').slice(0, 80) : '(empty)';

  const titleInput = el('input', { type: 'text', placeholder: '(optional)', autofocus: true });

  // Build a *full* snapshot: existing selected_child_id (preserves __empty__
  // markers) plus an explicit chosen-child entry for every parent in the
  // current active path. Without the latter, parents that lack an explicit
  // entry would default to "latest sibling" on jump-back, which can quietly
  // diverge once new siblings are generated.
  const snapshot = { ...(chat?.selected_child_id || {}) };
  let prevKey = '';
  for (const id of path) {
    snapshot[prevKey] = id;
    prevKey = id;
  }

  async function save() {
    try {
      await api.createBookmark(chatId, {
        title: titleInput.value.trim(),
        snippet,
        selected_child_id: snapshot,
      });
      closeModal();
      openBookmarksModal(chatId);
    } catch (e) { toast(`Save failed: ${e.message}`, 'error'); }
  }

  titleInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.ctrlKey && !e.metaKey) {
      e.preventDefault();
      save();
    }
  });

  const body = el('div', {},
    el('h3', {}, 'Bookmark this path'),
    el('div', { class: 'form-group' },
      el('label', {}, 'Title'),
      titleInput,
    ),
    el('div', { style: { fontSize: '12px', color: 'var(--text-mute)' } }, snippet),
    el('div', { class: 'modal-actions' },
      el('button', { class: 'btn ghost', onClick: () => closeModal() }, 'Cancel'),
      el('button', { class: 'btn primary', onClick: save }, 'Save'),
    ),
  );
  openModal(body);
  // Ensure focus lands on the title input. We schedule via rAF to outrun any
  // post-click focus settling the browser does after the "+ Bookmark" button
  // click that opened this modal.
  requestAnimationFrame(() => titleInput.focus());
}
