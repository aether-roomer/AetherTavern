/* Persistent save queue for entity edits.
 *
 * Each successful in-memory autosaver retry already covers transient
 * network blips, but loses everything on page reload / hard navigation.
 * This module mirrors the in-flight payload to ``localStorage`` so the
 * pending edit survives those events and can be flushed on the next
 * boot.
 *
 * Usage from a view's save fn:
 *   markPending('contact', draft.id, draft);
 *   try {
 *     await saveWithConflictHandling({...});
 *     clearPending('contact', draft.id);
 *   } catch (e) {
 *     // ``makeAutoSaver`` will retry; the pending mark stays so a reload
 *     // mid-retry doesn't drop the edit.
 *     throw e;
 *   }
 *
 * On boot, ``drainSaveQueue`` walks the queue and posts each entry
 * through the same conflict-aware flow — a 409 pops the standard
 * overwrite/reload modal.
 */

import { api, HttpError } from './api.js';
import { openConflictModal } from './conflict.js';


const STORAGE_KEY = 'pendingSaves';

/* Replay map: ``kind → (id, payload) => Promise``. For composite kinds
 * (chatMessage / chatBookmark) the ``id`` is ``"<chatId>:<entityId>"`` —
 * the SAVE_FN splits it and calls the corresponding two-arg API. The
 * ``settings`` kind is a singleton so it ignores ``id`` (callers pass
 * the constant string ``"settings"``). */
const SAVE_FNS = {
  contact:  (id, payload) => api.updateContact(id, payload),
  user:     (id, payload) => api.updateUser(id, payload),
  scenario: (id, payload) => api.updateScenario(id, payload),
  library:  (id, payload) => api.updateBrainLibrary(id, payload),
  contextPreset: (id, payload) => api.updateContextPreset(id, payload),
  chat:     (id, payload) => api.updateChat(id, payload),
  chatMessage: (id, payload) => {
    const [chatId, msgId] = id.split(':');
    return api.updateMessage(chatId, msgId, payload);
  },
  chatBookmark: (id, payload) => {
    const [chatId, bookmarkId] = id.split(':');
    return api.updateBookmark(chatId, bookmarkId, payload);
  },
  preset:   (id, payload) => api.updatePreset(id, payload),
  settings: (_id, payload) => api.putSettings(payload),
};

const ENTITY_LABELS = {
  contact: 'Contact',
  user: 'Persona',
  scenario: 'Scenario',
  library: 'Brain library',
  contextPreset: 'Context preset',
  chat: 'Chat',
  chatMessage: 'Message',
  chatBookmark: 'Bookmark',
  preset: 'Preset',
  settings: 'Settings',
};


function _readAll() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    return raw ? JSON.parse(raw) : {};
  } catch { return {}; }
}

function _writeAll(map) {
  try {
    if (!map || Object.keys(map).length === 0) {
      localStorage.removeItem(STORAGE_KEY);
    } else {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(map));
    }
  } catch {}
}

function _key(kind, id) { return `${kind}:${id}`; }


export function markPending(kind, id, payload) {
  if (!kind || !id || !payload) return;
  const all = _readAll();
  all[_key(kind, id)] = { kind, id, payload, ts: Date.now() };
  _writeAll(all);
}

export function clearPending(kind, id) {
  if (!kind || !id) return;
  const all = _readAll();
  delete all[_key(kind, id)];
  _writeAll(all);
}


/* Walk every queued entry and try to save it. Hits 409? Pops the same
 * conflict modal the live autosaver path uses. Hits a non-retryable
 * 4xx (e.g. 404 for a deleted entity)? Drops the entry. Everything
 * else is left for the next boot. */
export async function drainSaveQueue() {
  const all = _readAll();
  const entries = Object.values(all);
  if (entries.length === 0) return;

  for (const entry of entries) {
    const saveFn = SAVE_FNS[entry.kind];
    if (!saveFn) {
      clearPending(entry.kind, entry.id);
      continue;
    }
    let payload = entry.payload;
    while (true) {
      try {
        await saveFn(entry.id, payload);
        clearPending(entry.kind, entry.id);
        break;
      } catch (e) {
        if (e instanceof HttpError && e.status === 409) {
          const detail = (e.body && e.body.detail) || {};
          const decision = await openConflictModal({
            entityLabel: ENTITY_LABELS[entry.kind] || entry.kind,
            localUpdatedAt: payload && payload.updated_at,
            remoteUpdatedAt: detail.current_updated_at,
          });
          if (decision === 'overwrite') {
            if (detail.current_version_id) {
              payload = { ...payload, version_id: detail.current_version_id };
            }
            continue;
          }
          // Reload OR cancel both drop the queued payload — the user is
          // booting fresh, so "discard" / "leave it for later" both come
          // out to "drop the local copy". A live edit (markPending in a
          // view's save fn) will requeue if the user wants to retry.
          clearPending(entry.kind, entry.id);
          break;
        }
        if (
          e instanceof HttpError
          && e.status >= 400 && e.status < 500
          && e.status !== 408 && e.status !== 429
        ) {
          // Non-retryable client error (404 deleted, 400 validation, …).
          // The queued payload can't make progress; drop.
          clearPending(entry.kind, entry.id);
          break;
        }
        // Network / 5xx — leave queued for the next boot to retry.
        break;
      }
    }
  }
}


/* Read-only inspection used by tests + diagnostics. */
export function pendingSaveCount() {
  return Object.keys(_readAll()).length;
}
