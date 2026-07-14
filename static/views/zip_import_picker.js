/* Three-phase bulk-zip-import picker.
 *
 * Phase 1 — Upload: ``api.zipImportUpload`` runs an XHR with upload-progress
 *   events; we drive a "Uploading {name}" progress bar from 0% → 100%.
 *   Resolves to ``{token}``.
 * Phase 2 — Reading: ``api.zipImportToc(token)`` opens an SSE stream whose
 *   ``progress`` events tick "Examining N / M entries". The terminal
 *   ``manifest`` event gives us the picker payload.
 * Phase 3 — Picker: a search box + bulk-action menu sit above a virtualized
 *   row list. Search filters in place via the precomputed ``_search`` field;
 *   bulk-action commands operate on the visible (filtered) subset only.
 *   On submit we POST ``{token, selection}`` to ``/api/import-zip`` and
 *   stream its SSE bulk-import progress. On cancel we DELETE the token so
 *   the staging file doesn't sit on disk until the next server restart.
 *
 * The virtualizer renders only ~window-of-rows + buffer at a time so a
 * thousand-row manifest doesn't bog the picker down. Unmounted rows are
 * absent from the DOM entirely.
 */

import { api } from '../api.js';
import { el, debounce } from '../util.js';
import { openModal, closeModal, confirmModal } from '../ui.js';
import { makeSelect } from '../avatar_picker.js';
import { renderBubble } from '../aer_render.js';
import { createVirtList } from '../virt_list.js';


// ``[value, label]`` pairs per (kind, state). The first entry is the default.
const CONTACT_OPTIONS = {
  not_imported: [
    ['import', 'Import'], ['skip', 'Skip'],
  ],
  up_to_date: [
    ['reuse', 'Reuse existing'], ['replace', 'Replace'],
    ['copy', 'Import as new copy'], ['skip', 'Skip'],
  ],
  updated_upstream: [
    ['reuse', 'Reuse existing'], ['replace', 'Replace (apply update)'],
    ['copy', 'Import as new copy'], ['skip', 'Skip'],
  ],
  modified_locally: [
    ['reuse', 'Reuse existing'], ['replace', 'Replace (overwrite local edits)'],
    ['copy', 'Import as new copy'], ['skip', 'Skip'],
  ],
  conflict: [
    ['reuse', 'Reuse existing'], ['replace', 'Replace (overwrite local edits)'],
    ['copy', 'Import as new copy'], ['skip', 'Skip'],
  ],
};

const CHAT_OPTIONS = {
  not_imported: [
    ['import', 'Import'], ['skip', 'Skip'],
  ],
  up_to_date: [
    ['skip', 'Skip'], ['replace', 'Replace'], ['copy', 'Import as new copy'],
  ],
  updated_upstream: [
    ['skip', 'Skip'], ['replace', 'Replace (apply update)'],
    ['copy', 'Import as new copy'],
  ],
  modified_locally: [
    ['skip', 'Skip'], ['replace', 'Replace (overwrite local edits)'],
    ['copy', 'Import as new copy'],
  ],
  conflict: [
    ['skip', 'Skip'], ['replace', 'Replace (overwrite local edits)'],
    ['copy', 'Import as new copy'],
  ],
};

const FLAT_OPTIONS = {
  not_imported: [
    ['import', 'Import'], ['skip', 'Skip'],
  ],
  up_to_date: [
    ['skip', 'Skip'], ['replace', 'Replace'],
    ['copy', 'Import as new copy'],
  ],
};

const STATE_BADGES = {
  up_to_date: { label: 'Imported', tone: 'neutral' },
  updated_upstream: { label: 'Update available', tone: 'accent' },
  modified_locally: { label: 'Edited locally', tone: 'neutral' },
  conflict: { label: 'Conflict', tone: 'accent' },
};

const KIND_LABELS = {
  contact: 'Contact', user: 'User', scenario: 'Scenario',
  brain_library: 'Library', context_preset: 'Context preset',
  chat: 'Chat',
};

// Estimated row heights (px) — used by the virtualizer until a row mounts
// and reports its real height. Slight under-estimates are fine; a row's
// height is cached after first measurement.
const ROW_HEIGHT_CONTACT = 220;  // contact row + a couple of expanded space rows
const ROW_HEIGHT_FLAT = 80;


