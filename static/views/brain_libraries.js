/* Brain libraries tab: list + edit.
 *
 * A brain library is a reusable bundle of brains (name + description + tags +
 * avatar + brains) that any chat can attach 0..N of. Library brains land in
 * the AER system prompt after the contact / user / scenario unconditionals,
 * in attach order. See ``server/aer/format.py``.
 *
 * Exports:
 *   - ``renderBrainLibrariesTab`` / ``setupBrainLibrariesTab`` — the tab.
 *   - ``mountLibraryCascade`` — cascading dropdown helper used by the
 *     new-chat wizard and the chat detail page. Each picked library reveals
 *     another ``(none)``-default dropdown excluding already-picked entries;
 *     when no libraries remain, no new dropdown appears.
 */

import { api } from '../api.js';
import { state, setState, subscribe } from '../state.js';
import {
  el, formatRelative, downloadFromResponse, sortEntities, sortControls,
  lastUsedByEntity, dirtyDot, makeAutoSaver, favoriteStar, libraryAvatarEl,
  liveBadge, applyCrop,
} from '../util.js';
import { icon, confirmModal, toast } from '../ui.js';
import { renderBrainsEditor, renderCardImageBlock, mediaBlock } from './contacts.js';
import { brainCatalogEntries } from './brain_row.js';
import { saveWithConflictHandling } from '../conflict.js';
import { markPending, clearPending } from '../save_queue.js';
import { runImport } from './import_progress.js';
import { openCropModal } from './crop_modal.js';
import { makeAvatarPicker } from '../avatar_picker.js';
import { createVirtList } from '../virt_list.js';


/* ===========================================================================
 * Tab — list + detail
 * =========================================================================== */


export function renderBrainLibrariesTab(container) {
  container.replaceChildren();
  const pane = el('div', { class: 'list-pane' });
  pane.append(renderListHeader());
  const body = el('div', { class: 'list-body', id: 'library-list-body' });
  pane.append(body);
  container.append(pane);
  const detail = el('div', { class: 'content-pane', id: 'library-detail' });
  container.append(detail);
  mountList(body);
  refreshDetail();
}


let _searchTerm = '';

function renderListHeader() {
  const search = el('input', {
    class: 'list-search',
    type: 'text',
    placeholder: 'Search libraries…',
    value: _searchTerm,
    oninput: (e) => { _searchTerm = e.target.value.toLowerCase(); refreshList(); },
  });
  const sort = sortControls(state.librarySort || { mode: 'added', direction: 'desc' }, (v) => {
    setState({ librarySort: v });
    try { localStorage.setItem('librarySort', JSON.stringify(v)); } catch {}
    refreshList();
  });
  return el('div', { class: 'list-header' },
    el('div', { style: { display: 'flex', justifyContent: 'space-between', alignItems: 'center' } },
      el('h2', {}, 'Brain libraries'),
      el('div', { style: { display: 'flex', gap: '6px' } },
        el('label', { class: 'btn', for: 'library-import-input', title: 'Import library', style: { cursor: 'pointer' } }, icon('upload', 14)),
        el('input', {
          id: 'library-import-input', type: 'file',
          accept: '.json,.png,.zip,application/json,image/png,application/zip',
          class: 'hidden',
          onChange: async (e) => {
            const f = e.target.files[0];
            if (!f) return;
            const isZip = f.name.toLowerCase().endsWith('.zip');
            await runImport(f, { title: isZip ? 'Bulk import' : 'Import' });
            e.target.value = '';
          }
        }),
        el('button', {
          class: 'btn primary',
          title: 'New brain library',
          onClick: async () => {
            try {
              const l = await api.createBrainLibrary({ name: 'New library' });
              setState({ libraries: await api.listBrainLibraries(), activeLibraryId: l.id });
            } catch (e) { toast(e.message, 'error'); }
          }
        }, icon('plus', 14)),
      ),
    ),
    el('div', { style: { display: 'flex', gap: '6px', alignItems: 'center' } },
      search,
      sort,
    ),
  );
}


function _matchesSearch(l) {
  if (!_searchTerm) return true;
  const hay = `${l.name || ''} ${l.tags || ''} ${l.description || ''}`.toLowerCase();
  return hay.includes(_searchTerm);
}


/* Mirror of ``contacts.js#rowTooltip`` — description plus an optional
 * "Tags: …" line on the next line. The tooltip surfaces what doesn't
 * fit in the always-visible single-line subtitle. */
