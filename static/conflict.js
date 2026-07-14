/* Optimistic-concurrency helpers shared by every entity edit view.
 *
 * Each entity (Contact / User / Scenario / Chat) carries a ``version_id``
 * that the server rerolls on every save. The frontend's edit views
 * always work on a draft copy and call ``saveWithConflictHandling``,
 * which:
 *
 *   - Calls ``saveFn(draft)``. On success, copies the returned
 *     ``version_id`` + ``updated_at`` back into the draft so the next
 *     save uses fresh values.
 *   - On HTTP 409, reads the ``current_version_id`` and
 *     ``current_updated_at`` out of the conflict body, opens an
 *     overwrite-or-reload prompt, and either retries with the live
 *     id (overwrite) or replaces the draft with whatever ``getFn``
 *     returns (reload).
 *   - Other errors propagate so ``makeAutoSaver`` can apply its
 *     transient-failure backoff.
 */

import { el } from './util.js';
import { openModal, closeModal } from './ui.js';
import { HttpError } from './api.js';


function _formatDate(epochSeconds) {
  if (!epochSeconds) return '—';
  const d = new Date(epochSeconds * 1000);
  return d.toLocaleString(undefined, {
    year: 'numeric', month: 'short', day: 'numeric',
    hour: '2-digit', minute: '2-digit', second: '2-digit',
  });
}


/* Open a 3-action prompt: Overwrite / Reload / Cancel. Resolves to
 * ``'overwrite'`` | ``'reload'`` | ``null`` (cancel). The two visible
 * timestamps anchor the choice — without them the user can't tell which
 * version is "fresher". */
export function openConflictModal({ entityLabel, localUpdatedAt, remoteUpdatedAt }) {
  return new Promise((resolve) => {
    let settled = false;
    const settle = (v) => { if (!settled) { settled = true; resolve(v); } };

    const body = el('div', {},
      el('h3', {}, `${entityLabel} changed elsewhere`),
      el('p', {
        style: { color: 'var(--text-dim)', fontSize: '0.929rem', margin: '4px 0 10px' },
      },
        `This ${entityLabel.toLowerCase()} was edited in another tab or browser. ` +
        `Pick which version wins.`),
      el('div', {
        style: {
          background: 'var(--bg-2)', padding: '8px 10px',
          borderRadius: 'var(--radius-sm)', fontSize: '0.929rem',
          margin: '6px 0 14px',
        },
      },
        el('div', {}, el('strong', {}, 'Your edits: '), _formatDate(localUpdatedAt)),
        el('div', {}, el('strong', {}, 'Remote: '), _formatDate(remoteUpdatedAt)),
      ),
      el('p', {
        style: { color: 'var(--text-dim)', fontSize: '0.857rem', margin: '0 0 14px' },
      },
        el('strong', {}, 'Overwrite'), ' replaces the remote with your local edits. ',
        el('strong', {}, 'Reload'), ' discards your local edits and pulls the remote in.'),
      el('div', { class: 'modal-actions' },
        el('button', {
          class: 'btn ghost',
          onClick: () => { settle(null); closeModal(); },
        }, 'Cancel'),
        el('button', {
          class: 'btn',
          onClick: () => { settle('reload'); closeModal(); },
        }, 'Reload remote'),
        el('button', {
          class: 'btn danger',
          onClick: () => { settle('overwrite'); closeModal(); },
        }, 'Overwrite'),
      ),
    );
    openModal(body, { onClose: () => settle(null) });
  });
}


/* Wrap a save with version-conflict handling. ``draft`` is mutated:
 * its ``version_id`` and ``updated_at`` are kept in sync with whatever
 * the server most recently confirmed. On reload, ``onReload(latest)``
 * is the caller's hook to swap the draft contents in their view (the
 * caller knows how to repaint its inputs). */
export async function saveWithConflictHandling({
  draft, saveFn, getFn, entityLabel, onReload,
}) {
  while (true) {
    try {
      const saved = await saveFn(draft);
      if (saved && typeof saved === 'object') {
        if (saved.version_id) draft.version_id = saved.version_id;
        if (saved.updated_at) draft.updated_at = saved.updated_at;
      }
      return saved;
    } catch (e) {
      if (!(e instanceof HttpError) || e.status !== 409) throw e;
      const detail = (e.body && e.body.detail) || {};
      const decision = await openConflictModal({
        entityLabel,
        localUpdatedAt: draft.updated_at,
        remoteUpdatedAt: detail.current_updated_at,
      });
      if (decision === 'overwrite') {
        // Retry with the live version_id; loop until success or the
        // user backs out via cancel.
        if (detail.current_version_id) draft.version_id = detail.current_version_id;
        continue;
      }
      if (decision === 'reload') {
        const latest = await getFn();
        if (onReload) onReload(latest);
        return latest;
      }
      // Cancel — leave the draft as-is. The next user edit re-triggers
      // ``makeAutoSaver``; we'll surface the conflict again then. Throw
      // a sentinel error so the autosaver leaves the dot in ``error``
      // state rather than clearing it to "Saved".
      throw new HttpError(
        409, 'Save deferred — conflict not resolved',
        { detail: { code: 'version_conflict_deferred' } },
      );
    }
  }
}