/* Public entry point. Drives upload → ToC → picker → bulk-import,
 * threading a server-side token through. ``file`` is the .zip File.
 * Resolves to the bulk-import result on success; throws on cancel/error
 * (the token is wiped server-side in either case).
 *
 * Cancel paths the user has access to:
 *   - Cancel button / backdrop click / Escape on any phase's modal →
 *     aborts the in-flight XHR / EventSource / fetch, deletes the token.
 *   - Tab close / page reload → ``beforeunload`` fires a ``keepalive``
 *     DELETE so the staging dir is wiped immediately rather than
 *     waiting for the next server restart.
 */
export async function runZipImport(file, opts = {}) {
  let token = null;
  // ``suppressClose`` is set true around our own programmatic
  // ``closeModal`` calls so the modal's ``onClose`` doesn't treat a
  // clean phase transition as a user-initiated cancel.
  let suppressClose = false;

  // One AbortController spans the whole flow; user-initiated modal
  // closes and the ``beforeunload`` handler both flip it.
  const ac = new AbortController();
  function abortAndCleanup() {
    if (ac.signal.aborted) return;
    try { ac.abort(); } catch {}
    if (token) api.zipImportCancel(token);
  }
  function onModalClose() {
    if (!suppressClose) abortAndCleanup();
  }

  // ``beforeunload`` survives page tear-down via ``keepalive``. The
  // server's _clean_temp_dir would eventually catch a leftover on next
  // restart, but we'd rather not litter for the time in between.
  function onBeforeUnload() {
    if (token) {
      try {
        fetch(`/api/import-zip-token/${encodeURIComponent(token)}`, {
          method: 'DELETE', keepalive: true,
        });
      } catch {}
    }
  }
  window.addEventListener('beforeunload', onBeforeUnload);

  const progress = openProgressModal(opts.title || 'Bulk import', {
    onClose: onModalClose,
  });

  try {
    // Phase 1 — upload (XHR with upload.onprogress).
    progress.phase('Uploading');
    progress.label(`Uploading ${file.name}`);
    progress.bar(0, file.size || 0);
    progress.counter(formatBytes(0, file.size || 0));
    const { token: t } = await api.zipImportUpload(file, {
      signal: ac.signal,
      onProgress: ({ loaded, total }) => {
        progress.bar(loaded, total);
        progress.counter(formatBytes(loaded, total));
      },
    });
    token = t;

    // Phase 2 — preprocessing (SSE).
    progress.phase('Reading archive');
    progress.label('Reading archive…');
    progress.bar(0, 0);
    progress.counter('');
    const manifest = await api.zipImportToc(token, {
      signal: ac.signal,
      onProgress: (p) => {
        if (p.label) progress.label(p.label);
        if (p.total > 0) {
          progress.bar(p.current, p.total);
          progress.counter(`${p.current} / ${p.total}`);
        }
      },
    });

    // Clean transition to phase 3 — closing the progress modal here is
    // not a cancel, so silence the abort path.
    suppressClose = true;
    closeModal();
    suppressClose = false;

    // Phase 3 — picker. Has its own cancel path; backdrop / Escape /
    // Cancel button all route through the picker's onCancel.
    let selection;
    if (manifest.format === 'aer_bulk') {
      if (!manifest.contacts || !manifest.contacts.length) {
        throw new Error('Archive contained no contacts');
      }
      selection = await openAerPicker(manifest);
    } else if (manifest.format === 'flat_json') {
      if (!manifest.items || !manifest.items.length) {
        throw new Error('Archive contained no recognisable JSON exports');
      }
      selection = await openFlatPicker(manifest);
    } else {
      throw new Error('Archive contained no recognisable contents');
    }
    if (!selection) {
      abortAndCleanup();
      const err = new Error('Import cancelled');
      err.cancelled = true;
      throw err;
    }

    // Phase 4 — streaming bulk import. Pass ``onModalClose`` so the
    // import progress modal's Cancel/backdrop/Escape go through the
    // same abort path; the AbortController severs the fetch and the
    // server's ``finally`` wipes the staged archive.
    const result = await streamBulkImport(token, selection, ac.signal, {
      ...opts,
      onClose: onModalClose,
    });
    return result;
  } catch (e) {
    if (ac.signal.aborted || (e && e.cancelled)) {
      // User-initiated cancel: re-package whatever the in-flight phase
      // happened to throw (``AbortError`` from fetch, ``Upload aborted``
      // from XHR, etc.) as a uniform cancelled error so catch sites
      // can suppress the failure toast.
      const err = (e && e.cancelled) ? e : new Error('Import cancelled');
      err.cancelled = true;
      throw err;
    }
    if (token) api.zipImportCancel(token);
    throw e;
  } finally {
    window.removeEventListener('beforeunload', onBeforeUnload);
    // Belt-and-braces — close any modal still open. Always silenced
    // here since we've already either succeeded or thrown.
    suppressClose = true;
    closeModal();
  }
}


