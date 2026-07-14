/* Reusable progress modal for SSE-streaming imports.
 *
 * Pops a small modal with a label + progress bar + counter, calls
 * ``api.importFile(file, { onProgress, onConflict })`` under the hood, and
 * resolves/rejects with the import result when done. On UUID conflict, asks
 * the user whether to overwrite the existing entity or import as a copy.
 */

import { api } from '../api.js';
import { setState } from '../state.js';
import { el, avatarEl } from '../util.js';
import { openModal, closeModal, confirmModal, toast } from '../ui.js';
import { runZipImport } from './zip_import_picker.js';


/* Tab id + active-entity state key for each importable entity kind. */
const KIND_TO_NAV = {
  contact:       { tab: 'contacts',    activeKey: 'activeContactId' },
  user:          { tab: 'users',       activeKey: 'activeUserId' },
  scenario:      { tab: 'scenarios',   activeKey: 'activeScenarioId' },
  brain_library: { tab: 'libraries',   activeKey: 'activeLibraryId' },
  chat:          { tab: 'chats',       activeKey: 'activeChatId' },
};

const KIND_LABEL = {
  contact: 'contact',
  user: 'persona',
  scenario: 'scenario',
  brain_library: 'brain library',
  chat: 'chat',
};


/* Refresh whichever list is now relevant for ``kind`` so the cross-tab
 * navigation lands on a populated list. Chat composite imports may also
 * mint new contact / user / scenario / library sidecars, so refresh those
 * lists too — otherwise the just-imported character/persona doesn't show
 * up in its tab until the next full reload. */
async function _refreshListsForKind(kind) {
  if (kind === 'contact')  setState({ contacts: await api.listContacts() });
  else if (kind === 'user')          setState({ users: await api.listUsers() });
  else if (kind === 'scenario')      setState({ scenarios: await api.listScenarios() });
  else if (kind === 'brain_library') setState({ libraries: await api.listBrainLibraries() });
  else if (kind === 'chat') {
    const [contacts, users, scenarios, libraries, chats] = await Promise.all([
      api.listContacts(), api.listUsers(), api.listScenarios(),
      api.listBrainLibraries(), api.listChats(),
    ]);
    setState({ contacts, users, scenarios, libraries, chats });
  }
}


/* Auto-navigate to whichever tab the imported entity belongs to and open
 * its detail view. Used after a single-file import — the user dropped a
 * file on whatever tab they were on, and the server told us what kind it
 * was. The Import button accepts everything; the UI follows.
 *
 * ``opts.silent`` suppresses the toast (useful when the caller wants to
 * surface its own summary, e.g. zip imports that span multiple kinds). */
export async function navigateToImported(result, opts = {}) {
  if (!result || !result.kind) return;
  const nav = KIND_TO_NAV[result.kind];
  if (!nav) return;
  await _refreshListsForKind(result.kind);
  const patch = { activeTab: nav.tab };
  patch[nav.activeKey] = result.id;
  setState(patch);
  if (!opts.silent) {
    const label = KIND_LABEL[result.kind] || result.kind;
    const name = result.name || result.title || '';
    toast(name ? `Imported ${label}: ${name}` : `Imported ${label}.`, 'success');
  }
}


/* Single entry point for every per-tab Import button. Accepts JSON,
 * PNG (with embedded card data), or ZIP (bulk import) — dispatches by
 * extension at the picker level. After a single-file import succeeds
 * it auto-switches the user to whichever tab the kind belongs to. For
 * zip imports it shows a multi-kind summary toast and refreshes all
 * relevant lists.
 */
export async function runImport(file, opts = {}) {
  if (!file) return null;
  try {
    const r = await importWithProgress(file, opts);
    const isZip = file.name && file.name.toLowerCase().endsWith('.zip');
    if (isZip && r && r.imported) {
      const i = r.imported;
      const parts = [];
      if (i.contacts)  parts.push(`${i.contacts} contact${i.contacts === 1 ? '' : 's'}`);
      if (i.users)     parts.push(`${i.users} persona${i.users === 1 ? '' : 's'}`);
      if (i.scenarios) parts.push(`${i.scenarios} scenario${i.scenarios === 1 ? '' : 's'}`);
      if (i.libraries) parts.push(`${i.libraries} librar${i.libraries === 1 ? 'y' : 'ies'}`);
      if (i.chats)     parts.push(`${i.chats} chat${i.chats === 1 ? '' : 's'}`);
      toast(`Imported ${parts.join(', ') || 'nothing'}.`, 'success');
      // Refresh every list that might have changed.
      const [contacts, users, scenarios, libraries, chats] = await Promise.all([
        api.listContacts(), api.listUsers(), api.listScenarios(),
        api.listBrainLibraries(), api.listChats(),
      ]);
      setState({ contacts, users, scenarios, libraries, chats });
      return r;
    }
    if (r && r.kind) {
      await navigateToImported(r);
    }
    return r;
  } catch (err) {
    if (!err.cancelled) toast(`Import failed: ${err.message}`, 'error');
    return null;
  }
}


