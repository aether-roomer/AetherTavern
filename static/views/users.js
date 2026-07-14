/* Users (personas) tab: list + edit. */

import { api } from '../api.js';
import { state, setState, subscribe } from '../state.js';
import { el, formatRelative, downloadFromResponse, applyCrop, suggestionInput, sortEntities, sortControls, lastUsedByEntity, dirtyDot, makeAutoSaver, favoriteStar, liveBadge, userAvatarEl, placeholderAvatar } from '../util.js';
import { icon, openModal, confirmModal, toast } from '../ui.js';
import { renderBrainsEditor, renderCardImageBlock, mediaBlock } from './contacts.js';
import { renderEntityTTSSection } from './entity_tts_section.js';
import { brainCatalogEntries } from './brain_row.js';
import { saveWithConflictHandling } from '../conflict.js';
import { markPending, clearPending } from '../save_queue.js';
import { openNewChatWizard } from './new_chat_wizard.js';
import { runImport } from './import_progress.js';
import { openCropModal } from './crop_modal.js';
import {
  GENDER_SUGGESTIONS, GENDER_PLACEHOLDER,
  PRONOUN_SUGGESTIONS, PRONOUN_PLACEHOLDER,
  SPECIES_PLACEHOLDER,
} from '../constants.js';
import { createVirtList } from '../virt_list.js';


export function renderUsersTab(container) {
  container.replaceChildren();
  const pane = el('div', { class: 'list-pane' });
  pane.append(renderListHeader());
  const body = el('div', { class: 'list-body', id: 'user-list-body' });
  pane.append(body);
  container.append(pane);
  const detail = el('div', { class: 'content-pane', id: 'user-detail' });
  container.append(detail);
  mountList(body);
  refreshDetail();
}


let _searchTerm = '';