/* ====== Progress modal helper ====== */

function openProgressModal(title, opts = {}) {
  const phaseChip = el('div', { class: 'progress-phase hidden' }, '');
  const label = el('div', { class: 'progress-label' }, '');
  const fill = el('div', { class: 'progress-fill' });
  const track = el('div', { class: 'progress-track' }, fill);
  const counter = el('div', { class: 'progress-counter' }, '');
  const cancelBtn = el('button', {
    class: 'btn ghost', onClick: () => closeModal(),
  }, 'Cancel');
  const body = el('div', {},
    el('h3', {}, title),
    phaseChip, label, track, counter,
    el('div', { class: 'modal-actions' }, cancelBtn),
  );
  openModal(body, { onClose: opts.onClose });
  return {
    phase(text) {
      if (text) {
        phaseChip.textContent = text;
        phaseChip.classList.remove('hidden');
      } else {
        phaseChip.classList.add('hidden');
      }
    },
    label(text) { label.textContent = text || ''; },
    bar(current, total) {
      if (total > 0) {
        fill.style.width = `${(current / total) * 100}%`;
      } else {
        fill.style.width = '0%';
      }
    },
    counter(text) { counter.textContent = text || ''; },
  };
}


function formatBytes(loaded, total) {
  const mb = (n) => (n / (1024 * 1024)).toFixed(1);
  if (total > 0) return `${mb(loaded)} / ${mb(total)} MB`;
  return `${mb(loaded)} MB`;
}


/* ====== Picker (AER bulk format) ====== */

function openAerPicker(manifest) {
  return new Promise((resolve) => {
    let settled = false;
    const settle = (v) => { if (!settled) { settled = true; resolve(v); } };

    // Initial choices keyed by contact src_id; each is { action, spaces }.
    const choices = {};
    const initialAction = (c) => (CONTACT_OPTIONS[c.state] || CONTACT_OPTIONS.not_imported)[0][0];
    const initialSpaceAction = (s) => (CHAT_OPTIONS[s.chat_state] || CHAT_OPTIONS.not_imported)[0][0];
    for (const c of manifest.contacts) {
      const spaces = {};
      for (const s of c.spaces || []) spaces[s.src_id] = initialSpaceAction(s);
      choices[c.src_id] = { action: initialAction(c), spaces };
    }

    const introText = `${manifest.contacts.length} contact${
      manifest.contacts.length === 1 ? '' : 's'
    } in archive. Pick which to import; "already imported" rows default to skip.`;

    const shell = renderPickerShell({
      title: 'Bulk import',
      introText,
      items: manifest.contacts,
      searchOf: (c) => c._search || (c.name || '').toLowerCase(),
      estimatedHeight: ROW_HEIGHT_CONTACT,
      renderRow: (c) => renderContactRow(c, choices[c.src_id]),
      bulkApply: (visible, command) => {
        for (const c of visible) {
          // Map "import"/"skip" onto the state-valid action for each row.
          choices[c.src_id].action = nearestAction(
            CONTACT_OPTIONS[c.state] || CONTACT_OPTIONS.not_imported,
            command,
          );
          for (const s of c.spaces || []) {
            choices[c.src_id].spaces[s.src_id] = nearestAction(
              CHAT_OPTIONS[s.chat_state] || CHAT_OPTIONS.not_imported,
              command,
            );
          }
        }
      },
      resetDefaults: (visible) => {
        for (const c of visible) {
          choices[c.src_id].action = initialAction(c);
          for (const s of c.spaces || []) {
            choices[c.src_id].spaces[s.src_id] = initialSpaceAction(s);
          }
        }
      },
      onCancel: () => settle(null),
      onSubmit: async () => {
        const replaces = countAerReplaces(choices);
        if (replaces > 0) {
          const ok = await confirmModal(
            'Replace existing entities?',
            `${replaces} item${replaces === 1 ? '' : 's'} will be replaced. `
            + `Existing data (including any local edits) will be discarded.`,
            { danger: true, confirmLabel: 'Replace' },
          );
          if (!ok) {
            // User backed out — re-open the picker.
            shell.reopen();
            return;
          }
        }
        settle(collectAerSelection(choices));
        closeModal();
      },
    });
  });
}