function rowTooltip(l) {
  const desc = (l.description || '').trim();
  const tags = (l.tags || '').trim();
  if (desc && tags) return `${desc}\nTags: ${tags}`;
  return desc || tags || '';
}


function _librarySublineText(l) {
  // Description (when present) is the single visible subtitle; tags
  // fall through to the row's ``title=`` tooltip. Brain count lives in
  // its own badge element to the right (see ``refreshList``), modelled
  // on the chat-list message-count pill — keeps the count visible even
  // when a long description ellipsises.
  return (l.description || '').trim();
}


function _libraryUsageMap(chats) {
  // ``lastUsedByEntity`` only knows about single-id chat fields (contact_id /
  // user_id / scenario_id); libraries are referenced via the ``brain_library_ids``
  // list, so we roll our own usage map: latest chat updated_at per library id.
  // ``state.chats`` is sparse (paged adapter fills indices on scroll), so
  // skip ``undefined`` slots rather than dereferencing them.
  const out = {};
  for (const c of (chats || [])) {
    if (!c) continue;
    for (const lid of (c.brain_library_ids || [])) {
      const ts = c.updated_at || 0;
      if (!(lid in out) || out[lid] < ts) out[lid] = ts;
    }
  }
  return out;
}


let _virt = null;
let _visible = [];
let _emptyEl = null;
let _emptyMatchEl = null;

function mountList(body) {
  _emptyEl = el('div', { class: 'list-empty', style: { display: 'none' } },
    'No brain libraries yet.');
  _emptyMatchEl = el('div', { class: 'list-empty', style: { display: 'none' } },
    'No libraries match your search.');
  _virt = createVirtList(body, {
    count: 0,
    getItem: (i) => _visible[i] || null,
    renderItem: (l) => renderRow(l),
    estimatedHeight: 60,
    observeRows: false,
  });
  body.appendChild(_emptyEl);
  body.appendChild(_emptyMatchEl);
  refreshList();
}

function renderRow(l) {
  const isActive = state.activeLibraryId === l.id;
  const brainCount = l.brain_count != null ? l.brain_count : (l.brains || []).length;
  const countBadge = el('span', {
    class: 'row-msg-count row-inline-badge',
    title: brainCount === 1 ? '1 brain' : `${brainCount} brains`,
  }, String(brainCount));
  return el('div', {
    class: `list-row ${isActive ? 'active' : ''}`,
    title: rowTooltip(l),
    onClick: () => setState({ activeLibraryId: l.id }),
  },
    libraryAvatarEl(l, { cacheBust: l.updated_at }),
    el('div', { class: 'row-text' },
      el('div', { class: 'row-title' },
        // Name + count badge live in a single flex group so the badge
        // sits immediately after the (possibly truncated) name rather
        // than getting pushed to the far right next to the favorite
        // star. The group itself can shrink (long names ellipsis); the
        // badge inside stays fixed-size.
        el('span', { class: 'row-title-name-group' },
          el('span', { class: 'row-title-text' }, l.name),
          countBadge,
        ),
        favoriteStar(l, async (next) => {
          // Route through the open editor's draft when this library's
          // detail pane is showing — keeps version_id in sync via
          // saveWithConflictHandling. See contacts.js for the full
          // rationale.
          if (_openDraft && _openDraftSave && _openDraft.id === l.id) {
            _openDraft.favorite = next;
            _openDraftSave();
            return;
          }
          try {
            const updated = await api.setLibraryFavorite(l.id, next);
            setState({
              libraries: (state.libraries || []).map(
                x => x.id === updated.id ? updated : x,
              ),
            });
          } catch (err) { toast(err.message, 'error'); }
        }),
      ),
      el('div', { class: 'row-sub' }, _librarySublineText(l)),
    ),
  );
}

function refreshList() {
  if (!_virt) return;
  const all = state.libraries || [];
  if (all.length === 0) {
    _visible = [];
    _emptyEl.style.display = '';
    _emptyMatchEl.style.display = 'none';
    _virt.setCount(0);
    return;
  }
  const ss = state.librarySort || { mode: 'added', direction: 'desc' };
  _visible = sortEntities(
    all.filter(_matchesSearch),
    ss.mode,
    _libraryUsageMap(state.chats),
    ss.direction,
    { favoritesFirst: !!ss.favoritesFirst },
  );
  _emptyEl.style.display = 'none';
  _emptyMatchEl.style.display = _visible.length === 0 ? '' : 'none';
  _virt.setCount(_visible.length, { preserveScroll: true });
}


