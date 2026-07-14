/* Contacts tab: list pane + edit view (avatar, emotions, example chats, brains). */

import { api, startContactDeletionResponseStream } from '../api.js';
import { state, setState, subscribe } from '../state.js';
import { el, formatRelative, downloadFromResponse, shortId, avatarEl, applyCrop, suggestionInput, sortEntities, sortControls, lastUsedByEntity, dirtyDot, makeAutoSaver, favoriteStar, liveBadge, downloadJson, brainsToExportJson } from '../util.js';
import { makeSelect as pickerSelect } from '../avatar_picker.js';
import { icon, openModal, closeModal, confirmModal, toast } from '../ui.js';
import { saveWithConflictHandling } from '../conflict.js';
import { markPending, clearPending } from '../save_queue.js';
import {
  EMOTIONS, INTIMACIES, STYLES, RESPONSE_LENGTHS,
  GENDER_SUGGESTIONS, GENDER_PLACEHOLDER,
  PRONOUN_SUGGESTIONS, PRONOUN_PLACEHOLDER,
  SPECIES_PLACEHOLDER,
} from '../constants.js';
import { openNewChatWizard } from './new_chat_wizard.js';
import { openCropModal } from './crop_modal.js';
import { importWithProgress, runImport } from './import_progress.js';
import { renderBubbleRow } from './chat.js';
import { renderBrainRow, ensureBrainShape, brainCatalogEntries } from './brain_row.js';
import { renderReminderBrainEditor } from './reminder_brain_editor.js';
import { renderEntityTTSSection } from './entity_tts_section.js';
import { createVirtList } from '../virt_list.js';


export function renderContactsTab(container) {
  container.replaceChildren();

  const pane = el('div', { class: 'list-pane' });
  pane.append(renderListHeader());
  const body = el('div', { class: 'list-body', id: 'contact-list-body' });
  pane.append(body);
  container.append(pane);

  const detail = el('div', { class: 'content-pane', id: 'contact-detail' });
  container.append(detail);

  mountList(body);
  refreshDetail();
}


let _searchTerm = '';

function renderListHeader() {
  const search = el('input', {
    class: 'list-search',
    type: 'text',
    placeholder: 'Search contacts…',
    value: _searchTerm,
    oninput: (e) => { _searchTerm = e.target.value.toLowerCase(); refreshList(); },
  });
  const sort = sortControls(state.contactSort || { mode: 'added', direction: 'desc' }, (v) => {
    setState({ contactSort: v });
    try { localStorage.setItem('contactSort', JSON.stringify(v)); } catch {}
    refreshList();
  });
  return el('div', { class: 'list-header' },
    el('div', { style: { display: 'flex', justifyContent: 'space-between', alignItems: 'center' } },
      el('h2', {}, 'Contacts'),
      el('div', { style: { display: 'flex', gap: '6px' } },
        el('label', {
          class: 'btn',
          for: 'contact-import-input',
          title: 'Import contact',
          style: { cursor: 'pointer' }
        }, icon('upload', 14)),
        el('input', {
          id: 'contact-import-input', type: 'file',
          accept: '.json,.png,.zip,application/json,image/png,application/zip',
          class: 'hidden',
          onChange: async (e) => {
            const file = e.target.files[0];
            if (!file) return;
            const isZip = file.name.toLowerCase().endsWith('.zip');
            await runImport(file, { title: isZip ? 'Bulk import' : 'Import' });
            e.target.value = '';
          }
        }),
        el('button', {
          class: 'btn primary',
          title: 'New contact',
          onClick: async () => {
            try {
              const c = await api.createContact({ name: 'New contact' });
              setState({ contacts: await api.listContacts(), activeContactId: c.id });
            } catch (e) { toast(e.message, 'error'); }
          },
        }, icon('plus', 14)),
      ),
    ),
    el('div', { style: { display: 'flex', gap: '6px', alignItems: 'center' } },
      search,
      sort,
    ),
  );
}


function rowTooltip(c) {
  const desc = (c.description || '').trim();
  const tags = (c.tags || '').trim();
  if (desc && tags) return `${desc}\nTags: ${tags}`;
  return desc || tags || '';
}


function _matchesSearch(c) {
  if (!_searchTerm) return true;
  const hay = `${c.name || ''} ${c.tags || ''} ${c.description || ''}`.toLowerCase();
  return hay.includes(_searchTerm);
}


// Virtualization state: ``_visible`` is the filtered + sorted slice
// driving the virtualizer. Kept in sync by ``refreshList`` whenever the
// underlying data, search, or sort prefs change. Per-row ResizeObservers
// are disabled — contact rows are fixed-shape.
let _virt = null;
let _visible = [];
let _emptyEl = null;
let _emptyMatchEl = null;

function mountList(body) {
  _emptyEl = el('div', { class: 'list-empty', style: { display: 'none' } },
    'No contacts yet. Click ', el('strong', {}, 'New'), ' or ',
    el('strong', {}, 'Import'), ' to get started.');
  _emptyMatchEl = el('div', { class: 'list-empty', style: { display: 'none' } },
    'No contacts match your search.');

  _virt = createVirtList(body, {
    count: 0,
    getItem: (i) => _visible[i] || null,
    renderItem: (c) => renderRow(c),
    estimatedHeight: 60,
    observeRows: false,
  });
  body.appendChild(_emptyEl);
  body.appendChild(_emptyMatchEl);
  refreshList();
}

function renderRow(c) {
  const isActive = state.activeContactId === c.id;
  return el('div', {
    class: `list-row ${isActive ? 'active' : ''}`,
    title: rowTooltip(c),
    onClick: () => setState({ activeContactId: c.id }),
  },
    avatarEl(c, { cacheBust: c.updated_at }),
    el('div', { class: 'row-text' },
      el('div', { class: 'row-title' },
        el('span', { class: 'row-title-text' }, c.name),
        favoriteStar(c, async (next) => {
          // Route through the open editor's draft when this contact's
          // detail pane is showing — its autosave keeps version_id in
          // sync via saveWithConflictHandling. Side-channel PUTs would
          // bump the server but leave the draft stale, so the next
          // autosave (potentially many minutes later) 409s.
          if (_openDraft && _openDraftSave && _openDraft.id === c.id) {
            _openDraft.favorite = next;
            _openDraftSave();
            return;
          }
          try {
            const updated = await api.setContactFavorite(c.id, next);
            setState({
              contacts: (state.contacts || []).map(
                x => x.id === updated.id ? updated : x,
              ),
            });
          } catch (err) { toast(err.message, 'error'); }
        }),
      ),
      el('div', { class: 'row-sub' }, c.description || c.tags || ''),
    ),
  );
}

