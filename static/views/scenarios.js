/* Scenarios tab: list + edit. */

import { api } from '../api.js';
import { state, setState, subscribe } from '../state.js';
import { el, formatRelative, downloadFromResponse, sortEntities, sortControls, lastUsedByEntity, dirtyDot, makeAutoSaver, favoriteStar, scenarioAvatarEl, liveBadge } from '../util.js';
import { icon, confirmModal, toast } from '../ui.js';
import { renderBrainsEditor, renderCardImageBlock, mediaBlock } from './contacts.js';
import { brainCatalogEntries } from './brain_row.js';
import { saveWithConflictHandling } from '../conflict.js';
import { markPending, clearPending } from '../save_queue.js';
import { openNewChatWizard } from './new_chat_wizard.js';
import { runImport } from './import_progress.js';
import { openCropModal } from './crop_modal.js';
import { THEMES } from '../constants.js';
import { createVirtList } from '../virt_list.js';


export function renderScenariosTab(container) {
  container.replaceChildren();
  const pane = el('div', { class: 'list-pane' });
  pane.append(renderListHeader());
  const body = el('div', { class: 'list-body', id: 'scenario-list-body' });
  pane.append(body);
  container.append(pane);
  const detail = el('div', { class: 'content-pane', id: 'scenario-detail' });
  container.append(detail);
  mountList(body);
  refreshDetail();
}


let _searchTerm = '';