let _refreshDetailToken = 0;

async function refreshDetail() {
  const detail = document.getElementById('library-detail');
  if (!detail) return;
  _lastDetailId = state.activeLibraryId;
  if (!state.activeLibraryId) {
    detail.replaceChildren();
    _openDraft = null;
    _openDraftSave = null;
    detail.append(el('div', { class: 'content-empty' },
      el('div', {},
        el('h3', {}, 'Pick a brain library'),
        el('div', {}, 'Select one on the left to edit it.'),
      ),
    ));
    return;
  }
  const oldScroll = detail.querySelector('.page-scroll');
  const prevScrollTop = oldScroll ? oldScroll.scrollTop : 0;
  // Fetch the FULL library — Summary rows omit brain bodies.
  // Token + activeId guards drop stale fetches from rapid row clicks.
  const token = ++_refreshDetailToken;
  const requestedId = state.activeLibraryId;
  let l;
  try {
    l = await api.getBrainLibrary(requestedId);
  } catch (err) {
    if (token !== _refreshDetailToken) return;
    toast(err.message, 'error');
    return;
  }
  if (token !== _refreshDetailToken || state.activeLibraryId !== requestedId) return;
  if (!l) { setState({ activeLibraryId: null }); return; }
  detail.replaceChildren();
  detail.append(renderEditView(l));
  const newScroll = detail.querySelector('.page-scroll');
  if (newScroll) newScroll.scrollTop = prevScrollTop;
}