function openFlatPicker(manifest) {
  return new Promise((resolve) => {
    let settled = false;
    const settle = (v) => { if (!settled) { settled = true; resolve(v); } };

    const choices = {};
    const initialFlatAction = (item) => (
      (FLAT_OPTIONS[item.state] || FLAT_OPTIONS.not_imported)[0][0]
    );
    for (const item of manifest.items) {
      choices[item.path] = initialFlatAction(item);
    }

    // Tally per kind for the intro.
    const tally = { contact: 0, user: 0, scenario: 0, chat: 0 };
    for (const item of manifest.items) {
      if (tally[item.kind] !== undefined) tally[item.kind] += 1;
    }
    const tallyParts = Object.entries(tally)
      .filter(([, n]) => n > 0)
      .map(([kind, n]) => `${n} ${kind}${n === 1 ? '' : 's'}`);
    const introText = `${manifest.items.length} item${
      manifest.items.length === 1 ? '' : 's'
    } in archive (${tallyParts.join(', ')}). Existing entries default to skip.`;

    const shell = renderPickerShell({
      title: 'Bulk import',
      introText,
      items: manifest.items,
      searchOf: (item) => (
        item._search || (
          (item.name || '') + ' ' + (item.description || '')
        ).toLowerCase()
      ),
      estimatedHeight: ROW_HEIGHT_FLAT,
      renderRow: (item) => renderFlatItemRow(item, choices),
      bulkApply: (visible, command) => {
        for (const item of visible) {
          choices[item.path] = nearestAction(
            FLAT_OPTIONS[item.state] || FLAT_OPTIONS.not_imported,
            command,
          );
        }
      },
      resetDefaults: (visible) => {
        for (const item of visible) choices[item.path] = initialFlatAction(item);
      },
      onCancel: () => settle(null),
      onSubmit: async () => {
        const replaces = Object.values(choices)
          .filter(a => a === 'replace').length;
        if (replaces > 0) {
          const ok = await confirmModal(
            'Replace existing entities?',
            `${replaces} item${replaces === 1 ? '' : 's'} will be replaced. `
            + `Existing data (including any local edits) will be discarded.`,
            { danger: true, confirmLabel: 'Replace' },
          );
          if (!ok) {
            shell.reopen();
            return;
          }
        }
        const items = Object.entries(choices).map(
          ([path, action]) => ({ path, action }),
        );
        settle({ format: 'flat_json', items });
        closeModal();
      },
    });
  });
}


/* ====== Picker shell (search + bulk + virtualizer) ======
 *
 * Rendered once and re-attached on ``reopen()``. The closure-state
 * choices map lives in the caller; we just call back into ``renderRow``
 * which reads from it. */