export async function importWithProgress(file, opts = {}) {
  // Multi-entity AER zip archives go through a separate two-step picker —
  // ToC, user-driven selection, then SSE bulk import — instead of the
  // single-JSON ``conflict`` / ``name_matches`` flow below.
  if (file && file.name && file.name.toLowerCase().endsWith('.zip')) {
    return await runZipImport(file, opts);
  }

  // Phase chip above the label communicates the current sub-phase
  // ("Downloading" → "Building previews"). Hidden until a phase event
  // arrives so non-phased imports (legacy events, short imports) keep
  // their two-line layout.
  const phaseChip = el('div', { class: 'progress-phase hidden' }, '');
  const label = el('div', { class: 'progress-label' }, `Importing ${file.name}…`);
  const fill = el('div', { class: 'progress-fill' });
  const track = el('div', { class: 'progress-track' }, fill);
  const counter = el('div', { class: 'progress-counter' }, '');

  const body = el('div', {},
    el('h3', {}, opts.title || 'Importing'),
    phaseChip,
    label,
    track,
    counter,
  );
  openModal(body);

  const PHASE_LABELS = { download: 'Downloading', derive: 'Saving display images' };

  try {
    return await api.importFile(file, {
      onProgress: (p) => {
        if (p.phase && PHASE_LABELS[p.phase]) {
          phaseChip.textContent = PHASE_LABELS[p.phase];
          phaseChip.classList.remove('hidden');
        }
        if (p.label) label.textContent = p.label;
        if (p.total > 0) {
          fill.style.width = `${(p.current / p.total) * 100}%`;
          const failed = p.failed || 0;
          counter.textContent = failed > 0
            ? `${p.current} / ${p.total}  (${failed} failed)`
            : `${p.current} / ${p.total}`;
        } else {
          fill.style.width = '0%';
          counter.textContent = '';
        }
      },
      onConflict: async (info) => {
        // ``askConflict`` swaps the progress modal for its own (and a
        // confirm prompt on top of that for overwrites). Once the user
        // decides we re-open the progress UI so the follow-up upload's
        // events have somewhere to render.
        const decision = await askConflict(info);
        if (decision) openModal(body);
        return decision;
      },
      onNameMatches: async (info) => {
        const decisions = await askNameMatches(info);
        if (decisions) openModal(body);
        return decisions;
      },
    });
  } finally {
    closeModal();
  }
}


/* Prompt the user when an imported entity's UUID is already on disk. The
 * import-progress modal stays put under it until the user picks. Returns
 * ``'replace' | 'copy' | null`` (null = cancel). */
async function askConflict(info) {
  const kind = info.kind;
  const incoming = info.incoming_name || `(unnamed ${kind})`;
  const existing = info.existing_name || `(unnamed ${kind})`;

  return new Promise((resolve) => {
    let settled = false;
    const settle = (v) => { if (!settled) { settled = true; resolve(v); } };

    const body = el('div', {},
      el('h3', {}, 'Already exists'),
      el('p', { style: { color: 'var(--text-dim)', margin: '4px 0 4px' } },
        `A ${kind} with the same ID is already on disk:`),
      el('div', {
        style: {
          background: 'var(--bg-2)',
          padding: '8px 10px',
          borderRadius: 'var(--radius-sm)',
          fontSize: '0.929rem',
          margin: '6px 0 14px',
        },
      },
        el('div', {}, el('strong', {}, 'Existing: '), existing),
        el('div', {}, el('strong', {}, 'Incoming: '), incoming),
        el('div', { style: { color: 'var(--text-mute)', fontSize: '0.786rem', marginTop: '4px' } },
          info.id),
      ),
      el('p', { style: { color: 'var(--text-dim)', fontSize: '0.929rem' } },
        'Overwrite replaces the existing entity (and its files) with the imported one. ' +
        'New copy keeps both, assigning a fresh UUID to the import.'),
      el('div', { class: 'modal-actions' },
        el('button', { class: 'btn ghost', onClick: () => { settle(null); closeModal(); } }, 'Cancel'),
        el('button', {
          class: 'btn',
          onClick: async () => {
            // Overwrite is destructive — confirm again before committing.
            const ok = await confirmModal(
              `Overwrite ${existing}?`,
              `The existing ${kind} and its files will be replaced. This can't be undone.`,
              { danger: true, confirmLabel: 'Overwrite' },
            );
            if (ok) {
              settle('replace');
            } else {
              // User backed out of the destructive confirm — bring the
              // conflict prompt back so they can pick a different option
              // (or cancel the import outright).
              setTimeout(() => openModal(body, { onClose: () => settle(null) }), 0);
            }
          },
        }, 'Overwrite'),
        el('button', {
          class: 'btn primary',
          onClick: () => { settle('copy'); closeModal(); },
        }, 'New copy'),
      ),
    );
    openModal(body, { onClose: () => settle(null) });
  });
}