function renderEditView(library) {
  let draft = JSON.parse(JSON.stringify(library));

  const dot = dirtyDot();
  const save = makeAutoSaver(async () => {
    markPending('library', draft.id, draft);
    await saveWithConflictHandling({
      draft,
      saveFn: (d) => api.updateBrainLibrary(d.id, d),
      getFn: () => api.getBrainLibrary(draft.id),
      entityLabel: 'Brain library',
      onReload: async (latest) => {
        Object.assign(draft, latest);
        setState({ libraries: await api.listBrainLibraries() });
        _lastDetailId = null;
        refreshDetail();
      },
    });
    clearPending('library', draft.id);
    setState({ libraries: await api.listBrainLibraries() });
  }, dot, 400);

  // Publish for the list-pane favourite-star handler. See contacts.js.
  _openDraft = draft;
  _openDraftSave = save;

  const header = el('div', { class: 'page-header' });
  function refreshHeader() {
    const items = [
      el('h2', {}, dot, draft.name),
      el('span', { class: 'uuid' }, draft.id),
      el('div', { class: 'spacer' }),
      el('button', { class: 'btn', onClick: async () => {
        const r = await api.exportLibrary(draft.id);
        await downloadFromResponse(r, `${draft.name}-library.json`);
      } }, icon('download', 14), 'Export'),
    ];
    if (draft.card_image) {
      items.push(el('button', {
        class: 'btn',
        title: 'Export as PNG card with embedded data',
        onClick: async () => {
          const r = await api.exportLibraryCard(draft.id);
          await downloadFromResponse(r, `${draft.name}-library.png`);
        },
      }, icon('download', 14), 'Export card'));
    }
    items.push(el('button', {
      class: 'btn',
      title: 'Create a copy of this brain library',
      onClick: async () => {
        const copy = await api.duplicateBrainLibrary(draft.id);
        setState({ libraries: await api.listBrainLibraries(), activeLibraryId: copy.id });
      },
    }, icon('copy', 14), 'Duplicate'));
    items.push(el('button', { class: 'btn danger', onClick: async () => {
      const ok = await confirmModal(
        'Delete brain library?',
        `${draft.name} will be removed. Chats that referenced it keep a "missing library" marker so re-importing reattaches automatically.`,
        { danger: true },
      );
      if (!ok) return;
      await api.deleteBrainLibrary(draft.id);
      setState({
        libraries: await api.listBrainLibraries(),
        activeLibraryId: null,
      });
    } }, icon('trash', 14), 'Delete'));
    header.replaceChildren(...items);
  }
  refreshHeader();

  const baseInfo = el('div', { class: 'section' },
    el('h3', {}, 'Information (UI only)'),
    el('div', { class: 'form-grid' },
      el('div', { class: 'form-group' },
        el('label', {}, 'Name'),
        el('input', { type: 'text', value: draft.name, oninput: e => {
          draft.name = e.target.value;
          save();
          header.querySelector('h2').replaceChildren(dot, draft.name);
        } }),
      ),
      el('div', { class: 'form-group' },
        el('label', {}, 'Tags (comma-separated)'),
        el('input', { type: 'text', value: draft.tags || '', oninput: e => {
          draft.tags = e.target.value; save();
        } }),
      ),
    ),
    el('div', { class: 'form-group', style: { marginTop: '18px' } },
      el('label', {}, 'Description'),
      (function() {
        const t = el('textarea', { rows: 4, oninput: e => {
          draft.description = e.target.value; save();
        } });
        t.value = draft.description || '';
        return t;
      })(),
    ),
    el('div', { class: 'form-grid-2', style: { marginBottom: '18px' } },
      el('div', { class: 'form-group' },
        el('label', {}, 'Author'),
        el('input', {
          type: 'text', value: draft.author || '',
          oninput: e => { draft.author = e.target.value; save(); },
        }),
      ),
    ),
  );

  const avatarSection = el('div', { class: 'section' },
    el('h3', {}, 'Avatar & card image'),
    el('div', { class: 'media-row' },
      mediaBlock('Avatar', renderLibraryAvatarBlock(draft)),
      mediaBlock('Card image', renderCardImageBlock(draft, 'library', () => refreshHeader())),
    ),
  );

  const brainsBadge = liveBadge(() => String((draft.brains || []).length));
  const brainsSection = el('div', { class: 'section' },
    el('h3', {}, 'Brains', brainsBadge),
    renderBrainsEditor(
      draft.brains || [],
      (newBrains) => { draft.brains = newBrains; save(); brainsBadge.refresh(); },
      {
        getBrainCatalog: () => brainCatalogEntries(draft.brains || [], draft.name || 'this library'),
        ownerName: () => draft.name || 'Library',
        ownerId: () => draft.id,
      },
    ),
  );

  // Chats that attach this library — surfaced as a quick navigator.
  // ``ref_library_id`` is the server-side membership filter on
  // ``chat.brain_library_ids`` (kept distinct from ``contact_id`` etc.
  // so it's clear the match is over a list rather than a scalar fk).
  const recentBox = el('div', { class: 'section' },
    el('h3', {}, 'Chats that use this library'),
    el('div', { style: { color: 'var(--text-mute)', fontSize: '13px' } }, 'Loading…'),
  );
  api.listChats({
    ref_library_id: draft.id,
    sort: 'updated_at', direction: 'desc', limit: 5,
  }).then((resp) => {
    const items = (resp && resp.items) || [];
    // ``replaceChildren`` takes nodes as varargs — must spread.
    const rows = items.length
      ? items.map(c => el('div', {
          class: 'list-row',
          onClick: () => setState({ activeTab: 'chats', activeChatId: c.id }),
        },
          el('div', { class: 'row-text' },
            el('div', { class: 'row-title' }, c.title || 'Untitled'),
            el('div', { class: 'row-sub' }, formatRelative(c.updated_at)),
          ),
        ))
      : [el('div', { style: { color: 'var(--text-mute)', fontSize: '13px' } }, 'No chats yet.')];
    recentBox.replaceChildren(
      el('h3', {}, 'Chats that use this library'),
      ...rows,
    );
  }).catch(() => {
    recentBox.replaceChildren(
      el('h3', {}, 'Chats that use this library'),
      el('div', { style: { color: 'var(--text-mute)', fontSize: '13px' } }, 'Could not load.'),
    );
  });

  return el('div', { style: { display: 'flex', flexDirection: 'column', height: '100%' } },
    header,
    el('div', { class: 'page-scroll' },
      el('div', { class: 'page-body' }, baseInfo, avatarSection, brainsSection, recentBox),
    ),
  );
}


/* ---------- avatar block (mirrors users.js#renderUserAvatarBlock) ---------- */

