/* Context Presets tab: list + detail editor for Generic-mode prompt
 * templates.
 *
 * A preset has:
 *   - identity fields (name / description / author / prefix-names)
 *   - optional avatar + card image (same plumbing as brain libraries)
 *   - ``system_prompt_blocks: ContextPresetBlock[]`` — joined with \\n,
 *     empty-after-strip entries dropped
 *   - ``additional_messages: ContextPresetAdditionalMessage[]`` — emitted
 *     after the system message; "floating" messages stable-sort to the
 *     tail by depth (10 first, 0 last)
 *
 * The editor live-renders the prompt via ``api.previewContextPreset`` so
 * the author can see the materialised output as they author it. Macro
 * help is wired through ``api.getMacros``, opened from the toolbar's
 * "Macros" button.
 */

import { api } from '../api.js';
import { state, setState, subscribe } from '../state.js';
import {
  el, downloadFromResponse, sortControls, sortEntities,
  dirtyDot, makeAutoSaver, favoriteStar, applyCrop,
} from '../util.js';
import { icon, confirmModal, toast, openModal, closeModal, helpDetails } from '../ui.js';
import { makeSelect } from '../avatar_picker.js';
import { saveWithConflictHandling } from '../conflict.js';
import { markPending, clearPending } from '../save_queue.js';
import { openCropModal } from './crop_modal.js';
import { mediaBlock, renderCardImageBlock } from './contacts.js';
import { createVirtList } from '../virt_list.js';
import { runImport } from './import_progress.js';


/* ===========================================================================
 * Tab — list + detail
 * =========================================================================== */


export function renderContextPresetsTab(container) {
  container.replaceChildren();
  const pane = el('div', { class: 'list-pane' });
  pane.append(renderListHeader());
  const body = el('div', { class: 'list-body', id: 'context-preset-list-body' });
  pane.append(body);
  container.append(pane);
  const detail = el('div', { class: 'content-pane', id: 'context-preset-detail' });
  container.append(detail);
  mountList(body);
  refreshDetail();
}


export function setupContextPresetsTab() {
  // Re-render when list changes, when the active preset changes, or when
  // the settings provider_mode flips (the latter only affects rail
  // visibility, but if the user lands on this tab and then flips to AER
  // mode we want any open editor to stay coherent — it's a free re-render
  // and the tab won't be visible anyway).
  subscribe(() => {
    if (state.activeTab !== 'contextPresets') return;
    if (state.activeContextPresetId !== _lastDetailId) {
      refreshDetail();
    }
    refreshList();
  });
}


let _searchTerm = '';
let _virt = null;
let _visible = [];
let _emptyEl = null;
let _emptyMatchEl = null;
let _lastDetailId = null;
let _openDraft = null;
let _openDraftSave = null;