function refreshList() {
  if (!_virt) return;
  const all = state.contacts || [];
  if (all.length === 0) {
    _visible = [];
    _emptyEl.style.display = '';
    _emptyMatchEl.style.display = 'none';
    _virt.setCount(0);
    return;
  }
  const cs = state.contactSort || { mode: 'added', direction: 'desc' };
  _visible = sortEntities(
    all.filter(_matchesSearch),
    cs.mode,
    lastUsedByEntity(state.chats, 'contact_id'),
    cs.direction,
    { favoritesFirst: !!cs.favoritesFirst },
  );
  _emptyEl.style.display = 'none';
  _emptyMatchEl.style.display = _visible.length === 0 ? '' : 'none';
  // preserveScroll because subscribe fires on every state change — auto-
  // saves, active-id flips, etc. shouldn't snap the list back to the top.
  _virt.setCount(_visible.length, { preserveScroll: true });
}


let _refreshDetailToken = 0;

async function refreshDetail() {
  const detail = document.getElementById('contact-detail');
  if (!detail) return;
  _lastDetailId = state.activeContactId;
  if (!state.activeContactId) {
    detail.replaceChildren();
    _openDraft = null;
    _openDraftSave = null;
    detail.append(el('div', { class: 'content-empty' },
      el('div', {},
        el('h3', {}, 'Pick a contact'),
        el('div', {}, 'Select someone on the left to edit them.'),
      ),
    ));
    return;
  }
  // Preserve scroll position across the wholesale re-render.
  const oldScroll = detail.querySelector('.page-scroll');
  const prevScrollTop = oldScroll ? oldScroll.scrollTop : 0;
  // Fetch the FULL contact (not the Summary in state.contacts) — the
  // editor needs every persona / brain / scenario / TTS field, and an
  // editor draft must never start from a Summary or it'd save back as
  // a partial object. Token + activeId checks drop responses from
  // rapidly-clicked stale fetches.
  const token = ++_refreshDetailToken;
  const requestedId = state.activeContactId;
  let contact;
  try {
    contact = await api.getContact(requestedId);
  } catch (err) {
    if (token !== _refreshDetailToken) return;
    toast(err.message, 'error');
    return;
  }
  if (token !== _refreshDetailToken || state.activeContactId !== requestedId) return;
  if (!contact) { setState({ activeContactId: null }); return; }
  detail.replaceChildren();
  detail.append(renderEditView(contact));
  const newScroll = detail.querySelector('.page-scroll');
  if (newScroll) newScroll.scrollTop = prevScrollTop;
}