function renderLibraryAvatarBlock(draft) {
  const preview = el('div', {
    class: 'avatar',
    style: { width: '120px', height: '120px', borderRadius: 'var(--radius)' },
  });

  let inCrop = false;

  function refreshPreview() {
    preview.replaceChildren();
    if (draft.avatar) {
      const url = inCrop
        ? `/api/files/libraries/${draft.id}/avatar?v=${Date.now()}`
        : `/api/files/libraries/${draft.id}/avatar/display?v=${Date.now()}`;
      preview.append(el('img', { src: url, alt: draft.name }));
      applyCrop(preview, inCrop ? draft.avatar_crop : null);
    } else {
      preview.textContent = (draft.name || '?').slice(0, 1).toUpperCase();
      applyCrop(preview, null);
    }
  }
  refreshPreview();

  const fileInput = el('input', {
    type: 'file', accept: 'image/*', class: 'hidden', id: `library-avatar-input-${draft.id}`,
    onChange: async (e) => {
      const f = e.target.files[0];
      if (!f) return;
      try {
        const r = await api.uploadLibraryAvatar(draft.id, f);
        draft.avatar = r.avatar;
        draft.avatar_crop = null;
        if (r.version_id) draft.version_id = r.version_id;
        if (r.updated_at) draft.updated_at = r.updated_at;
        setState({ libraries: await api.listBrainLibraries() });
        refreshPreview();
        refreshButtons();
      } catch (err) { toast(`Upload failed: ${err.message}`, 'error'); }
      e.target.value = '';
    },
  });
  const uploadBtn = el('label', {
    for: `library-avatar-input-${draft.id}`,
    class: 'btn',
    style: { justifyContent: 'flex-start' },
  }, icon('upload', 14), draft.avatar ? 'Replace' : 'Upload');
  const cropBtn = el('button', {
    class: 'btn',
    title: 'Pick which square of the avatar to display',
    disabled: !draft.avatar,
    style: { justifyContent: 'flex-start' },
    onClick: () => {
      inCrop = true;
      refreshPreview();
      openCropModal({
        src: `/api/files/libraries/${draft.id}/avatar`,
        initial: draft.avatar_crop,
        title: `Crop avatar — ${draft.name}`,
        onChange: (crop) => applyCrop(preview, crop),
        onSave: async (crop) => {
          const next = (crop && (crop.x || crop.y || crop.w !== 1 || crop.h !== 1)) ? crop : null;
          draft.avatar_crop = next;
          try {
            const updated = await api.updateBrainLibrary(draft.id, draft);
            if (updated) Object.assign(draft, updated);
            setState({ libraries: await api.listBrainLibraries() });
          } catch (err) { toast(`Save failed: ${err.message}`, 'error'); }
        },
        onClose: () => { inCrop = false; refreshPreview(); },
      });
    },
  }, icon('crop', 14), 'Crop');
  const deleteBtn = el('button', {
    class: 'btn ghost danger',
    disabled: !draft.avatar,
    style: { justifyContent: 'flex-start' },
    onClick: async () => {
      try {
        const r = await api.deleteLibraryAvatar(draft.id);
        draft.avatar = null;
        draft.avatar_crop = null;
        if (r && r.version_id) draft.version_id = r.version_id;
        if (r && r.updated_at) draft.updated_at = r.updated_at;
        setState({ libraries: await api.listBrainLibraries() });
        refreshPreview();
        refreshButtons();
      } catch (err) { toast(err.message, 'error'); }
    },
  }, icon('trash', 14), 'Remove');

  function refreshButtons() {
    cropBtn.disabled = !draft.avatar;
    deleteBtn.disabled = !draft.avatar;
    uploadBtn.replaceChildren(icon('upload', 14), draft.avatar ? 'Replace' : 'Upload');
  }

  return el('div', { class: 'avatar-edit-row' },
    preview,
    fileInput,
    el('div', { style: { display: 'flex', flexDirection: 'column', gap: '6px', minWidth: '140px' } },
      uploadBtn, cropBtn, deleteBtn,
    ),
  );
}


/* ===========================================================================
 * Cascading-dropdown picker (shared by wizard + chat detail)
 * =========================================================================== */


const NONE_VALUE = '__none__';
const MISSING_PREFIX = '__missing__:';