function renderPickerShell(opts) {
  const {
    title, introText, items, searchOf, renderRow, estimatedHeight,
    bulkApply, resetDefaults, onCancel, onSubmit,
  } = opts;

  const searchInput = el('input', {
    class: 'zip-picker-search', type: 'text',
    placeholder: 'Search names, descriptions, tags…',
    autocomplete: 'off',
  });

  const bulkSelect = makeSelect({
    value: '',
    options: [
      ['', 'Bulk actions…'],
      ['import', 'Import all'],
      ['skip', 'Skip all'],
      ['reset', 'Reset to defaults'],
    ],
    onChange: (v) => {
      if (!v) return;
      const visible = currentItems();
      if (v === 'reset') resetDefaults(visible);
      else bulkApply(visible, v);
      bulkSelect.setValue('');
      // Rebuild rows so each select widget reflects the new action.
      virt.setItems(visible);
    },
  });

  const listEl = el('div', { class: 'zip-picker-list' });

  // Track the currently-displayed (post-search) item slice.
  let _items = items.slice();
  const currentItems = () => _items;

  const virt = virtualize(listEl, _items, renderRow, estimatedHeight);

  const onSearch = debounce(() => {
    const q = (searchInput.value || '').trim().toLowerCase();
    _items = q
      ? items.filter(it => searchOf(it).includes(q))
      : items.slice();
    virt.setItems(_items);
  }, 80);
  searchInput.addEventListener('input', onSearch);

  const body = el('div', { class: 'zip-picker' },
    el('h3', {}, title),
    el('p', { class: 'zip-picker-intro' }, introText),
    el('div', { class: 'zip-picker-header' },
      el('div', { class: 'zip-picker-search-wrap' }, searchInput),
      el('div', { class: 'zip-picker-bulk' }, bulkSelect),
    ),
    listEl,
    el('div', { class: 'modal-actions' },
      el('button', {
        class: 'btn ghost',
        onClick: () => { onCancel(); closeModal(); },
      }, 'Cancel'),
      el('button', {
        class: 'btn primary',
        onClick: onSubmit,
      }, 'Import'),
    ),
  );

  openModal(body, { size: 'large', onClose: () => onCancel() });

  // After the modal lays out, the list element finally has a real height —
  // give the virtualizer a chance to expand the visible window.
  requestAnimationFrame(() => virt.reflow());

  return {
    reopen() {
      // Used when the destructive-confirm dialog cancels and we want to
      // bring the picker back on top.
      setTimeout(() => {
        openModal(body, { size: 'large', onClose: () => onCancel() });
        requestAnimationFrame(() => virt.reflow());
      }, 0);
    },
    mountedCount: () => virt.mountedCount(),
  };
}


/* Map a bulk-action command (``import`` / ``skip``) onto the closest valid
 * action for a given options list. Falls back to the first option if the
 * literal command isn't there (e.g. ``import`` on an already-imported row
 * — picks ``replace``, the import-equivalent). */
function nearestAction(options, command) {
  const values = options.map(o => o[0]);
  if (values.includes(command)) return command;
  if (command === 'import' && values.includes('replace')) return 'replace';
  if (command === 'skip' && values.includes('skip')) return 'skip';
  return values[0];
}


/* ====== Row rendering (AER) ====== */

function renderContactRow(contact, choice) {
  const avatar = renderAvatar(contact.avatar_uri, contact.name);

  const meta = el('div', { class: 'zip-picker-meta' },
    el('div', { class: 'zip-picker-title' },
      contact.name || '(unnamed)',
      ...renderStateBadges(contact.state),
      contact.name_collision
        ? el('span', { class: 'zip-picker-badge zip-picker-badge-neutral' },
            `Matches '${contact.name_collision.name}'`)
        : null,
    ),
    renderDescOrTags(contact.description, contact.tags),
  );

  const select = makeSelect({
    value: choice.action,
    options: CONTACT_OPTIONS[contact.state] || CONTACT_OPTIONS.not_imported,
    onChange: (v) => {
      choice.action = v;
      row.classList.toggle('zip-picker-row-skipped', v === 'skip');
    },
  });

  const childList = el('div', { class: 'zip-picker-children' });
  if (contact.spaces && contact.spaces.length) {
    for (const s of contact.spaces) {
      childList.append(renderSpaceRow(s, choice.spaces));
    }
  } else {
    childList.append(
      el('div', { class: 'zip-picker-no-children' }, 'No chats in this contact.'),
    );
  }

  let expanded = true;
  const chevron = el('button', {
    type: 'button', class: 'zip-picker-chevron',
    title: 'Toggle chats', 'aria-label': 'Toggle chats',
    onClick: () => {
      expanded = !expanded;
      childList.classList.toggle('hidden', !expanded);
      chevron.textContent = expanded ? '▾' : '▸';
      // Tell the virtualizer to re-measure this row + shift the rows
      // below it. ResizeObserver can miss this case in some browsers,
      // so dispatch a custom event the virtualizer listens for too.
      row.dispatchEvent(new CustomEvent('virtRowResize', { bubbles: true }));
    },
  }, '▾');

  const row = el('div', { class: 'zip-picker-row zip-picker-row-contact' },
    el('div', { class: 'zip-picker-row-main' }, chevron, avatar, meta, select),
    childList,
  );
  if (choice.action === 'skip') row.classList.add('zip-picker-row-skipped');
  return row;
}