function renderEditView(contact) {
  // Always work on a draft copy; persist on blur/explicit save.
  let draft = JSON.parse(JSON.stringify(contact));

  const dot = dirtyDot();
  const save = makeAutoSaver(async () => {
    // Persist the in-flight payload to localStorage *before* hitting the
    // network. If the user reloads / closes the tab between this line
    // and the API success, ``drainSaveQueue`` on the next boot will
    // flush it (or pop the conflict modal if the entity changed
    // elsewhere in the meantime).
    markPending('contact', draft.id, draft);
    await saveWithConflictHandling({
      draft,
      saveFn: (d) => api.updateContact(d.id, d),
      getFn: () => api.getContact(draft.id),
      entityLabel: 'Contact',
      onReload: async (latest) => {
        // Replace the draft contents in place + force a detail re-render
        // so the open inputs reflect the reloaded values. Reload is an
        // explicit "discard local edits" choice, so losing input focus
        // is the right UX (vs. the autosave path which preserves it).
        Object.assign(draft, latest);
        setState({ contacts: await api.listContacts() });
        _lastDetailId = null;
        refreshDetail();
      },
    });
    clearPending('contact', draft.id);
    setState({ contacts: await api.listContacts() });
    // Don't refreshDetail — would lose focus. Caller updates state which triggers list refresh only.
  }, dot, 500);

  // Publish for the list-pane favourite-star handler so a toggle on the
  // open contact routes through this draft (keeping version_id in sync
  // via saveWithConflictHandling) instead of a side-channel PUT.
  _openDraft = draft;
  _openDraftSave = save;

  function field(label, key, opts = {}) {
    const tag = opts.multi ? 'textarea' : 'input';
    const node = el(tag, {
      type: 'text',
      rows: opts.rows || 3,
      oninput: e => { draft[key] = e.target.value; save(); },
    });
    node.value = draft[key] || '';
    return el('div', { class: 'form-group' }, el('label', {}, label), node);
  }

  const header = el('div', { class: 'page-header' });
  function refreshHeader() {
    const titleH2 = el('h2', {}, dot, draft.name);
    const items = [
      titleH2,
      el('span', { class: 'uuid' }, draft.id),
      el('div', { class: 'spacer' }),
      el('button', {
        class: 'btn primary',
        title: `Start a new chat with ${draft.name}`,
        onClick: () => openNewChatWizard({ contactId: draft.id }),
      }, icon('plus', 14), 'New chat'),
      el('button', {
        class: 'btn',
        onClick: async () => {
          const r = await api.exportContact(draft.id);
          await downloadFromResponse(r, `${draft.name}.json`);
        },
      }, icon('download', 14), 'Export'),
    ];
    if (draft.card_image) {
      items.push(el('button', {
        class: 'btn',
        title: 'Export as PNG card with embedded data',
        onClick: async () => {
          const r = await api.exportContactCard(draft.id);
          await downloadFromResponse(r, `${draft.name}.png`);
        },
      }, icon('download', 14), 'Export card'));
    }
    items.push(el('button', {
      class: 'btn',
      title: 'Create a copy of this contact',
      onClick: async () => {
        const copy = await api.duplicateContact(draft.id);
        setState({ contacts: await api.listContacts(), activeContactId: copy.id });
      },
    }, icon('copy', 14), 'Duplicate'));
    items.push(el('button', {
      class: 'btn danger',
      onClick: async () => {
        const ok = await confirmDeleteContact(draft);
        if (!ok) return;
        await api.deleteContact(draft.id);
        setState({ contacts: await api.listContacts(), activeContactId: null });
      },
    }, icon('trash', 14), 'Delete'));
    header.replaceChildren(...items);
  }
  refreshHeader();

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
      el('input', { type: 'text', value: draft.tags || '', placeholder: 'comma, separated, list', oninput: e => { draft.tags = e.target.value; save(); } }),
    ),
    field('Personality', 'persona', { multi: true, rows: 5 }),
    field('Appearance', 'appearance', { multi: true, rows: 4 }),
    el('div', { class: 'form-group checkbox' },
      el('input', { type: 'checkbox', checked: !!draft.cjk, id: 'cjk-' + draft.id, onChange: e => { draft.cjk = e.target.checked; save(); } }),
      el('label', { for: 'cjk-' + draft.id }, 'Japanese'),
    ),
  );

  const greetingSection = el('div', { class: 'section' },
    el('h3', {}, 'Greeting'),
    field('First message (auto-inserted on chat start)', 'greeting', { multi: true, rows: 3 }),
    el('div', { class: 'form-group' },
      el('label', {}, 'Greeting emotion'),
      makeEmotionSelect(draft.greeting_emotion, val => { draft.greeting_emotion = val; save(); }),
    ),
  );

  const defaultsSection = el('div', { class: 'section' },
    el('h3', {}, 'Defaults'),
    el('div', { class: 'form-grid-3' },
      el('div', { class: 'form-group' },
        el('label', {}, 'Default intimacy'),
        makeSelect(INTIMACIES, draft.default_intimacy, v => { draft.default_intimacy = v; save(); }),
      ),
      el('div', { class: 'form-group' },
        el('label', {}, 'Default style'),
        makeSelect(STYLES, draft.default_style, v => { draft.default_style = v; save(); }),
      ),
      el('div', { class: 'form-group' },
        el('label', {}, 'Default response length'),
        makeOptionalSelect(
          RESPONSE_LENGTHS.filter(x => x !== ''),
          draft.default_response_length,
          v => { draft.default_response_length = v; save(); },
          { emptyLabel: '(any)' },
        ),
      ),
    ),
  );

  const scenariosBadge = liveBadge(() => String((draft.scenarios || []).length));
  const emotionsBadge = liveBadge(() => `${Object.keys(draft.emotions || {}).length} / 24`);
  const exampleBadge = liveBadge(() => String((draft.example_chats || []).length));
  const brainsBadge = liveBadge(() => String((draft.brains || []).length));

  const scenariosSection = el('div', { class: 'section' },
    el('h3', {}, 'Scenarios', scenariosBadge),
    el('div', { style: { fontSize: '12px', color: 'var(--text-mute)', marginTop: '-6px', marginBottom: '8px' } },
      `Character-specific scenarios — pickable when starting a chat with ${draft.name}. ` +
      `A non-empty greeting overrides ${draft.name}'s default greeting.`,
    ),
    renderContactScenariosEditor(draft, () => { save(); scenariosBadge.refresh(); }),
  );

  const avatarSection = el('div', { class: 'section' },
    el('h3', {}, 'Avatar & card image'),
    el('div', { class: 'media-row' },
      mediaBlock('Avatar', renderAvatarBlock(draft)),
      mediaBlock('Card image', renderCardImageBlock(draft, 'contact', () => {
        setState({ contacts: state.contacts });
        refreshHeader();
      })),
    ),
  );

  const reminderSection = el('div', { class: 'section' },
    el('h3', {}, 'Reminder brain'),
    renderReminderBrainEditor(draft.reminder_brain, (next) => {
      draft.reminder_brain = next;
      save();
    }),
  );

  const emotionsSection = el('div', { class: 'section' },
    el('h3', {}, 'Emotion sprites', emotionsBadge),
    renderEmotionsGrid(draft, () => emotionsBadge.refresh()),
  );

  const exampleSection = el('div', { class: 'section' },
    el('h3', {}, 'Example chats', exampleBadge),
    renderExampleChatsEditor(draft, () => { save(); exampleBadge.refresh(); }),
  );

  const brainsSection = el('div', { class: 'section' },
    el('h3', {}, 'Brains', brainsBadge),
    renderBrainsEditor(
      draft.brains || [],
      (newBrains) => { draft.brains = newBrains; save(); brainsBadge.refresh(); },
      {
        getBrainCatalog: () => _contactBrainCatalog(draft),
        ownerName: () => draft.name || 'Contact',
        ownerId: () => draft.id,
      },
    ),
  );

  const recentBox = el('div', { class: 'section' },
    el('h3', {}, 'Recent chats with ' + draft.name),
    el('div', { style: { color: 'var(--text-mute)', fontSize: '13px' } }, 'Loading…'),
  );
  // Server-side filter — works regardless of which page is loaded in
  // ``state.chats`` (chats are paged). 5 most recent by updated_at.
  api.listChats({
    contact_id: draft.id,
    sort: 'updated_at', direction: 'desc', limit: 5,
  }).then((resp) => {
    const items = (resp && resp.items) || [];
    // ``replaceChildren`` takes nodes as varargs; spread the rendered
    // list so each row is a separate child. Passing the array as a
    // single arg silently calls ``toString`` on it → ``[object
    // HTMLDivElement]`` shows up where the rows should be.
    const rows = items.length
      ? items.map(c => el('div', {
          class: 'list-row',
          onClick: () => setState({ activeTab: 'chats', activeChatId: c.id })
        },
          el('div', { class: 'row-text' },
            el('div', { class: 'row-title' }, c.title || 'Untitled'),
            el('div', { class: 'row-sub' }, formatRelative(c.updated_at)),
          ),
        ))
      : [el('div', { style: { color: 'var(--text-mute)', fontSize: '13px' } }, 'No chats yet.')];
    recentBox.replaceChildren(
      el('h3', {}, 'Recent chats with ' + draft.name),
      ...rows,
    );
  }).catch(() => {
    recentBox.replaceChildren(
      el('h3', {}, 'Recent chats with ' + draft.name),
      el('div', { style: { color: 'var(--text-mute)', fontSize: '13px' } }, 'Could not load.'),
    );
  });

  const ttsSection = renderEntityTTSSection(draft, save, {
    entityKind: 'contact',
    settings: state.settings || {},
    onTabSwitch: () => setState({ activeTab: 'settings' }),
  });

  const body = el('div', { class: 'page-scroll' },
    el('div', { class: 'page-body' },
      baseInfo,
      greetingSection,
      defaultsSection,
      avatarSection,
      emotionsSection,
      scenariosSection,
      exampleSection,
      brainsSection,
      reminderSection,
      ttsSection,
      recentBox,
    ),
  );

  return el('div', { style: { display: 'flex', flexDirection: 'column', height: '100%' } }, header, body);
}


/* ----- Emotion grid + avatar ----- */