/* Mount a cascading sequence of avatar pickers into ``container``.
 *
 * - ``selected`` is the current ordered list of library ids (may contain
 *   unknown ids that surface as "missing library" rows).
 * - ``onChange(nextSelected)`` is fired whenever the user adds, removes,
 *   or replaces an entry. The implementation re-renders the cascade in
 *   place after each change so callers get a fresh local mount without
 *   having to manage picker lifetimes.
 *
 * Each picker excludes ids selected by *earlier* pickers (cascade rule).
 * The trailing ``(none)`` picker is omitted when every library is already
 * picked. Unknown ids render as greyed-out "Unknown library" rows so the
 * user can reassign or detach them (a chat attached to a since-deleted
 * library lands here).
 */
export function mountLibraryCascade(container, { selected = [], onChange } = {}) {
  let current = (selected || []).slice();
  function emit() {
    if (onChange) onChange(current.slice());
  }
  function rerender() {
    container.replaceChildren();
    const allLibs = state.libraries || [];
    const byId = new Map(allLibs.map(l => [l.id, l]));

    function _opts(excludeIdxAfter) {
      // Build the option list for the picker at position ``excludeIdxAfter``.
      // Excludes any ids selected at *earlier* positions plus any selected at
      // *later* positions (so swapping a real entry doesn't accidentally land
      // on a value that's already picked further down).
      const blocked = new Set();
      current.forEach((id, i) => { if (i !== excludeIdxAfter && id) blocked.add(id); });
      // Sort: favourites first, then by name.
      const sorted = [...allLibs].sort((a, b) => {
        if (!!b.favorite - !!a.favorite) return (!!b.favorite ? 1 : 0) - (!!a.favorite ? 1 : 0);
        return (a.name || '').localeCompare(b.name || '', undefined, { sensitivity: 'base' });
      });
      const opts = [{ value: NONE_VALUE, label: '(none)' }];
      // Show a "missing" placeholder when the current slot holds an unknown id —
      // so the picker has something to render as its selected value.
      const here = current[excludeIdxAfter];
      if (here && !byId.has(here)) {
        opts.push({
          value: MISSING_PREFIX + here,
          label: `Unknown library (${here.slice(0, 8)}…)`,
        });
      }
      for (const lib of sorted) {
        if (blocked.has(lib.id)) continue;
        opts.push({
          value: lib.id,
          label: lib.name || '(unnamed)',
          favorite: !!lib.favorite,
          getAvatar: () => libraryAvatarEl(lib, { cacheBust: Math.floor(lib.updated_at || 0) }),
        });
      }
      return opts;
    }

    function buildPicker(idx) {
      const here = current[idx];
      const value = !here
        ? NONE_VALUE
        : (byId.has(here) ? here : MISSING_PREFIX + here);
      const picker = makeAvatarPicker({
        value,
        options: _opts(idx),
        onChange: (v) => {
          if (v === NONE_VALUE) {
            // Remove this slot (and let the trailing picker re-materialize).
            current = current.filter((_, i) => i !== idx);
          } else if (v && v.startsWith(MISSING_PREFIX)) {
            // No-op: the user re-selected the placeholder value of an
            // already-missing entry. Keep current as-is.
          } else {
            current = [...current.slice(0, idx), v, ...current.slice(idx + 1)];
          }
          emit();
          rerender();
        },
      });
      const row = el('div', { class: 'library-cascade-row' });
      if (here && !byId.has(here)) {
        row.classList.add('missing');
        row.title = (
          'This library is no longer available — its data was probably deleted. '
          + 'Re-importing the library reattaches automatically; setting this row '
          + 'to (none) detaches it.'
        );
      }
      row.append(picker);
      return row;
    }

    for (let i = 0; i < current.length; i++) {
      container.append(buildPicker(i));
    }
    // Trailing "(none)" picker unless every known library is already picked.
    const usedKnownCount = current.filter(id => byId.has(id)).length;
    if (usedKnownCount < (state.libraries || []).length) {
      container.append(buildPicker(current.length));
    }
  }
  rerender();
  return {
    setSelected: (next) => { current = (next || []).slice(); rerender(); },
    getSelected: () => current.slice(),
  };
}


/* ===========================================================================
 * Subscription
 * =========================================================================== */


/* Module-level handle on the currently-open editor's draft + autosave
 * trigger. See ``static/views/contacts.js`` for the rationale. */
let _openDraft = null;
let _openDraftSave = null;

let _subscribed = false;
let _lastDetailId = null;
export function setupBrainLibrariesTab() {
  if (_subscribed) return;
  _subscribed = true;
  subscribe(() => {
    refreshList();
    if (state.activeLibraryId !== _lastDetailId) refreshDetail();
  });
}