function renderSpaceRow(space, spaceChoices) {
  const meta = el('div', { class: 'zip-picker-meta' },
    el('div', { class: 'zip-picker-title' },
      space.name,
      ...renderStateBadges(space.chat_state),
      space.has_overrides
        ? el('span', { class: 'zip-picker-badge zip-picker-badge-neutral' },
            'Custom scenario')
        : null,
    ),
    el('div', { class: 'zip-picker-meta-row' },
      el('span', { class: 'zip-picker-msg-count' },
        `${space.message_count} msg${space.message_count === 1 ? '' : 's'}`),
      space.last_update
        ? el('span', { class: 'zip-picker-time' }, space.last_update.slice(0, 10))
        : null,
    ),
    space.latest_snippet ? renderSnippet(space.latest_snippet) : null,
    space.scene_excerpt
      ? el('div', {
          class: 'zip-picker-desc', title: space.scene_excerpt,
        }, `Scene: ${space.scene_excerpt}`)
      : null,
    space.environment_excerpt
      ? el('div', {
          class: 'zip-picker-desc', title: space.environment_excerpt,
        }, `Env: ${space.environment_excerpt}`)
      : null,
  );

  const select = makeSelect({
    value: spaceChoices[space.src_id],
    options: CHAT_OPTIONS[space.chat_state] || CHAT_OPTIONS.not_imported,
    onChange: (v) => { spaceChoices[space.src_id] = v; },
  });

  return el('div', { class: 'zip-picker-row zip-picker-row-space' },
    el('div', { class: 'zip-picker-row-main' }, meta, select),
  );
}


/* ====== Row rendering (flat) ====== */

function renderFlatItemRow(item, choices) {
  const avatar = renderAvatar(item.avatar_uri, item.name);
  const kindLabel = KIND_LABELS[item.kind] || item.kind;

  const meta = el('div', { class: 'zip-picker-meta' },
    el('div', { class: 'zip-picker-title' },
      el('span', { class: 'zip-picker-badge zip-picker-badge-neutral' }, kindLabel),
      item.name || '(unnamed)',
      ...renderStateBadges(item.state),
    ),
    renderDescOrTags(item.description, item.tags),
    // Filename — entities often share a display name across versioned
    // exports (``Foo_v1.json`` / ``Foo_v2.json``); the path is what
    // distinguishes them. ``title`` lets a hover reveal a path that's
    // been ellipsised when the row is narrow.
    item.path
      ? el('div', {
          class: 'zip-picker-path',
          title: item.path,
        }, item.path)
      : null,
  );

  const select = makeSelect({
    value: choices[item.path],
    options: FLAT_OPTIONS[item.state] || FLAT_OPTIONS.not_imported,
    onChange: (v) => { choices[item.path] = v; },
  });

  return el('div', { class: 'zip-picker-row zip-picker-row-flat' },
    el('div', { class: 'zip-picker-row-main' }, avatar, meta, select),
  );
}


/* ====== Shared row helpers ====== */

function renderAvatar(avatarUri, name) {
  if (!avatarUri) {
    return el('div', { class: 'zip-picker-avatar zip-picker-monogram' },
      (name || '?').slice(0, 1).toUpperCase());
  }
  return el('img', {
    class: 'zip-picker-avatar', src: avatarUri,
    loading: 'lazy', referrerpolicy: 'no-referrer',
    onError: (e) => {
      const m = el('div', { class: 'zip-picker-avatar zip-picker-monogram' },
        (name || '?').slice(0, 1).toUpperCase());
      e.target.replaceWith(m);
    },
  });
}