function renderAvatarBlock(draft) {
  const preview = el('div', {
    class: 'avatar',
    style: { width: '120px', height: '120px', borderRadius: 'var(--radius)' },
  });

  // While the crop modal is open, fall back to the uncropped original
  // image and apply the crop via CSS so dragging gives a live preview
  // (the server-baked /display image already has the prior crop in it).
  let inCrop = false;

  function refreshPreview() {
    preview.replaceChildren();
    if (draft.avatar) {
      const url = inCrop
        ? `/api/files/contacts/${draft.id}/avatar?v=${Date.now()}`
        : `/api/files/contacts/${draft.id}/avatar/display?v=${Date.now()}`;
      preview.append(el('img', { src: url, alt: draft.name }));
      applyCrop(preview, inCrop ? draft.avatar_crop : null);
    } else if (draft.emotions && draft.emotions.neutral) {
      const url = inCrop
        ? `/api/files/contacts/${draft.id}/emotions/neutral?v=${Date.now()}`
        : `/api/files/contacts/${draft.id}/emotions/neutral/display?v=${Date.now()}`;
      preview.append(el('img', { src: url, alt: draft.name }));
      applyCrop(preview, inCrop ? draft.emotions_crop : null);
    } else {
      preview.textContent = draft.name.slice(0, 1).toUpperCase();
      applyCrop(preview, null);
    }
  }
  refreshPreview();

  const fileInput = el('input', {
    type: 'file', accept: 'image/*', class: 'hidden', id: `avatar-input-${draft.id}`,
    onChange: async (e) => {
      const f = e.target.files[0];
      if (!f) return;
      try {
        const r = await api.uploadAvatar(draft.id, f);
        // Sync bookkeeping (version_id + updated_at) onto draft so the
        // next save (e.g. cropping the just-uploaded avatar) doesn't 409
        // against the bumped server-side state.
        draft.avatar = r.avatar;
        draft.avatar_crop = null;  // server clears crop on new upload
        if (r.version_id) draft.version_id = r.version_id;
        if (r.updated_at) draft.updated_at = r.updated_at;
        setState({ contacts: await api.listContacts() });
        refreshPreview();
        refreshButtons();
      } catch (err) { toast(`Upload failed: ${err.message}`, 'error'); }
      e.target.value = '';
    },
  });
  const uploadBtn = el('label', {
    for: `avatar-input-${draft.id}`,
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
        src: `/api/files/contacts/${draft.id}/avatar`,
        initial: draft.avatar_crop,
        title: `Crop avatar — ${draft.name}`,
        onChange: (crop) => applyCrop(preview, crop),
        onSave: async (crop) => {
          const next = (crop && (crop.x || crop.y || crop.w !== 1 || crop.h !== 1)) ? crop : null;
          draft.avatar_crop = next;
          try {
            const updated = await api.updateContact(draft.id, draft);
            // Sync the returned bookkeeping (version_id + updated_at) so
            // subsequent autosaves don't 409 on a stale draft.
            if (updated) Object.assign(draft, updated);
            setState({ contacts: await api.listContacts() });
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
        const r = await api.deleteAvatar(draft.id);
        draft.avatar = null;
        draft.avatar_crop = null;
        if (r && r.version_id) draft.version_id = r.version_id;
        if (r && r.updated_at) draft.updated_at = r.updated_at;
        setState({ contacts: await api.listContacts() });
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
    el('div', {
      style: { display: 'flex', flexDirection: 'column', gap: '6px', minWidth: '140px' },
    }, uploadBtn, cropBtn, deleteBtn),
  );
}


/* Wrap an avatar/card edit block with a small uppercase caption so the
 * two media slots on an entity edit page read as a matched pair. */
export function mediaBlock(label, content) {
  return el('div', { class: 'media-block' },
    el('div', { class: 'media-block-label' }, label),
    content,
  );
}


/* Card-image edit block — shared across Contact / User / Scenario /
 * BrainLibrary edit views. ``kind`` selects the API verbs; ``draft`` is
 * the mutable entity. Calls ``onChange`` after a successful upload or
 * delete so the surrounding view can sync bookkeeping + refresh.
 *
 * The upload accepts any image format; the server validates decode
 * and preserves the format. PNG conversion only happens at card-
 * export time (the export route re-encodes to embed the JSON chunk).
 */
export function renderCardImageBlock(draft, kind, onChange) {
  const apiFns = _cardImageApiFns(kind);
  const inputId = `card-input-${kind}-${draft.id}`;
  const preview = el('div', { class: 'card-image-preview' });

  function refreshPreview() {
    preview.replaceChildren();
    if (draft.card_image) {
      const url = `${apiFns.displayUrl(draft.id)}?v=${Date.now()}`;
      preview.append(el('img', { src: url, alt: 'Card image' }));
    } else {
      preview.append(el('div', { class: 'card-image-empty' },
        'No card image. Upload a PNG / JPG / WebP to set one.'));
    }
  }
  refreshPreview();

  const fileInput = el('input', {
    type: 'file', accept: 'image/*', class: 'hidden', id: inputId,
    onChange: async (e) => {
      const f = e.target.files[0];
      if (!f) return;
      try {
        const r = await apiFns.upload(draft.id, f);
        draft.card_image = r.card_image;
        if (r.version_id) draft.version_id = r.version_id;
        if (r.updated_at) draft.updated_at = r.updated_at;
        refreshPreview();
        refreshButtons();
        if (onChange) onChange();
      } catch (err) {
        toast(`Card upload failed: ${err.message || err}`, 'error');
      }
      e.target.value = '';
    },
  });
  const uploadBtn = el('label', {
    for: inputId, class: 'btn',
    style: { justifyContent: 'flex-start' },
  }, icon('upload', 14), draft.card_image ? 'Replace' : 'Upload');
  const removeBtn = el('button', {
    class: 'btn ghost danger',
    disabled: !draft.card_image,
    style: { justifyContent: 'flex-start' },
    onClick: async () => {
      try {
        const r = await apiFns.remove(draft.id);
        draft.card_image = null;
        if (r && r.version_id) draft.version_id = r.version_id;
        if (r && r.updated_at) draft.updated_at = r.updated_at;
        refreshPreview();
        refreshButtons();
        if (onChange) onChange();
      } catch (err) { toast(err.message, 'error'); }
    },
  }, icon('trash', 14), 'Remove');

  function refreshButtons() {
    removeBtn.disabled = !draft.card_image;
    uploadBtn.replaceChildren(icon('upload', 14), draft.card_image ? 'Replace' : 'Upload');
  }

  return el('div', { class: 'card-image-edit avatar-edit-row' },
    preview,
    fileInput,
    el('div', { class: 'card-image-actions',
      style: { display: 'flex', flexDirection: 'column', gap: '6px', minWidth: '140px' } },
      uploadBtn, removeBtn),
  );
}


function _cardImageApiFns(kind) {
  switch (kind) {
    case 'contact':
      return {
        displayUrl: id => `/api/files/contacts/${id}/card-image/display`,
        upload: api.uploadContactCard,
        remove: api.deleteContactCard,
      };
    case 'user':
      return {
        displayUrl: id => `/api/files/users/${id}/card-image/display`,
        upload: api.uploadUserCard,
        remove: api.deleteUserCard,
      };
    case 'scenario':
      return {
        displayUrl: id => `/api/files/scenarios/${id}/card-image/display`,
        upload: api.uploadScenarioCard,
        remove: api.deleteScenarioCard,
      };
    case 'library':
      return {
        displayUrl: id => `/api/files/libraries/${id}/card-image/display`,
        upload: api.uploadLibraryCard,
        remove: api.deleteLibraryCard,
      };
    case 'context-preset':
      return {
        displayUrl: id => `/api/files/context-presets/${id}/card-image/display`,
        upload: api.uploadContextPresetCard,
        remove: api.deleteContextPresetCard,
      };
    default:
      throw new Error(`unknown card-image kind: ${kind}`);
  }
}


function renderEmotionsGrid(draft, onCountChange) {
  const grid = el('div', { class: 'emotion-grid' });
  // Track every image wrapper so a crop change can update them all live.
  const wrappers = [];
  // Same swap-trick as renderAvatarBlock: while the crop modal is open
  // we render uncropped originals and apply the crop via CSS so all 24
  // sprites preview the new rect in realtime.
  let inCrop = false;

  function applyCropToAll(crop) {
    for (const w of wrappers) applyCrop(w, crop);
  }

  function rebuildAll() {
    for (const cell of grid.querySelectorAll('[data-emotion-cell]')) {
      cell._rebuild?.();
    }
  }

  for (const em of EMOTIONS) {
    const cell = el('div', {
      style: {
        textAlign: 'center',
        padding: '6px', border: '1px solid var(--border)', borderRadius: 'var(--radius-sm)',
        background: 'var(--bg-1)',
      },
      title: em,
      dataset: { emotionCell: em },
    });

    const fileInput = el('input', {
      type: 'file', accept: 'image/*', class: 'hidden', id: `emotion-${draft.id}-${em}`,
      onChange: async (e) => {
        const f = e.target.files[0];
        if (!f) return;
        try {
          const r = await api.uploadEmotion(draft.id, em, f);
          // Update emotions locally + sync bookkeeping so the subsequent
          // crop save doesn't 409 against the bumped server state.
          draft.emotions = { ...(draft.emotions || {}), [em]: r.filename };
          if (r.version_id) draft.version_id = r.version_id;
          if (r.updated_at) draft.updated_at = r.updated_at;
          setState({ contacts: await api.listContacts() });
          rebuildCell();
          onCountChange?.();
        } catch (err) { toast(`Upload failed: ${err.message}`, 'error'); }
        e.target.value = '';
      },
    });

    function rebuildCell() {
      cell.replaceChildren();
      cell.append(fileInput);
      const has = !!(draft.emotions || {})[em];
      let imgWrapper;
      if (has) {
        const url = inCrop
          ? `/api/files/contacts/${draft.id}/emotions/${em}?v=${Date.now()}`
          : `/api/files/contacts/${draft.id}/emotions/${em}/display?v=${Date.now()}`;
        imgWrapper = el('div', {
          class: 'image-frame',
          style: { width: '100%', aspectRatio: '1/1', borderRadius: '6px' },
        }, el('img', { src: url, alt: em }));
        if (inCrop) applyCrop(imgWrapper, draft.emotions_crop);
      } else {
        imgWrapper = el('div', {
          style: { width: '100%', aspectRatio: '1/1', display: 'grid', placeItems: 'center',
                   color: 'var(--text-faint)', background: 'var(--bg-2)', borderRadius: '6px', fontSize: '0.786rem' },
        }, '+');
      }
      // Replace the previous wrapper for this cell in the registry.
      const idx = wrappers.findIndex(w => w.dataset.emotion === em);
      const newWrapper = imgWrapper;
      newWrapper.dataset.emotion = em;
      if (idx >= 0) wrappers[idx] = newWrapper;
      else if (has) wrappers.push(newWrapper);

      cell.append(imgWrapper);
      cell.append(el('div', { style: { fontSize: '0.786rem', color: 'var(--text-mute)', marginTop: '4px' } }, em));
      cell.append(actionsRow(has));
    }
    cell._rebuild = rebuildCell;

    function actionsRow(currentlyHas) {
      const row = el('div', { style: { display: 'flex', gap: '2px', justifyContent: 'center', marginTop: '4px' } });
      row.append(el('label', { for: `emotion-${draft.id}-${em}`, class: 'icon-btn', title: 'Upload' }, icon('upload', 12)));
      if (currentlyHas) {
        row.append(el('button', {
          class: 'icon-btn', title: 'Crop (applies to all emotions)',
          onClick: () => {
            inCrop = true;
            rebuildAll();
            openCropModal({
              src: `/api/files/contacts/${draft.id}/emotions/${em}`,
              initial: draft.emotions_crop,
              title: `Crop emotion sprites — ${draft.name}`,
              onChange: (crop) => applyCropToAll(crop),
              onSave: async (crop) => {
                const next = (crop && (crop.x || crop.y || crop.w !== 1 || crop.h !== 1)) ? crop : null;
                draft.emotions_crop = next;
                try {
                  const updated = await api.updateContact(draft.id, draft);
                  if (updated) Object.assign(draft, updated);
                  setState({ contacts: await api.listContacts() });
                } catch (err) { toast(`Save failed: ${err.message}`, 'error'); }
              },
              onClose: () => { inCrop = false; rebuildAll(); },
            });
          },
        }, icon('crop', 12)));
        row.append(el('button', {
          class: 'icon-btn danger', title: 'Remove',
          onClick: async (e) => {
            e.stopPropagation();
            try {
              const r = await api.deleteEmotion(draft.id, em);
              draft.emotions = { ...(draft.emotions || {}) };
              delete draft.emotions[em];
              if (r && r.version_id) draft.version_id = r.version_id;
              if (r && r.updated_at) draft.updated_at = r.updated_at;
              setState({ contacts: await api.listContacts() });
              rebuildCell();
              onCountChange?.();
            } catch (err) { toast(err.message, 'error'); }
          },
        }, icon('trash', 12)));
      }
      return row;
    }

    rebuildCell();
    grid.append(cell);
  }
  return grid;
}


/* ----- Example chats editor ----- */

function renderExampleChatsEditor(draft, onSave) {
  const list = el('div', {});

  function rerender() {
    list.replaceChildren();
    (draft.example_chats || []).forEach((ec, i) => {
      list.append(renderExampleChatRow(ec, i, () => { draft.example_chats.splice(i, 1); onSave(); rerender(); }, onSave));
    });
    list.append(el('button', { class: 'btn ghost', onClick: () => {
      draft.example_chats = draft.example_chats || [];
      draft.example_chats.push({ name: '', style: 'chat', user_name: '', messages: [] });
      onSave();
      rerender();
      _focusLastSectionInput(list);
    } }, '+ Add example chat'));
  }
  rerender();
  return list;
}

function renderExampleChatRow(ec, idx, onRemove, onSave) {
  const row = el('div', { class: 'section', style: { background: 'var(--bg-2)' } });

  function addMessage() {
    ec.messages = ec.messages || [];
    // Default new rows to the opposite role of the previous one — example
    // chats are conversations, so alternation is the overwhelmingly common
    // case. Empty list starts with a user line.
    const last = ec.messages[ec.messages.length - 1];
    const nextIsContact = last ? !last.is_contact : false;
    ec.messages.push({ is_contact: nextIsContact, text: '', emotion: 'neutral' });
    onSave();
    rerender();
    // Focus the new row's textarea so the user can keep typing without
    // reaching for the mouse — this is the same flow Ctrl+Enter triggers.
    const rows = row.querySelectorAll('.example-message-row');
    const lastRow = rows[rows.length - 1];
    const ta = lastRow && lastRow.querySelector('textarea');
    if (ta) ta.focus();
  }

  function rerender() {
    row.replaceChildren();
    row.append(
      el('div', { class: 'form-grid' },
        el('div', { class: 'form-group' },
          el('label', {}, 'Name'),
          el('input', { type: 'text', value: ec.name || '', oninput: e => { ec.name = e.target.value; onSave(); } }),
        ),
        el('div', { class: 'form-group' },
          el('label', {}, 'User name (in this example)'),
          el('input', { type: 'text', value: ec.user_name || '', oninput: e => { ec.user_name = e.target.value; onSave(); } }),
        ),
        el('div', { class: 'form-group' },
          el('label', {}, 'Style'),
          makeSelect(STYLES, ec.style, v => { ec.style = v; onSave(); }),
        ),
      ),
      el('div', { style: { marginTop: '8px' } },
        el('h4', { style: { fontSize: '13px', color: 'var(--text-mute)' } }, 'Messages'),
        ...((ec.messages || []).map((m, j) => renderExampleMessageRow(ec, m, j, onSave, rerender, addMessage))),
        el('button', { class: 'btn ghost', onClick: addMessage }, '+ Add message'),
      ),
      el('div', { style: { marginTop: '8px' } },
        el('button', {
          class: 'btn ghost danger',
          onClick: async () => {
            const ok = await confirmModal(
              'Remove example chat?',
              'This example chat will be permanently deleted.',
              { danger: true, confirmLabel: 'Remove' },
            );
            if (ok) onRemove();
          },
        }, 'Remove example chat'),
      ),
    );
  }
  rerender();
  return row;
}

function renderExampleMessageRow(ec, m, j, onSave, rerender, addMessage) {
  // Ctrl+Enter (or Cmd+Enter) anywhere inside the row adds another message.
  // The picker triggers explicitly let modifier-Enter bubble past their own
  // open/close handler, so this listener fires for sender / textarea /
  // emotion focus alike.
  const onRowKeydown = (e) => {
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      e.stopPropagation();
      addMessage && addMessage();
    }
  };
  const rowEl = el('div', {
    class: 'example-message-row' + (m.is_contact ? '' : ' is-user'),
    onKeyDown: onRowKeydown,
  });
  const senderPicker = pickerSelect({
    value: m.is_contact ? 'contact' : 'user',
    options: [
      { value: 'user', label: 'User says' },
      { value: 'contact', label: 'Contact says' },
    ],
    // Toggle the emotion slot's visibility via a class instead of rebuilding
    // the row — a full rerender would replace the picker we just selected
    // from and yank keyboard focus away from the user.
    onChange: (v) => {
      const next = v === 'contact';
      if (m.is_contact === next) return;
      m.is_contact = next;
      onSave();
      rowEl.classList.toggle('is-user', !next);
    },
  });
  const textarea = el('textarea', {
    rows: 2, value: m.text || '',
    placeholder: 'Message text — Ctrl+Enter for next',
    oninput: e => { m.text = e.target.value; onSave(); },
  });
  const emotionPicker = makeEmotionSelect(m.emotion, v => { m.emotion = v; onSave(); });
  emotionPicker.classList.add('emotion-slot');
  const trashBtn = el('button', {
    class: 'icon-btn danger',
    onClick: () => { ec.messages.splice(j, 1); onSave(); rerender(); },
  }, icon('trash', 14));
  rowEl.append(senderPicker, textarea, emotionPicker, trashBtn);
  return rowEl;
}


/* ----- Contact-scenarios editor ----- */

function _newScenarioId() {
  // Server uses uuid4().hex (no dashes); match that so on-disk YAML stays
  // consistent regardless of where the id was minted.
  if (window.crypto && typeof window.crypto.randomUUID === 'function') {
    return window.crypto.randomUUID().replace(/-/g, '');
  }
  // Fallback for old browsers — 32 hex chars from random.
  let s = '';
  for (let i = 0; i < 32; i++) s += Math.floor(Math.random() * 16).toString(16);
  return s;
}

function renderContactScenariosEditor(draft, onSave) {
  draft.scenarios = draft.scenarios || [];
  const wrap = el('div', {});

  function rerender() {
    wrap.replaceChildren();
    // "(none)" default radio — lets the user explicitly say "no scenario by
    // default" even when scenarios exist on the contact. Only useful with at
    // least one scenario; otherwise there's nothing to disambiguate.
    if (draft.scenarios.length > 0) {
      const noneChecked = !draft.default_scenario_id;
      wrap.append(el('label', {
        style: { display: 'flex', alignItems: 'center', gap: '6px',
                 fontSize: '12px', color: 'var(--text-mute)',
                 marginTop: '6px', marginBottom: '14px' },
      },
        el('input', {
          type: 'radio',
          name: `scenario-default-${draft.id}`,
          checked: noneChecked,
          onChange: e => {
            if (e.target.checked) {
              draft.default_scenario_id = null;
              onSave();
              rerender();
            }
          },
        }),
        '(none) — new chats start without a scenario',
      ));
    }
    draft.scenarios.forEach((cs, i) => {
      wrap.append(renderContactScenarioCard(draft, cs, i, onSave, rerender));
    });
    wrap.append(el('button', {
      class: 'btn ghost',
      onClick: () => {
        const cs = {
          id: _newScenarioId(),
          name: '',
          description: '',
          environment: '',
          scene: '',
          tags: '',
          cjk: false,
          greeting: '',
          greeting_emotion: null,
          style: null,
          intimacy: null,
          response_length: null,
          brains: [],
        };
        draft.scenarios.push(cs);
        // First scenario added becomes the default — usually what the user
        // wants, and avoids a freshly-imported character with one scenario
        // having ``default_scenario_id: null`` until they click the radio.
        if (draft.scenarios.length === 1) draft.default_scenario_id = cs.id;
        onSave();
        rerender();
        _focusLastSectionInput(wrap);
      },
    }, '+ Add scenario'));
  }
  rerender();
  return wrap;
}

function renderContactScenarioCard(draft, cs, idx, onSave, rerenderList) {
  const isDefault = draft.default_scenario_id === cs.id;
  const titleText = (cs.name || '').trim() || `Scenario ${idx + 1}`;
  const head = el('div', { style: { display: 'flex', alignItems: 'center', gap: '10px', marginBottom: '8px' } },
    el('strong', {}, titleText),
    el('label', { style: { display: 'flex', alignItems: 'center', gap: '4px', fontSize: '12px', color: 'var(--text-mute)' } },
      el('input', {
        type: 'radio',
        name: `scenario-default-${draft.id}`,
        checked: isDefault,
        onChange: e => {
          if (e.target.checked) {
            draft.default_scenario_id = cs.id;
            onSave();
            rerenderList();
          }
        },
      }),
      'Default',
    ),
  );

  const nameInput = el('input', {
    type: 'text',
    value: cs.name || '',
    placeholder: 'e.g. At Home',
    oninput: e => { cs.name = e.target.value; head.querySelector('strong').textContent = (cs.name || '').trim() || `Scenario ${idx + 1}`; onSave(); },
  });

  const greetingTa = el('textarea', { rows: 3, oninput: e => { cs.greeting = e.target.value; onSave(); } });
  greetingTa.value = cs.greeting || '';

  const sceneTa = el('textarea', { rows: 2, oninput: e => { cs.scene = e.target.value; onSave(); } });
  sceneTa.value = cs.scene || '';

  const envTa = el('textarea', { rows: 2, oninput: e => { cs.environment = e.target.value; onSave(); } });
  envTa.value = cs.environment || '';

  return el('div', { class: 'section nested-card', style: { background: 'var(--bg-2)' } },
    head,
    el('div', { class: 'form-group' }, el('label', {}, 'Name'), nameInput),
    el('div', { class: 'form-group' },
      el('label', {}, 'Greeting'),
      greetingTa,
      el('div', { class: 'hint' }, 'Overrides the contact greeting when non-empty.'),
    ),
    el('div', { class: 'form-group' },
      el('label', {}, 'Greeting emotion'),
      makeEmotionSelect(cs.greeting_emotion, v => { cs.greeting_emotion = v; onSave(); }),
    ),
    el('div', { class: 'form-group' }, el('label', {}, 'Scene'), sceneTa),
    el('div', { class: 'form-group' }, el('label', {}, 'Environment'), envTa),
    el('div', { class: 'form-group' },
      el('label', {}, 'Tags'),
      el('input', { type: 'text', value: cs.tags || '', placeholder: 'comma, separated, list', oninput: e => { cs.tags = e.target.value; onSave(); } }),
    ),
    el('div', { class: 'form-grid-3' },
      el('div', { class: 'form-group' },
        el('label', {}, 'Style override'),
        makeOptionalSelect(STYLES, cs.style, v => { cs.style = v; onSave(); }),
      ),
      el('div', { class: 'form-group' },
        el('label', {}, 'Intimacy override'),
        makeOptionalSelect(INTIMACIES, cs.intimacy, v => { cs.intimacy = v; onSave(); }),
      ),
      el('div', { class: 'form-group' },
        el('label', {}, 'Response length'),
        makeOptionalSelect(RESPONSE_LENGTHS.filter(x => x !== ''), cs.response_length, v => { cs.response_length = v; onSave(); }),
      ),
    ),
    el('div', { class: 'form-group checkbox', style: { marginTop: '18px' } },
      el('input', { type: 'checkbox', checked: !!cs.cjk, id: `scenario-cjk-${cs.id}`, onChange: e => { cs.cjk = e.target.checked; onSave(); } }),
      el('label', { for: `scenario-cjk-${cs.id}` }, 'Japanese'),
    ),
    (function () {
      const csBrainsBadge = liveBadge(() => String((cs.brains || []).length));
      return el('div', { class: 'form-group' },
        el('label', {}, 'Brains ', csBrainsBadge),
        renderBrainsEditor(
          cs.brains || [],
          (newBrains) => { cs.brains = newBrains; onSave(); csBrainsBadge.refresh(); },
          {
            getBrainCatalog: () => _contactBrainCatalog(draft, cs),
            ownerName: () => (`${draft.name || 'Contact'} - ${cs.name || 'Scenario'}`),
            ownerId: () => cs.id,
          },
        ),
      );
    })(),
    el('div', { style: { marginTop: '8px' } },
      el('button', {
        class: 'btn ghost danger',
        onClick: async () => {
          const ok = await confirmModal(
            'Remove scenario?',
            `"${(cs.name || '').trim() || 'This scenario'}" will be permanently deleted from ${draft.name}.`,
            { danger: true, confirmLabel: 'Remove' },
          );
          if (!ok) return;
          draft.scenarios.splice(idx, 1);
          if (draft.default_scenario_id === cs.id) {
            draft.default_scenario_id = draft.scenarios.length ? draft.scenarios[0].id : null;
          }
          onSave();
          rerenderList();
        },
      }, 'Remove scenario'),
    ),
  );
}


/* ----- Brains editor ----- */

export function renderBrainsEditor(brains, onChange, opts = {}) {
  const wrap = el('div', {});
  const getBrainCatalog = opts.getBrainCatalog || (() => []);
  // ``ownerName`` (optional) — the entity name surfaced in the
  // brain-library JSON's ``name`` field. ``ownerId`` (optional) — its
  // UUID; the first 8 hex chars are folded into the download filename
  // so two contacts named "Alice" don't produce filename collisions.
  const ownerName = opts.ownerName || (() => 'Brains');
  const ownerId = opts.ownerId || (() => '');

  function rerender() {
    wrap.replaceChildren();
    (brains || []).forEach((b) => {
      const row = renderBrainRow(b, {
        onChange: () => onChange(brains),
        onDelete: () => {
          const i = brains.indexOf(b);
          if (i >= 0) brains.splice(i, 1);
          onChange(brains);
          rerender();
        },
        // Pass the function through so sibling-brain renames flow into
        // the "Brain entry active" dropdown live (each dropdown re-fetches
        // on focus). The brain row itself filters out self.
        brainCatalog: getBrainCatalog,
      });
      // Visual harmony with the surrounding contact-scenario brain rows.
      row.style.background = 'var(--bg-2)';
      wrap.append(row);
    });

    const actions = el('div', { class: 'brain-editor-actions', style: { display: 'flex', gap: '6px', flexWrap: 'wrap' } });
    actions.append(el('button', { class: 'btn ghost', onClick: () => {
      const fresh = { name: '', content: '' };
      ensureBrainShape(fresh);
      brains.push(fresh);
      onChange(brains);
      rerender();
      _focusLastSectionInput(wrap);
    } }, '+ Add brain'));

    actions.append(_importBrainsButton(brains, onChange, rerender));
    actions.append(_exportBrainsButton(brains, ownerName, ownerId));

    wrap.append(actions);
  }
  rerender();
  return wrap;
}


function _importBrainsButton(brains, onChange, rerender) {
  const fileInput = el('input', {
    type: 'file',
    accept: '.json,.png,application/json,image/png',
    style: { display: 'none' },
    onchange: async (e) => {
      const file = e.target.files?.[0];
      e.target.value = '';
      if (!file) return;
      try {
        const result = await api.importBrains(file);
        const incoming = Array.isArray(result?.brains) ? result.brains : [];
        if (!incoming.length) {
          toast('No brains found in import.', 'error');
          return;
        }
        for (const b of incoming) {
          ensureBrainShape(b);
          brains.push(b);
        }
        onChange(brains);
        rerender();
        const count = incoming.length;
        const src = result?.source_name ? ` from ${result.source_name}` : '';
        toast(`Imported ${count} brain${count === 1 ? '' : 's'}${src}.`);
      } catch (err) {
        toast(`Import failed: ${err.message || err}`, 'error');
      }
    },
  });
  const btn = el('button', {
    class: 'btn ghost',
    onClick: () => fileInput.click(),
    title: 'Import brains from JSON or PNG',
  }, 'Import brains…');
  const wrap = el('span', {}, fileInput, btn);
  return wrap;
}


function _exportBrainsButton(brains, ownerName, ownerId) {
  return el('button', {
    class: 'btn ghost',
    onClick: () => {
      const name = (typeof ownerName === 'function' ? ownerName() : ownerName) || 'Brains';
      const id = (typeof ownerId === 'function' ? ownerId() : ownerId) || '';
      if (!brains.length) {
        toast('No brains to export.', 'error');
        return;
      }
      const payload = brainsToExportJson(name, brains);
      const safe = (name || 'brains').replace(/[^A-Za-z0-9._-]+/g, '-').toLowerCase();
      const idStem = id ? `-${id.slice(0, 8)}` : '';
      downloadJson(`${safe}${idStem}-brains.json`, payload);
    },
    title: 'Download brains as a brain-library JSON',
  }, 'Export brains');
}


/* ----- Delete-confirmation with streamed deletion response ----- */

function confirmDeleteContact(contact) {
  // The deletion response endpoint is contact-based (no chat required), so
  // freshly-imported contacts get the same farewell treatment as ones with
  // active chats. The stream is decoupled from any specific user persona —
  // the server picks one (most-recent chat partner, else any).
  return new Promise(resolve => {
    let settled = false;
    const settle = (v) => { if (!settled) { settled = true; resolve(v); } };

    const stack = el('div', { class: 'deletion-response' });
    const header = el('div', { class: 'meta-row', style: { marginBottom: '4px' } },
      el('span', { class: 'sender-name' }, contact.name),
    );
    const typingRow = el('div', { class: 'bubble-row contact' },
      el('div', { class: 'emotion-sprite no-image' }, '…'),
      el('div', { class: 'bubble typing streaming' }, 'typing…'),
    );
    stack.append(header, typingRow);

    const body = el('div', {},
      el('h3', {}, `Delete ${contact.name}?`),
      el('p', { style: { color: 'var(--text-mute)', fontSize: '0.857rem', margin: '4px 0 12px' } },
        `${contact.name} will be removed permanently.`),
      stack,
      el('div', { class: 'modal-actions' },
        el('button', {
          class: 'btn ghost',
          onClick: () => { stream && stream.close(); settle(false); closeModal(); },
        }, 'Cancel'),
        el('button', {
          class: 'btn danger',
          onClick: () => { stream && stream.close(); settle(true); closeModal(); },
        }, 'Delete'),
      ),
    );
    openModal(body, {
      size: 'large',
      onClose: () => { if (stream) stream.close(); settle(false); },
    });

    const stream = startContactDeletionResponseStream(contact.id, {
      onBubble: (b) => {
        const row = renderBubbleRow(b, contact, true);
        stack.insertBefore(row, typingRow);
        stack.scrollTop = stack.scrollHeight;
      },
      onDone: () => { typingRow.remove(); },
      onError: (err) => {
        typingRow.remove();
        stack.append(el('div', {
          style: { color: 'var(--text-mute)', fontStyle: 'italic', fontSize: '0.857rem', marginTop: '6px' },
        }, `(no farewell — ${err.message || 'inference unavailable'})`));
      },
    });
  });
}


/* ----- Helpers ----- */

/* Move keyboard focus to the first text-style input in the most recently
 * appended ``.section`` child of ``container``. Used after "+ Add X" actions
 * so the user can start typing the new entry's name immediately. */
function _focusLastSectionInput(container) {
  const sections = container.querySelectorAll(':scope > .section');
  const last = sections[sections.length - 1];
  const input = last && last.querySelector('input[type="text"], textarea');
  if (input) input.focus();
}


/* Catalog of brains a "Brain entry active" condition can reference inside
 * this contact's editor. Spans the contact's globals and the brains nested
 * under each of its scenarios. ``activeScenario`` (optional) marks the
 * currently-being-edited ContactScenario so its label uses "self / …"
 * instead of the scenario's name. */
function _contactBrainCatalog(contact, activeScenario = null) {
  const contactName = (contact && contact.name) || 'this character';
  const entries = brainCatalogEntries(contact.brains || [], contactName);
  for (const cs of (contact.scenarios || [])) {
    const isSelf = activeScenario && cs.id === activeScenario.id;
    const label = isSelf ? 'self' : `${contactName} / ${cs.name || 'Scenario'}`;
    entries.push(...brainCatalogEntries(cs.brains || [], label));
  }
  return entries;
}


function makeSelect(options, current, onChange) {
  return pickerSelect({
    value: current,
    options: options.map(o => ({ value: o, label: _capWords(o) })),
    onChange,
  });
}

function makeOptionalSelect(options, current, onChange, opts = {}) {
  // Empty value (``__inherit``) maps to ``null`` — used for contact-scenario
  // overrides where "leave alone" is the default. ``opts.emptyLabel``
  // overrides the "(inherit)" wording — useful at the contact level where
  // ``null`` means "no preference" rather than "fall through to a parent".
  const emptyLabel = opts.emptyLabel || '(inherit)';
  return pickerSelect({
    value: current == null ? '__inherit' : current,
    options: [
      { value: '__inherit', label: emptyLabel },
      ...options.map(o => ({ value: o, label: _capWords(o) })),
    ],
    onChange: (v) => onChange(v === '__inherit' ? null : v),
  });
}

/* Display-only capitalisation: values stay lowercase on the wire / in the
 * AER prompt; the dropdown shows ``Chat`` / ``Roleplay, Novel Style`` /
 * ``Stranger`` etc. so the UI doesn't read like CLI flags. */
function _capWords(s) {
  if (!s) return s;
  return s.split(' ').map(w => w.charAt(0).toUpperCase() + w.slice(1)).join(' ');
}

function makeEmotionSelect(current, onChange) {
  // Contact-side messages (greeting, example chat contact lines) must always
  // carry an emotion — default to neutral when nothing is set rather than
  // exposing a "(none)" option.
  return pickerSelect({
    value: current || 'neutral',
    options: EMOTIONS,
    onChange,
  });
}

/* Module-level handle on the currently-open editor's draft + autosave
 * trigger. List-pane handlers (favorite star) route their PUT through
 * here when the targeted contact's detail pane is showing — otherwise
 * the server bumps version_id while the open draft keeps the stale one,
 * and the next autosave 10 minutes later 409s. */
let _openDraft = null;
let _openDraftSave = null;

/* Subscribe to refreshes. */
let _subscribed = false;
let _lastDetailId = null;
export function setupContactsTab() {
  if (_subscribed) return;
  _subscribed = true;
  subscribe(() => {
    refreshList();
    // Only re-render the detail pane when the selected contact changes —
    // otherwise auto-save would yank the textarea/cursor out from under
    // whatever the user is typing on every keystroke debounce.
    if (state.activeContactId !== _lastDetailId) refreshDetail();
  });
}