function renderListHeader() {
  const search = el('input', {
    class: 'list-search',
    type: 'text',
    placeholder: 'Search presets…',
    value: _searchTerm,
    oninput: (e) => { _searchTerm = e.target.value.toLowerCase(); refreshList(); },
  });
  const sort = sortControls(
    state.contextPresetSort || { mode: 'added', direction: 'desc' },
    (v) => {
      setState({ contextPresetSort: v });
      try { localStorage.setItem('contextPresetSort', JSON.stringify(v)); } catch {}
      refreshList();
    },
  );
  const importInput = el('input', {
    id: 'context-preset-import-input', type: 'file',
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
  return el('div', { class: 'list-header' },
    el('div', { style: { display: 'flex', justifyContent: 'space-between', alignItems: 'center' } },
      el('h2', {}, 'Context presets'),
      el('div', { style: { display: 'flex', gap: '6px' } },
        el('label', {
          class: 'btn',
          for: 'context-preset-import-input',
          title: 'Import context preset',
          style: { cursor: 'pointer' },
        }, icon('upload', 14)),
        importInput,
        el('button', {
          class: 'btn primary',
          title: 'New context preset',
          onClick: async () => {
            try {
              const p = await api.createContextPreset({ name: 'New context preset' });
              setState({
                contextPresets: await api.listContextPresets(),
                activeContextPresetId: p.id,
              });
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


function _matchesSearch(p) {
  if (!_searchTerm) return true;
  const hay = `${p.name || ''} ${p.author || ''} ${p.description || ''}`.toLowerCase();
  return hay.includes(_searchTerm);
}


function mountList(body) {
  _emptyEl = el('div', { class: 'list-empty', style: { display: 'none' } },
    'No context presets yet.');
  _emptyMatchEl = el('div', { class: 'list-empty', style: { display: 'none' } },
    'No presets match your search.');
  _virt = createVirtList(body, {
    count: 0,
    getItem: (i) => _visible[i] || null,
    renderItem: (p) => renderRow(p),
    estimatedHeight: 60,
    observeRows: false,
  });
  body.appendChild(_emptyEl);
  body.appendChild(_emptyMatchEl);
  refreshList();
}


function renderRow(p) {
  const isActive = state.activeContextPresetId === p.id;
  const blockCount = p.block_count != null ? p.block_count : 0;
  const msgCount = p.message_count != null ? p.message_count : 0;
  const blockBadge = el('span', {
    class: 'row-msg-count row-inline-badge',
    title: `${blockCount} block${blockCount === 1 ? '' : 's'} · ${msgCount} message${msgCount === 1 ? '' : 's'}`,
  }, `${blockCount}/${msgCount}`);
  const avatarEl = el('div', { class: 'avatar' });
  if (p.avatar) {
    avatarEl.append(el('img', {
      src: `/api/files/context-presets/${p.id}/avatar/display?v=${Math.floor(p.updated_at || 0)}`,
      alt: p.name,
    }));
    applyCrop(avatarEl, null);
  } else {
    avatarEl.textContent = (p.name || '?').slice(0, 1).toUpperCase();
  }
  return el('div', {
    class: `list-row ${isActive ? 'active' : ''}`,
    title: (p.description || '').trim(),
    onClick: () => setState({ activeContextPresetId: p.id }),
  },
    avatarEl,
    el('div', { class: 'row-text' },
      el('div', { class: 'row-title' },
        el('span', { class: 'row-title-name-group' },
          el('span', { class: 'row-title-text' }, p.name),
          blockBadge,
        ),
        favoriteStar(p, async (next) => {
          if (_openDraft && _openDraftSave && _openDraft.id === p.id) {
            _openDraft.favorite = next;
            _openDraftSave();
            return;
          }
          try {
            const updated = await api.setContextPresetFavorite(p.id, next);
            setState({
              contextPresets: (state.contextPresets || []).map(
                x => x.id === updated.id ? updated : x,
              ),
            });
          } catch (err) { toast(err.message, 'error'); }
        }),
      ),
      el('div', { class: 'row-sub' }, (p.description || '').trim()),
    ),
  );
}


function refreshList() {
  if (!_virt) return;
  const all = state.contextPresets || [];
  if (all.length === 0) {
    _visible = [];
    _emptyEl.style.display = '';
    _emptyMatchEl.style.display = 'none';
    _virt.setCount(0);
    return;
  }
  const ss = state.contextPresetSort || { mode: 'added', direction: 'desc' };
  _visible = sortEntities(
    all.filter(_matchesSearch),
    ss.mode,
    undefined,                     // no last-used map: presets aren't chat-attached
    ss.direction,
    { favoritesFirst: !!ss.favoritesFirst },
  );
  _emptyEl.style.display = 'none';
  _emptyMatchEl.style.display = _visible.length === 0 ? '' : 'none';
  _virt.setCount(_visible.length, { preserveScroll: true });
}


let _refreshDetailToken = 0;

async function refreshDetail() {
  const detail = document.getElementById('context-preset-detail');
  if (!detail) return;
  _lastDetailId = state.activeContextPresetId;
  if (!state.activeContextPresetId) {
    detail.replaceChildren();
    _openDraft = null;
    _openDraftSave = null;
    detail.append(el('div', { class: 'content-empty' },
      el('div', {},
        el('h3', {}, 'Pick a context preset'),
        el('div', {}, 'Select one on the left to edit it.'),
      ),
    ));
    return;
  }
  const oldScroll = detail.querySelector('.page-scroll');
  const prevScrollTop = oldScroll ? oldScroll.scrollTop : 0;
  const token = ++_refreshDetailToken;
  const requestedId = state.activeContextPresetId;
  let p;
  try {
    p = await api.getContextPreset(requestedId);
  } catch (err) {
    if (token !== _refreshDetailToken) return;
    toast(err.message, 'error');
    return;
  }
  if (token !== _refreshDetailToken || state.activeContextPresetId !== requestedId) return;
  if (!p) { setState({ activeContextPresetId: null }); return; }
  detail.replaceChildren();
  detail.append(renderEditView(p));
  const newScroll = detail.querySelector('.page-scroll');
  if (newScroll) newScroll.scrollTop = prevScrollTop;
}


/* ===========================================================================
 * Editor
 * =========================================================================== */


function renderEditView(preset) {
  let draft = JSON.parse(JSON.stringify(preset));
  // Ensure ids on every block / message so the reorder buttons have
  // stable handles. Server defaults to ``new_id`` but legacy payloads
  // may have skipped them.
  for (const b of draft.system_prompt_blocks || []) {
    if (!b.id) b.id = _localId();
  }
  for (const m of draft.additional_messages || []) {
    if (!m.id) m.id = _localId();
    for (const b of (m.blocks || [])) {
      if (!b.id) b.id = _localId();
    }
  }

  const dot = dirtyDot();
  const save = makeAutoSaver(async () => {
    markPending('contextPreset', draft.id, draft);
    await saveWithConflictHandling({
      draft,
      saveFn: (d) => api.updateContextPreset(d.id, d),
      getFn: () => api.getContextPreset(draft.id),
      entityLabel: 'Context preset',
      onReload: async (latest) => {
        Object.assign(draft, latest);
        setState({ contextPresets: await api.listContextPresets() });
        _lastDetailId = null;
        refreshDetail();
      },
    });
    clearPending('contextPreset', draft.id);
    setState({ contextPresets: await api.listContextPresets() });
    schedulePreview();
  }, dot, 400);

  _openDraft = draft;
  _openDraftSave = save;

  // ----- Header (Name + actions) -----

  const header = el('div', { class: 'page-header' });
  function refreshHeader() {
    const items = [
      el('h2', {}, dot, draft.name),
      el('span', { class: 'uuid' }, draft.id),
      el('div', { class: 'spacer' }),
      el('button', {
        class: 'btn',
        title: 'Show macro reference',
        onClick: () => openMacrosHelpModal(),
      }, icon('info', 14), 'Macros'),
      el('button', {
        class: 'btn',
        onClick: async () => {
          const r = await api.exportContextPreset(draft.id);
          await downloadFromResponse(r, `${draft.name}-preset.json`);
        },
      }, icon('download', 14), 'Export'),
    ];
    if (draft.card_image) {
      items.push(el('button', {
        class: 'btn',
        title: 'Export as PNG card with embedded data',
        onClick: async () => {
          const r = await api.exportContextPresetCard(draft.id);
          await downloadFromResponse(r, `${draft.name}-preset.png`);
        },
      }, icon('download', 14), 'Export card'));
    }
    items.push(el('button', {
      class: 'btn',
      title: 'Create a copy of this context preset',
      onClick: async () => {
        const copy = await api.duplicateContextPreset(draft.id);
        setState({
          contextPresets: await api.listContextPresets(),
          activeContextPresetId: copy.id,
        });
      },
    }, icon('copy', 14), 'Duplicate'));
    items.push(el('button', {
      class: 'btn danger',
      onClick: async () => {
        const ok = await confirmModal(
          'Delete context preset?',
          `${draft.name} will be removed. Provider configs still pointing at it will resolve to no preset until you pick another one.`,
          { danger: true },
        );
        if (!ok) return;
        await api.deleteContextPreset(draft.id);
        setState({
          contextPresets: await api.listContextPresets(),
          activeContextPresetId: null,
        });
      },
    }, icon('trash', 14), 'Delete'));
    header.replaceChildren(...items);
  }
  refreshHeader();

  function setName(v) {
    draft.name = v;
    header.querySelector('h2').replaceChildren(dot, draft.name);
  }

  // ----- General section -----

  const generalSection = el('div', { class: 'section' },
    el('h3', {}, 'Information'),
    el('div', { class: 'form-grid' },
      el('div', { class: 'form-group' },
        el('label', {}, 'Name'),
        el('input', {
          type: 'text', value: draft.name,
          oninput: (e) => { setName(e.target.value); save(); },
        }),
      ),
      el('div', { class: 'form-group' },
        el('label', {}, 'Author'),
        el('input', {
          type: 'text', value: draft.author || '',
          oninput: (e) => { draft.author = e.target.value; save(); },
        }),
      ),
    ),
    el('div', { class: 'form-group', style: { marginTop: '18px' } },
      el('label', {}, 'Description'),
      (() => {
        const t = el('textarea', {
          rows: 3,
          oninput: (e) => { draft.description = e.target.value; save(); },
        });
        t.value = draft.description || '';
        return t;
      })(),
    ),
    (() => {
      const cbId = `prefix-names-${draft.id}`;
      const cb = el('input', {
        type: 'checkbox', id: cbId,
        onchange: (e) => { draft.prefix_names = e.target.checked; save(); },
      });
      cb.checked = !!draft.prefix_names;
      return el('div', {
        class: 'form-group checkbox', style: { marginTop: '14px' },
      },
        cb,
        el('label', { for: cbId }, 'Prefix names'),
        helpDetails(
          el('div', {},
            'When on, each chat message in the API payload is prefixed with ',
            el('code', {}, 'Name:\\n'),
            ' so the model can tell speakers apart. Turn off when the upstream'
            + ' already wraps each message with role metadata that makes the prefix redundant.',
          ),
          { label: 'About prefix names' },
        ),
      );
    })(),
  );

  // ----- Avatar + card image -----

  const avatarSection = el('div', { class: 'section' },
    el('h3', {}, 'Avatar & card image'),
    el('div', { class: 'media-row' },
      mediaBlock('Avatar', renderContextPresetAvatarBlock(draft)),
      mediaBlock('Card image', renderCardImageBlock(
        draft, 'context-preset', () => refreshHeader(),
      )),
    ),
  );

  // ----- System Prompt panel -----

  const systemPanel = el('div', { class: 'section' });
  function rerenderSystemPanel() {
    systemPanel.replaceChildren(el('div', { class: 'help-row' },
      el('h3', {}, 'System prompt'),
      helpDetails([
        el('div', {},
          'Blocks are joined with newlines into the system message. ',
          'Disable a block to park it without losing the content.'),
        el('ul', {},
          el('li', {}, el('strong', {}, 'Enabled'), ' — toggle to include / exclude from the prompt.'),
          el('li', {}, 'Drag the grip handle to reorder, or use the ↑ / ↓ buttons.'),
        ),
      ], { label: 'About system-prompt blocks' }),
    ));
    const blocks = draft.system_prompt_blocks || [];
    const dragGroup = `sys:${draft.id}`;
    blocks.forEach((b, i) => {
      systemPanel.append(renderBlockCard({
        block: b,
        index: i,
        total: blocks.length,
        onMove: (dir) => {
          const j = i + dir;
          if (j < 0 || j >= blocks.length) return;
          [blocks[i], blocks[j]] = [blocks[j], blocks[i]];
          save();
          rerenderSystemPanel();
        },
        onDelete: async () => {
          if ((b.content || '').trim()) {
            const ok = await confirmModal(
              'Delete block?',
              `${(b.name || 'Untitled')} will be permanently removed.`,
              { danger: true },
            );
            if (!ok) return;
          }
          blocks.splice(i, 1);
          save();
          rerenderSystemPanel();
        },
        onChange: () => { save(); schedulePreview(); },
        onToggleEnabled: () => {
          save();
          schedulePreview();
        },
        dragGroup,
        onReorder: (srcId, before) => {
          _reorderById(blocks, srcId, b.id, before);
          save();
          rerenderSystemPanel();
        },
      }));
    });
    systemPanel.append(el('button', {
      class: 'btn ghost',
      style: { marginTop: '8px' },
      onClick: () => {
        blocks.push({
          id: _localId(),
          name: 'New block',
          enabled: true,
          content: '',
        });
        save();
        rerenderSystemPanel();
      },
    }, icon('plus', 14), ' Add block'));
  }
  rerenderSystemPanel();

  // ----- Preview pane -----

  const previewPane = el('div', { class: 'section' });
  let previewTimer = null;
  let previewSeq = 0;

  // ``_latestPreviewMessages`` mirrors the most recent ``preview``
  // response's additional_messages list. ``null`` means the first
  // preview round-trip hasn't returned yet — keep the per-card body
  // on its "(preview loading…)" placeholder rather than overwriting
  // with the misleading "(empty / disabled)" message.
  let _latestPreviewMessages = null;

  function _previewForMsg(msgId) {
    if (!_latestPreviewMessages) return null;
    return _latestPreviewMessages.find(m => m.id === msgId) || null;
  }

  function _paintMessagePreviewBox(box, matched) {
    box.replaceChildren();
    if (!matched) {
      box.append(el('div', { class: 'help-text' },
        '(empty or disabled — nothing reaches the API for this message)'));
      return;
    }
    const lines = (matched.content || '').split('\n').length + 1;
    const ta = el('textarea', {
      class: 'preview-text',
      rows: Math.max(2, Math.min(12, lines)),
      readonly: true,
    });
    ta.value = matched.content || '';
    box.append(ta);
  }

  function _repaintAllMessagePreviews() {
    // Skip while the first preview round-trip is in flight — the
    // initial "(preview loading…)" placeholder is the right thing to
    // show before any server response.
    if (_latestPreviewMessages === null) return;
    const boxes = messagesArea.querySelectorAll('[data-msg-preview-id]');
    for (const box of boxes) {
      _paintMessagePreviewBox(box, _previewForMsg(box.dataset.msgPreviewId));
    }
  }

  function rerenderPreview(payload) {
    previewPane.replaceChildren(el('div', { class: 'help-row' },
      el('h3', {}, 'Preview'),
      helpDetails([
        el('div', {}, 'Rendered with built-in dummy values for contact, user, scenario, and chat fields.'),
        el('div', {}, 'Additional messages render in their own per-message preview panel below.'),
      ], { label: 'About preview' }),
    ));
    const sysTa = el('textarea', {
      class: 'preview-text',
      rows: Math.max(8, Math.min(24, (payload.system_prompt || '').split('\n').length + 1)),
      readonly: true,
    });
    sysTa.value = payload.system_prompt || '';
    previewPane.append(el('div', { class: 'form-group' },
      el('label', {}, 'System message'),
      sysTa,
    ));
    _latestPreviewMessages = payload.additional_messages || [];
    _repaintAllMessagePreviews();
  }

  function schedulePreview() {
    if (previewTimer) clearTimeout(previewTimer);
    previewTimer = setTimeout(async () => {
      const myTurn = ++previewSeq;
      try {
        const payload = await api.previewContextPreset({ preset: draft });
        if (myTurn !== previewSeq) return;
        rerenderPreview(payload);
      } catch (err) {
        if (myTurn !== previewSeq) return;
        rerenderPreview({ system_prompt: `(preview failed: ${err.message})`, additional_messages: [] });
      }
    }, 300);
  }
  schedulePreview();

  // ----- Additional Messages area -----
  //
  // ``messagesArea`` is the container that holds the section header
  // panel, every per-message ``.section`` panel, and the trailing
  // "+ Add message" row. Each rerender wipes and repopulates it so
  // structural changes (add / delete / float-toggle) just stamp out
  // a fresh list of sibling panels — no nested container around the
  // cards means each panel reads as a standalone block, like the
  // top-level "Information" / "Avatar & card image" sections.

  const messagesArea = el('div', { class: 'context-preset-msg-area' });
  function rerenderMessagesPanel() {
    messagesArea.replaceChildren();
    const headerPanel = el('div', { class: 'section context-preset-msg-header' },
      el('div', { class: 'help-row' },
        el('h3', {}, 'Additional messages'),
        helpDetails([
          el('div', {},
            'Messages sent to the model after the system message. ',
            'Each has a speaker role and either a single body or joined sub-blocks.'),
          el('ul', {},
            el('li', {}, el('strong', {}, 'Enabled'), ' — toggle to include or park the message without deleting it.'),
            el('li', {}, el('strong', {}, 'Float'), ' — when on, the message is pulled out of the authored order and ',
              'inserted near the tail of the conversation, sorted by depth. ',
              'Higher depth lands earlier (depth 10 first, depth 0 last).'),
            el('li', {}, el('strong', {}, 'Role'), ' — speaker role on the API payload (', el('code', {}, 'system'),
              ' / ', el('code', {}, 'user'), ' / ', el('code', {}, 'assistant'), ').'),
            el('li', {}, el('strong', {}, 'Mode'), ' — ', el('code', {}, 'simple'),
              ' is a single textarea; ', el('code', {}, 'blocks'), ' lets you compose joinable sub-blocks like the system prompt.'),
            el('li', {}, 'Drag the grip handle to reorder within the same bucket ',
              '(static, or float-at-same-depth), or use ↑ / ↓.'),
          ),
        ], { label: 'About additional messages' }),
      ),
    );
    messagesArea.append(headerPanel);
    const msgs = draft.additional_messages || [];
    // Compute the display order: float-disabled first (authored), then
    // float-enabled grouped by depth desc.
    const staticMsgs = [];
    const floatMsgs = [];
    msgs.forEach((m, i) => {
      const entry = { msg: m, idx: i };
      if (m.float_enabled) floatMsgs.push(entry);
      else staticMsgs.push(entry);
    });
    floatMsgs.sort((a, b) => b.msg.float_depth - a.msg.float_depth);
    const displayed = [...staticMsgs, ...floatMsgs];

    displayed.forEach((entry, displayIdx) => {
      const isFirstFloat =
        entry.msg.float_enabled
        && (displayIdx === 0 || !displayed[displayIdx - 1].msg.float_enabled);
      if (isFirstFloat && staticMsgs.length > 0 && floatMsgs.length > 0) {
        messagesArea.append(el('hr', { class: 'float-divider' }));
      }
      // Drag group: messages can only reorder within the same bucket —
      // static together, and float-at-the-same-depth together. Cross-bucket
      // moves would require flipping float_enabled / float_depth, which is
      // surprising; the toggles and slider exist for that.
      const dragGroup = entry.msg.float_enabled
        ? `msgs:${draft.id}:float:${entry.msg.float_depth || 0}`
        : `msgs:${draft.id}:static`;
      messagesArea.append(renderMessageCard({
        msg: entry.msg,
        authoredIdx: entry.idx,
        // Move within the displayed group: for static section,
        // swaps authored-list neighbours of float-disabled entries; for
        // float section, swaps neighbours within the same depth.
        canMoveUp: _canMoveDisplayed(displayed, displayIdx, -1),
        canMoveDown: _canMoveDisplayed(displayed, displayIdx, +1),
        onMove: (dir) => {
          const target = displayed[displayIdx + dir];
          if (!target) return;
          // Swap by authored index.
          const a = entry.idx, b = target.idx;
          [msgs[a], msgs[b]] = [msgs[b], msgs[a]];
          save();
          rerenderMessagesPanel();
        },
        onDelete: async () => {
          if (
            (entry.msg.simple_content || '').trim()
            || (entry.msg.blocks || []).some(b => (b.content || '').trim())
          ) {
            const ok = await confirmModal(
              'Delete message?',
              `${(entry.msg.name || 'Untitled')} will be permanently removed.`,
              { danger: true },
            );
            if (!ok) return;
          }
          msgs.splice(entry.idx, 1);
          save();
          rerenderMessagesPanel();
        },
        onStructuralChange: () => { save(); rerenderMessagesPanel(); },
        onFieldChange: () => { save(); schedulePreview(); },
        dragGroup,
        onReorder: (srcId, before) => {
          _reorderById(msgs, srcId, entry.msg.id, before);
          save();
          rerenderMessagesPanel();
        },
      }));
    });

    // "+ Add message" lives at the bottom of the area, NOT inside any
    // section panel, so it visually anchors the bottom of the list.
    messagesArea.append(el('div', { class: 'context-preset-msg-add' },
      el('button', {
        class: 'btn ghost',
        onClick: () => {
          msgs.push({
            id: _localId(),
            name: 'New message',
            enabled: true,
            role: 'system',
            mode: 'simple',
            simple_content: '',
            blocks: [],
            float_enabled: false,
            float_depth: 0,
          });
          save();
          rerenderMessagesPanel();
          // Repaint previews for the newly-mounted card from the latest
          // cached payload (so the empty/disabled placeholder appears
          // immediately) and schedule a fresh preview fetch.
          _repaintAllMessagePreviews();
          schedulePreview();
        },
      }, icon('plus', 14), ' Add message'),
    ));
    // After a structural rerender, fan the latest preview output into
    // the freshly-mounted preview boxes so existing messages don't
    // briefly show the empty-placeholder before the next debounced
    // schedulePreview round-trip returns.
    _repaintAllMessagePreviews();
  }
  rerenderMessagesPanel();

  // ----- Assemble -----

  return el('div', { style: { display: 'flex', flexDirection: 'column', height: '100%' } },
    header,
    el('div', { class: 'page-scroll' },
      el('div', { class: 'page-body' },
        generalSection, avatarSection, systemPanel, previewPane, messagesArea,
      ),
    ),
  );
}


function _canMoveDisplayed(displayed, idx, dir) {
  const here = displayed[idx];
  const there = displayed[idx + dir];
  if (!there) return false;
  // Same group (both static or both float) AND same float_depth if float.
  if (here.msg.float_enabled !== there.msg.float_enabled) return false;
  if (here.msg.float_enabled && here.msg.float_depth !== there.msg.float_depth) return false;
  return true;
}


/* Reorder ``arr`` by moving the item whose ``.id === srcId`` next to the
 * item whose ``.id === targetId``. ``before`` chooses which side. Used by
 * the HTML5 drag-drop handler on each card. */
function _reorderById(arr, srcId, targetId, before) {
  if (srcId === targetId) return;
  const srcIdx = arr.findIndex(x => x.id === srcId);
  if (srcIdx < 0) return;
  const [item] = arr.splice(srcIdx, 1);
  const tgtIdx = arr.findIndex(x => x.id === targetId);
  if (tgtIdx < 0) {
    // Target gone (shouldn't happen) — reinsert at original-ish position.
    arr.splice(Math.min(srcIdx, arr.length), 0, item);
    return;
  }
  arr.splice(before ? tgtIdx : tgtIdx + 1, 0, item);
}


/* Wire HTML5 drag-and-drop on a card. The grip element is the drag
 * source (so text-selection in inputs stays unaffected); ``group`` is
 * an opaque key — only drops between cards sharing the same group take
 * effect. ``getId()`` resolves the card's current id (closure-captured
 * so it survives rerenders) and ``onDrop(srcId, before)`` performs the
 * reorder. The card's drop position is computed from the cursor's Y
 * relative to the card's midline. */
const _DT_ID = 'application/x-aether-id';
const _DT_GROUP = 'application/x-aether-group';

function _attachDrag(card, gripEl, group, getId, onDrop) {
  gripEl.setAttribute('draggable', 'true');
  gripEl.addEventListener('dragstart', (e) => {
    if (!e.dataTransfer) return;
    e.dataTransfer.effectAllowed = 'move';
    try {
      e.dataTransfer.setData(_DT_ID, getId());
      e.dataTransfer.setData(_DT_GROUP, group);
    } catch {}
    if (e.dataTransfer.setDragImage) {
      e.dataTransfer.setDragImage(card, 12, 12);
    }
    card.classList.add('dragging');
  });
  gripEl.addEventListener('dragend', () => {
    card.classList.remove('dragging');
    card.classList.remove('drop-before', 'drop-after');
  });
  card.addEventListener('dragover', (e) => {
    if (!e.dataTransfer) return;
    // Group / id are only readable inside ``drop`` (security). The best
    // we can do here is allow-drop optimistically and let drop() bail
    // when the group doesn't match.
    const types = e.dataTransfer.types;
    if (!types || (!types.includes(_DT_ID) && !types.includes(_DT_GROUP))) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
    const rect = card.getBoundingClientRect();
    const before = e.clientY < rect.top + rect.height / 2;
    card.classList.toggle('drop-before', before);
    card.classList.toggle('drop-after', !before);
  });
  card.addEventListener('dragleave', (e) => {
    // Only clear when leaving the card itself (avoid flicker across
    // child elements).
    if (e.relatedTarget && card.contains(e.relatedTarget)) return;
    card.classList.remove('drop-before', 'drop-after');
  });
  card.addEventListener('drop', (e) => {
    if (!e.dataTransfer) return;
    card.classList.remove('drop-before', 'drop-after');
    const srcGroup = e.dataTransfer.getData(_DT_GROUP);
    if (srcGroup !== group) return;
    const srcId = e.dataTransfer.getData(_DT_ID);
    if (!srcId) return;
    e.preventDefault();
    const rect = card.getBoundingClientRect();
    const before = e.clientY < rect.top + rect.height / 2;
    onDrop(srcId, before);
  });
}


/* Eye / eye-off icon toggle for "Enabled" — matches brain_row's
 * brain-disabled-toggle pattern (active = disabled).
 * ``label`` is the entity word used in the title attribute. */
function _enabledToggle(getEnabled, setEnabled, label) {
  const btn = el('button', {
    type: 'button',
    class: 'icon-btn enabled-toggle' + (getEnabled() ? '' : ' off'),
    title: getEnabled() ? `Disable ${label}` : `Enable ${label}`,
    'aria-label': getEnabled() ? `Disable ${label}` : `Enable ${label}`,
  }, icon(getEnabled() ? 'eye' : 'eye-off', 16));
  btn.addEventListener('click', () => {
    setEnabled(!getEnabled());
    btn.classList.toggle('off', !getEnabled());
    btn.title = getEnabled() ? `Disable ${label}` : `Enable ${label}`;
    btn.setAttribute('aria-label', btn.title);
    const iconEl = btn.querySelector('svg');
    if (iconEl) iconEl.replaceWith(icon(getEnabled() ? 'eye' : 'eye-off', 16));
  });
  return btn;
}


function _gripHandle() {
  return el('span', {
    class: 'move-grip',
    title: 'Drag to reorder',
    'aria-label': 'Drag to reorder',
  }, icon('grip', 14));
}


function _localId() {
  if (window.crypto && window.crypto.randomUUID) {
    return window.crypto.randomUUID().replace(/-/g, '');
  }
  return Array.from({ length: 32 }, () => Math.floor(Math.random() * 16).toString(16)).join('');
}


/* ===========================================================================
 * Block card (used inside System Prompt + blocks-mode messages)
 * =========================================================================== */


function renderBlockCard({
  block, index, total, onMove, onDelete, onChange, onToggleEnabled,
  dragGroup, onReorder,
}) {
  const card = el('div', { class: 'context-preset-block' + (block.enabled ? '' : ' disabled') });
  const nameInput = el('input', {
    type: 'text',
    value: block.name || '',
    placeholder: 'Block name',
    oninput: (e) => { block.name = e.target.value; onChange(); },
    style: { flex: '1', minWidth: '0' },
  });
  const enabledBtn = _enabledToggle(
    () => !!block.enabled,
    (v) => { block.enabled = v; card.classList.toggle('disabled', !v); onToggleEnabled(); },
    'block',
  );
  const grip = _gripHandle();
  const upBtn = el('button', {
    class: 'btn ghost icon-btn',
    title: 'Move up', disabled: index === 0,
    onClick: () => onMove(-1),
  }, '↑');
  const downBtn = el('button', {
    class: 'btn ghost icon-btn',
    title: 'Move down', disabled: index === total - 1,
    onClick: () => onMove(+1),
  }, '↓');
  const delBtn = el('button', {
    class: 'btn ghost danger icon-btn',
    title: 'Delete block',
    onClick: onDelete,
  }, '✕');

  const headerRow = el('div', { class: 'context-preset-block-header' },
    grip, nameInput, enabledBtn, upBtn, downBtn, delBtn,
  );
  const contentTa = el('textarea', {
    class: 'context-preset-block-content',
    rows: 6,
    oninput: (e) => { block.content = e.target.value; onChange(); },
  });
  contentTa.value = block.content || '';
  card.append(headerRow, contentTa);

  if (dragGroup && onReorder) {
    _attachDrag(card, grip, dragGroup, () => block.id, (srcId, before) => onReorder(srcId, before));
  }
  return card;
}


/* ===========================================================================
 * Additional-message card
 * =========================================================================== */


function renderMessageCard({
  msg, authoredIdx, canMoveUp, canMoveDown,
  onMove, onDelete, onStructuralChange, onFieldChange,
  dragGroup, onReorder,
}) {
  // Each message lives in its own ``.section`` panel — top-level
  // sibling of "Information" / "Avatar" / "System prompt" / "Preview".
  // The ``context-preset-message`` class still applies the
  // disabled-dim + drop-indicator + body wiring.
  const card = el('div', {
    class: 'section context-preset-message' + (msg.enabled ? '' : ' disabled'),
  });

  const grip = _gripHandle();
  const nameInput = el('input', {
    type: 'text',
    value: msg.name || '',
    placeholder: 'Message name',
    oninput: (e) => { msg.name = e.target.value; onFieldChange(); },
    style: { flex: '1', minWidth: '0' },
  });
  const enabledBtn = _enabledToggle(
    () => !!msg.enabled,
    (v) => { msg.enabled = v; card.classList.toggle('disabled', !v); onFieldChange(); },
    'message',
  );
  // Float toggle — re-renders the panel so the card resorts into the
  // correct bucket. ``onStructuralChange`` triggers ``rerenderMessagesPanel``.
  const floatBtn = el('button', {
    type: 'button',
    class: 'icon-btn float-toggle' + (msg.float_enabled ? ' on' : ''),
    title: msg.float_enabled
      ? 'Floating — pinned near message tail by depth'
      : 'Static — emitted in authored order',
    'aria-label': msg.float_enabled ? 'Disable float' : 'Enable float',
    onClick: () => {
      msg.float_enabled = !msg.float_enabled;
      onStructuralChange();
    },
  }, icon('wave', 16));
  const upBtn = el('button', {
    class: 'btn ghost icon-btn',
    title: 'Move up', disabled: !canMoveUp,
    onClick: () => onMove(-1),
  }, '↑');
  const downBtn = el('button', {
    class: 'btn ghost icon-btn',
    title: 'Move down', disabled: !canMoveDown,
    onClick: () => onMove(+1),
  }, '↓');
  const delBtn = el('button', {
    class: 'btn ghost danger icon-btn',
    title: 'Delete message',
    onClick: onDelete,
  }, '✕');

  const headerRow = el('div', { class: 'context-preset-message-header' },
    grip, nameInput, enabledBtn, floatBtn, upBtn, downBtn, delBtn,
  );
  card.append(headerRow);

  // Body — simple vs blocks.
  if (msg.mode === 'simple') {
    const ta = el('textarea', {
      class: 'context-preset-block-content',
      rows: 5,
      oninput: (e) => { msg.simple_content = e.target.value; onFieldChange(); },
    });
    ta.value = msg.simple_content || '';
    card.append(ta);
  } else {
    const blocksWrap = el('div', { class: 'context-preset-inner-blocks' });
    const blocks = msg.blocks = msg.blocks || [];
    const innerDragGroup = `msg-inner:${msg.id}`;
    function rerenderInner() {
      blocksWrap.replaceChildren();
      blocks.forEach((b, i) => {
        blocksWrap.append(renderBlockCard({
          block: b,
          index: i,
          total: blocks.length,
          onMove: (dir) => {
            const j = i + dir;
            if (j < 0 || j >= blocks.length) return;
            [blocks[i], blocks[j]] = [blocks[j], blocks[i]];
            onFieldChange();
            rerenderInner();
          },
          onDelete: async () => {
            if ((b.content || '').trim()) {
              const ok = await confirmModal(
                'Delete block?',
                `${(b.name || 'Untitled')} will be removed from this message.`,
                { danger: true },
              );
              if (!ok) return;
            }
            blocks.splice(i, 1);
            onFieldChange();
            rerenderInner();
          },
          onChange: onFieldChange,
          onToggleEnabled: onFieldChange,
          dragGroup: innerDragGroup,
          onReorder: (srcId, before) => {
            _reorderById(blocks, srcId, b.id, before);
            onFieldChange();
            rerenderInner();
          },
        }));
      });
      blocksWrap.append(el('button', {
        class: 'btn ghost',
        style: { marginTop: '6px' },
        onClick: () => {
          blocks.push({
            id: _localId(), name: 'New block', enabled: true, content: '',
          });
          onFieldChange();
          rerenderInner();
        },
      }, icon('plus', 14), ' Add block'));
    }
    rerenderInner();
    card.append(blocksWrap);
  }

  // ----- Footer — role, mode, depth (when float) -----

  const roleSelect = makeSelect({
    value: msg.role || 'system',
    options: [
      { value: 'system', label: 'System' },
      { value: 'user', label: 'User' },
      { value: 'assistant', label: 'Assistant' },
    ],
    onChange: (v) => { msg.role = v; onFieldChange(); },
  });
  const modeSelect = makeSelect({
    value: msg.mode || 'simple',
    options: [
      { value: 'simple', label: 'Simple' },
      { value: 'blocks', label: 'Blocks' },
    ],
    onChange: (v) => { msg.mode = v; onStructuralChange(); },
  });
  const depthLabel = el('span', { class: 'context-preset-depth-label' },
    `depth ${msg.float_depth || 0}`);
  const depthInput = el('input', {
    type: 'range', min: '0', max: '10', step: '1',
    value: String(msg.float_depth || 0),
    title: 'Float depth — higher floats earlier (depth 10 first, depth 0 last)',
    oninput: (e) => {
      msg.float_depth = parseInt(e.target.value, 10) || 0;
      depthLabel.textContent = `depth ${msg.float_depth}`;
      onFieldChange();
    },
    onchange: () => {
      // Re-sort the display when the depth changes (which may move the
      // card to a different position in the panel).
      onStructuralChange();
    },
  });
  const footerRow = el('div', { class: 'context-preset-message-footer' },
    el('span', { class: 'context-preset-message-role' }, 'Role:', roleSelect),
    el('span', { class: 'context-preset-message-mode' }, 'Mode:', modeSelect),
  );
  if (msg.float_enabled) {
    footerRow.append(el('span', { class: 'context-preset-message-depth' },
      'Depth:', depthInput, depthLabel,
    ));
  }
  card.append(footerRow);

  // Per-message preview — collapsed by default. The body element is
  // tagged with ``data-msg-preview-id`` so the surrounding edit view's
  // ``_repaintAllMessagePreviews`` can find and refresh it from the
  // latest preview payload without re-rendering the whole card.
  const previewBody = el('div', {
    class: 'context-preset-msg-preview-body',
    'data-msg-preview-id': msg.id,
  },
    el('div', { class: 'help-text' }, '(preview loading…)'),
  );
  const previewWrap = el('details', { class: 'context-preset-msg-preview' },
    el('summary', {}, 'Preview'),
    previewBody,
  );
  card.append(previewWrap);

  if (dragGroup && onReorder) {
    _attachDrag(card, grip, dragGroup, () => msg.id, (srcId, before) => onReorder(srcId, before));
  }
  return card;
}


/* ===========================================================================
 * Macros help modal — sourced from GET /api/macros
 * =========================================================================== */


const _CATEGORY_ORDER = [
  'control_flow',
  'comparison',
  'boolean',
  'identity',
  'fields',
  'chat',
  'brains',
  'history',
  'time',
  'random',
  'runtime',
  'format',
  'other',
];

const _CATEGORY_LABELS = {
  control_flow: 'Control flow',
  comparison: 'Comparison',
  boolean: 'Boolean',
  identity: 'Identity & character',
  fields: 'Contact / user / scenario fields',
  chat: 'Chat',
  brains: 'Brains',
  history: 'Chat history',
  time: 'Date & time',
  random: 'Randomness',
  runtime: 'Runtime',
  format: 'Formatting',
  other: 'Other',
};


export async function openMacrosHelpModal() {
  let macros;
  try {
    macros = await api.getMacros();
  } catch (e) {
    toast(`Could not load macros: ${e.message}`, 'error');
    return;
  }

  // Group by category.
  const groups = new Map();
  for (const m of macros) {
    const cat = m.category || 'other';
    if (!groups.has(cat)) groups.set(cat, []);
    groups.get(cat).push(m);
  }
  const orderedCats = [
    ..._CATEGORY_ORDER.filter(c => groups.has(c)),
    ...[...groups.keys()].filter(c => !_CATEGORY_ORDER.includes(c)),
  ];

  const help = el('div', { class: 'help-text' },
    'Tokens enclosed in ',
    el('code', {}, '{{...}}'),
    ' are expanded when the preset is rendered. Use the scoped ',
    el('code', {}, '{{if X}}...{{/if}}'),
    ' form for multi-line conditional sections (prefix with ',
    el('code', {}, '#'),
    ' to preserve whitespace). The inline form ',
    el('code', {}, '{{if X::then::else}}'),
    ' is convenient for short ternaries within a single line.',
  );

  const search = el('input', {
    class: 'list-search',
    type: 'text',
    placeholder: 'Search macros…',
    oninput: (e) => filter(e.target.value.toLowerCase().trim()),
    style: { marginBottom: '12px' },
  });

  const groupEls = new Map();
  const rowEls = [];
  const sections = [];
  for (const cat of orderedCats) {
    const section = el('div', { class: 'macros-help-group' },
      el('h4', {}, _CATEGORY_LABELS[cat] || cat),
    );
    for (const m of groups.get(cat)) {
      const row = el('div', { class: 'macros-help-row' },
        el('code', { class: 'macros-help-name' }, m.name),
        el('div', { class: 'macros-help-desc' }, m.description || ''),
        m.example
          ? el('code', { class: 'macros-help-example' }, m.example)
          : null,
      );
      section.append(row);
      rowEls.push({
        row,
        haystack: `${m.name} ${m.description || ''} ${m.example || ''}`.toLowerCase(),
        category: cat,
      });
    }
    groupEls.set(cat, section);
    sections.push(section);
  }

  function filter(term) {
    if (!term) {
      for (const { row } of rowEls) row.style.display = '';
      for (const section of groupEls.values()) section.style.display = '';
      return;
    }
    for (const { row, haystack } of rowEls) {
      row.style.display = haystack.includes(term) ? '' : 'none';
    }
    for (const [cat, section] of groupEls.entries()) {
      const anyVisible = rowEls.some(
        e => e.category === cat && e.row.style.display !== 'none',
      );
      section.style.display = anyVisible ? '' : 'none';
    }
  }

  const body = el('div', { class: 'macros-help' },
    el('h3', { style: { marginTop: '0' } }, 'Macros'),
    help,
    search,
    ...sections,
    el('div', { class: 'modal-actions' },
      el('button', { class: 'btn', onClick: () => closeModal() }, 'Close'),
    ),
  );
  openModal(body, { size: 'large' });
}


/* ===========================================================================
 * Avatar block (mirrors brain_libraries.js#renderLibraryAvatarBlock)
 * =========================================================================== */


function renderContextPresetAvatarBlock(draft) {
  const preview = el('div', {
    class: 'avatar',
    style: { width: '120px', height: '120px', borderRadius: 'var(--radius)' },
  });

  let inCrop = false;

  function refreshPreview() {
    preview.replaceChildren();
    if (draft.avatar) {
      const url = inCrop
        ? `/api/files/context-presets/${draft.id}/avatar?v=${Date.now()}`
        : `/api/files/context-presets/${draft.id}/avatar/display?v=${Date.now()}`;
      preview.append(el('img', { src: url, alt: draft.name }));
      applyCrop(preview, inCrop ? draft.avatar_crop : null);
    } else {
      preview.textContent = (draft.name || '?').slice(0, 1).toUpperCase();
      applyCrop(preview, null);
    }
  }
  refreshPreview();

  const fileInput = el('input', {
    type: 'file', accept: 'image/*', class: 'hidden',
    id: `context-preset-avatar-input-${draft.id}`,
    onChange: async (e) => {
      const f = e.target.files[0];
      if (!f) return;
      try {
        const r = await api.uploadContextPresetAvatar(draft.id, f);
        draft.avatar = r.avatar;
        draft.avatar_crop = null;
        if (r.version_id) draft.version_id = r.version_id;
        if (r.updated_at) draft.updated_at = r.updated_at;
        setState({ contextPresets: await api.listContextPresets() });
        refreshPreview();
        refreshButtons();
      } catch (err) { toast(`Upload failed: ${err.message}`, 'error'); }
      e.target.value = '';
    },
  });
  const uploadBtn = el('label', {
    for: `context-preset-avatar-input-${draft.id}`,
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
        src: `/api/files/context-presets/${draft.id}/avatar`,
        initial: draft.avatar_crop,
        title: `Crop avatar — ${draft.name}`,
        onChange: (crop) => applyCrop(preview, crop),
        onSave: async (crop) => {
          const next = (crop && (crop.x || crop.y || crop.w !== 1 || crop.h !== 1)) ? crop : null;
          draft.avatar_crop = next;
          try {
            const updated = await api.updateContextPreset(draft.id, draft);
            if (updated) Object.assign(draft, updated);
            setState({ contextPresets: await api.listContextPresets() });
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
        const r = await api.deleteContextPresetAvatar(draft.id);
        draft.avatar = null;
        draft.avatar_crop = null;
        if (r && r.version_id) draft.version_id = r.version_id;
        if (r && r.updated_at) draft.updated_at = r.updated_at;
        setState({ contextPresets: await api.listContextPresets() });
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