/* Body line for an AER contact row. Description wins the visible line
 * when present; tags fold into the hover title alongside it. With only
 * tags present, tags promote to the visible line.
 *
 * Returns ``null`` when neither is set, so the caller can drop it into
 * the meta column without a wrapper guard. */
function renderDescOrTags(description, tags) {
  const tagsText = (tags && tags.length) ? tags.join(', ') : '';
  if (description) {
    const title = tagsText
      ? `${description}\nTags: ${tagsText}`
      : description;
    return el('div', {
      class: 'zip-picker-desc', title,
    }, description);
  }
  if (tagsText) {
    return el('div', {
      class: 'zip-picker-tags', title: tagsText,
    }, tagsText);
  }
  return null;
}


function renderSnippet(text) {
  // Run the chat snippet through the AER inline renderer so asterisk /
  // underscore markup shows the same way it does in the live chat view.
  // ``renderBubble`` already escapes its input. The plain-text source
  // goes on ``title`` so a hover reveals the full snippet when the row
  // is narrow enough to ellipsis.
  const div = el('div', { class: 'zip-picker-snippet', title: text });
  div.innerHTML = renderBubble(text);
  return div;
}


function renderStateBadges(state) {
  const badge = STATE_BADGES[state];
  if (!badge) return [];
  return [
    el('span', {
      class: `zip-picker-badge zip-picker-badge-${badge.tone}`,
    }, badge.label),
  ];
}


/* ====== Selection collection ====== */

function collectAerSelection(choices) {
  const contacts = [];
  for (const [src_id, c] of Object.entries(choices)) {
    contacts.push({
      src_id,
      action: c.action,
      spaces: Object.entries(c.spaces).map(
        ([sid, ca]) => ({ src_id: sid, chat_action: ca }),
      ),
    });
  }
  return { format: 'aer_bulk', contacts };
}


function countAerReplaces(choices) {
  let n = 0;
  for (const c of Object.values(choices)) {
    if (c.action === 'replace') n += 1;
    for (const a of Object.values(c.spaces)) {
      if (a === 'replace') n += 1;
    }
  }
  return n;
}


/* ====== Bulk import streaming ====== */

async function streamBulkImport(token, selection, signal, opts = {}) {
  // ``suppressClose`` lets us distinguish the natural finally-close from
  // a user-initiated close (Cancel / Escape / backdrop) — only the
  // latter calls the caller's ``onClose`` which aborts the fetch.
  let suppressClose = false;
  const progress = openProgressModal(opts.title || 'Importing', {
    onClose: () => { if (!suppressClose && opts.onClose) opts.onClose(); },
  });
  progress.phase('Importing');
  progress.label('Starting bulk import…');

  const PHASE_LABELS = {
    bulk: 'Importing',
    download: 'Downloading',
    derive: 'Saving display images',
  };

  try {
    return await api.zipImportSelected(token, selection, {
      signal,
      onProgress: (p) => {
        if (p.phase && PHASE_LABELS[p.phase]) {
          progress.phase(PHASE_LABELS[p.phase]);
        }
        if (p.label) progress.label(p.label);
        if (p.total > 0) {
          progress.bar(p.current, p.total);
          const failed = p.failed || 0;
          progress.counter(failed > 0
            ? `${p.current} / ${p.total}  (${failed} failed)`
            : `${p.current} / ${p.total}`);
        }
      },
    });
  } finally {
    suppressClose = true;
    closeModal();
  }
}


/* ====== Picker virtualizer shim ======
 *
 * Wraps the shared ``createVirtList`` from ``static/virt_list.js`` with
 * an items-array / ``setItems`` API. Stable identity: the same
 * ``_items`` array reference survives a search filter, and the
 * closure-captured ``getItem`` reads from it on each mount so a row
 * swap is one array replace + setCount.
 */

function virtualize(scrollContainer, items, renderItem, estimatedHeight) {
  let _items = items.slice();
  const virt = createVirtList(scrollContainer, {
    count: _items.length,
    getItem: (i) => _items[i],
    renderItem,
    estimatedHeight,
  });

  return {
    reflow: () => virt.refresh(),
    setItems(newItems) {
      _items = newItems.slice();
      virt.setCount(_items.length);
    },
    mountedCount: () => virt.mountedCount(),
  };
}