function renderListHeader() {
  const search = el('input', {
    class: 'list-search',
    type: 'text',
    placeholder: 'Search scenarios…',
    value: _searchTerm,
    oninput: (e) => { _searchTerm = e.target.value.toLowerCase(); refreshList(); },
  });
  const sort = sortControls(state.scenarioSort || { mode: 'added', direction: 'desc' }, (v) => {
    setState({ scenarioSort: v });
    try { localStorage.setItem('scenarioSort', JSON.stringify(v)); } catch {}
    refreshList();
  });
  return el('div', { class: 'list-header' },
    el('div', { style: { display: 'flex', justifyContent: 'space-between', alignItems: 'center' } },
      el('h2', {}, 'Scenarios'),
      el('div', { style: { display: 'flex', gap: '6px' } },
        el('label', { class: 'btn', for: 'scenario-import-input', title: 'Import scenario', style: { cursor: 'pointer' } }, icon('upload', 14)),
        el('input', {
          id: 'scenario-import-input', type: 'file',
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
          title: 'New scenario',
          onClick: async () => {
            try {
              const s = await api.createScenario({ name: 'New scenario' });
              setState({ scenarios: await api.listScenarios(), activeScenarioId: s.id });
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


function _matchesSearch(s) {
  if (!_searchTerm) return true;
  const hay = `${s.name || ''} ${s.tags || ''} ${s.description || ''}`.toLowerCase();
  return hay.includes(_searchTerm);
}


let _virt = null;
let _visible = [];
let _emptyEl = null;
let _emptyMatchEl = null;

function mountList(body) {
  _emptyEl = el('div', { class: 'list-empty', style: { display: 'none' } },
    'No scenarios yet.');
  _emptyMatchEl = el('div', { class: 'list-empty', style: { display: 'none' } },
    'No scenarios match your search.');
  _virt = createVirtList(body, {
    count: 0,
    getItem: (i) => _visible[i] || null,
    renderItem: (s) => renderRow(s),
    estimatedHeight: 60,
    observeRows: false,
  });
  body.appendChild(_emptyEl);
  body.appendChild(_emptyMatchEl);
  refreshList();
}

function renderRow(s) {
  const isActive = state.activeScenarioId === s.id;
  return el('div', {
    class: `list-row ${isActive ? 'active' : ''}`,
    onClick: () => setState({ activeScenarioId: s.id }),
  },
    scenarioAvatarEl(s),
    el('div', { class: 'row-text' },
      el('div', { class: 'row-title' },
        el('span', { class: 'row-title-text' }, s.name),
        favoriteStar(s, async (next) => {
          // Route through the open editor's draft when this scenario's
          // detail pane is showing — keeps version_id in sync via
          // saveWithConflictHandling. See contacts.js for the full
          // rationale.
          if (_openDraft && _openDraftSave && _openDraft.id === s.id) {
            _openDraft.favorite = next;
            _openDraftSave();
            return;
          }
          try {
            const updated = await api.setScenarioFavorite(s.id, next);
            setState({
              scenarios: (state.scenarios || []).map(
                x => x.id === updated.id ? updated : x,
              ),
            });
          } catch (err) { toast(err.message, 'error'); }
        }),
      ),
      el('div', { class: 'row-sub' }, s.tags || s.description || ''),
    ),
  );
}

function refreshList() {
  if (!_virt) return;
  const all = state.scenarios || [];
  if (all.length === 0) {
    _visible = [];
    _emptyEl.style.display = '';
    _emptyMatchEl.style.display = 'none';
    _virt.setCount(0);
    return;
  }
  const ss = state.scenarioSort || { mode: 'added', direction: 'desc' };
  _visible = sortEntities(
    all.filter(_matchesSearch),
    ss.mode,
    lastUsedByEntity(state.chats, 'scenario_id'),
    ss.direction,
    { favoritesFirst: !!ss.favoritesFirst },
  );
  _emptyEl.style.display = 'none';
  _emptyMatchEl.style.display = _visible.length === 0 ? '' : 'none';
  _virt.setCount(_visible.length, { preserveScroll: true });
}


let _refreshDetailToken = 0;

async function refreshDetail() {
  const detail = document.getElementById('scenario-detail');
  if (!detail) return;
  _lastDetailId = state.activeScenarioId;
  if (!state.activeScenarioId) {
    detail.replaceChildren();
    _openDraft = null;
    _openDraftSave = null;
    detail.append(el('div', { class: 'content-empty' },
      el('div', {}, el('h3', {}, 'Pick a scenario'), el('div', {}, 'Select one on the left to edit it.')),
    ));
    return;
  }
  const oldScroll = detail.querySelector('.page-scroll');
  const prevScrollTop = oldScroll ? oldScroll.scrollTop : 0;
  // Fetch the FULL scenario — Summary rows omit environment/scene/brains.
  // Token + activeId guards drop stale fetches from rapid row clicks.
  const token = ++_refreshDetailToken;
  const requestedId = state.activeScenarioId;
  let s;
  try {
    s = await api.getScenario(requestedId);
  } catch (err) {
    if (token !== _refreshDetailToken) return;
    toast(err.message, 'error');
    return;
  }
  if (token !== _refreshDetailToken || state.activeScenarioId !== requestedId) return;
  if (!s) { setState({ activeScenarioId: null }); return; }
  detail.replaceChildren();
  detail.append(renderEditView(s));
  const newScroll = detail.querySelector('.page-scroll');
  if (newScroll) newScroll.scrollTop = prevScrollTop;
}


function renderEditView(scenario) {
  let draft = JSON.parse(JSON.stringify(scenario));

  const dot = dirtyDot();
  const save = makeAutoSaver(async () => {
    markPending('scenario', draft.id, draft);
    await saveWithConflictHandling({
      draft,
      saveFn: (d) => api.updateScenario(d.id, d),
      getFn: () => api.getScenario(draft.id),
      entityLabel: 'Scenario',
      onReload: async (latest) => {
        Object.assign(draft, latest);
        setState({ scenarios: await api.listScenarios() });
        _lastDetailId = null;
        refreshDetail();
      },
    });
    clearPending('scenario', draft.id);
    setState({ scenarios: await api.listScenarios() });
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
      el('button', { class: 'btn primary', onClick: () => openNewChatWizard({ scenarioId: draft.id }) }, icon('plus', 14), 'New chat'),
      el('button', { class: 'btn', onClick: async () => {
        const r = await api.exportScenario(draft.id);
        await downloadFromResponse(r, `${draft.name}-scenario.json`);
      } }, icon('download', 14), 'Export'),
    ];
    if (draft.card_image) {
      items.push(el('button', {
        class: 'btn',
        title: 'Export as PNG card with embedded data',
        onClick: async () => {
          const r = await api.exportScenarioCard(draft.id);
          await downloadFromResponse(r, `${draft.name}-scenario.png`);
        },
      }, icon('download', 14), 'Export card'));
    }
    items.push(el('button', {
      class: 'btn',
      title: 'Create a copy of this scenario',
      onClick: async () => {
        const copy = await api.duplicateScenario(draft.id);
        setState({ scenarios: await api.listScenarios(), activeScenarioId: copy.id });
      },
    }, icon('copy', 14), 'Duplicate'));
    items.push(el('button', { class: 'btn danger', onClick: async () => {
      const ok = await confirmModal('Delete scenario?', `${draft.name} will be removed.`, { danger: true });
      if (!ok) return;
      await api.deleteScenario(draft.id);
      setState({ scenarios: await api.listScenarios(), activeScenarioId: null });
    } }, icon('trash', 14), 'Delete'));
    header.replaceChildren(...items);
  }
  refreshHeader();

  const descriptionTa = el('textarea', {
    rows: 3,
    oninput: e => { draft.description = e.target.value; save(); },
  });
  descriptionTa.value = draft.description || '';

  const baseInfo = el('div', { class: 'section' },
    el('h3', {}, 'Scenario'),
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
    el('div', { class: 'form-group' },
      el('label', {}, 'Environment'),
      (function() { const t = el('textarea', { rows: 3, oninput: e => { draft.environment = e.target.value; save(); } }); t.value = draft.environment || ''; return t; })(),
    ),
    el('div', { class: 'form-group' },
      el('label', {}, 'Scene'),
      (function() { const t = el('textarea', { rows: 4, oninput: e => { draft.scene = e.target.value; save(); } }); t.value = draft.scene || ''; return t; })(),
    ),
    el('div', { class: 'form-group' },
      el('label', {}, 'Tags (comma-separated)'),
      el('input', { type: 'text', value: draft.tags || '', oninput: e => { draft.tags = e.target.value; save(); } }),
    ),
    el('div', { class: 'form-group checkbox' },
      el('input', { type: 'checkbox', checked: !!draft.cjk, id: 'cjk-' + draft.id, onChange: e => { draft.cjk = e.target.checked; save(); } }),
      el('label', { for: 'cjk-' + draft.id }, 'Japanese'),
    ),
  );

  const backgroundSection = renderBackgroundSection(draft, save);

  const cardImageSection = el('div', { class: 'section' },
    el('h3', {}, 'Card image'),
    mediaBlock('Card image', renderCardImageBlock(draft, 'scenario', () => refreshHeader())),
  );

  const brainsBadge = liveBadge(() => String((draft.brains || []).length));
  const brainsSection = el('div', { class: 'section' },
    el('h3', {}, 'Brains', brainsBadge),
    renderBrainsEditor(
      draft.brains || [],
      (newBrains) => { draft.brains = newBrains; save(); brainsBadge.refresh(); },
      {
        getBrainCatalog: () => brainCatalogEntries(draft.brains || [], draft.name || 'this scenario'),
        ownerName: () => draft.name || 'Scenario',
        ownerId: () => draft.id,
      },
    ),
  );

  const recentBox = el('div', { class: 'section' },
    el('h3', {}, 'Recent chats in this scenario'),
    el('div', { style: { color: 'var(--text-mute)', fontSize: '13px' } }, 'Loading…'),
  );
  api.listChats({
    scenario_id: draft.id,
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
      el('h3', {}, 'Recent chats in this scenario'),
      ...rows,
    );
  }).catch(() => {
    recentBox.replaceChildren(
      el('h3', {}, 'Recent chats in this scenario'),
      el('div', { style: { color: 'var(--text-mute)', fontSize: '13px' } }, 'Could not load.'),
    );
  });

  return el('div', { style: { display: 'flex', flexDirection: 'column', height: '100%' } },
    header,
    el('div', { class: 'page-scroll' },
      el('div', { class: 'page-body' }, baseInfo, backgroundSection, cardImageSection, brainsSection, recentBox),
    ),
  );
}


/* ---------- background editor ---------- */

function renderBackgroundSection(draft, save) {
  const section = el('div', { class: 'section' },
    el('h3', {}, 'Background image'),
  );
  const body = el('div', {});
  section.append(body);

  function refresh() {
    body.replaceChildren();
    if (!draft.background_image) {
      body.append(renderUploadEmpty(draft, () => refresh()));
      return;
    }

    // Live re-applied across the focal picker, the live preview, and every
    // theme cell whenever the user drags a slider. Each consumer wires its
    // element into ``previewTargets`` and the helper sets the same set of
    // CSS custom properties on every one.
    const previewTargets = [];
    function applyPreviewVars() {
      for (const t of previewTargets) applyBackgroundVars(t, draft);
    }

    const focalPicker = renderFocalPicker(draft, save, applyPreviewVars);
    const avatarMiniWrap = el('div', { class: 'bg-avatar-mini-wrap' });
    function rebuildAvatarMini() {
      avatarMiniWrap.replaceChildren(scenarioAvatarEl(draft, { class: 'large' }));
    }
    rebuildAvatarMini();

    const replaceInput = el('input', {
      type: 'file', accept: 'image/*', class: 'hidden',
      id: `bg-replace-${draft.id}`,
    });
    replaceInput.onchange = async (e) => {
      const f = e.target.files[0];
      if (!f) { e.target.value = ''; return; }
      try {
        await api.uploadScenarioBackground(draft.id, f);
        const fresh = await api.getScenario(draft.id);
        Object.assign(draft, fresh);
        setState({ scenarios: await api.listScenarios() });
        refresh();
      } catch (err) { toast(`Upload failed: ${err.message}`, 'error'); }
      e.target.value = '';
    };

    const replaceBtn = el('label', {
      for: `bg-replace-${draft.id}`,
      class: 'btn',
      style: { cursor: 'pointer', justifyContent: 'flex-start' },
    }, icon('upload', 14), 'Replace');

    const cropBtn = el('button', {
      class: 'btn',
      title: 'Pick which square of the image fills the list avatar slot',
      style: { justifyContent: 'flex-start' },
      onClick: () => {
        openCropModal({
          src: `/api/files/scenarios/${draft.id}/background?v=${Math.floor(draft.updated_at || 0)}`,
          initial: draft.avatar_crop,
          title: `Crop avatar — ${draft.name}`,
          onSave: async (crop) => {
            const next = (crop && (crop.x || crop.y || crop.w !== 1 || crop.h !== 1)) ? crop : null;
            draft.avatar_crop = next;
            try {
              const updated = await api.updateScenario(draft.id, draft);
              Object.assign(draft, updated);
              setState({ scenarios: await api.listScenarios() });
              rebuildAvatarMini();
            } catch (err) { toast(`Save failed: ${err.message}`, 'error'); }
          },
        });
      },
    }, icon('crop', 14), 'Crop avatar');

    const removeBtn = el('button', {
      class: 'btn ghost danger',
      style: { justifyContent: 'flex-start' },
      onClick: async () => {
        try {
          await api.deleteScenarioBackground(draft.id);
          const fresh = await api.getScenario(draft.id);
          Object.assign(draft, fresh);
          setState({ scenarios: await api.listScenarios() });
          refresh();
        } catch (err) { toast(err.message, 'error'); }
      },
    }, icon('trash', 14), 'Remove');

    const sideButtons = el('div', {
      style: { display: 'flex', flexDirection: 'column', gap: '6px', minWidth: '160px' },
    },
      replaceBtn, cropBtn, removeBtn,
      el('div', { style: { marginTop: '6px', color: 'var(--text-mute)', fontSize: '0.786rem' } },
        'List avatar:'),
      avatarMiniWrap,
    );

    const topRow = el('div', { class: 'bg-edit-row' }, focalPicker, sideButtons);

    const modeToggle = renderModeToggle(draft, save, () => {
      // Toggle disabled state on the focal picker — focal point is irrelevant
      // in tile mode.
      focalPicker.classList.toggle('disabled', draft.background_mode === 'tile');
      applyPreviewVars();
    });
    if (draft.background_mode === 'tile') focalPicker.classList.add('disabled');

    const sliderGrid = el('div', { class: 'bg-slider-grid' },
      renderSlider('Blur', 0, 20, draft.background_blur || 0, 'px',
        (v) => { draft.background_blur = v; save(); applyPreviewVars(); }),
      renderSlider('Tint', 0, 100, draft.background_tint_strength || 0, '%',
        (v) => { draft.background_tint_strength = v; save(); applyPreviewVars(); }),
      renderSlider('Dim', 0, 100, draft.background_dim || 0, '%',
        (v) => { draft.background_dim = v; save(); applyPreviewVars(); }),
      renderSlider('Brighten', 0, 100, draft.background_brighten || 0, '%',
        (v) => { draft.background_brighten = v; save(); applyPreviewVars(); }),
    );

    const livePreview = renderLivePreview();
    previewTargets.push(livePreview);

    const themeGrid = renderThemeGrid(previewTargets);

    body.append(
      replaceInput,
      topRow,
      modeToggle,
      sliderGrid,
      el('div', { style: { marginTop: '14px', color: 'var(--text-mute)', fontSize: '0.857rem' } }, 'Preview'),
      livePreview,
      el('div', { style: { marginTop: '14px', color: 'var(--text-mute)', fontSize: '0.857rem' } },
        'Across themes (click a tile to switch the active theme)'),
      themeGrid,
    );

    applyPreviewVars();
  }

  refresh();
  return section;
}


function renderUploadEmpty(draft, onUploaded) {
  const fileInput = el('input', {
    type: 'file', accept: 'image/*', class: 'hidden',
    id: `bg-input-empty-${draft.id}`,
  });
  fileInput.onchange = async (e) => {
    const f = e.target.files[0];
    if (!f) { e.target.value = ''; return; }
    try {
      await api.uploadScenarioBackground(draft.id, f);
      const fresh = await api.getScenario(draft.id);
      Object.assign(draft, fresh);
      setState({ scenarios: await api.listScenarios() });
      onUploaded();
    } catch (err) { toast(`Upload failed: ${err.message}`, 'error'); }
    e.target.value = '';
  };
  return el('div', { class: 'bg-empty' },
    el('div', {}, 'Add an image to use as a backdrop in chats with this scenario.'),
    el('div', { style: { marginTop: '12px' } },
      fileInput,
      el('label', {
        for: `bg-input-empty-${draft.id}`,
        class: 'btn primary',
        style: { cursor: 'pointer' },
      }, icon('upload', 14), 'Upload image'),
    ),
  );
}


function renderFocalPicker(draft, save, onChange) {
  const surface = el('div', { class: 'focal-picker' });
  const img = el('img', {
    src: `/api/files/scenarios/${draft.id}/background?v=${Math.floor(draft.updated_at || 0)}`,
    alt: '', draggable: false,
  });
  const dot = el('div', { class: 'focal-dot' });
  surface.append(img, dot);

  function applyDot() {
    const f = draft.background_focal || [0.5, 0.5];
    dot.style.left = `${f[0] * 100}%`;
    dot.style.top = `${f[1] * 100}%`;
  }
  applyDot();

  function setFromEvent(e) {
    if (surface.classList.contains('disabled')) return;
    const r = surface.getBoundingClientRect();
    const x = Math.max(0, Math.min(1, (e.clientX - r.left) / r.width));
    const y = Math.max(0, Math.min(1, (e.clientY - r.top) / r.height));
    draft.background_focal = [x, y];
    applyDot();
    save();
    if (onChange) onChange();
  }

  let dragging = false;
  surface.addEventListener('pointerdown', (e) => {
    if (surface.classList.contains('disabled')) return;
    dragging = true;
    try { surface.setPointerCapture(e.pointerId); } catch {}
    setFromEvent(e);
    e.preventDefault();
  });
  surface.addEventListener('pointermove', (e) => {
    if (dragging) setFromEvent(e);
  });
  function stop() { dragging = false; }
  surface.addEventListener('pointerup', stop);
  surface.addEventListener('pointercancel', stop);

  return surface;
}


function renderModeToggle(draft, save, onChange) {
  const name = `bg-mode-${draft.id}`;
  function makeRadio(value, label) {
    const input = el('input', {
      type: 'radio',
      name, value,
      checked: (draft.background_mode || 'cover') === value,
      onChange: (e) => {
        if (!e.target.checked) return;
        draft.background_mode = value;
        save();
        if (onChange) onChange();
      },
    });
    return el('label', {}, input, label);
  }
  return el('div', { class: 'bg-mode-toggle' },
    el('span', { style: { color: 'var(--text-mute)', fontSize: '0.857rem', marginRight: '4px' } }, 'Mode:'),
    makeRadio('cover', 'Cover'),
    makeRadio('tile', 'Tile'),
  );
}


function renderSlider(label, min, max, initial, suffix, onInput) {
  const valEl = el('span', { class: 'bg-slider-val' }, `${initial}${suffix}`);
  const slider = el('input', {
    type: 'range', min: String(min), max: String(max), value: String(initial),
    oninput: (e) => {
      const v = +e.target.value;
      valEl.textContent = `${v}${suffix}`;
      onInput(v);
    },
  });
  return el('div', { class: 'bg-slider-row' },
    el('label', {}, label),
    slider,
    valEl,
  );
}


function renderLivePreview() {
  return el('div', { class: 'chat-messages-wrap bg-preview bg-live-preview' },
    el('div', { class: 'chat-bg', 'aria-hidden': 'true' },
      el('div', { class: 'chat-bg-image' }),
      el('div', { class: 'chat-bg-tint' }),
      el('div', { class: 'chat-bg-dim' }),
      el('div', { class: 'chat-bg-brighten' }),
    ),
    el('div', { class: 'preview-bubble' }, 'A bubble of chat sits over the background here.'),
  );
}


function renderThemeGrid(previewTargets) {
  const grid = el('div', { class: 'bg-theme-grid' });
  for (const t of THEMES) {
    const cell = el('button', {
      type: 'button',
      class: 'chat-messages-wrap bg-preview bg-theme-cell' +
        ((state.settings && state.settings.theme) === t.id ? ' selected' : ''),
      'data-theme': t.id,
      title: `Switch to ${t.name}`,
      onClick: async () => {
        if ((state.settings && state.settings.theme) === t.id) return;
        try {
          const patch = { theme: t.id };
          markPending('settings', 'settings', patch);
          const updated = await api.putSettings(patch);
          clearPending('settings', 'settings');
          setState({ settings: updated });
          document.documentElement.dataset.theme = updated.theme;
          // Refresh selected ring across all cells.
          for (const c of grid.querySelectorAll('.bg-theme-cell')) {
            c.classList.toggle('selected', c.dataset.theme === updated.theme);
          }
        } catch (err) { toast(`Theme save failed: ${err.message}`, 'error'); }
      },
    },
      el('div', { class: 'chat-bg', 'aria-hidden': 'true' },
        el('div', { class: 'chat-bg-image' }),
        el('div', { class: 'chat-bg-tint' }),
        el('div', { class: 'chat-bg-dim' }),
        el('div', { class: 'chat-bg-brighten' }),
      ),
      el('div', { class: 'bg-theme-cell-label' }, t.name),
    );
    previewTargets.push(cell);
    grid.append(cell);
  }
  return grid;
}


/* Apply the same CSS variables / data attributes the chat pane uses to a
 * preview element, given a scenario draft. Mirrors ``applyScenarioBackground``
 * in views/chat.js but reads directly from a draft (live, in-flight changes)
 * rather than the persisted state. */
function applyBackgroundVars(elt, draft) {
  if (!draft.background_image) {
    elt.removeAttribute('data-has-bg');
    elt.removeAttribute('data-mode');
    return;
  }
  const v = Math.floor(draft.updated_at || 0);
  const url = `/api/files/scenarios/${draft.id}/background?v=${v}`;
  const focal = draft.background_focal || [0.5, 0.5];
  elt.setAttribute('data-has-bg', 'true');
  elt.setAttribute('data-mode', draft.background_mode || 'cover');
  elt.style.setProperty('--bg-image', `url("${url}")`);
  elt.style.setProperty('--bg-focal-x', `${(focal[0] * 100).toFixed(2)}%`);
  elt.style.setProperty('--bg-focal-y', `${(focal[1] * 100).toFixed(2)}%`);
  elt.style.setProperty('--bg-blur', `${draft.background_blur || 0}px`);
  elt.style.setProperty('--bg-dim', `${(draft.background_dim || 0) / 100}`);
  elt.style.setProperty('--bg-brighten', `${(draft.background_brighten || 0) / 100}`);
  elt.style.setProperty('--bg-tint-strength', `${(draft.background_tint_strength || 0) / 100}`);
}


/* Module-level handle on the currently-open editor's draft + autosave
 * trigger. See ``static/views/contacts.js`` for the rationale. */
let _openDraft = null;
let _openDraftSave = null;

let _subscribed = false;
let _lastDetailId = null;
export function setupScenariosTab() {
  if (_subscribed) return;
  _subscribed = true;
  subscribe(() => {
    refreshList();
    if (state.activeScenarioId !== _lastDetailId) refreshDetail();
  });
}