/* Prompt the user when one or more chat-composite sidecars match an existing
 * entity by name (different UUIDs). Each role gets a candidate list with
 * preview + an "Import as new" radio. Resolves to a ``{role: id|"new"}``
 * map (or ``null`` to cancel). */
async function askNameMatches(info) {
  return new Promise((resolve) => {
    let settled = false;
    const settle = (v) => { if (!settled) { settled = true; resolve(v); } };

    // Per-role chosen value. Default each to "new" so a quick Confirm just
    // imports the source data; the user has to actively pick a candidate.
    const choices = {};
    for (const item of info.items) choices[item.role] = 'new';

    const sections = info.items.map(item => renderRoleSection(item, (v) => {
      choices[item.role] = v;
    }));

    const body = el('div', { class: 'name-match-modal' },
      el('h3', {}, 'Names match existing entities'),
      el('p', { style: { color: 'var(--text-dim)', fontSize: '0.929rem', margin: '4px 0 12px' } },
        'These pieces of the imported chat share a name with something on disk ' +
        'but a different UUID. Pick which to use for each role.'),
      ...sections,
      el('div', { class: 'modal-actions' },
        el('button', { class: 'btn ghost', onClick: () => { settle(null); closeModal(); } }, 'Cancel'),
        el('button', {
          class: 'btn primary',
          onClick: () => { settle({ ...choices }); closeModal(); },
        }, 'Confirm'),
      ),
    );
    openModal(body, { size: 'large', onClose: () => settle(null) });
  });
}


function renderRoleSection(item, onPick) {
  const { role, kind, incoming, candidates } = item;
  const radioName = `nm-${role}`;

  // Group of choices: each candidate + "Import as new" at the end.
  const opts = el('div', { class: 'name-match-options' });

  for (const cand of candidates) {
    opts.append(renderCandidateRow(kind, cand, radioName, false, () => onPick(cand.id)));
  }
  // "Import as new" — uses the incoming preview so the user can see what
  // they'd be importing if they don't reuse anything.
  opts.append(renderCandidateRow(kind, incoming, radioName, true, () => onPick('new')));

  return el('div', { class: 'name-match-role' },
    el('h4', {},
      `${role.charAt(0).toUpperCase() + role.slice(1)}: `,
      el('span', { style: { color: 'var(--text-dim)', fontWeight: '400' } }, incoming.name || '(unnamed)'),
    ),
    opts,
  );
}


function renderCandidateRow(kind, entity, radioName, isImportNew, onPick) {
  const radio = el('input', {
    type: 'radio', name: radioName,
    checked: !!isImportNew,
    onChange: (e) => { if (e.target.checked) onPick(); },
  });
  // Avatar slot: existing contacts get a real avatar via avatarEl; everything
  // else gets a first-letter monogram so users / scenarios still have a
  // visual anchor in the row.
  let avatar;
  if (kind === 'contact' && !isImportNew) {
    // Synthesize a Contact-shape just enough for ``avatarEl`` to fall through
    // to the right server URL.
    avatar = avatarEl({
      id: entity.id,
      name: entity.name,
      avatar: entity.has_avatar ? '_' : null,
      emotions: entity.has_neutral_emotion ? { neutral: '_' } : {},
    });
  } else {
    avatar = el('div', { class: 'avatar' }, (entity.name || '?').slice(0, 1).toUpperCase());
  }

  const lines = [
    el('div', { class: 'name-match-title' },
      isImportNew ? 'Import as new' : entity.name || '(unnamed)',
    ),
  ];
  if (!isImportNew && entity.id) {
    lines.push(el('div', { class: 'name-match-uuid' }, entity.id));
  }
  if (entity.description) {
    lines.push(el('div', { class: 'name-match-desc' }, entity.description));
  } else if (entity.tags) {
    lines.push(el('div', { class: 'name-match-desc' }, entity.tags));
  }
  if (kind === 'scenario' && entity.environment) {
    lines.push(el('div', { class: 'name-match-desc' }, entity.environment));
  }

  const label = el('label', { class: 'name-match-row' },
    radio,
    avatar,
    el('div', { class: 'name-match-meta' }, ...lines),
  );
  return label;
}