function renderListHeader() {
  const search = el('input', {
    class: 'list-search',
    type: 'text',
    placeholder: 'Search personas…',
    value: _searchTerm,
    oninput: (e) => { _searchTerm = e.target.value.toLowerCase(); refreshList(); },
  });
  const sort = sortControls(state.userSort || { mode: 'added', direction: 'desc' }, (v) => {
    setState({ userSort: v });
    try { localStorage.setItem('userSort', JSON.stringify(v)); } catch {}
    refreshList();
  });
  return el('div', { class: 'list-header' },
    el('div', { style: { display: 'flex', justifyContent: 'space-between', alignItems: 'center' } },
      el('h2', {}, 'User personas'),
      el('div', { style: { display: 'flex', gap: '6px' } },
        el('label', { class: 'btn', for: 'user-import-input', title: 'Import persona', style: { cursor: 'pointer' } }, icon('upload', 14)),
        el('input', {
          id: 'user-import-input', type: 'file',
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
          title: 'New persona',
          onClick: async () => {
            try {
              const u = await api.createUser({ name: 'New persona' });
              setState({ users: await api.listUsers(), activeUserId: u.id });
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


function rowTooltip(u) {
  const desc = (u.description || '').trim();
  const tags = (u.tags || '').trim();
  if (desc && tags) return `${desc}\nTags: ${tags}`;
  return desc || tags || '';
}


function _matchesSearch(u) {
  if (!_searchTerm) return true;
  const hay = `${u.name || ''} ${u.tags || ''} ${u.description || ''}`.toLowerCase();
  return hay.includes(_searchTerm);
}


let _virt = null;
let _visible = [];
let _emptyEl = null;
let _emptyMatchEl = null;

function mountList(body) {
  _emptyEl = el('div', { class: 'list-empty', style: { display: 'none' } },
    'No personas yet.');
  _emptyMatchEl = el('div', { class: 'list-empty', style: { display: 'none' } },
    'No personas match your search.');
  _virt = createVirtList(body, {
    count: 0,
    getItem: (i) => _visible[i] || null,
    renderItem: (u) => renderRow(u),
    estimatedHeight: 60,
    observeRows: false,
  });
  body.appendChild(_emptyEl);
  body.appendChild(_emptyMatchEl);
  refreshList();
}

function renderRow(u) {
  const isActive = state.activeUserId === u.id;
  return el('div', {
    class: `list-row ${isActive ? 'active' : ''}`,
    title: rowTooltip(u),
    onClick: () => setState({ activeUserId: u.id }),
  },
    userAvatarEl(u, { cacheBust: u.updated_at }) || placeholderAvatar(u.name),
    el('div', { class: 'row-text' },
      el('div', { class: 'row-title' },
        el('span', { class: 'row-title-text' }, u.name),
        favoriteStar(u, async (next) => {
          // Route through the open editor's draft when this persona's
          // detail pane is showing — keeps version_id in sync via
          // saveWithConflictHandling. See contacts.js for the full
          // rationale.
          if (_openDraft && _openDraftSave && _openDraft.id === u.id) {
            _openDraft.favorite = next;
            _openDraftSave();
            return;
          }
          try {
            const updated = await api.setUserFavorite(u.id, next);
            setState({
              users: (state.users || []).map(
                x => x.id === updated.id ? updated : x,
              ),
            });
          } catch (err) { toast(err.message, 'error'); }
        }),
      ),
      el('div', { class: 'row-sub' }, u.description || u.tags || ''),
    ),
  );
}

function refreshList() {
  if (!_virt) return;
  const all = state.users || [];
  if (all.length === 0) {
    _visible = [];
    _emptyEl.style.display = '';
    _emptyMatchEl.style.display = 'none';
    _virt.setCount(0);
    return;
  }
  const us = state.userSort || { mode: 'added', direction: 'desc' };
  _visible = sortEntities(
    all.filter(_matchesSearch),
    us.mode,
    lastUsedByEntity(state.chats, 'user_id'),
    us.direction,
    { favoritesFirst: !!us.favoritesFirst },
  );
  _emptyEl.style.display = 'none';
  _emptyMatchEl.style.display = _visible.length === 0 ? '' : 'none';
  _virt.setCount(_visible.length, { preserveScroll: true });
}


let _refreshDetailToken = 0;

async function refreshDetail() {
  const detail = document.getElementById('user-detail');
  if (!detail) return;
  _lastDetailId = state.activeUserId;
  if (!state.activeUserId) {
    detail.replaceChildren();
    _openDraft = null;
    _openDraftSave = null;
    detail.append(el('div', { class: 'content-empty' },
      el('div', {}, el('h3', {}, 'Pick a persona'), el('div', {}, 'Select someone on the left to edit.')),
    ));
    return;
  }
  const oldScroll = detail.querySelector('.page-scroll');
  const prevScrollTop = oldScroll ? oldScroll.scrollTop : 0;
  // Fetch the FULL persona — Summary rows omit persona/brains/etc.
  // Token + activeId guards drop stale fetches from rapid row clicks.
  const token = ++_refreshDetailToken;
  const requestedId = state.activeUserId;
  let user;
  try {
    user = await api.getUser(requestedId);
  } catch (err) {
    if (token !== _refreshDetailToken) return;
    toast(err.message, 'error');
    return;
  }
  if (token !== _refreshDetailToken || state.activeUserId !== requestedId) return;
  if (!user) { setState({ activeUserId: null }); return; }
  detail.replaceChildren();
  detail.append(renderEditView(user));
  const newScroll = detail.querySelector('.page-scroll');
  if (newScroll) newScroll.scrollTop = prevScrollTop;
}


function renderEditView(user) {
  let draft = JSON.parse(JSON.stringify(user));

  const dot = dirtyDot();
  const save = makeAutoSaver(async () => {
    markPending('user', draft.id, draft);
    await saveWithConflictHandling({
      draft,
      saveFn: (d) => api.updateUser(d.id, d),
      getFn: () => api.getUser(draft.id),
      entityLabel: 'Persona',
      onReload: async (latest) => {
        Object.assign(draft, latest);
        setState({ users: await api.listUsers() });
        _lastDetailId = null;
        refreshDetail();
      },
    });
    clearPending('user', draft.id);
    setState({ users: await api.listUsers() });
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
      el('button', { class: 'btn primary', onClick: () => openNewChatWizard({ userId: draft.id }) }, icon('plus', 14), 'New chat'),
      el('button', { class: 'btn', onClick: async () => {
        const r = await api.exportUser(draft.id);
        await downloadFromResponse(r, `${draft.name}-persona.json`);
      } }, icon('download', 14), 'Export'),
    ];
    if (draft.card_image) {
      items.push(el('button', {
        class: 'btn',
        title: 'Export as PNG card with embedded data',
        onClick: async () => {
          const r = await api.exportUserCard(draft.id);
          await downloadFromResponse(r, `${draft.name}-persona.png`);
        },
      }, icon('download', 14), 'Export card'));
    }
    items.push(el('button', {
      class: 'btn',
      title: 'Create a copy of this persona',
      onClick: async () => {
        const copy = await api.duplicateUser(draft.id);
        setState({ users: await api.listUsers(), activeUserId: copy.id });
      },
    }, icon('copy', 14), 'Duplicate'));
    items.push(el('button', { class: 'btn danger', onClick: async () => {
      const ok = await confirmModal('Delete persona?', `${draft.name} will be removed permanently.`, { danger: true });
      if (!ok) return;
      await api.deleteUser(draft.id);
      setState({ users: await api.listUsers(), activeUserId: null });
    } }, icon('trash', 14), 'Delete'));
    header.replaceChildren(...items);
  }
  refreshHeader();

  function field(label, key, opts = {}) {
    const tag = opts.multi ? 'textarea' : 'input';
    const node = el(tag, { type: 'text', rows: opts.rows || 3, oninput: e => { draft[key] = e.target.value; save(); } });
    node.value = draft[key] || '';
    return el('div', { class: 'form-group' }, el('label', {}, label), node);
  }

  const descriptionTa = el('textarea', {
    rows: 3,
    oninput: e => { draft.description = e.target.value; save(); },
  });
  descriptionTa.value = draft.description || '';

  const baseInfo = el('div', { class: 'section' },
    el('h3', {}, 'Identity'),
    el('div', { class: 'form-grid-3' },
      el('div', { class: 'form-group' },
        el('label', {}, 'Name'),
        el('input', { type: 'text', value: draft.name, oninput: e => { draft.name = e.target.value; save(); header.querySelector('h2').replaceChildren(dot, draft.name); } }),
      ),
    ),
    el('div', { class: 'form-group', style: { marginTop: '18px' } },
      el('label', {}, 'Description (UI only)'),
      descriptionTa,
    ),
    el('div', { class: 'form-grid-3', style: { marginTop: '18px', marginBottom: '18px' } },
      el('div', { class: 'form-group' },
        el('label', {}, 'Author'),
        el('input', {
          type: 'text', value: draft.author || '',
          oninput: e => { draft.author = e.target.value; save(); },
        }),
      ),
    ),
    el('div', { class: 'form-grid-3' },
      el('div', { class: 'form-group' },
        el('label', {}, 'Gender'),
        suggestionInput(draft.gender, `gender-suggestions-${draft.id}`,
          GENDER_SUGGESTIONS, v => { draft.gender = v; save(); },
          { placeholder: GENDER_PLACEHOLDER }),
      ),
      el('div', { class: 'form-group' },
        el('label', {}, 'Pronouns'),
        suggestionInput(draft.pronouns, `pronoun-suggestions-${draft.id}`,
          PRONOUN_SUGGESTIONS, v => { draft.pronouns = v; save(); },
          { placeholder: PRONOUN_PLACEHOLDER }),
      ),
      el('div', { class: 'form-group' },
        el('label', {}, 'Species'),
        el('input', {
          type: 'text', value: draft.species || '', placeholder: SPECIES_PLACEHOLDER,
          oninput: e => { draft.species = e.target.value; save(); },
        }),
      ),
    ),
    el('div', { class: 'form-group', style: { marginTop: '18px' } },
      el('label', {}, 'Tags'),
      el('input', { type: 'text', value: draft.tags || '', oninput: e => { draft.tags = e.target.value; save(); } }),
    ),
    field('Personality', 'persona', { multi: true, rows: 5 }),
    field('Appearance', 'appearance', { multi: true, rows: 4 }),
    el('div', { class: 'form-group checkbox' },
      el('input', { type: 'checkbox', checked: !!draft.cjk, id: 'cjk-' + draft.id, onChange: e => { draft.cjk = e.target.checked; save(); } }),
      el('label', { for: 'cjk-' + draft.id }, 'Japanese'),
    ),
  );

  const avatarSection = el('div', { class: 'section' },
    el('h3', {}, 'Avatar & card image'),
    el('div', { class: 'media-row' },
      mediaBlock('Avatar', renderUserAvatarBlock(draft)),
      mediaBlock('Card image', renderCardImageBlock(draft, 'user', () => refreshHeader())),
    ),
  );

  const brainsBadge = liveBadge(() => String((draft.brains || []).length));
  const brainsSection = el('div', { class: 'section' },
    el('h3', {}, 'Brains', brainsBadge),
    renderBrainsEditor(
      draft.brains || [],
      (newBrains) => { draft.brains = newBrains; save(); brainsBadge.refresh(); },
      {
        getBrainCatalog: () => brainCatalogEntries(draft.brains || [], draft.name || 'this user'),
        ownerName: () => draft.name || 'Persona',
        ownerId: () => draft.id,
      },
    ),
  );

  const recentBox = el('div', { class: 'section' },
    el('h3', {}, 'Recent chats with this persona'),
    el('div', { style: { color: 'var(--text-mute)', fontSize: '13px' } }, 'Loading…'),
  );
  api.listChats({
    user_id: draft.id,
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
      el('h3', {}, 'Recent chats with this persona'),
      ...rows,
    );
  }).catch(() => {
    recentBox.replaceChildren(
      el('h3', {}, 'Recent chats with this persona'),
      el('div', { style: { color: 'var(--text-mute)', fontSize: '13px' } }, 'Could not load.'),
    );
  });

  const ttsSection = renderEntityTTSSection(draft, save, {
    entityKind: 'user',
    settings: state.settings || {},
    onTabSwitch: () => setState({ activeTab: 'settings' }),
  });

  const body = el('div', { class: 'page-scroll' },
    el('div', { class: 'page-body' }, baseInfo, avatarSection, brainsSection, ttsSection, recentBox),
  );

  return el('div', { style: { display: 'flex', flexDirection: 'column', height: '100%' } }, header, body);
}


function renderUserAvatarBlock(draft) {
  const preview = el('div', {
    class: 'avatar',
    style: { width: '120px', height: '120px', borderRadius: 'var(--radius)' },
  });

  // While the crop modal is open, swap to the uncropped original + CSS
  // crop so dragging gives a live preview. Default state uses the
  // pre-cropped /display image.
  let inCrop = false;

  function refreshPreview() {
    preview.replaceChildren();
    if (draft.avatar) {
      const url = inCrop
        ? `/api/files/users/${draft.id}/avatar?v=${Date.now()}`
        : `/api/files/users/${draft.id}/avatar/display?v=${Date.now()}`;
      preview.append(el('img', { src: url, alt: draft.name }));
      applyCrop(preview, inCrop ? draft.avatar_crop : null);
    } else {
      preview.textContent = (draft.name || '?').slice(0, 1).toUpperCase();
      applyCrop(preview, null);
    }
  }
  refreshPreview();

  const fileInput = el('input', {
    type: 'file', accept: 'image/*', class: 'hidden', id: `user-avatar-input-${draft.id}`,
    onChange: async (e) => {
      const f = e.target.files[0];
      if (!f) return;
      try {
        const r = await api.uploadUserAvatar(draft.id, f);
        draft.avatar = r.avatar;
        draft.avatar_crop = null;  // server clears crop on new upload
        if (r.version_id) draft.version_id = r.version_id;
        if (r.updated_at) draft.updated_at = r.updated_at;
        setState({ users: await api.listUsers() });
        refreshPreview();
        refreshButtons();
      } catch (err) { toast(`Upload failed: ${err.message}`, 'error'); }
      e.target.value = '';
    },
  });
  const uploadBtn = el('label', {
    for: `user-avatar-input-${draft.id}`,
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
        src: `/api/files/users/${draft.id}/avatar`,
        initial: draft.avatar_crop,
        title: `Crop avatar — ${draft.name}`,
        onChange: (crop) => applyCrop(preview, crop),
        onSave: async (crop) => {
          const next = (crop && (crop.x || crop.y || crop.w !== 1 || crop.h !== 1)) ? crop : null;
          draft.avatar_crop = next;
          try {
            const updated = await api.updateUser(draft.id, draft);
            if (updated) Object.assign(draft, updated);
            setState({ users: await api.listUsers() });
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
        const r = await api.deleteUserAvatar(draft.id);
        draft.avatar = null;
        draft.avatar_crop = null;
        if (r && r.version_id) draft.version_id = r.version_id;
        if (r && r.updated_at) draft.updated_at = r.updated_at;
        setState({ users: await api.listUsers() });
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


/* Module-level handle on the currently-open editor's draft + autosave
 * trigger. See ``static/views/contacts.js`` for the rationale — keeps a
 * list-pane favourite-star toggle from leaking a server-side version_id
 * bump past the open editor's draft. */
let _openDraft = null;
let _openDraftSave = null;

let _subscribed = false;
let _lastDetailId = null;
export function setupUsersTab() {
  if (_subscribed) return;
  _subscribed = true;
  subscribe(() => {
    refreshList();
    if (state.activeUserId !== _lastDetailId) refreshDetail();
  });
}
