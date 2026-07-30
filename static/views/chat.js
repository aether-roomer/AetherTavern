/* Chat view: header, messages, input, per-bubble streaming. */

import { api, startGenerationStream } from '../api.js';
import { state, setState, subscribe, setChatInMap } from '../state.js';
import { el, escapeHtml, formatRelative, slugify, userAvatarEl, avatarEl, scenarioAvatarEl, placeholderAvatar, getChatById, visibleViewport } from '../util.js';
import { icon, openModal, closeModal, confirmModal, toast } from '../ui.js';
import { renderBubble } from '../aer_render.js';
import { renderGenericBubble, hasVisibleGenericContent } from '../render.js';
import { renderReasoningTray } from './reasoning_tray.js';
import {
  attachAvatarTrigger,
  makeMessageInfoBadge,
} from './message_info_popover.js';
import { makeAvatarPicker, makeSelect as pickerSelect } from '../avatar_picker.js';
import { buildModelControls, hasModelOverride } from './model_controls.js';
import { openEditMessageModal } from './edit_message_modal.js';
import { openEditBrainsModal } from './edit_brains_modal.js';
import { openBookmarksModal } from './bookmarks_modal.js';
import { setSmartScrollFn as _searchSetSmartScrollFn,
  reapplyHighlightsAfterRefresh as _searchReapplyHighlights,
  openSearch as _openSearch } from './chat_search.js';
import { saveWithConflictHandling } from '../conflict.js';
import { mountLibraryCascade } from './brain_libraries.js';
import { TTSPlayer, primeAudioSession } from '../tts_player.js';
import { shouldAutoplayTTS, resolveTTSConfig, stripForTTS } from '../tts_helpers.js';
import { playNotification } from '../notification.js';


/* ---------- per-chat input drafts (localStorage) ----------
 *
 * Persist the chat-input textarea on every keystroke under
 * ``chatDraft:{uuid}`` so the user can switch chats / close the tab /
 * reload the browser and pick up where they left off. Cleared on
 * successful send and on chat deletion. */
const CHAT_DRAFT_PREFIX = 'chatDraft:';

function loadChatDraft(chatId) {
  if (!chatId) return '';
  try { return localStorage.getItem(CHAT_DRAFT_PREFIX + chatId) || ''; }
  catch { return ''; }
}

function saveChatDraft(chatId, text) {
  if (!chatId) return;
  try {
    if (text) localStorage.setItem(CHAT_DRAFT_PREFIX + chatId, text);
    else localStorage.removeItem(CHAT_DRAFT_PREFIX + chatId);
  } catch {}
}

function clearChatDraft(chatId) {
  if (!chatId) return;
  try { localStorage.removeItem(CHAT_DRAFT_PREFIX + chatId); } catch {}
}


/* Build a richer toast for the ``brain_budget`` SSE error kind. Lists the
 * top offenders by token count and hints at the available remedies. Only
 * unconditional brains can hit this — conditional brains drop tail-first
 * inside ``_maybe_splice_conditional_block`` instead. */
/* Resolve the currently-active generic provider's ``streaming`` flag.
 * ``true`` when not in generic mode (AER's streaming is parser-driven
 * and not user-toggleable). Defaults to ``true`` if the lookup falls
 * through — surprising silence is worse than the streaming default. */
function _activeGenericStreamingEnabled() {
  const s = state.settings || {};
  if (s.provider_mode !== 'generic') return true;
  const g = s.generic;
  if (!g) return true;
  const provider = g.provider;
  if (provider === 'openai_compatible') {
    const activeId = g.openai_compatible && g.openai_compatible.active_id;
    const list = (g.openai_compatible && g.openai_compatible.custom_providers) || [];
    const cfg = list.find(c => c.id === activeId);
    if (cfg) return cfg.streaming !== false;
    return true;
  }
  if (provider && g[provider]) return g[provider].streaming !== false;
  return true;
}


function _showBrainBudgetToast(err) {
  const offenders = Array.isArray(err.offenders) ? err.offenders : [];
  const cap = Number(err.cap) || 0;
  const total = Number(err.total) || 0;

  const body = el('div', { class: 'toast-body' },
    el('div', { class: 'toast-title' }, 'Brain budget exceeded'),
    el('div', {},
      `Always-on brains use ${total.toLocaleString()} of ${cap.toLocaleString()} allowed tokens.`),
  );
  if (offenders.length) {
    const list = el('ul', { class: 'toast-list' });
    for (const o of offenders) {
      list.append(el('li', {}, _brainOffenderName(o),
        el('span', { class: 'toast-list-meta' }, ` — ${Number(o.tokens).toLocaleString()} tokens`),
      ));
    }
    body.append(list);
  }
  body.append(el('div', { class: 'toast-hint' },
    'Fixes: trim a brain\'s content, add activation keys so it only fires when needed, or pick a larger context preset in Settings.'));

  toast(body, 'error', { duration: 12000, dismissible: true });
}


/* Render a brain offender's name. When the server attributed the brain to a
 * specific entity, the name becomes a link that jumps to that entity's edit
 * view. Unattributed offenders fall back to plain text. */
function _brainOffenderName(offender) {
  const text = String(offender.name || '(unnamed)');
  const nav = _navStateForOffender(offender);
  if (!nav) return el('span', { class: 'toast-list-name' }, text);
  return el('button', {
    class: 'toast-list-link',
    type: 'button',
    title: 'Open the editor for this brain',
    onClick: (e) => {
      e.preventDefault();
      e.stopPropagation();
      setState(nav);
    },
  }, text);
}


function _navStateForOffender(o) {
  if (!o || !o.owner_id) return null;
  switch (o.kind) {
    case 'contact':
    case 'contact_scenario':
      return { activeTab: 'contacts', activeContactId: o.owner_id };
    case 'user':
      return { activeTab: 'users', activeUserId: o.owner_id };
    case 'scenario':
      return { activeTab: 'scenarios', activeScenarioId: o.owner_id };
    case 'brain_library':
      return { activeTab: 'libraries', activeLibraryId: o.owner_id };
    case 'chat_message':
      // No deep-link to the message yet — drop the user into the chat
      // they came from, since that's where the per-message brain lives.
      return { activeTab: 'chats', activeChatId: o.owner_id };
    default:
      return null;
  }
}


export function renderChatView(container) {
  container.replaceChildren();
  // Drop any prior message-nav lock — it pointed at a different chat's tree.
  _navLockedId = null;
  _virtualSlotShown = false;
  // Reset TTS state when the active chat changes — leftover queue from a
  // previous chat should not bleed across.
  if (_ttsChatId && _ttsChatId !== state.activeChatId) disposeTTSPlayer();
  _ttsChatId = state.activeChatId;
  if (!state.activeChatId) {
    container.append(emptyState());
    return () => {};
  }
  const chat = getChatById(state, state.activeChatId);
  if (!chat) { container.append(emptyState()); return () => {}; }

  // Clear any cached messages that belong to a previously-viewed chat —
  // otherwise the synchronous initial `refreshMessages` below would
  // paint the previous chat's bubbles for one frame before
  // `loadActiveChat` fetches and replaces them. Direct mutation
  // (not setState) so we don't re-trigger subscribers mid-render.
  const cacheIsFresh = state.chatMessagesFor === chat.id;
  if (!cacheIsFresh) {
    state.chatMessages = [];
    state.activePathIds = [];
    state.contextTokens = null;
    state.contextActiveBrains = [];
    state.contextActiveBrainsFromLastGen = false;
  }

  const view = el('div', { class: 'chat-view' });
  const header = renderHeader(chat);
  const config = renderConfig(chat);
  const messagesWrap = el('div', { class: 'chat-messages-wrap', id: 'chat-messages-wrap' },
    el('div', { class: 'chat-bg', 'aria-hidden': 'true' },
      el('div', { class: 'chat-bg-image' }),
      el('div', { class: 'chat-bg-tint' }),
      el('div', { class: 'chat-bg-dim' }),
      el('div', { class: 'chat-bg-brighten' }),
    ),
  );
  const messages = el('div', { class: 'chat-messages', id: 'chat-messages' });
  messagesWrap.append(messages);
  // Hovering pause / stop buttons for TTS — start hidden, become
  // visible when the player has a queue.
  _ttsOverlay = buildTTSOverlay();
  messagesWrap.append(_ttsOverlay);
  const input = renderInputArea(chat);
  view.append(header, config, messagesWrap, input);
  container.append(view);

  // Apply the scenario background (if any) to the messages wrap.
  applyScenarioBackground(messagesWrap, _scenarioForChat(chat));

  // Initial messages render — only if the cached `state.chatMessages`
  // belongs to the current chat. Otherwise leave the container empty
  // until `loadActiveChat` fetches: an empty container is less jarring
  // than a brief "No messages yet" flash for chats that DO have
  // messages but haven't been loaded yet this session.
  if (cacheIsFresh) {
    refreshMessages(chat);
  }

  // Drop focus into the input bar so the user can start typing immediately
  // on tab/chat switch. Skipped if a modal is up (don't steal focus from it).
  // Dispatch a synthetic input event so the auto-resize handler runs against
  // the now-mounted textarea (scrollHeight reads 0 before mount). Run it
  // unconditionally: it sizes a restored multi-line draft AND normalizes the
  // empty box to its content height, so it no longer keeps the slightly taller
  // rows="1" height and visibly shrink a pixel on the first keystroke.
  const ta = input.querySelector('textarea');
  if (ta) {
    ta.dispatchEvent(new Event('input'));
    const modalRoot = document.getElementById('modal-root');
    if (!modalRoot || modalRoot.classList.contains('hidden')) {
      ta.focus();
      // Caret to end so the user resumes typing where they left off.
      const len = ta.value.length;
      ta.setSelectionRange(len, len);
    }
  }

  // Auto-fetch messages whenever the active chat changes.
  loadActiveChat(chat.id);
  return () => {};
}


/* ---------- header & config ---------- */

function renderHeader(chat) {
  const titleInput = el('input', {
    class: 'title',
    type: 'text',
    value: chat.title || '',
    placeholder: 'Untitled chat',
    onChange: async (e) => {
      try {
        // Source the freshest chat at save time — the inline config bar
        // auto-saves while the title input is focused, so the captured
        // ``chat`` closure goes stale. See ``patch()`` in renderConfig.
        const live = getChatById(state, chat.id) || chat;
        const draft = { ...live, title: e.target.value };
        await saveWithConflictHandling({
          draft,
          saveFn: (d) => api.updateChat(chat.id, d),
          getFn: () => api.getChat(chat.id),
          entityLabel: 'Chat',
          onReload: async () => {
            // Refresh state.chats with the fresh remote copy BEFORE
            // re-mounting so the new view sees the reloaded values.
            // saveWithConflictHandling already fetched the fresh chat
            // for us, but state.chats also drives the inline config bar
            // / title; reload the list so renderChatView reads current.
            setState({ chats: await api.listChats() });
            const slot = document.getElementById('chat-content-pane');
            if (slot) renderChatView(slot);
          },
        });
        setChatInMap(chat.id, draft);
        const all = await api.listChats();
        setState({ chats: all });
      } catch (err) { toast(`Could not save title: ${err.message}`, 'error'); }
    },
  });

  // Surface broken references right next to the title so the user notices
  // before they hit a generation error. Clicking opens the info modal where
  // the dangling entity can be reassigned.
  const missing = [];
  if (chat.contact_id && !state.contacts.find(c => c.id === chat.contact_id)) missing.push('contact');
  if (chat.user_id && !(state.users || []).find(u => u.id === chat.user_id)) missing.push('user');
  if (chat.scenario_id && !(state.scenarios || []).find(s => s.id === chat.scenario_id)) missing.push('scenario');
  const missingMarker = missing.length
    ? el('button', {
        class: 'chat-missing-marker',
        title: `Missing ${missing.join(', ')} — click to reassign`,
        onClick: () => openChatInfoModal(chat),
      }, '⚠ ', missing.join(', '), ' missing')
    : null;

  // Mobile-only ``…`` toggle: tapping it hides the chat title and shows
  // the controls row (info / bookmarks / search / export / delete). Tap
  // again to collapse back to title-only. Hidden on desktop via CSS —
  // desktop has the room for everything inline. Rendered AFTER the
  // controls in source order so it stays at the right edge of the
  // header in both states — same screen position whether you're
  // tapping it to expand or collapse.
  const moreToggle = el('button', {
    class: 'icon-btn header-more-toggle',
    title: 'More actions',
    onClick: (e) => {
      const hdr = e.currentTarget.closest('.chat-header');
      if (hdr) hdr.classList.toggle('controls-expanded');
    },
  }, icon('more', 22));

  return el('div', { class: 'chat-header' },
    titleInput,
    missingMarker,
    el('div', { class: 'controls' },
      // Mobile-only gear icon: opens the chat-settings modal (intimacy /
      // style / length / preset / tags / japanese). Hidden on desktop via
      // CSS — desktop has the inline .chat-config row instead.
      el('button', {
        id: 'mobile-chat-settings',
        class: 'icon-btn', title: 'Chat settings',
        onClick: () => openChatSettingsModal(chat),
      }, icon('settings', 18)),
      el('button', {
        class: 'icon-btn', title: 'Chat info',
        onClick: () => openChatInfoModal(chat),
      }, icon('info', 18)),
      el('button', {
        class: 'icon-btn', title: 'Bookmarks',
        onClick: () => openBookmarksModal(chat.id),
      }, icon('bookmark', 18)),
      // Search messages — same surface Ctrl+F opens, but mobile users
      // (no physical keyboard) need a tappable trigger.
      el('button', {
        class: 'icon-btn', title: 'Search messages (Ctrl+F)',
        onClick: () => _openSearch(),
      }, icon('search', 18)),
      el('button', {
        class: 'icon-btn', title: 'Export chat',
        onClick: async () => {
          try {
            const r = await api.exportChat(chat.id);
            const { downloadFromResponse } = await import('../util.js');
            await downloadFromResponse(r, `chat-${chat.title || 'chat'}.json`);
          } catch (e) { toast(`Export failed: ${e.message}`, 'error'); }
        },
      }, icon('download', 18)),
      el('button', {
        class: 'icon-btn danger', title: 'Delete chat',
        onClick: async () => {
          const ok = await confirmModal('Delete chat?',
            'This will remove the chat and its messages permanently.',
            { danger: true, confirmLabel: 'Delete' });
          if (!ok) return;
          await api.deleteChat(chat.id);
          clearChatDraft(chat.id);
          const all = await api.listChats();
          setState({ chats: all, activeChatId: null });
        },
      }, icon('trash', 18)),
      el('button', {
        class: 'icon-btn', title: 'Close chat',
        onClick: () => setState({ activeChatId: null }),
      }, icon('x', 18)),
    ),
    moreToggle,
  );
}


function openChatInfoModal(chat) {
  // Working draft — committed only when the user clicks Save. Lets us
  // reassign contact/user/scenario/libraries without partial saves on each
  // change.
  const draft = {
    contact_id: chat.contact_id,
    user_id: chat.user_id,
    scenario_id: chat.scenario_id || '',
    contact_scenario_id: chat.contact_scenario_id || '',
    brain_library_ids: Array.isArray(chat.brain_library_ids)
      ? chat.brain_library_ids.slice()
      : [],
  };

  function copyButton(id) {
    return el('button', {
      class: 'icon-btn', title: 'Copy ID',
      onClick: async () => {
        try { await navigator.clipboard.writeText(id); toast('ID copied', 'success'); }
        catch { toast('Copy failed', 'error'); }
      },
    }, icon('copy', 14));
  }

  function uuidEl(id) {
    if (!id) return el('span', { class: 'info-uuid muted' }, '—');
    // First 8 chars are enough to disambiguate visually; the trailing ``…``
    // signals that the value is truncated. Full id is still available via
    // the hover tooltip and the copy button next to it.
    return el('code', { class: 'info-uuid', title: id }, id.slice(0, 8) + '…');
  }

  function readOnlyRow(label, name, id) {
    return el('div', { class: 'info-row' },
      el('div', { class: 'info-label' }, label),
      el('div', { class: 'info-value' },
        el('span', { class: 'info-name' }, name),
        uuidEl(id),
        copyBtn(id),
      ),
    );
  }
  function copyBtn(id) { return id ? copyButton(id) : null; }

  function makeAssignRow(label, currentId, options, opts = {}) {
    const uuidSlot = el('span', { class: 'info-uuid-slot' });
    const refresh = () => {
      uuidSlot.replaceChildren();
      if (currentId) uuidSlot.append(uuidEl(currentId), copyButton(currentId));
      else uuidSlot.append(uuidEl(null));
    };
    function buildPickerOpts() {
      const list = options || [];
      const favs = list.filter(x => x.favorite);
      const rest = list.filter(x => !x.favorite);
      const result = [];
      if (opts.allowNone) {
        result.push({
          value: '',
          label: '(none)',
          groupAfter: favs.length > 0 || rest.length > 0,
        });
      }
      favs.forEach((x, i) => result.push({
        value: x.id,
        label: x.name,
        getAvatar: opts.avatarFn ? () => opts.avatarFn(x) : undefined,
        favorite: true,
        groupAfter: i === favs.length - 1 && rest.length > 0,
      }));
      rest.forEach(x => result.push({
        value: x.id,
        label: x.name,
        getAvatar: opts.avatarFn ? () => opts.avatarFn(x) : undefined,
      }));
      // Surface an orphan reference as a synthetic option so the user can
      // see the dangling id rather than landing on a silent "(none)".
      if (currentId && !list.some(x => x.id === currentId)) {
        result.push({ value: currentId, label: '(missing)' });
      }
      return result;
    }
    const matched = !currentId || (options || []).some(x => x.id === currentId);
    const row = el('div', { class: 'info-row' + (currentId && !matched ? ' missing' : '') },
      el('div', { class: 'info-label' }, label),
    );
    const picker = makeAvatarPicker({
      value: currentId || '',
      options: buildPickerOpts(),
      onChange: (v) => {
        currentId = v || null;
        opts.onChange(currentId);
        refresh();
        // Rebuild options so the synthetic (missing) entry drops once the
        // user picks a real one; sync the .missing class for the same reason.
        picker.setOptions(buildPickerOpts());
        if ((options || []).some(x => x.id === currentId)) {
          row.classList.remove('missing');
        }
      },
    });
    refresh();
    row.append(el('div', { class: 'info-value' }, picker, uuidSlot));
    return row;
  }

  const contacts = state.contacts || [];
  const users = state.users || [];
  const scenarios = state.scenarios || [];

  // Scenario row: a single dropdown that mixes character-specific scenarios
  // (sorted to the top with a divider) and global scenarios. Selection
  // value is "global:<id>" / "contact:<id>" / "" — re-split on save into
  // ``scenario_id`` / ``contact_scenario_id`` (which the server treats as
  // mutually exclusive).
  function makeScenarioRow() {
    function readSel() {
      return draft.contact_scenario_id
        ? `contact:${draft.contact_scenario_id}`
        : draft.scenario_id ? `global:${draft.scenario_id}` : '';
    }
    let currentSel = readSel();
    const uuidSlot = el('span', { class: 'info-uuid-slot' });

    function refreshUuid() {
      uuidSlot.replaceChildren();
      const id = draft.contact_scenario_id || draft.scenario_id || null;
      if (id) uuidSlot.append(uuidEl(id), copyButton(id));
      else uuidSlot.append(uuidEl(null));
    }

    function buildOpts() {
      const result = [];
      const contact = contacts.find(c => c.id === draft.contact_id);
      const cscens = (contact && contact.scenarios) || [];
      const sortedCscens = [...cscens].sort((a, b) => (a.name || '').localeCompare(b.name || ''));
      const favGlobals = scenarios.filter(s => s.favorite);
      const restGlobals = scenarios.filter(s => !s.favorite);
      const hasOthers = sortedCscens.length > 0 || favGlobals.length > 0 || restGlobals.length > 0;
      result.push({ value: '', label: '(none)', groupAfter: hasOthers });
      sortedCscens.forEach((s, i) => {
        const isDefault = s.id === (contact && contact.default_scenario_id);
        result.push({
          value: `contact:${s.id}`,
          label: `${s.name || '(unnamed)'}${isDefault ? '  (default)' : ''}`,
          getAvatar: contact ? () => avatarEl(contact) : undefined,
          groupAfter: i === sortedCscens.length - 1
            && (favGlobals.length > 0 || restGlobals.length > 0),
        });
      });
      favGlobals.forEach((s, i) => result.push({
        value: `global:${s.id}`,
        label: s.name,
        getAvatar: () => scenarioAvatarEl(s),
        favorite: true,
        groupAfter: i === favGlobals.length - 1 && restGlobals.length > 0,
      }));
      restGlobals.forEach(s => result.push({
        value: `global:${s.id}`,
        label: s.name,
        getAvatar: () => scenarioAvatarEl(s),
      }));
      // Orphan reference fallback so the user sees what they're losing.
      if (currentSel && !result.some(o => o.value === currentSel)) {
        result.push({ value: currentSel, label: '(missing)' });
      }
      return result;
    }

    const picker = makeAvatarPicker({
      value: currentSel,
      options: buildOpts(),
      onChange: (v) => {
        currentSel = v || '';
        if (currentSel.startsWith('contact:')) {
          draft.contact_scenario_id = currentSel.slice('contact:'.length);
          draft.scenario_id = '';
        } else if (currentSel.startsWith('global:')) {
          draft.scenario_id = currentSel.slice('global:'.length);
          draft.contact_scenario_id = '';
        } else {
          draft.scenario_id = '';
          draft.contact_scenario_id = '';
        }
        refreshUuid();
        // Drop the synthetic (missing) entry now that the user has chosen.
        picker.setOptions(buildOpts());
      },
    });

    refreshUuid();

    const row = el('div', { class: 'info-row' },
      el('div', { class: 'info-label' }, 'Scenario'),
      el('div', { class: 'info-value' }, picker, uuidSlot),
    );
    // The contact row calls this when the user reassigns the contact —
    // contact-scoped scenarios for the previous contact would be stale.
    row.refreshForContactChange = () => {
      currentSel = readSel();
      picker.setOptions(buildOpts());
      picker.setValue(currentSel);
      refreshUuid();
    };
    return row;
  }

  const scenarioRow = makeScenarioRow();
  const libraryCascadeEl = el('div', { class: 'library-cascade' });
  mountLibraryCascade(libraryCascadeEl, {
    selected: draft.brain_library_ids,
    onChange: (next) => { draft.brain_library_ids = next; },
  });
  const libraryRow = el('div', { class: 'info-row' },
    el('div', { class: 'info-label' }, 'Brain libraries'),
    el('div', { class: 'info-value' }, libraryCascadeEl),
  );

  const body = el('div', { class: 'chat-info' },
    el('h3', {}, 'Chat info'),
    readOnlyRow('Chat', chat.title || '(untitled)', chat.id),
    makeAssignRow('Contact', draft.contact_id, contacts, {
      avatarFn: (c) => avatarEl(c),
      onChange: v => {
        draft.contact_id = v;
        // Switching contact invalidates a contact-scenario reference.
        draft.contact_scenario_id = '';
        scenarioRow.refreshForContactChange();
      },
    }),
    makeAssignRow('User', draft.user_id, users, {
      avatarFn: (u) => userAvatarEl(u) || placeholderAvatar(u.name),
      onChange: v => { draft.user_id = v; },
    }),
    scenarioRow,
    libraryRow,
    el('div', { class: 'modal-actions' },
      el('button', { class: 'btn ghost', onClick: () => closeModal() }, 'Cancel'),
      el('button', {
        class: 'btn primary',
        onClick: async () => {
          const sameLibs = (
            JSON.stringify(draft.brain_library_ids || [])
            === JSON.stringify(chat.brain_library_ids || [])
          );
          const changed =
            draft.contact_id !== chat.contact_id ||
            draft.user_id !== chat.user_id ||
            (draft.scenario_id || null) !== (chat.scenario_id || null) ||
            (draft.contact_scenario_id || null) !== (chat.contact_scenario_id || null) ||
            !sameLibs;
          if (!changed) { closeModal(); return; }
          try {
            // Read the freshest chat at save time — the inline config bar
            // (intimacy / style / length / cjk / tags) auto-saves through
            // ``patch()`` while the modal is open, so the captured ``chat``
            // closure is stale by then. Spreading stale fields here would
            // overwrite the user's recent inline edits.
            const live = getChatById(state, chat.id) || chat;
            const updated = {
              ...live,
              contact_id: draft.contact_id,
              user_id: draft.user_id,
              scenario_id: draft.scenario_id || null,
              contact_scenario_id: draft.contact_scenario_id || null,
              brain_library_ids: draft.brain_library_ids || [],
            };
            await saveWithConflictHandling({
              draft: updated,
              saveFn: (d) => api.updateChat(chat.id, d),
              getFn: () => api.getChat(chat.id),
              entityLabel: 'Chat',
              onReload: async () => {
                setState({ chats: await api.listChats() });
                const slot = document.getElementById('chat-content-pane');
                if (slot) renderChatView(slot);
              },
            });
            // Keep chatMap in sync — ``updated`` carries the server's
            // post-save ``version_id`` (in-place updated by
            // ``saveWithConflictHandling``). Subsequent reads in the
            // chat view, patch(), and title save all source from here.
            setChatInMap(chat.id, updated);
            const [all, deps] = await Promise.all([
              api.listChats(),
              // Participants / scenario / libraries may all have changed
              // — refresh the full-entity deps the chat view consumes
              // (background sliders, pick-macro flag, TTS resolve).
              _fetchActiveChatDeps(updated),
            ]);
            setState({ chats: all, ...deps });
            // Re-render the chat view so the header re-evaluates whether
            // any references are still missing — the standard subscribe
            // path only repaints on tab / active-chat changes.
            const slot = document.getElementById('chat-content-pane');
            if (slot) renderChatView(slot);
            // Contact / user / scenario shifts rebuild the prompt header,
            // so the previous count is genuinely misleading — flush.
            scheduleContextRefresh(chat.id, { flush: true });
            toast('Chat updated', 'success');
            closeModal();
          } catch (err) {
            toast(`Could not save: ${err.message}`, 'error');
          }
        },
      }, 'Save'),
    ),
  );
  openModal(body);
}


function _renderRerollPickButton(chat) {
  if (!state.chatUsesPickMacro) return null;
  return el('button', {
    type: 'button',
    class: 'icon-btn reroll-pick-btn',
    title: 'Reroll {{pick}} macros (takes effect on next reply)',
    'aria-label': 'Reroll pick macros',
    onClick: async () => {
      try {
        await api.rerollPicks(chat.id);
        toast('Pick macros will reroll on the next reply.', 'info');
      } catch (e) {
        toast(`Could not reroll picks: ${e.message}`, 'error');
      }
    },
  }, icon('dice', 16));
}


/* Persist a single Chat field through the conflict-aware save flow. Shared by
 * the inline config controls (``patch``) and the Model-settings popover. */
async function persistChatField(chat, key, value) {
  try {
    // Source the freshest copy at save time — inline edits land in
    // ``state.chats`` / ``chatMap`` but a captured closure goes stale, so
    // spreading it would clobber prior edits.
    const live = getChatById(state, chat.id) || chat;
    const updated = { ...live, [key]: value };
    if (key === 'response_length' && value === '') updated.response_length = null;
    await saveWithConflictHandling({
      draft: updated,
      saveFn: (d) => api.updateChat(chat.id, d),
      getFn: () => api.getChat(chat.id),
      entityLabel: 'Chat',
      onReload: () => {
        const slot = document.getElementById('chat-content-pane');
        if (slot) renderChatView(slot);
      },
    });
    setChatInMap(chat.id, updated);
    const all = await api.listChats();
    setState({ chats: all });
    // Style / length / overrides etc. all feed the prompt, so the ctx count
    // is now stale; schedule a refresh.
    scheduleContextRefresh(chat.id);
  } catch (e) { toast(`Could not save: ${e.message}`, 'error'); }
}


function renderConfig(chat, opts = {}) {
  const intimacyOpts = ['stranger', 'acquaintance', 'close', 'romantic'];
  const styleOpts = ['chat', 'roleplay'];
  const lengthOpts = ['', 'short', 'medium', 'long', 'very long', 'para',
    'rapid spam', 'spam', 'spammy short', 'spammy medium', 'spammy long', 'spammy very long', 'spammy para'];

  function patch(key, value) {
    return () => persistChatField(chat, key, value);
  }

  function makeSelect(label, key, options, current, opts = {}) {
    const emptyLabel = opts.emptyLabel || '(default)';
    // Display each word capitalised — the values stay lowercase on the
    // wire / in the AER prompt; this is purely a label transform.
    const displayOpts = options.map(o => ({
      value: o,
      label: o
        ? o.split(' ').map(w => w.charAt(0).toUpperCase() + w.slice(1)).join(' ')
        : emptyLabel,
    }));
    const picker = pickerSelect({
      value: current ?? '',
      options: displayOpts,
      onChange: (v) => patch(key, v)(),
    });
    return el('label', {}, label, picker);
  }

  const presetOptions = [
    { value: '', label: '(default)' },
    ...((state.presets || []).map(p => ({ value: p.id, label: p.name }))),
  ];
  const presetSelect = pickerSelect({
    value: chat.preset_id || '',
    options: presetOptions,
    onChange: (v) => patch('preset_id', v || null)(),
  });

  const cjkToggle = el('label', { class: 'cjk-toggle', title: 'Japanese' },
    el('input', {
      type: 'checkbox', checked: !!chat.cjk,
      onChange: (e) => patch('cjk', e.target.checked)(),
    }),
    'Japanese',
  );

  // Free-form chat tags. ``onChange`` on text inputs fires on blur — that's
  // the right cadence here: users tweak tags then look away or tab on.
  const tagsInput = el('label', { class: 'chat-tags', title: 'Chat tags (comma-separated)' },
    'Tags',
    el('input', {
      type: 'text',
      value: chat.tags || '',
      placeholder: 'comma, separated',
      onChange: (e) => patch('tags', e.target.value)(),
    }),
  );

  // ``opts.layout === 'stacked'`` is used by the mobile chat-settings modal
  // (one column, full-width inputs); the desktop inline row uses the default
  // flex-wrap row instead.
  const stacked = opts.layout === 'stacked';
  // Provider / Model / Context-preset overrides. Stacked layouts (mobile gear
  // modal) show the three pickers inline; the desktop bar shows a compact
  // ``Model`` button that opens the same pickers in a popover modal — keeps the
  // bar from overcrowding.
  const modelControls = stacked
    ? buildModelControls({
        getChat: () => getChatById(state, chat.id) || chat,
        settings: state.settings,
        onChange: (field, value) => persistChatField(chat, field, value),
      })
    : [];
  const modelBtn = stacked ? null : el('button', {
    type: 'button',
    class: 'chat-config-model-btn' + (hasModelOverride(chat) ? ' has-override' : ''),
    title: 'Provider / model / context preset for this chat',
    onClick: () => openModelSettingsModal(chat),
  }, 'Model', ...(hasModelOverride(chat) ? [el('span', { class: 'override-dot' })] : []));
  const rerollBtn = _renderRerollPickButton(chat);
  return el('div', { class: stacked ? 'chat-config stacked' : 'chat-config' },
    ...(rerollBtn ? [rerollBtn] : []),
    makeSelect('Intimacy', 'intimacy', intimacyOpts, chat.intimacy),
    makeSelect('Style', 'style', styleOpts, chat.style),
    makeSelect('Length', 'response_length', lengthOpts, chat.response_length || '', { emptyLabel: '(any)' }),
    el('label', {}, 'Preset', presetSelect),
    ...modelControls,
    ...(modelBtn ? [modelBtn] : []),
    tagsInput,
    cjkToggle,
    el('span', { style: { flex: stacked ? '0' : 1 } }),
    el('span', { class: 'chat-stats' }, _ctxStatText()),
  );
}


/* Mobile-only chat-settings modal. Opens from the gear icon in the chat
 * header (which is hidden on desktop). Reuses ``renderConfig`` with the
 * stacked layout so the same controls + ``patch`` save logic apply. */
function openChatSettingsModal(chat) {
  // Source the freshest chat at modal-open time — inline edits elsewhere
  // (or per-tab refreshes) update state.chats; spreading the captured
  // closure would clobber prior edits.
  const live = getChatById(state, chat.id) || chat;
  const body = el('div', {},
    el('h3', {}, 'Chat settings'),
    renderConfig(live, { layout: 'stacked' }),
    el('div', { class: 'modal-actions' },
      el('button', { class: 'btn primary', onClick: () => closeModal() }, 'Done'),
    ),
  );
  openModal(body);
}


/* Desktop popover for the per-chat Provider / Model / Context-preset overrides,
 * opened from the compact ``Model`` button in the settings bar. The three
 * pickers come from the shared ``buildModelControls`` builder, laid out as
 * ``.form-group`` rows to match the new-chat wizard. */
function openModelSettingsModal(chat) {
  const live = getChatById(state, chat.id) || chat;
  // Mirror the override values locally so the bar button's dot can update
  // immediately — chatMap only reflects the change after the async save lands.
  const cur = {
    provider_override: live.provider_override || null,
    model_overrides: { ...(live.model_overrides || {}) },
    context_preset_override: live.context_preset_override || null,
  };
  const refreshDot = () => {
    const btn = document.querySelector('.chat-config:not(.stacked) .chat-config-model-btn');
    if (!btn) return;
    const has = !!cur.provider_override || !!cur.context_preset_override
      || Object.keys(cur.model_overrides).length > 0;
    btn.classList.toggle('has-override', has);
    let dot = btn.querySelector('.override-dot');
    if (has && !dot) btn.append(el('span', { class: 'override-dot' }));
    else if (!has && dot) dot.remove();
  };
  const onChange = (field, value) => {
    cur[field] = field === 'model_overrides' ? { ...value } : value;
    refreshDot();
    persistChatField(live, field, value);
  };
  const body = el('div', {},
    el('h3', {}, 'Model settings'),
    el('div', { class: 'model-overrides' },
      ...buildModelControls({
        getChat: () => getChatById(state, chat.id) || chat,
        settings: state.settings,
        layout: 'form-group',
        onChange,
      }),
    ),
    el('div', { class: 'modal-actions' },
      el('button', { class: 'btn primary', onClick: () => closeModal() }, 'Done'),
    ),
  );
  openModal(body);
}


// The ``.chat-stats`` span that ``renderConfig`` paints lives inside the
// chat view and isn't reactive to ``state`` changes by itself. Subscribe
// once at module load and patch its text in place whenever any of the
// stat values change — covers the SSE ``context`` event during streaming
// AND the debounced refresh below. No-op if the span isn't currently
// mounted (e.g. user is on a different tab).
/* Modal listing every brain that contributed to the next-turn prompt,
 * grouped by source, with token costs. Each row is clickable and deep-
 * links to the owning entity's edit page — same navigation map the
 * brain-budget toast uses (see ``_navStateForOffender``). */
function openActiveBrainsModal() {
  const brains = (state.contextActiveBrains || []).slice();
  // Group by owner_name so the modal reads "Aria · scenario", "Shared
  // lore", etc., with their brains underneath. Unattributed brains (rare —
  // would imply a Brain.id collision between an active entity and a
  // surfaced offender) fall through into an "Other" bucket.
  const groups = new Map();
  let total = 0;
  for (const b of brains) {
    total += b.tokens || 0;
    const key = b.owner_name || 'Other';
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(b);
  }
  const fromLastGen = !!state.contextActiveBrainsFromLastGen;
  const body = el('div', {},
    el('h3', {}, 'Active brains'),
    el('div', { class: 'active-brains-summary' },
      `${brains.length} brain${brains.length === 1 ? '' : 's'}`,
      ' · ',
      `${total.toLocaleString('en-US')} tokens`,
    ),
    el('div', { class: 'active-brains-provenance' },
      fromLastGen
        ? 'Snapshot of the brains that were active when generating the latest message.'
        : 'Preview of the brains that would fire on the next generation. '
          + 'Random conditional brains will re-roll at gen time, so the actual set may differ.',
    ),
    ...Array.from(groups.entries()).map(([owner, list]) =>
      el('div', { class: 'active-brains-group' },
        el('div', { class: 'active-brains-group-head' }, owner),
        el('div', { class: 'active-brains-group-list' },
          ...list.map(b => {
            const nav = b.deleted ? null : _navStateForOffender(b);
            const label = b.name + (b.conditional ? ' · conditional' : '');
            const row = el('div', {
              class: 'active-brains-row' + (b.deleted ? ' deleted' : ''),
              title: b.deleted
                ? 'This brain (or its owning entity) has been deleted since the previous generation.'
                : undefined,
            });
            // Group name + (optional) trash marker in a single left-hand
            // span so the row stays a clean 2-column flex: [name | tokens].
            const left = el('span', { class: 'active-brains-row-left' });
            if (nav) {
              left.append(el('button', {
                class: 'active-brains-link',
                type: 'button',
                title: 'Open the editor for this brain',
                onClick: (e) => {
                  e.preventDefault();
                  setState(nav);
                  closeModal();
                },
              }, label));
            } else {
              left.append(el('span', { class: 'active-brains-label' }, label));
            }
            if (b.deleted) {
              left.append(icon('trash', 12));
            }
            row.append(left);
            row.append(el('span', { class: 'active-brains-tokens' },
              `${(b.tokens || 0).toLocaleString('en-US')} tk`));
            return row;
          }),
        ),
      ),
    ),
    el('div', { class: 'modal-actions' },
      el('button', { class: 'btn primary', onClick: () => closeModal() }, 'Done'),
    ),
  );
  openModal(body);
}


function _ctxStatText() {
  if (state.contextTokens == null) return 'ctx: ?';
  let s = `ctx: ${state.contextTokens.toLocaleString('en-US')}`;
  if (state.contextMsgsTotal != null) {
    if (state.contextMsgsIn != null && state.contextMsgsIn < state.contextMsgsTotal) {
      s += ` · msgs: ${state.contextMsgsIn}/${state.contextMsgsTotal}`;
    } else {
      s += ` · msgs: ${state.contextMsgsTotal}`;
    }
  }
  const n = (state.contextActiveBrains || []).length;
  if (n > 0) s += ` · brains: ${n}`;
  return s;
}
let _lastCtxKey = '';
function paintCtxStat() {
  const span = document.querySelector('.chat-stats');
  if (!span) return;
  span.replaceChildren(_ctxStatText());
  // Append an (i) button next to "brains: N" when there are any. Click
  // opens a per-brain breakdown modal grouped by source. Stays inline in
  // the same span so the existing flex/font sizing carries over.
  if ((state.contextActiveBrains || []).length > 0) {
    const btn = el('button', {
      type: 'button',
      class: 'chat-stats-info',
      title: 'Show active brains',
      'aria-label': 'Show active brains',
      onClick: (e) => {
        e.preventDefault();
        e.stopPropagation();
        openActiveBrainsModal();
      },
    }, icon('info', 13));
    span.append(' ', btn);
  }
}
subscribe(() => {
  const brainsKey = (state.contextActiveBrains || []).length;
  const key = `${state.contextTokens}|${state.contextMsgsIn}|${state.contextMsgsTotal}|${state.contextOldestId || ''}|${brainsKey}`;
  if (key === _lastCtxKey) return;
  _lastCtxKey = key;
  paintCtxStat();
  // ``_refreshContextDivider`` is cheap (single querySelector + an
  // optional insert/remove) and handles both "rollover boundary moved"
  // and "rollover state flipped on/off" — token-only updates take the
  // early-return inside the helper.
  _refreshContextDivider();
});

/* Draw / move the dashed divider that marks the boundary above the
 * earliest message still in-context. Only visible when something has
 * actually been rolled over AND the boundary message is in the loaded
 * window — otherwise the divider would land in a spacer. */
function _refreshContextDivider() {
  const root = document.getElementById('chat-messages');
  if (!root) return;
  const existing = root.querySelector('.context-divider');
  if (existing) existing.remove();
  if (!state.contextOldestId) return;
  if (state.contextMsgsIn != null && state.contextMsgsTotal != null
      && state.contextMsgsIn >= state.contextMsgsTotal) return;
  const target = root.querySelector(
    `.msg[data-msg-id="${CSS.escape(state.contextOldestId)}"]`,
  );
  if (!target) return;
  const div = el('div', { class: 'context-divider', title: 'Older messages have been trimmed from context' });
  target.parentNode.insertBefore(div, target);
}


/* ---------- scenario background ---------- */

function _scenarioForChat(chat) {
  // ``state.activeChatScenario`` is the full Scenario fetched by
  // ``loadActiveChat`` (and refreshed after chat-config saves). The
  // list-pane ``state.scenarios`` is a Summary projection that strips
  // ``background_blur`` / ``background_dim`` / ``background_brighten`` /
  // ``background_tint_strength`` — using it here makes those sliders
  // silently no-op in the chat view.
  if (!chat || !chat.scenario_id) return null;
  const s = state.activeChatScenario;
  return s && s.id === chat.scenario_id ? s : null;
}

/* Apply (or clear) the scenario-background CSS variables and data
 * attributes on the messages wrap. Idempotent — safe to call repeatedly. */
function applyScenarioBackground(wrap, scenario) {
  if (!wrap) return;
  if (!scenario || !scenario.background_image) {
    wrap.removeAttribute('data-has-bg');
    wrap.removeAttribute('data-mode');
    for (const v of ['--bg-image', '--bg-focal-x', '--bg-focal-y',
                     '--bg-blur', '--bg-dim', '--bg-brighten',
                     '--bg-tint-strength']) {
      wrap.style.removeProperty(v);
    }
    return;
  }
  // Cache-bust on updated_at so re-uploads / crop changes refresh in place.
  const v = Math.floor(scenario.updated_at || 0);
  const url = `/api/files/scenarios/${scenario.id}/background?v=${v}`;
  const focal = scenario.background_focal || [0.5, 0.5];
  wrap.setAttribute('data-has-bg', 'true');
  wrap.setAttribute('data-mode', scenario.background_mode || 'cover');
  wrap.style.setProperty('--bg-image', `url("${url}")`);
  wrap.style.setProperty('--bg-focal-x', `${(focal[0] * 100).toFixed(2)}%`);
  wrap.style.setProperty('--bg-focal-y', `${(focal[1] * 100).toFixed(2)}%`);
  wrap.style.setProperty('--bg-blur', `${scenario.background_blur || 0}px`);
  wrap.style.setProperty('--bg-dim', `${(scenario.background_dim || 0) / 100}`);
  wrap.style.setProperty('--bg-brighten', `${(scenario.background_brighten || 0) / 100}`);
  wrap.style.setProperty('--bg-tint-strength', `${(scenario.background_tint_strength || 0) / 100}`);
}

/* Re-apply the bg layer when the active chat's scenario changes — covers
 * uploads, crop tweaks, slider drags from the editor in another tab. */
let _lastBgKey = '';
subscribe(() => {
  const wrap = document.getElementById('chat-messages-wrap');
  if (!wrap) return;
  const chat = getChatById(state, state.activeChatId);
  const scen = _scenarioForChat(chat);
  const focal = scen?.background_focal;
  const key = scen ? [
    scen.id, scen.background_image, scen.background_mode,
    scen.background_blur, scen.background_dim, scen.background_brighten,
    scen.background_tint_strength,
    focal ? `${focal[0]},${focal[1]}` : '', scen.updated_at,
  ].join('|') : '';
  if (key === _lastBgKey) return;
  _lastBgKey = key;
  applyScenarioBackground(wrap, scen);
});


// Debounced re-tokenisation of the next-turn context. By default the
// previous count stays on screen until the new one lands ~2s later — small
// edits (style / length / cjk) don't move the count enough to be worth a
// flicker. For *structural* changes (branch swap, bookmark restore, edit /
// delete / restore of a message) the caller passes ``flush: true`` so the
// stat shows ``ctx: ?`` immediately, since the previous count is now
// genuinely misleading. Skipped entirely while a generation is in flight —
// the SSE ``context`` event already provides an authoritative count.
let _ctxDebounce = null;
let _ctxToken = 0;
export function scheduleContextRefresh(chatId, opts = {}) {
  if (!chatId) return;
  if (state.generating) return;
  const myToken = ++_ctxToken;
  if (_ctxDebounce) clearTimeout(_ctxDebounce);
  if (opts.flush) setState({
    contextTokens: null, contextMsgsIn: null, contextMsgsTotal: null, contextOldestId: null,
    contextActiveBrains: [], contextActiveBrainsFromLastGen: false,
  });
  const delayMs = opts.delayMs ?? 2000;
  _ctxDebounce = setTimeout(async () => {
    if (myToken !== _ctxToken) return;
    if (state.activeChatId !== chatId) return;
    if (state.generating) return;
    try {
      const r = await api.contextTokens(chatId);
      if (myToken !== _ctxToken) return;
      if (state.activeChatId !== chatId) return;
      setState({
        contextTokens: r.total_tokens,
        contextMsgsIn: r.messages_in_context ?? null,
        contextMsgsTotal: r.messages_total ?? null,
        contextOldestId: r.oldest_in_context_id ?? null,
        contextActiveBrains: r.active_brains || [],
        contextActiveBrainsFromLastGen: !!r.active_brains_from_last_gen,
      });
    } catch (e) {
      // Leave at ``?`` — failure is rare (only when chat references a
      // missing entity or brain budget is blown), and a toast on every
      // edit would be noisier than helpful.
      console.warn('context-tokens fetch failed:', e);
    }
  }, delayMs);
}


/* ---------- messages render ---------- */

// Virtualization: very long chats blow out the DOM if every path message
// renders unconditionally. Instead we keep a fixed-size window of messages
// loaded, padded with two spacer divs whose heights account for the
// off-screen prefix and suffix. The window slides as the user scrolls, via
// an IntersectionObserver that watches both spacers. Heights of rendered
// messages are cached so spacer math is exact for messages we've seen and
// uses an estimate for the rest.
const VIRT_WINDOW_TARGET = 90;       // messages loaded at once
const VIRT_TRIGGER_MARGIN = '600px 0px';   // IO rootMargin
const VIRT_ESTIMATED_HEIGHT = 120;   // px fallback for unmeasured messages

let _virtCurrentChatId = null;
let _virtWindowStart = 0;
let _virtWindowEnd = 0;
const _virtHeightCache = new Map();  // msgId -> measured offsetHeight
let _virtTopSpacer = null;
let _virtBottomSpacer = null;
let _virtObserver = null;
// Set when generation starts; cleared at the end of the next refreshMessages
// call. Extends the IO-callback gate past ``state.generating`` flipping so
// the post-stream loadActiveChat has a chance to settle before any window
// slide kicks in.
let _virtPinnedUntilNextLoad = false;
// True for the very first refresh after a chat switch (or initial load).
// At that point the messages container is empty + freshly laid out, and
// any "smooth scroll into the new bottom" animation would cross the top
// spacer's IntersectionObserver threshold mid-animation — firing a
// spurious slide that lands the user at the chat's start.
let _virtFirstRefreshAfterSwitch = false;

function _virtSpacerHeight(path, fromIdx, toIdx) {
  let total = 0;
  for (let i = fromIdx; i < toIdx; i++) {
    const id = path[i] && path[i].id;
    total += (id && _virtHeightCache.get(id)) || VIRT_ESTIMATED_HEIGHT;
  }
  return total;
}

function _virtBuildSpacer(path, fromIdx, toIdx, kind) {
  const div = el('div', { class: `virt-spacer virt-spacer-${kind}` });
  div.style.height = `${_virtSpacerHeight(path, fromIdx, toIdx)}px`;
  div.style.flexShrink = '0';
  div.style.pointerEvents = 'none';
  return div;
}

function _virtComputeWindow(path, opts, wasNearBottom) {
  if (path.length === 0) {
    _virtWindowStart = 0;
    _virtWindowEnd = 0;
    return;
  }
  if (opts.virtSlide) {
    // IO callback already bumped the indices — just clamp to path bounds.
    _virtWindowEnd = Math.min(path.length, _virtWindowEnd);
    _virtWindowStart = Math.max(0, Math.min(_virtWindowStart, _virtWindowEnd));
    if (_virtWindowEnd - _virtWindowStart < VIRT_WINDOW_TARGET && _virtWindowStart > 0) {
      _virtWindowStart = Math.max(0, _virtWindowEnd - VIRT_WINDOW_TARGET);
    }
    return;
  }
  if (opts.targetMsgId) {
    // Centre the window on a specific message id (search nav, virtualized
    // branch-swipe target). Wins over preserveScroll because the caller
    // explicitly wants this id materialised.
    const idx = path.findIndex(m => m.id === opts.targetMsgId);
    if (idx >= 0) {
      const half = Math.floor(VIRT_WINDOW_TARGET / 2);
      _virtWindowStart = Math.max(0, idx - half);
      _virtWindowEnd = Math.min(path.length, _virtWindowStart + VIRT_WINDOW_TARGET);
      _virtWindowStart = Math.max(0, _virtWindowEnd - VIRT_WINDOW_TARGET);
      return;
    }
  }
  // Anchor at the tip when:
  //   • caller explicitly asked (`opts.anchorTip` — set by sendUserMessage
  //     so the freshly-persisted user bubble lands in the rendered window
  //     instead of the bottom spacer above the typing indicator);
  //   • currently generating (the typing indicator + tail must be loaded);
  //   • the existing window is uninitialised or now past the path's end;
  //   • the user was already near the bottom AND the caller didn't ask us
  //     to preserve scroll (initial chat open, streaming completion,
  //     incoming new turn). Branch swipes pass preserveScroll so the
  //     window stays put even when wasNearBottom is true.
  const anchorTip = opts.anchorTip
    || state.generating
    || _virtWindowEnd === 0
    || _virtWindowEnd > path.length
    || (wasNearBottom && !opts.preserveScroll);
  if (anchorTip) {
    _virtWindowEnd = path.length;
    _virtWindowStart = Math.max(0, _virtWindowEnd - VIRT_WINDOW_TARGET);
    return;
  }
  // Otherwise preserve the current window range, clamped to path bounds —
  // the user is mid-scroll and would be jarred by a sudden jump.
  _virtWindowEnd = Math.min(path.length, _virtWindowEnd);
  _virtWindowStart = Math.max(0, Math.min(_virtWindowStart, _virtWindowEnd));
  if (_virtWindowEnd - _virtWindowStart < VIRT_WINDOW_TARGET && _virtWindowStart > 0) {
    _virtWindowStart = Math.max(0, _virtWindowEnd - VIRT_WINDOW_TARGET);
  }
}

function _virtMeasureRendered(root) {
  for (const msgEl of root.querySelectorAll(':scope > .msg')) {
    const id = msgEl.dataset.msgId;
    if (id) _virtHeightCache.set(id, msgEl.offsetHeight);
  }
}

// Width-change handling: bubble heights depend on viewport width (text
// wrapping), so any width change invalidates ``_virtHeightCache`` for
// the off-window prefix and suffix. Without re-anchoring, spacer heights
// become wrong, scrollTop no longer maps to the same message, and the
// next slide computes its target with stale numbers.
//
// We track the message currently at viewport top via a scroll listener
// (rAF-debounced) — that way when the ResizeObserver fires (which
// happens AFTER layout has reflowed to the new width, i.e. too late to
// query "msg at viewport top" using offsetTop math, since the heights
// are already different) we have a pre-resize id to pin to. The
// observer wipes ``_virtHeightCache`` and re-renders with the tracked
// msg anchored; rendered msgs are re-measured on the upcoming rAF,
// repopulating the cache cleanly.
let _virtResizeObserver = null;
let _virtResizeObserverRoot = null;
let _virtLastWidth = 0;
let _virtScrollListenerRoot = null;
let _virtTrackedTopMsgId = null;
let _virtTrackedScrollUpdatePending = false;

function _virtSetupResizeObserver(root) {
  if (_virtResizeObserverRoot === root) return;
  if (_virtResizeObserver) _virtResizeObserver.disconnect();
  _virtResizeObserverRoot = root;
  // Sentinel: the first ``observe()`` callback delivers the current
  // contentRect width (which differs from clientWidth by the box
  // model's padding), so we let the first delivery seed the baseline
  // instead of pre-seeding from a different measurement and falsely
  // firing _virtOnWidthChange on chat open.
  _virtLastWidth = -1;
  _virtResizeObserver = new ResizeObserver((entries) => {
    if (state.generating || _virtPinnedUntilNextLoad) return;
    if (_swipeState && _swipeState.locked) return;
    for (const entry of entries) {
      const w = Math.round(entry.contentRect.width);
      if (w === _virtLastWidth) continue;
      const wasUnknown = _virtLastWidth === -1;
      _virtLastWidth = w;
      if (wasUnknown) continue;
      _virtOnWidthChange();
      return;
    }
  });
  _virtResizeObserver.observe(root);
}

function _virtSetupScrollAnchorTracker(root) {
  if (_virtScrollListenerRoot === root) return;
  if (_virtScrollListenerRoot) {
    _virtScrollListenerRoot.removeEventListener('scroll', _virtTrackScrollAnchor);
  }
  _virtScrollListenerRoot = root;
  root.addEventListener('scroll', _virtTrackScrollAnchor, { passive: true });
  _virtTrackScrollAnchor();
}

function _virtTrackScrollAnchor() {
  if (_virtTrackedScrollUpdatePending) return;
  _virtTrackedScrollUpdatePending = true;
  requestAnimationFrame(() => {
    _virtTrackedScrollUpdatePending = false;
    const root = document.getElementById('chat-messages');
    if (!root) return;
    const allMessages = state.chatMessages || [];
    const path = state.activePathIds.map(id => allMessages.find(m => m.id === id)).filter(Boolean);
    if (path.length === 0) {
      _virtTrackedTopMsgId = null;
      return;
    }
    _virtTrackedTopMsgId = _virtIdentifyViewportAnchor(root, path);
  });
}

function _virtIdentifyViewportAnchor(root, path) {
  // Pick the message currently at viewport top. Branch first on
  // "spacer vs rendered" using the rendered window's bounds — for a
  // user sitting in a spacer, the rendered msgs' offsetTops aren't
  // useful (they're all below the viewport). Then within the rendered
  // area, return the first msg whose bottom is past scrollTop, which
  // handles both "scrollTop is inside a msg" and "scrollTop is in the
  // flex gap between two msgs" cases correctly.
  const scrollTop = root.scrollTop;
  const renderedMsgs = root.querySelectorAll(':scope > .msg');
  const firstRendered = renderedMsgs[0];
  const lastRendered = renderedMsgs[renderedMsgs.length - 1];
  if (!firstRendered) {
    return path.length > 0 ? path[0].id : null;
  }
  // Above the rendered window → the user is in the top spacer.
  if (scrollTop < firstRendered.offsetTop) {
    let cumH = 0;
    for (let i = 0; i < _virtWindowStart && i < path.length; i++) {
      const h = _virtHeightCache.get(path[i].id) || VIRT_ESTIMATED_HEIGHT;
      if (cumH + h > scrollTop) return path[i].id;
      cumH += h;
    }
    return firstRendered.dataset.msgId;
  }
  // Below the rendered window → the user is in the bottom spacer.
  const lastBottom = lastRendered.offsetTop + lastRendered.offsetHeight;
  if (scrollTop >= lastBottom) {
    let cumH = lastBottom;
    for (let i = _virtWindowEnd; i < path.length; i++) {
      const h = _virtHeightCache.get(path[i].id) || VIRT_ESTIMATED_HEIGHT;
      if (cumH + h > scrollTop) return path[i].id;
      cumH += h;
    }
    return path[path.length - 1].id;
  }
  // Inside the rendered window → first msg whose bottom is past scrollTop.
  for (const msgEl of renderedMsgs) {
    if (scrollTop < msgEl.offsetTop + msgEl.offsetHeight) {
      return msgEl.dataset.msgId;
    }
  }
  return lastRendered.dataset.msgId;
}

function _virtOnWidthChange() {
  const chat = getChatById(state, state.activeChatId);
  if (!chat) return;
  const root = document.getElementById('chat-messages');
  if (!root) return;

  const allMessages = state.chatMessages || [];
  const path = state.activePathIds.map(id => allMessages.find(m => m.id === id)).filter(Boolean);
  const pathLength = path.length;
  if (pathLength === 0) return;

  // Use the pre-resize tracked viewport-top msg as the anchor. The
  // scroll listener (which runs in step 6 of "update the rendering",
  // before the resize-observer step 14) keeps this in sync with the
  // user's actual scroll position. Identifying from current layout
  // here would pin to whatever sits at scrollTop in the just-reflowed
  // (new-width) layout — typically a different msg, drifting the
  // user's anchor by a row or more per resize.
  let anchorMsgId = _virtTrackedTopMsgId;
  if (!anchorMsgId || !path.some(m => m.id === anchorMsgId)) {
    anchorMsgId = _virtIdentifyViewportAnchor(root, path);
  }

  // Drop the cache wholesale — every entry is potentially stale at the
  // new width. The post-refresh rAF measure repopulates entries for
  // currently-rendered msgs; off-window msgs fall back to estimates
  // until they next scroll into view.
  _virtHeightCache.clear();

  // refreshMessages's targetMsgId branch centres the msg in the window
  // and in the viewport. We override afterwards to top-align the anchor
  // — preserves "what the user was looking at" more faithfully than
  // centring across resize. Sync set first to get close, then rAF to
  // settle after rendered-msg measurements (which can shift offsetTop
  // for spacer indices via height cache repopulation, in turn nudging
  // the layout).
  refreshMessages(chat, { targetMsgId: anchorMsgId });
  const pinAnchor = () => {
    const target = root.querySelector(
      `.msg[data-msg-id="${CSS.escape(anchorMsgId)}"]`,
    );
    if (target) root.scrollTop = target.offsetTop;
  };
  pinAnchor();
  requestAnimationFrame(pinAnchor);
}

let _virtObserverRoot = null;

function _virtSetupObserver(root) {
  // An IntersectionObserver's ``root`` is locked at construction time.
  // When the chat-messages container is replaced (tab switch back into
  // the chat tab destroys the old element and renderChatView creates a
  // fresh one), the existing observer is still pointed at the detached
  // node and silently never fires — so spacers never expand, and any
  // sibling-switch that grows the path past the cached window leaves
  // those messages stranded in the bottom spacer until reload. Rebuild
  // the observer whenever the root element identity changes.
  if (!_virtObserver || _virtObserverRoot !== root) {
    if (_virtObserver) _virtObserver.disconnect();
    _virtObserverRoot = root;
    _virtObserver = new IntersectionObserver((entries) => {
      // Gate: defer slides that would race with an in-flight gesture or
      // streaming flow. The trailing _swipeJustFired window catches the
      // brief post-pointerup interval before the swipe-commit settles.
      if (state.generating || _virtPinnedUntilNextLoad) return;
      if (_swipeState && _swipeState.locked) return;
      if (_swipeJustFired) return;
      for (const entry of entries) {
        if (!entry.isIntersecting) continue;
        if (entry.target === _virtTopSpacer || entry.target === _virtBottomSpacer) {
          _virtSlide();
          return;
        }
      }
    }, { root, rootMargin: VIRT_TRIGGER_MARGIN });
  }
  _virtObserver.disconnect();
  if (_virtTopSpacer) _virtObserver.observe(_virtTopSpacer);
  if (_virtBottomSpacer) _virtObserver.observe(_virtBottomSpacer);
}

function _virtSlide() {
  const chat = getChatById(state, state.activeChatId);
  if (!chat) return;
  const root = document.getElementById('chat-messages');
  if (!root) return;

  const allMessages = state.chatMessages || [];
  const path = state.activePathIds.map(id => allMessages.find(m => m.id === id)).filter(Boolean);
  const pathLength = path.length;
  if (pathLength === 0) return;

  // Compute the target window centred on the message currently at the
  // viewport top. In one shot the user lands inside the new rendered
  // window, the IO state cleanly transitions to non-intersecting, and
  // the next user-driven scroll re-arms the trigger naturally. Handles
  // arbitrary scroll distance (scrollbar drag, pgup×N) in a single pass
  // without cascading.
  const scrollTop = root.scrollTop;
  let cumH = 0;
  let topIdx = 0;
  for (; topIdx < pathLength; topIdx++) {
    const id = path[topIdx].id;
    const h = _virtHeightCache.get(id) || VIRT_ESTIMATED_HEIGHT;
    if (cumH + h > scrollTop) break;
    cumH += h;
  }
  const half = Math.floor(VIRT_WINDOW_TARGET / 2);
  let newStart = Math.max(0, topIdx - half);
  let newEnd = Math.min(pathLength, newStart + VIRT_WINDOW_TARGET);
  newStart = Math.max(0, newEnd - VIRT_WINDOW_TARGET);

  if (newStart === _virtWindowStart && newEnd === _virtWindowEnd) return;

  _virtWindowStart = newStart;
  _virtWindowEnd = newEnd;
  refreshMessages(chat, { virtSlide: true });
  // The slide changes which msgs are in the DOM but doesn't fire a
  // scroll event (scrollTop is unchanged), so the rAF-debounced scroll
  // tracker won't pick up the new viewport-top msg on its own. Update
  // it here so a resize observer fire that follows the slide pins to
  // the correct anchor.
  _virtTrackedTopMsgId = _virtIdentifyViewportAnchor(root, path);
  // No scrollTop adjustment: spacer heights account for off-window
  // indices, so content y-positions are preserved (modulo small drift
  // from estimate-vs-measured spacer entries) and the user keeps
  // viewing the same message.
}


/* Fetch the chat-dependent entities the chat view consumes off the
 * ``activeChat*`` slots, plus the server-computed pick-macro flag. The
 * list endpoints return ``*Summary`` projections that strip ``tts`` /
 * ``persona`` / ``brains`` / ``background_blur`` etc., so anything
 * non-summary-safe routes through these full entities. All sub-fetches
 * tolerate failure (404 from a deleted participant) — the missing-deps
 * banner covers absent entities. */
async function _fetchActiveChatDeps(chat) {
  const [contact, user, scenario, picks] = await Promise.all([
    chat.contact_id  ? api.getContact(chat.contact_id).catch(() => null)
                     : Promise.resolve(null),
    chat.user_id     ? api.getUser(chat.user_id).catch(() => null)
                     : Promise.resolve(null),
    chat.scenario_id ? api.getScenario(chat.scenario_id).catch(() => null)
                     : Promise.resolve(null),
    api.chatUsesPickMacro(chat.id).then(r => !!r.uses).catch(() => false),
  ]);
  return {
    activeChatContact: contact,
    activeChatUser: user,
    activeChatScenario: scenario,
    chatUsesPickMacro: picks,
  };
}


export async function loadActiveChat(chatId, opts = {}) {
  try {
    const [chat, messages, path] = await Promise.all([
      api.getChat(chatId),
      api.listMessages(chatId),
      api.activePath(chatId),
    ]);
    const deps = await _fetchActiveChatDeps(chat);
    // Stash the full Chat (with chat-tree state) in ``chatMap`` so
    // consumers — refreshMessages reads ``selected_child_id`` /
    // ``last_deleted_child``, the chat info modal needs the full
    // body for save spreads — can ``getChatById`` it back regardless
    // of whether the chat is in the visible list pane page.
    // ``api.getChat`` returns a plain Chat without ``message_count``
    // (the list endpoint computes it from the cached field on disk),
    // so re-derive from the active path we already loaded — matches
    // what the list endpoint reports (active branch only, not every
    // message on disk) and keeps the chat-list badge accurate after
    // a click and after every message-mutating action.
    const merged = { ...chat, message_count: path.length };
    setChatInMap(chatId, merged);
    // Also patch state.chats so the list row (rendered with
    // ChatSummary fields) picks up the fresh ``message_count`` etc.
    // The patch is a no-op if the chat isn't in the current page.
    const chats = state.chats.map(c => c.id === chatId ? { ...c, message_count: path.length } : c);
    setState({
      chats,
      chatMessages: messages,
      activePathIds: path.map(m => m.id),
      ...deps,
      // Tracks which chat the cached messages belong to — used by
      // `renderChatView` to detect (and clear) stale cache when the
      // active chat changes.
      chatMessagesFor: chatId,
    });
    refreshMessages(chat, {
      preserveScroll: !!opts.preserveScroll,
      anchorTip: !!opts.anchorTip,
    });
    // Path / messages just changed — kick off a debounced re-tokenisation so
    // the ctx stat reflects the new prompt. The SSE ``context`` event takes
    // precedence during generation; ``scheduleContextRefresh`` skips itself
    // in that case. ``flush`` forwards through for structural changes
    // (branch swap, bookmark restore, edit / delete / restore of a message).
    scheduleContextRefresh(chatId, { flush: !!opts.flush });
  } catch (e) { toast(`Could not load chat: ${e.message}`, 'error'); }
}


/* External-trigger wrapper for ``refreshMessages``. Modules that mutate
 * ``state.chatMessages`` (e.g. the per-message brain editor) call this so the
 * already-rendered messages pick up the new server data instead of staying
 * frozen on their closure-captured ``msg`` objects until the user navigates
 * away and back. */
export function refreshActiveChatMessages(opts = {}) {
  const chat = getChatById(state, state.activeChatId);
  if (!chat) return;
  refreshMessages(chat, { preserveScroll: true, ...opts });
}


function refreshMessages(chat, opts = {}) {
  const root = document.getElementById('chat-messages');
  if (!root || !chat) return;

  // Chat switch invalidates virtualization caches.
  if (_virtCurrentChatId !== chat.id) {
    _virtCurrentChatId = chat.id;
    _virtHeightCache.clear();
    _virtWindowStart = 0;
    _virtWindowEnd = 0;
    _virtFirstRefreshAfterSwitch = true;
  }

  // Capture scroll state *before* re-rendering. The caller can force the
  // scroll to be preserved (branch nav clicks shouldn't ever yank the page);
  // otherwise auto-scroll only when the user was already near the bottom.
  const preserveScroll = !!opts.preserveScroll;
  const wasNearBottom = !preserveScroll && (
    root.scrollHeight === 0 ||
    root.scrollTop + root.clientHeight >= root.scrollHeight - 100
  );
  const prevScroll = root.scrollTop;
  const prevScrollHeight = root.scrollHeight;
  // Pre-existing shrink-filler from a previous swap whose animation is
  // still in flight — drop it now so we don't double up. Its scrollHeight
  // contribution would otherwise corrupt our shrink-amount calculation.
  const stalefiller = root.querySelector('.shrink-filler');
  if (stalefiller) stalefiller.remove();

  // Detach the in-flight typing indicator (if any) before replaceChildren
  // wipes it. The reroll's SSE bubbles still target it via closure, and the
  // virtual-sibling lock relies on its data-msg-id, so we re-attach it at
  // the end of the render below — but only while a generation is actually
  // in flight. Once gen completes we let ``replaceChildren`` wipe the
  // indicator atomically alongside the old messages, so the user doesn't
  // see a brief "no indicator, no new message" gap.
  const existingIndicator = state.generating
    ? document.getElementById('typing-indicator')
    : null;

  // Synchronously remove the OLD content first, freezing the displayed
  // scroll position before we paint the replacement. The browser doesn't
  // get a frame between this removal and the append loop below — they're
  // all sync — so the user never sees the cleared state.
  root.replaceChildren();

  const allMessages = state.chatMessages || [];
  const path = state.activePathIds.map(id => allMessages.find(m => m.id === id)).filter(Boolean);

  _virtComputeWindow(path, opts, wasNearBottom);

  // Top spacer (heights of indices [0, _virtWindowStart) — measured where
  // we've seen them, estimated otherwise).
  if (_virtWindowStart > 0) {
    _virtTopSpacer = _virtBuildSpacer(path, 0, _virtWindowStart, 'top');
    root.append(_virtTopSpacer);
  } else {
    _virtTopSpacer = null;
  }

  // Window messages. ``prevSender`` seeds from the last off-window message
  // so a same-sender run continues correctly across the spacer boundary.
  let prevSender = _virtWindowStart > 0 ? path[_virtWindowStart - 1].sender : null;
  for (let i = _virtWindowStart; i < _virtWindowEnd; i++) {
    const msg = path[i];
    const isGroupStart = msg.sender !== prevSender;
    root.append(renderMessage(msg, chat, allMessages, { isGroupStart }));
    prevSender = msg.sender;
  }

  // Bottom spacer (heights of indices [_virtWindowEnd, path.length)).
  if (_virtWindowEnd < path.length) {
    _virtBottomSpacer = _virtBuildSpacer(path, _virtWindowEnd, path.length, 'bottom');
    root.append(_virtBottomSpacer);
  } else {
    _virtBottomSpacer = null;
  }

  // Undelete row: if the next-position from the path tip is __empty__ AND
  // there are soft-deleted siblings there, show a restore prompt. Works
  // both for mid-chat deletions (path tip = some message) and for root-
  // level deletions (path is empty, tip = root).
  const tail = path.length > 0 ? path[path.length - 1] : null;
  const childKey = tail ? tail.id : '';
  const sel = chat.selected_child_id ? chat.selected_child_id[childKey] : undefined;
  let appendedUndelete = false;
  if (sel === '__empty__') {
    const parentId = tail ? tail.id : null;
    const deletedSiblings = allMessages.filter(m => m.parent_id === parentId);
    if (deletedSiblings.length > 0) {
      const justDeletedId = chat.last_deleted_child?.[childKey];
      root.append(renderUndeleteRow(chat, deletedSiblings, justDeletedId));
      appendedUndelete = true;
    }
  }

  if (path.length === 0 && !appendedUndelete) {
    root.append(el('div', { class: 'list-empty', style: { padding: '64px 16px' } },
      'No messages yet. Type something below to start.'
    ));
  }

  // Re-attach the in-flight typing indicator at the end so a mid-reroll
  // ``loadActiveChat`` (e.g. from sibling-swap nav) doesn't wipe it.
  if (existingIndicator) root.appendChild(existingIndicator);

  // Slide-driven re-renders skip the whole scroll positioning / shrink-
  // filler / rAF bottom-reaffirm block — the slide caller adjusts
  // scrollTop itself based on the spacer-height delta so the user's
  // viewport content stays put. Same goes for ``targetMsgId`` refreshes:
  // we centre the target message in view synchronously, which keeps the
  // top + bottom spacers far from the viewport edges and prevents the
  // IntersectionObserver from firing additional slides during a smooth
  // scroll (which would yank the target back out of the window).
  if (opts.virtSlide || opts.targetMsgId) {
    if (opts.targetMsgId) {
      const target = root.querySelector(
        `.msg[data-msg-id="${CSS.escape(opts.targetMsgId)}"]`,
      );
      if (target) {
        const center = Math.max(0,
          target.offsetTop - (root.clientHeight - target.offsetHeight) / 2);
        root.scrollTop = center;
      }
    }
    requestAnimationFrame(() => { _virtMeasureRendered(root); });
    _virtSetupObserver(root);
    _virtSetupResizeObserver(root);
    _virtSetupScrollAnchorTracker(root);
    _searchReapplyHighlights();
    _refreshContextDivider();
    return;
  }

  // Sync the scroll position before the browser paints the new layout,
  // otherwise the empty-DOM moment between ``replaceChildren`` and the
  // append loop briefly clamps scrollTop, then the rAF restores it — the
  // user perceives that as a one-frame upward bounce.
  //
  // For preserveScroll branch swaps where the new content is *shorter*
  // than the old, naively setting `scrollTop = prevScroll` makes the
  // browser auto-clamp instantly to the new max — visible as a sudden
  // "scroll-down clack". Instead, append a shrink-filler that maintains
  // the old layout height, then animate filler.height + scrollTop down
  // together via rAF so the user sees a smooth transition into the
  // new (shorter) content.
  const newScrollHeight = root.scrollHeight;
  const shrinkAmount = Math.max(0, prevScrollHeight - newScrollHeight);
  const newMaxScroll = Math.max(0, newScrollHeight - root.clientHeight);
  const isTouchInput = window.matchMedia
    && window.matchMedia('(pointer: coarse)').matches;
  // The shrink-filler animation maintains the old layout height with a
  // temporary spacer, then animates filler.height + scrollTop down in
  // lockstep so the user sees a smooth transition into the new (shorter)
  // content. Used both for preserveScroll branch swaps (shorter sibling)
  // and for streaming completion (touch) when the indicator is wider
  // than the persisted msg by enough that auto-clamp would visibly
  // pop the layout up. ``prevScroll > newMaxScroll`` already implies
  // the user was at or past the new bottom, so we don't need a
  // separate wasNearBottom check.
  const shouldShrinkSmooth = shrinkAmount > 0 && prevScroll > newMaxScroll
    && (preserveScroll || isTouchInput);
  if (shouldShrinkSmooth) {
    const filler = el('div', { class: 'shrink-filler' });
    filler.style.height = `${shrinkAmount}px`;
    filler.style.flexShrink = '0';
    filler.style.pointerEvents = 'none';
    root.appendChild(filler);
    root.scrollTop = prevScroll;
    const fromScroll = prevScroll;
    const toScroll = newMaxScroll;
    const startTime = performance.now();
    const duration = 250;
    const animate = (now) => {
      const t = Math.min(1, (now - startTime) / duration);
      const eased = 1 - Math.pow(1 - t, 3);  // ease-out cubic
      filler.style.height = `${shrinkAmount * (1 - eased)}px`;
      root.scrollTop = fromScroll + (toScroll - fromScroll) * eased;
      if (t < 1) requestAnimationFrame(animate);
      else filler.remove();
    };
    requestAnimationFrame(animate);
  } else if (wasNearBottom && isTouchInput && !_virtFirstRefreshAfterSwitch) {
    // Touch: ease the new tail into view rather than snapping. The
    // first-paint position is whatever the browser auto-clamped after
    // replaceChildren (= 0); set it instantly to prevScroll first so
    // the animation starts where the user was, then smooth-scroll to
    // the new bottom.
    //
    // Skipped for the first refresh after a chat switch — smoothly
    // scrolling from 0 to newMaxScroll across a long virtualized chat
    // crosses the top-spacer's IntersectionObserver threshold mid-
    // animation, fires slide events, and the user lands somewhere
    // mid-history instead of at the bottom. Snap directly there.
    root.scrollTop = Math.min(prevScroll, newMaxScroll);
    requestAnimationFrame(() => {
      root.scrollTo({ top: newMaxScroll, behavior: 'smooth' });
    });
  } else {
    if (wasNearBottom) root.scrollTop = newMaxScroll;
    else root.scrollTop = Math.min(prevScroll, newMaxScroll);
  }

  requestAnimationFrame(() => {
    // Re-affirm the bottom anchor in case images / fonts settled and grew
    // the layout between sync update above and this frame. Smooth on
    // touch when there's prior content to ease away from; snap on the
    // first refresh after a chat switch (a smooth scroll there would
    // cross the top-spacer's IO threshold and slide the user off-bottom).
    if (wasNearBottom) {
      if (isTouchInput && !_virtFirstRefreshAfterSwitch) {
        root.scrollTo({
          top: root.scrollHeight - root.clientHeight,
          behavior: 'smooth',
        });
      } else {
        root.scrollTop = root.scrollHeight;
      }
    }
    // Reapply the nav-lock highlight after the wholesale re-render. If the
    // locked id is no longer on the active path (deleted, branch dropped),
    // we silently release — except when the lock is on the virtual reroll
    // sibling, which intentionally has no DOM element of its own. Window
    // virtualization can also hide the locked message; we leave the lock
    // intact in that case so it re-highlights when the slide brings it back.
    const virtualLocked = _virtualMsg && _navLockedId === _virtualMsg.id;
    const lockedOnPath = _navLockedId && state.activePathIds.includes(_navLockedId);
    if (_navLockedId && !virtualLocked && !lockedOnPath) {
      _navLockedId = null;
    }
    applyNavLockHighlight();
    _virtMeasureRendered(root);
  });
  _virtSetupObserver(root);
  _virtSetupResizeObserver(root);
  _virtSetupScrollAnchorTracker(root);
  _searchReapplyHighlights();
  _refreshContextDivider();
  // Clear the streaming pin at the end of any non-slide refresh — the
  // post-stream ``loadActiveChat`` has now landed.
  _virtPinnedUntilNextLoad = false;
  _virtFirstRefreshAfterSwitch = false;
}


function renderUndeleteRow(chat, deletedSiblings, justDeletedId) {
  // Prefer the message the user *just* deleted; fall back to the most-recent
  // sibling for cases where the chat predates the last-deleted tracking.
  const target =
    deletedSiblings.find(m => m.id === justDeletedId) ||
    [...deletedSiblings].sort((a, b) => (b.timestamp || 0) - (a.timestamp || 0))[0];
  return el('div', { class: 'undelete-row' },
    icon('undo', 16),
    el('div', { class: 'undelete-text' }, 'Recently deleted'),
    el('button', {
      class: 'btn',
      onClick: async () => {
        await api.restoreMessage(chat.id, target.id);
        await loadActiveChat(chat.id, { flush: true });
      },
    }, 'Undelete'),
  );
}


function renderMessage(msg, chat, allMessages, opts = {}) {
  const isContact = msg.sender === 'contact';
  const contact = state.contacts.find(c => c.id === chat.contact_id);
  const user = state.users.find(u => u.id === chat.user_id);
  const isGroupStart = opts.isGroupStart !== false;

  const wrap = el('div', {
    class: `msg ${isContact ? 'contact' : 'user'} ${isGroupStart ? 'group-start' : 'group-cont'}`,
    dataset: { msgId: msg.id },
  });

  // For user messages we wrap the header + bubbles in an inline-flex column
  // so the container sizes to the widest of (header content, bubble content),
  // capped at 80% — keeps the username's left edge connected to the bubble.
  const target = isContact ? wrap : el('div', { class: 'user-content' });
  if (!isContact) wrap.append(target);

  // Decide whether the first bubble will render with an avatar slot. The
  // popover trigger lands on the avatar if present, or on a small ``(i)``
  // badge in the meta-row next to the sender name when the bubble is
  // "bare". The conditions mirror ``renderBubbleRow``'s avatar-priority
  // chain so the trigger is always reachable from somewhere visible.
  const firstBubble = (msg.body && msg.body[0]) || null;
  const isGenericBare =
    isContact && msg.origin === 'generic' &&
    (firstBubble && firstBubble.emotion == null) &&
    !(contact && contact.avatar);
  const isUserBare = !isContact && !userAvatarEl(user);
  const bubbleHasNoAvatar = isGenericBare || isUserBare;

  if (isGroupStart) {
    // First message of a same-sender run: show the sender-name + controls.
    // The info badge sits inline next to the name when no avatar is present.
    const senderName = _senderNameEl(msg.sender_name, isContact ? contact : user, isContact ? 'contact' : 'user');
    const metaRow = el('div', { class: 'meta-row' },
      senderName,
      ...(bubbleHasNoAvatar ? [makeMessageInfoBadge(msg)] : []),
      el('span', { style: { flex: 1 } }),
      renderControls(msg, chat, allMessages),
    );
    target.append(metaRow);
  } else {
    // Continuation: hover-only floating controls overlay (top-right).
    target.append(el('div', { class: 'controls-overlay' },
      renderControls(msg, chat, allMessages),
    ));
  }

  if (msg.brains && msg.brains.length) {
    target.append(el('div', { class: 'meta-row brain-badge-row' },
      el('span', { class: 'brain-badge' },
        '🧠 ' + msg.brains.map(b => b.name).join(', ')),
    ));
  }

  // Reasoning tray lives INSIDE the first bubble at the top, when
  // ``message.reasoning`` is populated. Bubble-mode-agnostic; today only
  // Generic emits reasoning. The tray inherits the bubble's padding so
  // it reads as part of the message, not a sibling block above it.
  // Open state is tracked in ``_expandedReasoningIds`` so it survives
  // re-renders (refresh after stream, branch swap, virtualization slide).
  let pendingTray = msg.reasoning ? renderReasoningTray(msg.reasoning, {
    startExpanded: _expandedReasoningIds.has(msg.id),
    onToggle: (expanded) => {
      if (expanded) _expandedReasoningIds.add(msg.id);
      else _expandedReasoningIds.delete(msg.id);
    },
  }) : null;

  let firstRow = true;
  for (const bubble of msg.body) {
    const row = renderBubbleRow(bubble, contact, isContact, user, msg.origin);
    if (firstRow && pendingTray) {
      const bubbleEl = row.querySelector('.bubble');
      if (bubbleEl) bubbleEl.insertBefore(pendingTray, bubbleEl.firstChild);
      else target.append(pendingTray);
      pendingTray = null;
    }
    if (firstRow && !bubbleHasNoAvatar) {
      // Attach the popover trigger to the avatar element in the first
      // bubble row. Subsequent bubbles don't get their own trigger —
      // popover anchors on the first avatar regardless of which bubble
      // the user clicks (mirrors the meta-row's per-message position).
      const avatarEl = row.querySelector('.emotion-sprite, .avatar');
      if (avatarEl) attachAvatarTrigger(avatarEl, msg);
    }
    target.append(row);
    firstRow = false;
  }
  // Defensive: if msg.body was empty but reasoning was set, surface the
  // tray standalone rather than dropping it.
  if (pendingTray) target.append(pendingTray);

  // Per-message attachments (generic mode multimodal images). Rendered
  // below the bubble stack as inline thumbnails. Click opens the
  // original in a new tab.
  if (msg.attachments && msg.attachments.length) {
    const row = el('div', { class: 'bubble-attachments' });
    for (const att of msg.attachments) {
      if (att.mime && att.mime.startsWith('image/')) {
        const url = `/api/files/chats/${chat.id}/attachments/${att.id}`;
        const link = el('a', {
          href: url,
          target: '_blank',
          rel: 'noopener',
          title: att.filename || 'attachment',
        }, el('img', { src: url, alt: att.filename || 'attachment' }));
        row.append(link);
      }
    }
    if (row.children.length) target.append(row);
  }

  return wrap;
}


// Sender-name link. Anchor element so the browser shows the link cursor
// natively and right-click → "Open in new tab" works; the SPA intercepts
// plain clicks via ``preventDefault`` and routes through ``setState`` so
// no full page load happens. Falls back to a plain span when the contact
// or user has been deleted, leaving dangling rows non-interactive.
function _senderNameEl(name, entity, kind) {
  if (!entity || !entity.id) return el('span', { class: 'sender-name' }, name || '');
  const tab = kind === 'user' ? 'users' : 'contacts';
  const slug = `${slugify(entity.name || '')}-${(entity.id || '').slice(0, 8)}`;
  return el('a', {
    class: 'sender-name',
    href: `/${tab}/${slug}`,
    title: kind === 'user' ? 'Open user' : 'Open contact',
    onClick: (e) => {
      // Plain left-click navigates within the SPA; modifier-clicks (cmd /
      // ctrl / shift / middle-click) fall through to native handling so
      // the user can open in a new tab the usual way.
      if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
      e.preventDefault();
      if (kind === 'user') setState({ activeTab: 'users', activeUserId: entity.id });
      else setState({ activeTab: 'contacts', activeContactId: entity.id });
    },
  }, name || '');
}


export function renderBubbleRow(bubble, contact, isContact, user, origin) {
  // Dispatch on the per-message ``origin``:
  // - "generic" → markdown via the marked-backed renderer with HTML
  //   sanitization gated by ``state.settings.sanitize_generic_html``
  //   (default on). Returns a DocumentFragment which we append.
  // - "aer" / "manual" / legacy (no origin field) → AER's
  //   line-highlighter, same as today. Returns an HTML string.
  // Manual messages explicitly route through AER too: user-typed text
  // is plain text the user typed, and AER's highlighter is unobtrusive.
  // Surprising the user with markdown rendering (lists, fences, links)
  // of their own input is worse than the alternative.
  const bubbleEl = el('div', { class: 'bubble' });
  if (origin === 'generic') {
    bubbleEl.classList.add('generic');
    const sanitize = !(state.settings && state.settings.sanitize_generic_html === false);
    // This is the committed static render (chat load, post-stream rebuild,
    // branch nav, virtualization slide). Activate `<script>` tags here — the
    // streaming path fills the bubble via renderGenericIntoBubble, which
    // leaves them inert until this rebuild runs after generation completes.
    bubbleEl.appendChild(renderGenericBubble(bubble.text, {
      sanitizeHtml: sanitize,
      executeScripts: true,
    }));
  } else {
    // Contact-side AER bubbles collapse the model's ``\n\n`` paragraph
    // spacing to a single break (see aer_render.js); user-typed text renders
    // verbatim so a message the user formatted by hand keeps its blank lines.
    bubbleEl.innerHTML = renderBubble(bubble.text, { collapseBlankLines: isContact });
  }

  if (!isContact) {
    // User avatar sits to the LEFT of the bubble — same internal layout
    // as a contact row, just right-aligned as a block via the parent
    // ``.user-content``. ``userAvatarEl`` returns null when no avatar is
    // set, in which case the bubble lays out exactly as before.
    const avatar = userAvatarEl(user);
    if (avatar) {
      return el('div', { class: 'bubble-row user with-avatar' }, avatar, bubbleEl);
    }
    return el('div', { class: 'bubble-row user' }, bubbleEl);
  }

  // Contact-side: Generic-origin avatar priority is emotion sprite (if
  // emotion is non-null AND the contact has it) → contact avatar →
  // bare bubble. AER-origin uses the existing renderEmotionSprite
  // fallback chain. Both code paths land at renderEmotionSprite — it
  // already implements the right priority order (emotion sprite →
  // contact avatar → text fallback); we only need to suppress the
  // sprite entirely when origin is "generic" AND emotion is null AND
  // the contact has no avatar (the "bare bubble" case).
  if (origin === 'generic' && bubble.emotion == null) {
    const hasContactAvatar = contact && contact.avatar;
    if (!hasContactAvatar) {
      return el('div', { class: 'bubble-row contact bare' }, bubbleEl);
    }
  }
  return el('div', { class: 'bubble-row contact' },
    renderEmotionSprite(bubble.emotion, contact),
    bubbleEl,
  );
}


function renderEmotionSprite(emotion, contact) {
  emotion = emotion || 'neutral';
  let imgUrl = null;
  if (contact && contact.emotions && contact.emotions[emotion]) {
    imgUrl = `/api/files/contacts/${contact.id}/emotions/${emotion}/display`;
  } else if (contact && contact.avatar) {
    imgUrl = `/api/files/contacts/${contact.id}/avatar/display`;
  } else if (contact && contact.emotions && contact.emotions.neutral) {
    imgUrl = `/api/files/contacts/${contact.id}/emotions/neutral/display`;
  }
  if (imgUrl) {
    return el('div', { class: 'emotion-sprite', title: emotion },
      el('img', { src: imgUrl, alt: emotion }),
    );
  }
  return el('div', { class: 'emotion-sprite no-image', title: emotion }, emotion);
}


function renderControls(msg, chat, allMessages) {
  const isContact = msg.sender === 'contact';
  // Include the in-flight virtual sibling so the branch counter shows
  // ``N+1/N+1`` (or e.g. ``4/5`` after curleft from the virtual) instead
  // of dropping back to the persisted-only count mid-reroll.
  const siblings = (() => {
    let s = allMessages.filter(m => m.parent_id === msg.parent_id);
    if (_virtualMsg && _virtualMsg.parent_id === msg.parent_id) s = [...s, _virtualMsg];
    return s.sort((a, b) => (a.timestamp || 0) - (b.timestamp || 0));
  })();
  const idx = siblings.findIndex(m => m.id === msg.id);

  const controls = el('div', { class: 'controls' });

  if (siblings.length > 1) {
    // The virtual sibling has no on-disk row, so clicking onto it can only
    // move the local lock; the in-flight reroll's persist sets the parent's
    // selected_child for us when it lands.
    const swap = async (target) => {
      if (!target) return;
      if (target.id === _virtualMsg?.id || msg.id === _virtualMsg?.id) {
        _virtualSlotShown = target.id === _virtualMsg?.id;
        _navLockedId = target.id;
        applyNavLockHighlight({ smooth: true });
        return;
      }
      await api.selectChild(chat.id, msg.parent_id, target.id);
      await loadActiveChat(chat.id, { preserveScroll: true, flush: true });
    };
    const branch = el('div', { class: 'branch-nav' },
      el('button', {
        class: 'icon-btn', title: 'Previous branch',
        disabled: idx <= 0,
        onClick: () => swap(siblings[idx - 1]),
      }, icon('prev', 14)),
      el('span', { style: { fontSize: '11px', color: 'var(--text-mute)' } }, `${idx + 1}/${siblings.length}`),
      el('button', {
        class: 'icon-btn', title: 'Next branch',
        disabled: idx >= siblings.length - 1,
        onClick: () => swap(siblings[idx + 1]),
      }, icon('next', 14)),
    );
    controls.append(branch);
  }

  if (isContact) {
    controls.append(el('button', {
      class: 'icon-btn', title: 'Regenerate',
      onClick: () => { primeAudioSession(); regenerate(msg, chat); },
    }, icon('reroll', 14)));
  }
  // Speaker — play this message via TTS regardless of the global /
  // per-entity gate (manual playback always works as long as a key
  // is configured for the resolved provider). Hidden if the message
  // has nothing speakable (e.g. only code fences that stripForTTS
  // would empty). Morphs into a Stop button while THIS message is
  // being voiced so a second click cancels playback.
  const hasSpeakable = (msg.body || []).some(s => stripForTTS(s.text || ''));
  if (hasSpeakable) {
    const isActive = _ttsSpeakingMsgId === msg.id
      && _ttsPlayer && _ttsPlayer.state !== 'idle';
    controls.append(el('button', {
      class: 'icon-btn' + (isActive ? ' danger' : ''),
      'data-tts-msg-id': msg.id,
      title: isActive ? 'Stop playback' : 'Play this message',
      'aria-label': isActive ? 'Stop playback' : 'Play this message',
      onClick: (e) => {
        e.stopPropagation();
        primeAudioSession();  // gesture still warm — useful on iOS
        if (_ttsSpeakingMsgId === msg.id
            && _ttsPlayer && _ttsPlayer.state !== 'idle') {
          _ttsPlayer.stop();   // toggle off
        } else {
          speakMessage(msg, chat);
        }
      },
    }, icon(isActive ? 'stop' : 'speaker', 14)));
  }
  controls.append(el('button', {
    class: 'icon-btn', title: 'Edit',
    onClick: () => openEditMessageModal(chat.id, msg),
  }, icon('edit', 14)));
  const hasBrains = !!(msg.brains && msg.brains.length);
  controls.append(el('button', {
    class: 'icon-btn' + (hasBrains ? ' has-brains' : ''),
    title: hasBrains ? `Brains (${msg.brains.length})` : 'Brains',
    onClick: () => {
      // Resolve the freshest message from state so a previously-saved brain
      // round-trips through a reopen instead of replaying the stale closure
      // captured at the time this row was rendered.
      const fresh = (state.chatMessages || []).find(m => m.id === msg.id) || msg;
      openEditBrainsModal(chat.id, fresh);
    },
  }, icon('brain', 14)));
  controls.append(el('button', {
    class: 'icon-btn danger', title: 'Delete branch (undo from the bottom of the chat)',
    onClick: async () => {
      await api.deleteMessage(chat.id, msg.id);
      await loadActiveChat(chat.id, { flush: true });
    },
  }, icon('trash', 14)));

  return controls;
}


/* ---------- TTS playback (lazy per chat-view mount) ---------- */

let _ttsPlayer = null;
let _ttsOverlay = null;
let _ttsChatId = null;
// Id of the message currently being voiced (or queued for voicing) by
// the speaker button. Drives the per-message speaker→stop morph. The
// auto-TTS path leaves this ``null`` because it speaks bubbles as
// they stream rather than a whole persisted message.
let _ttsSpeakingMsgId = null;


function ensureTTSPlayer() {
  if (!_ttsPlayer) {
    _ttsPlayer = new TTSPlayer();
    _ttsPlayer.onError((err) => toast(`TTS error: ${err.message || err}`, 'error'));
    _ttsPlayer.onStateChange((s) => {
      updateTTSOverlay(s);
      if (s === 'idle') _ttsSpeakingMsgId = null;
      repaintSpeakerButtons();
    });
  }
  return _ttsPlayer;
}


function disposeTTSPlayer() {
  if (_ttsPlayer) {
    _ttsPlayer.stop();
    _ttsPlayer = null;
  }
  _ttsOverlay = null;
  _ttsChatId = null;
  _ttsSpeakingMsgId = null;
}


/* Walk every speaker button in the chat-messages container and
 * repaint its icon based on whether its message id matches the
 * currently-speaking message. Cheap — there are at most a few dozen
 * messages visible at once. */
function repaintSpeakerButtons() {
  const root = document.getElementById('chat-messages');
  if (!root) return;
  for (const btn of root.querySelectorAll('[data-tts-msg-id]')) {
    const active = btn.dataset.ttsMsgId === _ttsSpeakingMsgId;
    btn.replaceChildren(icon(active ? 'stop' : 'speaker', 14));
    btn.title = active ? 'Stop playback' : 'Play this message';
    btn.setAttribute('aria-label', active ? 'Stop playback' : 'Play this message');
    btn.classList.toggle('danger', active);
  }
}


function buildTTSOverlay() {
  const pauseBtn = el('button', {
    type: 'button',
    class: 'tts-icon-btn',
    'data-tts-pause': '1',
    title: 'Pause',
    'aria-label': 'Pause',
    onClick: () => {
      if (!_ttsPlayer) return;
      if (_ttsPlayer.state === 'playing') _ttsPlayer.pause();
      else if (_ttsPlayer.state === 'paused') _ttsPlayer.resume();
    },
  }, icon('pause', 16));
  const stopBtn = el('button', {
    type: 'button',
    class: 'tts-icon-btn',
    title: 'Stop and clear TTS queue',
    'aria-label': 'Stop TTS',
    onClick: () => { _ttsPlayer?.stop(); },
  }, icon('stop', 16));
  return el('div', { class: 'tts-controls hidden' }, pauseBtn, stopBtn);
}


function updateTTSOverlay(state) {
  if (!_ttsOverlay) return;
  _ttsOverlay.classList.toggle('hidden', state === 'idle');
  const pauseBtn = _ttsOverlay.querySelector('[data-tts-pause]');
  if (pauseBtn) {
    pauseBtn.replaceChildren(state === 'paused' ? icon('play', 16) : icon('pause', 16));
    pauseBtn.title = state === 'paused' ? 'Resume' : 'Pause';
    pauseBtn.setAttribute('aria-label', state === 'paused' ? 'Resume' : 'Pause');
  }
}


/* Auto-TTS hook called from triggerGeneration's onBubble. Walks the
 * autoplay gate; if it passes, slices + enqueues. No-op on greeting. */
function autoplayBubble(bubble, chat, contact, user, isGreeting) {
  if (isGreeting) return;
  const settings = state.settings || {};
  if (!shouldAutoplayTTS('contact', contact, user, settings)) return;
  const cfg = resolveTTSConfig('contact', contact, user, settings);
  const clean = stripForTTS(bubble.text || '');
  if (!clean) return;
  ensureTTSPlayer().enqueue(clean, cfg);
}


/* Speaker-button handler used by ``renderControls``. Stops any
 * in-flight queue (the user just asked to hear THIS message), then
 * enqueues every bubble in ``msg.body``. */
function speakMessage(msg, chat) {
  const contact = state.activeChatContact;
  const user = state.activeChatUser;
  const sender = msg.sender || 'contact';
  const settings = state.settings || {};
  const cfg = resolveTTSConfig(sender, contact, user, settings);
  const player = ensureTTSPlayer();
  // Set BEFORE stop() so the state-change repaint sees the new id.
  _ttsSpeakingMsgId = msg.id;
  player.stop();
  // Defer the enqueue a frame so the stop()'s pause+detach fully
  // settles in the audio session before we line up the next set.
  setTimeout(() => {
    for (const sub of msg.body || []) {
      const clean = stripForTTS(sub.text || '');
      if (clean) player.enqueue(clean, cfg);
    }
    repaintSpeakerButtons();
  }, 0);
}


/* ---------- input / generation ---------- */

let _activeStream = null;

// Keyboard-nav lock: id of the message currently "selected" via arrow-key
// nav from an empty input bar. Survives sibling-swaps (we re-resolve to
// whatever the chat's selected_child becomes) and clears on type, ArrowDown
// past the bottom, End, Escape, or chat switch.
let _navLockedId = null;

// Virtual sibling pinned in place during a reroll. The new message hasn't
// been persisted yet but should still take a slot in the parent's sibling
// list so keyboard nav (e.g. curleft from N/N) lands on the right index
// instead of N-1/N. Cleared in ``triggerGeneration``'s finally; the lock
// then snaps to whatever the server's ``selected_child`` resolved to.
let _virtualMsg = null;

// Single-slot visibility flag at the virtual's parent_id: ``true`` ⇒ typing
// indicator visible, original tail hidden; ``false`` ⇒ original tail visible,
// indicator hidden. Driven ONLY by explicit left/right navigation between
// the virtual and its siblings — Up/Down/End/Esc don't touch it, so the
// indicator stays visible while the user moves their selection elsewhere.
let _virtualSlotShown = false;

// Set when the user rerolls a mid-chat (non-tail) contact message: holds
// that message's id so ``_syncVirtualVisibility`` knows to flip visibility
// on it + its downstream descendants. Null for the common tail-of-path
// reroll, where ``_syncVirtualVisibility`` falls back to walking the path.
// Cleared with the rest of the virtual state in ``triggerGeneration``'s
// finally.
let _virtualReplacingId = null;

// Per-message reasoning-tray expand state. Re-renders (refreshMessages,
// virtSlide windows, etc.) build fresh tray elements; without an external
// "this msg's tray is open" record the user's expansion is silently lost
// every refresh. Updated by the tray's ``onToggle`` callback and seeded
// in ``triggerGeneration``'s finally when the streaming tray ends expanded.
const _expandedReasoningIds = new Set();


// Type-to-focus: when the user starts typing on the chat page and no other
// editable element is focused, redirect the keystroke into the chat input.
// Module-level so it survives tab switches without re-binding (the textarea-
// presence check below makes it a no-op outside the chats tab).
document.addEventListener('keydown', (e) => {
  const ta = document.querySelector('.chat-input-area textarea');
  if (!ta) return;
  const ae = document.activeElement;
  if (ae === ta) return;
  const tag = ae?.tagName;
  if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || tag === 'BUTTON'
      || ae?.isContentEditable) return;
  const modalRoot = document.getElementById('modal-root');
  if (modalRoot && !modalRoot.classList.contains('hidden')) return;
  // Plain printable characters only — bail on modifier combos or named keys
  // like Shift, Tab, Escape, F-keys, arrow keys, etc.
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  if (e.key.length !== 1) return;

  ta.focus();
  // The keystroke fired on document so we have to insert the character
  // ourselves; the textarea won't replay it now that focus has moved.
  const start = ta.selectionStart;
  const end = ta.selectionEnd;
  ta.value = ta.value.slice(0, start) + e.key + ta.value.slice(end);
  ta.selectionStart = ta.selectionEnd = start + 1;
  ta.dispatchEvent(new Event('input'));
  e.preventDefault();
});


// Any mouse click in the document releases the keyboard-nav lock — once
// the user reaches for the mouse they're done with arrow-key navigation,
// and the highlight outline becomes visual noise.
document.addEventListener('click', () => {
  if (_navLockedId) clearNavLock();
});


// Tap-to-reveal for per-message controls on touch / hover-less devices.
// Bails out on real-hover desktops where `:hover` already reveals the
// controls (and so this handler never runs on desktop, never alters
// `_navLockedId` semantics, never fires for keyboard-driven flows).
//
// Behavior on touch:
//   • Tap a message body → toggle `.controls-revealed` on that message,
//     clear it from siblings.
//   • Tap a control button on a NOT-revealed message → swallow the tap
//     (capture-phase preventDefault + stopPropagation) and reveal only,
//     so the user doesn't trigger an invisible delete/reroll by accident.
//     Second tap on the now-visible button fires its action normally.
//   • Tap outside any message (or in chat-messages whitespace) → clear
//     all reveals.
document.addEventListener('click', (e) => {
  if (window.matchMedia && window.matchMedia('(hover: hover)').matches) return;
  // A swipe just fired — eat any trailing click so we don't also reveal
  // controls or fire button actions on the bubble we just navigated past.
  if (_swipeJustFired) {
    e.preventDefault();
    e.stopPropagation();
    return;
  }
  const messagesRoot = document.getElementById('chat-messages');
  if (!messagesRoot) return;
  const msgEl = e.target.closest('.msg');
  // Click outside any message (anywhere on the page) — clear reveals so
  // the class doesn't persist invisibly into other tabs.
  if (!msgEl || !messagesRoot.contains(msgEl)) {
    for (const m of document.querySelectorAll('.msg.controls-revealed')) {
      m.classList.remove('controls-revealed');
    }
    return;
  }
  const isControlBtn = !!e.target.closest('.controls, .controls-overlay');
  const isRevealed = msgEl.classList.contains('controls-revealed');
  if (isControlBtn) {
    if (!isRevealed) {
      // First tap on an invisible control button — reveal-only, swallow.
      e.preventDefault();
      e.stopPropagation();
      msgEl.classList.add('controls-revealed');
    }
    // Already revealed: let the click propagate to the button handler.
  } else {
    // Body tap: toggle.
    msgEl.classList.toggle('controls-revealed');
  }
  // Single-reveal: clear siblings so only one message is open at a time.
  for (const m of document.querySelectorAll('.msg.controls-revealed')) {
    if (m !== msgEl) m.classList.remove('controls-revealed');
  }
}, true);


// Full nav-key set anywhere on the chats tab (outside a modal or another
// editable element) — Up/Down/Left/Right/Esc/End all behave the same as
// when the chat input has focus. Lets the user keep navigating + unlock
// without having to click back into the input bar after each detour to
// a button or list row.
document.addEventListener('keydown', (e) => {
  // The textarea's own keydown handler runs first on the bubble path and
  // calls ``preventDefault`` for any nav key it consumes. Since navUp
  // *blurs* the textarea, the active-element guard below would otherwise
  // pass and we'd run handleNavKey twice for one press — the second pass
  // would step the lock further (e.g. visible-bottom → path[length-2])
  // even though the user meant a single step.
  if (e.defaultPrevented) return;
  if (e.shiftKey || e.ctrlKey || e.altKey || e.metaKey) return;
  if (state.activeTab !== 'chats') return;
  const modalRoot = document.getElementById('modal-root');
  if (modalRoot && !modalRoot.classList.contains('hidden')) return;
  // Selects/inputs/contentEditable shouldn't lose their normal caret-or-
  // option behaviour; the textarea has its own handler we don't want to
  // double-fire.
  const ae = document.activeElement;
  if (ae) {
    const tag = ae.tagName;
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || ae.isContentEditable) return;
  }
  // Input-bar text gates nav-mode: empty = fair game.
  const ta = document.querySelector('.chat-input-area textarea');
  if (!ta || ta.value !== '') return;
  const chat = getChatById(state, state.activeChatId);
  if (!chat) return;
  if (handleNavKey(chat, e)) e.preventDefault();
});


// Alt+Enter anywhere on the chats tab → Impersonate, even when the chat
// input isn't focused. When the input *is* focused its own keydown handles
// the combo (and calls preventDefault), so this document-level fallback bails
// on text fields to avoid firing twice — and on a focused contact bubble the
// keyboard-nav set above doesn't claim Alt+Enter, so this is the only path.
document.addEventListener('keydown', (e) => {
  if (e.key !== 'Enter' || !e.altKey || e.ctrlKey || e.metaKey) return;
  if (e.defaultPrevented) return;
  if (state.activeTab !== 'chats') return;
  const modalRoot = document.getElementById('modal-root');
  if (modalRoot && !modalRoot.classList.contains('hidden')) return;
  const ae = document.activeElement;
  if (ae) {
    const tag = ae.tagName;
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || ae.isContentEditable) return;
  }
  const chat = getChatById(state, state.activeChatId);
  if (!chat) return;
  e.preventDefault();
  triggerImpersonate(chat);
});


// Touch swipe → branch nav. Replaces the prev/next arrow buttons (which
// are hidden on touch via the input-mode media query in styles.css). Gated
// on `e.pointerType === 'touch'` so desktop pointer-down-and-drag never
// triggers it. Critically: the branch swap MUST preserve scroll position —
// `navBranchPrev`/`navBranchNext` already pass `preserveScroll: true` to
// `loadActiveChat` so this works automatically.
//
// The bubble translates with the user's finger as visual feedback. On
// release: past `commit threshold` it animates off-screen and fires the
// branch swap; before threshold it snaps back to translateX(0). The
// existing `_swipeJustFired` flag suppresses the trailing click so the
// tap-to-reveal handler doesn't also fire.
// Lock at 8 px so the bubble starts tracking the finger almost instantly
// (visual feedback as the user begins dragging). The commit threshold —
// which decides whether release triggers a branch swap or snap-back —
// is enforced separately on pointerup, so accidental small motions
// don't navigate.
const SWIPE_LOCK_PX = 8;
let _swipeState = null;
let _swipeJustFired = false;
function _swipeReset(msgEl, ...peeks) {
  if (msgEl) {
    msgEl.style.transition = 'transform .2s ease-out';
    msgEl.style.transform = 'translateX(0)';
  }
  for (const peekEl of peeks) {
    if (!peekEl) continue;
    peekEl.style.transition = 'transform .2s ease-out';
    // Reset to the off-screen position so it slides back out.
    peekEl.style.transform = peekEl._peekRest || '';
  }
  setTimeout(() => {
    if (msgEl) {
      msgEl.style.transition = '';
      msgEl.style.transform = '';
    }
    for (const peekEl of peeks) {
      if (peekEl) peekEl.remove();
    }
  }, 220);
}
/* Smart-scroll the chat-messages pane so the just-swapped message is
 * naturally visible after a swipe commit:
 *   • If the message is the LAST one in the pane: anchor it to the
 *     bottom (full message visible at the bottom edge).
 *   • If there's more content below: anchor so the message is fully
 *     visible AND a small peek of the following message in the active
 *     path (NOT a sibling — the next descendant) is visible at the
 *     bottom, so the user knows there's more to scroll to.
 *   • If the message is taller than the pane: scroll to show its TOP
 *     (since we can't fit the whole thing).
 * Soft-scrolls via `behavior: smooth`. */
const SCROLL_PEEK = 24;
const SCROLL_BOTTOM_GAP = 20;
/* ID-keyed wrapper around ``_smartScrollToMessage`` that materialises the
 * target message into the loaded window first if virtualization has hidden
 * it. Both the branch-swipe commit and any future search-match navigation
 * route through this so they don't silently no-op when the target is
 * outside the current window. */
function _smartScrollToMessageById(id, root) {
  if (!root || !id) return;
  let msgEl = root.querySelector(`.msg[data-msg-id="${id}"]`);
  if (!msgEl) {
    const chat = getChatById(state, state.activeChatId);
    if (chat) {
      refreshMessages(chat, { preserveScroll: true, targetMsgId: id });
      msgEl = root.querySelector(`.msg[data-msg-id="${id}"]`);
    }
  }
  if (msgEl) _smartScrollToMessage(msgEl, root);
}

// Hand the helper to the search module so prev/next can materialize
// virtualized matches into the loaded window before scrolling.
_searchSetSmartScrollFn(_smartScrollToMessageById);

function _smartScrollToMessage(msgEl, root) {
  if (!msgEl || !root) return;
  const rootHeight = root.clientHeight;
  const msgTop = msgEl.offsetTop;
  const msgHeight = msgEl.offsetHeight;
  const msgBottom = msgTop + msgHeight;
  const currentTop = root.scrollTop;
  const currentBottom = currentTop + rootHeight;
  const hasMoreBelow = !!msgEl.nextElementSibling;

  let target = currentTop;
  if (msgHeight > rootHeight) {
    // Truly doesn't fit even at zero top padding — anchor at the top
    // with a small breathing gap above; bottom is necessarily off-
    // screen below.
    target = Math.max(0, msgTop - 8);
  } else if (msgHeight > rootHeight - SCROLL_BOTTOM_GAP) {
    // Almost fits — would need to push the top off to get the full
    // bottom gap. Sacrifice the top padding instead: anchor msg.top
    // exactly at the viewport top so the user gets a smaller-but-
    // positive bottom gap (rootHeight - msgHeight px) without the
    // start of the msg ever scrolling out of view.
    target = Math.max(0, msgTop);
  } else if (!hasMoreBelow) {
    // Comfortably fits — anchor with the full breathing room above the
    // input bar.
    if (msgBottom > currentBottom - SCROLL_BOTTOM_GAP) {
      target = msgBottom - rootHeight + SCROLL_BOTTOM_GAP;
    } else if (msgTop < currentTop) {
      target = Math.max(0, msgTop - 8);
    }
  } else {
    // Not last — fit current + a peek of the next message. The peek
    // (24 px) plus the 8 px buffer below already gives ~32 px of
    // visible content below the message bottom, so no extra gap needed.
    const desiredBottom = msgBottom + SCROLL_PEEK;
    if (desiredBottom > currentBottom) {
      target = desiredBottom - rootHeight + 8;
    } else if (msgTop < currentTop) {
      target = Math.max(0, msgTop - 8);
    }
  }
  if (Math.abs(target - currentTop) > 1) {
    root.scrollTo({ top: target, behavior: 'smooth' });
  }
}


/* Render the sibling we're swiping toward as an absolutely-positioned
 * element overlaying the same coordinates as the message being swiped,
 * but offset 100 % to the side via transform. As the user drags, both
 * move together; the peek slides in as the current bubble slides out.
 *
 * Returns null when there's no existing sibling to peek (e.g. swiping
 * past the last sibling on a contact message — that triggers a brand-new
 * reroll, and there's nothing to peek-render). */
function _buildSwipePeek(currentEl, target, chat) {
  if (!target || !currentEl) return null;
  const messagesRoot = document.getElementById('chat-messages');
  if (!messagesRoot) return null;
  const allMessages = state.chatMessages || [];
  const peekEl = renderMessage(target, chat, allMessages, { isGroupStart: true });
  // Match the current message's offset within .chat-messages so the peek
  // sits at the same vertical / horizontal slot. The hosting flex layout
  // doesn't include absolutely-positioned children in its sizing, so the
  // peek doesn't disturb other messages.
  const top = currentEl.offsetTop;
  const left = currentEl.offsetLeft;
  const width = currentEl.offsetWidth;
  peekEl.style.position = 'absolute';
  peekEl.style.top = `${top}px`;
  peekEl.style.left = `${left}px`;
  peekEl.style.width = `${width}px`;
  // Sit behind the current bubble so handles, controls etc. on the
  // currently-interacted bubble win pointer events.
  peekEl.style.zIndex = '0';
  // Initial off-screen rest position is set by the caller via the
  // `_peekRest` property + transform, based on swipe direction.
  messagesRoot.appendChild(peekEl);
  return peekEl;
}
document.addEventListener('pointerdown', (e) => {
  if (e.pointerType !== 'touch') return;
  // No branch-nav swipes while a generation is in flight — interactions
  // with the typing indicator and its in-flight virtual sibling produce
  // race conditions (peek size jumps, scroll-to-random-place, peek
  // overlaying current sibling). The user can wait for the stream to
  // complete or cancel via the send button (which morphs into Cancel).
  if (state.generating) return;
  // Defer to the edge-swipe handler when the gesture starts near the left
  // edge — a back-swipe shouldn't simultaneously rotate branches.
  if (e.clientX <= 20) return;
  const messagesRoot = document.getElementById('chat-messages');
  if (!messagesRoot || !messagesRoot.contains(e.target)) return;
  const bubbleRow = e.target.closest('.bubble-row');
  if (!bubbleRow) return;
  const msgEl = e.target.closest('.msg');
  if (!msgEl || !msgEl.dataset.msgId) return;
  // Don't start a swipe on a button / link — the user is tapping it.
  if (e.target.closest('button, a, input, textarea, select')) return;
  _swipeState = {
    msgId: msgEl.dataset.msgId,
    msgEl,
    startX: e.clientX,
    startY: e.clientY,
    locked: false,
    abandoned: false,
    direction: null,
    pointerId: e.pointerId,
  };
});
document.addEventListener('pointermove', (e) => {
  if (!_swipeState || e.pointerId !== _swipeState.pointerId) return;
  if (_swipeState.abandoned) return;
  const dx = e.clientX - _swipeState.startX;
  const dy = e.clientY - _swipeState.startY;
  const adx = Math.abs(dx);
  const ady = Math.abs(dy);
  if (!_swipeState.locked) {
    // Vertical motion wins first → user is scrolling, not swiping.
    if (ady > 10 && ady > adx) {
      _swipeState.abandoned = true;
      return;
    }
    // Horizontal motion crosses the threshold → swipe locks.
    if (adx > SWIPE_LOCK_PX && adx > ady) {
      _swipeState.locked = true;
      _swipeState.direction = dx > 0 ? 'right' : 'left';
      // No transition while the finger is down — the bubble follows
      // instantly. Transition is added on pointerup for the
      // commit/snap-back animation.
      _swipeState.msgEl.style.transition = 'none';

      // Lock the chat-messages scroll position so the content doesn't
      // wobble vertically when the user's finger drifts during a swipe.
      // The scroll-event listener (added below) snaps scrollTop back if
      // the browser pans despite our `e.preventDefault()` calls — belt
      // and braces, since `touch-action: pan-y` on `.bubble-row` is
      // sampled at touchstart and doesn't update mid-touch.
      const messagesRoot = document.getElementById('chat-messages');
      if (messagesRoot) {
        const lockedTop = messagesRoot.scrollTop;
        const onScroll = () => {
          if (messagesRoot.scrollTop !== lockedTop) {
            messagesRoot.scrollTop = lockedTop;
          }
        };
        messagesRoot.addEventListener('scroll', onScroll);
        _swipeState.releaseScrollLock = () => {
          messagesRoot.removeEventListener('scroll', onScroll);
        };
      }

      // Pre-render BOTH peek messages (next on right, prev on left) at
      // lock time so the user can swipe in either direction during the
      // gesture and see the corresponding sibling slide in. Each peek
      // is only rendered if a corresponding existing sibling exists —
      // swiping past the last contact-message sibling triggers a fresh
      // reroll (no message to peek), and swipe-right at the first
      // sibling does nothing.
      const chat = getChatById(state, state.activeChatId);
      const msg = _findMsg(_swipeState.msgId);
      if (chat && msg) {
        const siblings = _siblingsOf(msg.parent_id);
        const idx = siblings.findIndex(m => m.id === msg.id);
        const nextSibling = (idx >= 0 && idx < siblings.length - 1) ? siblings[idx + 1] : null;
        const prevSibling = (idx > 0) ? siblings[idx - 1] : null;
        // Captured for the post-commit smart-scroll: after the branch
        // swap lands, the new active sibling is at this id and we
        // scroll-into-view on it.
        _swipeState.nextTargetId = nextSibling?.id || null;
        _swipeState.prevTargetId = prevSibling?.id || null;
        if (nextSibling) {
          const peekRight = _buildSwipePeek(_swipeState.msgEl, nextSibling, chat);
          if (peekRight) {
            peekRight._peekRest = 'translateX(calc(100% + 16px))';
            peekRight.style.transition = 'none';
            peekRight.style.transform = peekRight._peekRest;
            _swipeState.peekRight = peekRight;
          }
        }
        if (prevSibling) {
          const peekLeft = _buildSwipePeek(_swipeState.msgEl, prevSibling, chat);
          if (peekLeft) {
            peekLeft._peekRest = 'translateX(calc(-100% - 16px))';
            peekLeft.style.transition = 'none';
            peekLeft.style.transform = peekLeft._peekRest;
            _swipeState.peekLeft = peekLeft;
          }
        }
      }
    }
  }
  if (_swipeState.locked) {
    // Track the finger. Both peeks (left/right) follow offset by their
    // own width plus a 16 px gap; only the one in the direction of swipe
    // is on-screen, the other stays out of view on the opposite side.
    //
    // When there's no existing sibling to peek toward, the message
    // resists past a small threshold instead of scrolling freely into
    // empty space. Swipe-right at the first sibling: nothing to peek;
    // swipe-left past the last sibling: triggers a reroll (which
    // creates a virtual sibling and shouldn't show a peek per user
    // request). Both cases want the rubber-band feel.
    let displayDx = dx;
    const swipingRightNoPrev = dx > 0 && !_swipeState.peekLeft;
    const swipingLeftNoNext = dx < 0 && !_swipeState.peekRight;
    if (swipingRightNoPrev || swipingLeftNoNext) {
      const sign = dx >= 0 ? 1 : -1;
      const adx = Math.abs(dx);
      const RESIST_FROM = 40;
      displayDx = adx <= RESIST_FROM
        ? dx
        : sign * (RESIST_FROM + (adx - RESIST_FROM) * 0.25);
    }
    _swipeState.msgEl.style.transform = `translateX(${displayDx}px)`;
    if (_swipeState.peekRight) {
      _swipeState.peekRight.style.transform = `translateX(calc(100% + 16px + ${dx}px))`;
    }
    if (_swipeState.peekLeft) {
      _swipeState.peekLeft.style.transform = `translateX(calc(-100% - 16px + ${dx}px))`;
    }
    e.preventDefault();
  }
});
document.addEventListener('pointerup', (e) => {
  if (!_swipeState || e.pointerId !== _swipeState.pointerId) return;
  const swipe = _swipeState;
  _swipeState = null;
  if (swipe.releaseScrollLock) swipe.releaseScrollLock();
  if (!swipe.locked) return;
  const dx = e.clientX - swipe.startX;
  const commitThreshold = Math.min(140, window.innerWidth / 4);
  const chat = getChatById(state, state.activeChatId);
  const msg = _findMsg(swipe.msgId);
  // Suppress the trailing click + tap-to-reveal.
  _swipeJustFired = true;
  setTimeout(() => { _swipeJustFired = false; }, 250);
  if (!chat || !msg || Math.abs(dx) <= commitThreshold) {
    // Snap back — gesture didn't pass the commit threshold or there's
    // no message to navigate to.
    _swipeReset(swipe.msgEl, swipe.peekRight, swipe.peekLeft);
    return;
  }
  // Commit direction is the SIGN of dx at release (not the locked
  // direction): if the user swiped right past threshold, commit to prev;
  // if left, commit to next.
  const commitDirection = dx > 0 ? 'right' : 'left';
  // Swipe-right with no prev sibling is a no-op (no message to navigate
  // to and no reroll either) — snap back instead of trying to commit
  // and animating off-screen.
  if (commitDirection === 'right' && !swipe.prevTargetId) {
    _swipeReset(swipe.msgEl, swipe.peekRight, swipe.peekLeft);
    return;
  }
  const activePeek = commitDirection === 'left' ? swipe.peekRight : swipe.peekLeft;
  const inactivePeek = commitDirection === 'left' ? swipe.peekLeft : swipe.peekRight;
  // Commit: animate the bubble off in the swipe direction (and the
  // active peek into its slot), then fire the branch swap.
  const exitX = commitDirection === 'right' ? window.innerWidth : -window.innerWidth;
  swipe.msgEl.style.transition = 'transform .2s ease-out';
  swipe.msgEl.style.transform = `translateX(${exitX}px)`;
  if (activePeek) {
    activePeek.style.transition = 'transform .2s ease-out';
    activePeek.style.transform = 'translateX(0)';
  }
  if (inactivePeek) {
    // Send the unused peek back to its rest position so it doesn't
    // visually lag behind during the commit animation.
    inactivePeek.style.transition = 'transform .2s ease-out';
    inactivePeek.style.transform = inactivePeek._peekRest;
  }
  setTimeout(() => {
    (async () => {
      const root = document.getElementById('chat-messages');
      const navPromise = commitDirection === 'left'
        ? navBranchNext(chat, msg, { silent: true })
        : navBranchPrev(chat, msg, { silent: true });
      try { await navPromise; } catch {}
      // Smart-scroll: bring the newly-active message into view.
      const newActiveId = commitDirection === 'left'
        ? swipe.nextTargetId
        : swipe.prevTargetId;
      if (newActiveId && root && !root.querySelector('.shrink-filler')) {
        // Use the id-keyed wrapper so a virtualized-out target is brought
        // back into the window before scrolling.
        _smartScrollToMessageById(newActiveId, root);
      }
      setTimeout(() => {
        if (swipe.peekRight) swipe.peekRight.remove();
        if (swipe.peekLeft) swipe.peekLeft.remove();
      }, 600);
    })();
  }, 180);
});
document.addEventListener('pointercancel', (e) => {
  if (!_swipeState || e.pointerId !== _swipeState.pointerId) return;
  const swipe = _swipeState;
  _swipeState = null;
  if (swipe.releaseScrollLock) swipe.releaseScrollLock();
  if (swipe.locked) _swipeReset(swipe.msgEl, swipe.peekRight, swipe.peekLeft);
});


// PageUp/PageDown: scroll the right pane (active chat / entity edit view)
// when there's content, otherwise the left pane (whichever list is
// mounted). Skipped when a modal is open OR when an editable element
// (textarea / text input / contenteditable) holds focus — there the
// browser's native caret-movement-by-page is the right behavior, and
// hijacking it would scroll the surrounding page out from under the
// user mid-edit.
document.addEventListener('keydown', (e) => {
  if (e.key !== 'PageUp' && e.key !== 'PageDown') return;
  const modalRoot = document.getElementById('modal-root');
  if (modalRoot && !modalRoot.classList.contains('hidden')) return;
  const active = document.activeElement;
  if (active) {
    // Multi-line edit context: let the browser's caret-by-page handle
    // PgUp / PgDown so the user can scroll long content within the
    // textarea / contenteditable. Single-line ``<input>`` has no
    // meaningful page motion, so we keep stealing the keys for the
    // surrounding scroll pane.
    if (active.tagName === 'TEXTAREA') return;
    if (active.isContentEditable) return;
  }
  const target =
    document.getElementById('chat-messages') ||           // chat tab right pane
    document.querySelector('.content-pane .page-scroll') || // entity edit views
    document.querySelector('.list-body');                  // any list pane
  if (!target) return;
  const dir = e.key === 'PageUp' ? -1 : 1;
  target.scrollBy({ top: dir * target.clientHeight * 0.9, behavior: 'smooth' });
  e.preventDefault();
});

function renderInputArea(chat) {
  // Touch devices: plain Enter inserts a newline (default browser behavior)
  // and the on-screen send button submits — matches WhatsApp / iMessage /
  // Discord / Telegram conventions, where there's no Shift+Enter shortcut
  // on the on-screen keyboard. Desktop keeps Enter→send / Ctrl+Enter→newline.
  const isTouch = window.matchMedia && window.matchMedia('(pointer: coarse)').matches;
  const ta = el('textarea', {
    placeholder: isTouch
      ? 'Type a message…'
      : 'Type a message…  (Enter to send, Ctrl+Enter for newline)',
    rows: 1,
    enterkeyhint: 'enter',
  });
  // Restore any persisted draft for this chat. Height is fixed up after
  // mount via the input event the caller dispatches in renderChatView —
  // ta.scrollHeight is 0 until the textarea is in the DOM.
  ta.value = loadChatDraft(chat.id);

  // Single morphing button: behaves as Send normally, flips to Cancel while
  // generating. State transitions are driven by ``setSendBtnMode`` from
  // ``triggerGeneration``.
  const sendBtn = el('button', {
    class: 'btn primary',
    id: 'chat-send-btn',
    onClick: () => {
      if (state.generating) {
        api.cancelGeneration(chat.id).catch(() => {});
        return;
      }
      // iOS Safari blocks programmatic <audio>.play() unless the
      // first .play() of the session fires inside a real user
      // gesture. Send is the natural moment to unlock it.
      primeAudioSession();
      sendUserMessage(chat, ta.value, ta);
    },
  }, icon('send', 14), el('span', { class: 'btn-label' }, 'Send'));

  ta.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      // Alt+Enter → Impersonate (have the model write the user's next
      // turn). Checked before the touch / newline / send branches so the
      // combo behaves the same regardless of platform.
      if (e.altKey && !e.ctrlKey && !e.metaKey) {
        e.preventDefault();
        triggerImpersonate(chat);
        return;
      }
      // Touch devices: don't intercept plain Enter — let the OS keyboard
      // insert a newline. Ctrl/Shift modifiers fall through to the
      // desktop-style handlers below (rarely meaningful on a virtual
      // keyboard but cheap to keep).
      if (isTouch && !e.ctrlKey && !e.metaKey && !e.shiftKey) return;
      if (e.ctrlKey || e.metaKey) {
        // Ctrl/Cmd + Enter inserts a newline at the cursor.
        e.preventDefault();
        const start = ta.selectionStart;
        const end = ta.selectionEnd;
        ta.value = ta.value.slice(0, start) + '\n' + ta.value.slice(end);
        ta.selectionStart = ta.selectionEnd = start + 1;
        ta.dispatchEvent(new Event('input'));
      } else if (!e.shiftKey) {
        e.preventDefault();
        // Enter is a no-op during streaming AND through the brief
        // "settling" window after the stream ends but before the
        // post-completion ``loadActiveChat`` lands the new message in
        // state. Without the second guard the user can race Enter into
        // that gap and post a second user message before the contact
        // bubble exists, ending up with two consecutive user turns.
        if (state.generating || _virtPinnedUntilNextLoad) return;
        // Empty input + a nav-locked message → edit that message rather
        // than (no-op) sending nothing.
        if (ta.value === '' && _navLockedId && handleNavKey(chat, e)) return;
        // Plain Enter otherwise sends.
        sendBtn.click();
      }
      return;
    }

    // Arrow-key message nav: only when the input is empty so we don't
    // hijack normal caret movement during composition.
    if (ta.value !== '' || e.shiftKey || e.ctrlKey || e.altKey || e.metaKey) return;
    if (handleNavKey(chat, e)) e.preventDefault();
  });
  ta.addEventListener('input', () => {
    ta.style.height = 'auto';
    ta.style.height = Math.min(ta.scrollHeight, 200) + 'px';
    // Typing always releases the message-nav lock — the user has switched
    // back to "compose mode".
    if (ta.value !== '' && _navLockedId) clearNavLock();
    saveChatDraft(chat.id, ta.value);
  });

  const menuBtn = makeChatInputMenu(chat, ta);

  // Chip row (above the input) for pending file attachments. Attachments
  // are kept across a Generic↔AER mode switch (never silently dropped),
  // but the row visibly fades in AER mode with a hint that the model
  // won't see them on send. Hidden entirely when empty.
  //
  // Server-persisted: ``chat.pending_attachments`` is the source of
  // truth so chips survive a chat switch, page reload, or tab close.
  // The upload / delete attachment routes maintain it under the chat
  // lock; ``POST /messages`` consumes the list atomically on send.
  const chipsRow = el('div', { class: 'chat-input-chips', hidden: true });
  ta._pendingAttachments = Array.isArray(chat.pending_attachments)
    ? chat.pending_attachments.map(a => ({ ...a }))
    : [];
  ta._renderAttachmentChips = () => {
    const atts = ta._pendingAttachments || [];
    chipsRow.replaceChildren();
    chipsRow.hidden = atts.length === 0;
    const isGeneric = state.settings && state.settings.provider_mode === 'generic';
    chipsRow.classList.toggle('faded', !isGeneric);
    chipsRow.title = isGeneric
      ? ''
      : 'AER mode won’t send attachments to the model. Switch to Generic to use them.';
    for (const att of atts) {
      const chip = el('div', { class: 'chat-input-chip' });
      const thumb = el('img', {
        class: 'chat-input-chip-thumb',
        src: `/api/files/chats/${chat.id}/attachments/${att.id}`,
        alt: att.filename || 'attachment',
      });
      const meta = el('div', { class: 'chat-input-chip-meta' },
        el('div', { class: 'chat-input-chip-name' }, att.filename || 'attachment'),
        el('div', { class: 'chat-input-chip-size' },
          _formatBytes(att.byte_size)),
      );
      const remove = el('button', {
        class: 'icon-btn',
        type: 'button',
        title: 'Remove attachment',
        'aria-label': 'Remove attachment',
        onClick: async () => {
          const idx = atts.indexOf(att);
          if (idx >= 0) atts.splice(idx, 1);
          ta._renderAttachmentChips();
          _syncPendingAttachmentsToChatMap(chat.id, atts);
          try { await api.deleteChatAttachment(chat.id, att.id); }
          catch { /* best-effort cleanup */ }
        },
      }, icon('x', 14));
      chip.append(thumb, meta, remove);
      chipsRow.append(chip);
    }
  };

  // Re-paint the chips when the provider mode flips (toggles the fade /
  // hint title) so the user sees the change without having to navigate
  // away and back. Subscriber self-detaches once the row unmounts.
  let _lastChipMode = state.settings && state.settings.provider_mode;
  const _chipModeUnsub = subscribe(() => {
    if (!chipsRow.isConnected) { _chipModeUnsub(); return; }
    const cur = state.settings && state.settings.provider_mode;
    if (cur !== _lastChipMode) {
      _lastChipMode = cur;
      ta._renderAttachmentChips();
    }
  });

  const inputArea = el('div', { class: 'chat-input-area' },
    menuBtn,
    ta,
    el('div', { class: 'chat-input-actions' },
      sendBtn,
    ),
  );
  setupAttachmentPasteAndDrop(chat, ta, inputArea);
  return el('div', { class: 'chat-input-area-wrap' },
    chipsRow,
    inputArea,
  );
}

function _formatBytes(n) {
  if (n == null) return '';
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}


/** Mirror the textarea's local pendingAttachments list into the
 * cached chat in state.chatMap so a later renderChatView (after a chat
 * switch / page state restore) reads the freshly-uploaded chips
 * instead of stale empty state. The server is already the source of
 * truth — this just keeps the in-memory cache from lagging behind. */
function _syncPendingAttachmentsToChatMap(chatId, atts) {
  const cached = state.chatMap && state.chatMap.get(chatId);
  if (!cached) return;
  const updated = {
    ...cached,
    pending_attachments: atts.map(a => ({ ...a })),
  };
  setChatInMap(chatId, updated);
}


/* ---------- Chat input menu (Continue / Impersonate / Attach) ----------
 *
 * Hamburger (three-bar) menu button to the left of the input. Click opens a
 * popover stacked above the button with Continue (only when the active tip is
 * a contact message), Impersonate, and — in generic mode — Attach. Click the
 * menu button again, Esc, or outside-click to close. In generic mode the
 * button also morphs into a paperclip (attach) while open, so a second click
 * on it opens the file picker directly (the same action as the Attach row).
 */
function makeChatInputMenu(chat, ta) {
  // The button has two visual states:
  //   - Closed: ``menu`` (hamburger) icon. Click opens the popover.
  //   - Open:
  //       * Generic mode: button itself becomes a paperclip and clicking
  //         it opens the file picker. Continue/Impersonate/Attach stack
  //         above in the popover. So two consecutive clicks ⇒ attach.
  //       * AER mode: stays as the ``menu`` icon. Click closes the
  //         popover (same icon, toggle).
  //
  // No debounce: the second click naturally hits a different action
  // because the button's meaning changes on first click.
  let panel = null;
  let outsideHandler = null;
  let escHandler = null;
  let isOpen = false;

  const btn = el('button', {
    class: 'icon-btn chat-input-menu-btn',
    type: 'button',
    title: 'More…',
    'aria-label': 'More options',
  }, icon('menu', 22, 2.4));

  function setBtnIcon(name) {
    btn.replaceChildren(icon(name, 22, 2.4));
  }

  function close() {
    if (panel) {
      panel.remove();
      panel = null;
    }
    if (outsideHandler) {
      document.removeEventListener('mousedown', outsideHandler, true);
      outsideHandler = null;
    }
    if (escHandler) {
      document.removeEventListener('keydown', escHandler);
      escHandler = null;
    }
    isOpen = false;
    btn.classList.remove('open');
    setBtnIcon('menu');
    btn.title = 'More…';
    btn.setAttribute('aria-label', 'More options');
  }

  function open() {
    if (panel) return;
    const isGeneric = state.settings && state.settings.provider_mode === 'generic';
    const path = state.activePathIds || [];
    const msgsById = state.chatMessages || [];
    let tipIsContact = false;
    if (path.length) {
      const tipId = path[path.length - 1];
      const tip = msgsById.find(m => m.id === tipId);
      tipIsContact = !!(tip && tip.sender === 'contact');
    }
    const rows = [];
    if (tipIsContact) {
      rows.push(makeMenuRow('Continue', () => {
        close();
        triggerGeneration(chat, {
          mode: 'continue',
          parentId: path[path.length - 1],
          // ``continueAppendToId`` makes new bubbles stream into the
          // original message's DOM, so the user sees text growing in
          // place instead of a separate "new reply" block.
          continueAppendToId: path[path.length - 1],
        });
      }));
    }
    rows.push(makeMenuRow('Impersonate', () => {
      close();
      triggerImpersonate(chat);
    }));
    if (isGeneric) {
      // Same action as clicking the morphed paperclip button, surfaced as an
      // explicit (icon-less, like the other rows) entry so attach is
      // discoverable without the two-click dance.
      rows.push(makeMenuRow('Attach', () => {
        close();
        openChatAttachmentPicker(chat, ta);
      }));
    }

    panel = el('div', { class: 'chat-input-menu-panel' }, ...rows);
    document.body.append(panel);
    positionMenu(panel, btn);

    outsideHandler = (e) => {
      if (panel && (panel.contains(e.target) || btn.contains(e.target))) return;
      close();
    };
    document.addEventListener('mousedown', outsideHandler, true);
    escHandler = (e) => { if (e.key === 'Escape') close(); };
    document.addEventListener('keydown', escHandler);
    isOpen = true;
    btn.classList.add('open');
    if (isGeneric) {
      // Button morphs into the attach (paperclip) action.
      setBtnIcon('paperclip');
      btn.title = 'Attach…';
      btn.setAttribute('aria-label', 'Attach a file');
    }
  }

  btn.addEventListener('click', () => {
    if (isOpen) {
      const isGeneric = state.settings && state.settings.provider_mode === 'generic';
      close();
      if (isGeneric) {
        openChatAttachmentPicker(chat, ta);
      }
      return;
    }
    open();
  });

  return btn;
}

function makeMenuRow(label, onClick) {
  return el('button', {
    class: 'chat-input-menu-row',
    type: 'button',
    onClick,
  }, label);
}

function positionMenu(panel, anchorBtn) {
  // Stack above the button, left edge aligned. The panel is body-portalled
  // so it escapes any ancestor overflow clipping.
  const rect = anchorBtn.getBoundingClientRect();
  const margin = 8;
  // Measure after mount so width/height are known.
  panel.style.position = 'fixed';
  panel.style.visibility = 'hidden';
  panel.style.left = '0px';
  panel.style.top = '0px';
  // Force layout.
  void panel.offsetWidth;
  const w = panel.offsetWidth;
  const h = panel.offsetHeight;
  // Clamp to the visible box rather than the layout viewport: this panel is
  // anchored to the chat input bar, which is exactly where the on-screen
  // keyboard is.
  const vp = visibleViewport();
  let left = rect.left;
  if (left + w + margin > vp.left + vp.width) {
    left = vp.left + vp.width - w - margin;
  }
  if (left < vp.left + margin) left = vp.left + margin;
  let top = rect.top - h - 6;
  if (top < vp.top + margin) top = rect.bottom + 6;
  panel.style.left = `${Math.round(left)}px`;
  panel.style.top = `${Math.round(top)}px`;
  panel.style.visibility = 'visible';
}

// Upload one image File as a chat attachment and reflect it as a pending chip.
// Shared by the file picker, paste, and drag-and-drop.
async function addChatAttachmentFile(chat, ta, file) {
  try {
    const att = await api.uploadChatAttachment(chat.id, file);
    if (!ta._pendingAttachments) ta._pendingAttachments = [];
    ta._pendingAttachments.push(att);
    if (typeof ta._renderAttachmentChips === 'function') ta._renderAttachmentChips();
    _syncPendingAttachmentsToChatMap(chat.id, ta._pendingAttachments);
  } catch (err) {
    toast(`Upload failed: ${err.message}`, 'error');
  }
}


// Wire image paste (Ctrl/Cmd+V, or the context-menu Paste — both fire `paste`)
// and drag-and-drop onto the input bar into the attachment flow. Generic mode
// only, mirroring the attach button; non-image clipboard content (text) is left
// to paste normally.
function setupAttachmentPasteAndDrop(chat, ta, inputArea) {
  const isGeneric = () => !!(state.settings && state.settings.provider_mode === 'generic');

  ta.addEventListener('paste', (e) => {
    if (!isGeneric()) return;
    const files = [...((e.clipboardData && e.clipboardData.items) || [])]
      .filter(it => it.kind === 'file' && it.type.startsWith('image/'))
      .map(it => it.getAsFile())
      .filter(Boolean);
    if (!files.length) return;
    e.preventDefault();
    files.forEach(f => addChatAttachmentFile(chat, ta, f));
  });

  const draggingFiles = (dt) => !!dt && [...dt.types].includes('Files');
  inputArea.addEventListener('dragover', (e) => {
    if (!isGeneric() || !draggingFiles(e.dataTransfer)) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = 'copy';
    inputArea.classList.add('drag-over');
  });
  inputArea.addEventListener('dragleave', (e) => {
    // Ignore the dragleave that fires when moving onto a child element.
    if (!inputArea.contains(e.relatedTarget)) inputArea.classList.remove('drag-over');
  });
  inputArea.addEventListener('drop', (e) => {
    if (!isGeneric() || !draggingFiles(e.dataTransfer)) return;
    e.preventDefault();
    inputArea.classList.remove('drag-over');
    const files = [...(e.dataTransfer.files || [])].filter(f => f.type.startsWith('image/'));
    if (!files.length) { toast('Only image attachments are supported.', 'error'); return; }
    files.forEach(f => addChatAttachmentFile(chat, ta, f));
  });
}


function openChatAttachmentPicker(chat, ta) {
  // Caller passes the textarea explicitly so we don't reach into the
  // DOM via a global selector. The closure that owns the menu button
  // already has the reference.
  if (!ta) return;
  const input = el('input', {
    type: 'file',
    accept: 'image/*',
    class: 'hidden',
  });
  let cleaned = false;
  const cleanup = () => {
    if (cleaned) return;
    cleaned = true;
    input.remove();
    window.removeEventListener('focus', onFocusFallback);
  };
  // ``change`` fires when the user picks a file. ``cancel`` fires when
  // they dismiss the dialog (modern browsers — Chrome ≥113, Firefox
  // ≥91, Safari ≥16). The window-focus fallback covers older browsers:
  // focus returns to the page when the dialog closes regardless of how;
  // a short delay lets ``change`` fire first when applicable.
  input.addEventListener('change', async (e) => {
    const f = e.target.files && e.target.files[0];
    if (!f) { cleanup(); return; }
    await addChatAttachmentFile(chat, ta, f);
    cleanup();
  });
  input.addEventListener('cancel', cleanup);
  const onFocusFallback = () => {
    // Defer so ``change`` has time to fire first on browsers that
    // don't support ``cancel``. If ``change`` fired, cleanup already
    // ran (the cleaned flag short-circuits this).
    setTimeout(cleanup, 200);
  };
  window.addEventListener('focus', onFocusFallback);
  document.body.append(input);
  input.click();
}


function setSendBtnMode(mode) {
  const btn = document.getElementById('chat-send-btn');
  if (!btn) return;
  if (mode === 'cancel') {
    btn.className = 'btn danger';
    btn.replaceChildren(icon('stop', 14), el('span', { class: 'btn-label' }, 'Cancel'));
  } else {
    btn.className = 'btn primary';
    btn.replaceChildren(icon('send', 14), el('span', { class: 'btn-label' }, 'Send'));
  }
}


/* ---------- keyboard message nav (arrow keys from empty input) ---------- */

function applyNavLockHighlight(opts = {}) {
  for (const el of document.querySelectorAll('.msg.nav-locked')) {
    el.classList.remove('nav-locked');
  }
  _syncVirtualVisibility();
  if (!_navLockedId) return;
  let target = document.querySelector(`.msg[data-msg-id="${_navLockedId}"]`);
  // Lock points at the virtual sibling — show the highlight on the typing
  // indicator that's already standing in for the in-flight new message.
  if (!target && _virtualMsg && _navLockedId === _virtualMsg.id) {
    target = document.getElementById('typing-indicator');
  }
  if (target) {
    target.classList.add('nav-locked');
    // ``smooth`` is appropriate when the user just initiated nav, but it
    // animates a visible bounce when refreshMessages re-applies the
    // highlight after a generation lands (the new bubble's
    // scroll-margin-bottom triggers a soft upward scroll). Auto/instant
    // by default; callers opt into smooth.
    target.scrollIntoView({
      block: 'nearest',
      behavior: opts.smooth ? 'smooth' : 'auto',
    });
  }
}


// Single-slot selection during a reroll: the typing indicator and the real
// sibling that occupies the same parent slot share visibility — exactly
// one is rendered at a time, picked by ``_virtualSlotShown``. Saves the
// user from seeing two stacked bubbles for what is conceptually the same
// slot. Tracks ``_virtualSlotShown`` rather than the lock so navigating
// up/down/end while a reroll is in flight leaves the indicator visible.
let _streamFollow = null;

function _syncVirtualVisibility() {
  const indicator = document.getElementById('typing-indicator');
  if (!_virtualMsg || !indicator) return;
  // Build the list of .msg elements to hide while the indicator stands
  // in for the new sibling slot. Mid-chat case (``_virtualReplacingId``
  // set): walk forward from the rerolled msg, collecting it + every .msg
  // sibling up to the indicator (those are the original branch's
  // downstream messages). Tail case: the path tail when its parent
  // matches the virtual's — the legacy single-element check.
  const hideChain = [];
  if (_virtualReplacingId) {
    let cur = document.querySelector(`.msg[data-msg-id="${_virtualReplacingId}"]`);
    while (cur && cur !== indicator) {
      if (cur.classList.contains('msg')) hideChain.push(cur);
      cur = cur.nextElementSibling;
    }
  } else {
    const path = _activePathMessages();
    const tail = path[path.length - 1];
    const tailEl = tail && tail.parent_id === _virtualMsg.parent_id
      ? document.querySelector(`.msg[data-msg-id="${tail.id}"]`)
      : null;
    if (tailEl) hideChain.push(tailEl);
  }
  indicator.style.display = _virtualSlotShown ? '' : 'none';
  for (const el of hideChain) {
    el.style.display = _virtualSlotShown ? 'none' : '';
  }
}


function _siblingsOf(parent_id) {
  const all = state.chatMessages || [];
  let siblings = all.filter(m => m.parent_id === parent_id);
  if (_virtualMsg && _virtualMsg.parent_id === parent_id) {
    siblings = [...siblings, _virtualMsg];
  }
  return siblings.sort((a, b) => (a.timestamp || 0) - (b.timestamp || 0));
}


function _findMsg(id) {
  if (_virtualMsg && _virtualMsg.id === id) return _virtualMsg;
  return (state.chatMessages || []).find(m => m.id === id);
}


// Branch-nav block injected into the typing indicator while a virtual
// reroll-sibling is in flight. Only the navigation arrows + counter; no
// edit/delete since the message doesn't yet exist on disk.
function _renderVirtualBranchNav(chat) {
  if (!_virtualMsg) return null;
  const siblings = _siblingsOf(_virtualMsg.parent_id);
  const idx = siblings.findIndex(m => m.id === _virtualMsg.id);
  return el('div', { class: 'controls' },
    el('div', { class: 'branch-nav' },
      el('button', {
        class: 'icon-btn', title: 'Previous branch',
        // Operate on the virtual explicitly so the buttons act on the
        // indicator's siblings even when the lock has moved elsewhere
        // (e.g. user navigated up while the reroll streams).
        onClick: (e) => { e.stopPropagation(); navBranchPrev(chat, _virtualMsg); },
      }, icon('prev', 14)),
      el('span', { style: { fontSize: '11px', color: 'var(--text-mute)' } },
        `${idx + 1}/${siblings.length}`),
      el('button', {
        class: 'icon-btn', title: 'Next branch',
        onClick: (e) => { e.stopPropagation(); navBranchNext(chat, _virtualMsg); },
      }, icon('next', 14)),
    ),
  );
}

function clearNavLock() {
  if (!_navLockedId) return;
  _navLockedId = null;
  applyNavLockHighlight();
}

function navScrollToBottom() {
  const root = document.getElementById('chat-messages');
  if (root) root.scrollTop = root.scrollHeight;
  // The keyboard nav ended (End / Esc / ArrowDown past the tail) — return
  // focus to the input bar so the user can type without grabbing the mouse.
  const ta = document.querySelector('.chat-input-area textarea');
  if (ta) ta.focus();
}

function _activePathMessages() {
  const all = state.chatMessages || [];
  return (state.activePathIds || []).map(id => all.find(m => m.id === id)).filter(Boolean);
}

function navUp(_chat) {
  const path = _activePathMessages();
  if (!path.length) return;
  if (!_navLockedId) {
    // First press establishes the lock on the visible bottom of the chat:
    // the virtual indicator if a reroll is showing it, otherwise the path
    // tail. Subsequent Ups step further up; this also makes the typing
    // indicator reachable from no-lock state via Up.
    if (_virtualMsg && _virtualSlotShown) {
      _navLockedId = _virtualMsg.id;
    } else {
      _navLockedId = path[path.length - 1].id;
    }
  } else if (_navLockedId === _virtualMsg?.id) {
    // Step up off the virtual past the hidden original tail to the
    // message above the slot. ``_virtualSlotShown`` doesn't change so
    // the indicator stays visible.
    if (path.length >= 2) _navLockedId = path[path.length - 2].id;
    // Only the tail in the path → no further up; keep lock on virtual.
  } else {
    const idx = path.findIndex(m => m.id === _navLockedId);
    if (idx > 0) _navLockedId = path[idx - 1].id;
    // idx === 0 → already at the top of the path; no-op.
  }
  applyNavLockHighlight({ smooth: true });
}

function navDown(_chat) {
  if (!_navLockedId) {
    // ArrowDown with no lock — just scroll to the bottom of the chat. Lets
    // the user shake off any visible "stranded" state without having to type.
    navScrollToBottom();
    return;
  }
  if (_navLockedId === _virtualMsg?.id) {
    // Locked on the virtual sibling at the bottom — release. Don't touch
    // ``_virtualSlotShown`` so the indicator stays visible after the lock
    // clears (the user has only released the selection, not navigated away
    // from the slot).
    clearNavLock();
    navScrollToBottom();
    return;
  }
  const path = _activePathMessages();
  const idx = path.findIndex(m => m.id === _navLockedId);
  if (idx < 0) { clearNavLock(); return; }
  if (idx >= path.length - 1) {
    // At the path's tail. If a virtual is occupying the bottom slot, step
    // onto it; otherwise release.
    if (_virtualMsg && _virtualSlotShown) {
      _navLockedId = _virtualMsg.id;
      applyNavLockHighlight({ smooth: true });
      return;
    }
    clearNavLock();
    navScrollToBottom();
    return;
  }
  const nextIdx = idx + 1;
  // If the next-in-path is a tail that the virtual has hidden, skip past it
  // to the virtual itself.
  if (nextIdx === path.length - 1 && _virtualMsg && _virtualSlotShown) {
    _navLockedId = _virtualMsg.id;
  } else {
    _navLockedId = path[nextIdx].id;
  }
  applyNavLockHighlight({ smooth: true });
}

async function navBranchPrev(chat, opMsgArg, opts = {}) {
  // Operate on the locked message; if no lock and no explicit msg, fall
  // back to whatever sits at the visible bottom slot — the virtual when
  // an in-flight reroll is showing it, otherwise the path tail. Lets
  // curleft from an empty input bar swap the bottom slot directly,
  // without first flashing a lock highlight on the tail.
  // ``opts.silent`` (used by the touch swipe gesture) skips the nav-lock
  // outline + selection: a swipe expresses "navigate", not "select", so
  // we don't want to leave a highlight on the bubble afterwards.
  let msg = opMsgArg || _findMsg(_navLockedId);
  if (!msg) {
    if (_virtualMsg && _virtualSlotShown) {
      msg = _virtualMsg;
    } else {
      const path = _activePathMessages();
      msg = path[path.length - 1];
    }
    if (!msg) return;
  }
  const siblings = _siblingsOf(msg.parent_id);
  if (siblings.length <= 1) return;
  const idx = siblings.findIndex(m => m.id === msg.id);
  // No wrap on the left edge: at idx 0 there's nothing to step back to,
  // and wrapping is too easy to overshoot when the user is stepping
  // through a long sibling list — once past the wrap they'd be at N/N
  // where curright triggers a reroll, not an undo. Symmetric with the
  // counter (1/N stays at 1/N).
  if (idx <= 0) return;
  const target = siblings[idx - 1];
  // Skip the server roundtrip when either side is the virtual sibling — it
  // doesn't exist on disk yet, and the in-flight reroll's persist will set
  // the parent's selected_child for us.
  if (msg.id === _virtualMsg?.id || target.id === _virtualMsg?.id) {
    _virtualSlotShown = target.id === _virtualMsg?.id;
    // Always sync visibility — the swipe-driven `silent` path skips
    // applyNavLockHighlight (which would have called this), but the
    // typing-indicator/tail display swap needs to happen regardless.
    _syncVirtualVisibility();
    if (!opts.silent) {
      _navLockedId = target.id;
      applyNavLockHighlight({ smooth: true });
    }
    return;
  }
  await api.selectChild(chat.id, msg.parent_id, target.id);
  await loadActiveChat(chat.id, { preserveScroll: true, flush: true });
  if (!opts.silent) {
    _navLockedId = target.id;
    applyNavLockHighlight({ smooth: true });
  }
}

function _ensureNavLockedOnTail() {
  if (_navLockedId) return true;
  const path = _activePathMessages();
  if (!path.length) return false;
  _navLockedId = path[path.length - 1].id;
  applyNavLockHighlight({ smooth: true });
  return true;
}


// Single source of truth for keyboard message-nav. Both the textarea
// keydown and the document-level keydown (when focus is elsewhere) feed
// keys through here. Returns ``true`` if the key was consumed.
function handleNavKey(chat, e) {
  if (e.key === 'ArrowUp') {
    // Move out of the input on the way up — the visible focus should
    // follow the keyboard nav onto the message itself, not stay in the
    // textarea.
    const ta = document.querySelector('.chat-input-area textarea');
    if (ta && document.activeElement === ta) ta.blur();
    navUp(chat);
    return true;
  }
  if (e.key === 'ArrowDown') { navDown(chat); return true; }
  if (e.key === 'ArrowLeft') {
    if (!_ensureNavLockedOnTail()) return false;
    navBranchPrev(chat);
    return true;
  }
  if (e.key === 'ArrowRight') {
    // Skip ``_ensureNavLockedOnTail`` so the lock never visually settles on
    // the original tail before a reroll fires — ``navBranchNext`` falls
    // back to the path tail internally when there's no current lock, and
    // when the press triggers a reroll the lock lands directly on the
    // virtual sibling instead of flashing on the tail first.
    navBranchNext(chat);
    return true;
  }
  if (e.key === 'Enter') {
    // Edit the nav-locked message. Skip if no lock or if the lock is on the
    // virtual reroll target (no persisted message to edit yet).
    if (!_navLockedId) return false;
    if (_virtualMsg && _navLockedId === _virtualMsg.id) return false;
    const msg = _findMsg(_navLockedId);
    if (!msg) return false;
    openEditMessageModal(chat.id, msg);
    return true;
  }
  if (e.key === 'End') { clearNavLock(); navScrollToBottom(); return true; }
  if (e.key === 'Escape') {
    if (!_navLockedId) return false;  // let other Esc handlers (cancel) run
    clearNavLock();
    navScrollToBottom();
    return true;
  }
  return false;
}


async function navBranchNext(chat, opMsgArg, opts = {}) {
  // Operate on the locked message; if no lock and no explicit msg, fall
  // back to whatever sits at the visible bottom slot — the virtual when
  // an in-flight reroll is showing it, otherwise the path tail. This
  // skips the lock-on-tail step that would otherwise flash a highlight
  // on the original message just before a reroll replaces it with the
  // typing indicator.
  // ``opts.silent`` (used by the touch swipe gesture) skips the nav-lock
  // outline + selection — see navBranchPrev for the rationale.
  let msg = opMsgArg || _findMsg(_navLockedId);
  if (!msg) {
    if (_virtualMsg && _virtualSlotShown) {
      msg = _virtualMsg;
    } else {
      const path = _activePathMessages();
      msg = path[path.length - 1];
    }
    if (!msg) return;
  }
  const siblings = _siblingsOf(msg.parent_id);
  const idx = siblings.findIndex(m => m.id === msg.id);
  if (idx < siblings.length - 1) {
    const target = siblings[idx + 1];
    if (msg.id === _virtualMsg?.id || target.id === _virtualMsg?.id) {
      _virtualSlotShown = target.id === _virtualMsg?.id;
      // Always sync visibility — see the matching note in navBranchPrev.
      _syncVirtualVisibility();
      if (!opts.silent) {
        _navLockedId = target.id;
        applyNavLockHighlight({ smooth: true });
      }
      return;
    }
    await api.selectChild(chat.id, msg.parent_id, target.id);
    await loadActiveChat(chat.id, { preserveScroll: true, flush: true });
    if (!opts.silent) {
      _navLockedId = target.id;
      applyNavLockHighlight({ smooth: true });
    }
    return;
  }
  // Past the last sibling. Contact messages reroll (the virtual takes the
  // new slot so subsequent left/right stays index-correct); non-contact
  // messages no-op (no wrap, symmetric with the no-wrap on curleft at
  // idx 0). While a reroll is already in flight (``state.generating`` /
  // ``_virtualMsg`` set), curright is a no-op for contact messages too
  // so the user can't queue a second reroll on top of the first.
  if (msg.sender === 'contact' && !state.generating && !_virtualMsg) {
    const contact = state.contacts.find(c => c.id === chat.contact_id);
    const maxTs = (state.chatMessages || []).reduce(
      (m, x) => Math.max(m, x.timestamp || 0), 0,
    );
    _virtualMsg = {
      id: 'virtual-reroll-' + Date.now(),
      parent_id: msg.parent_id,
      sender: 'contact',
      sender_name: contact?.name || '',
      body: [],
      brains: [],
      timestamp: maxTs + 1,
    };
    _virtualSlotShown = true;
    // Mid-chat reroll (msg is not the path tail): stamp the rerolled id so
    // ``_syncVirtualVisibility`` hides msg + its downstream descendants
    // and the indicator visually occupies the rerolled slot instead of
    // landing at the bottom of the chat below the original branch.
    const path = _activePathMessages();
    const isTail = path.length > 0 && path[path.length - 1].id === msg.id;
    _virtualReplacingId = isTail ? null : msg.id;
    // Carry the user's selection into the virtual at the same slot when
    // they had something explicitly locked before pressing right (typically
    // the tail they're rerolling — Up to select, Right to reroll). When
    // there was NO prior lock — i.e. the press came straight from an empty
    // input bar — the right-press is a pure reroll action, not a selection,
    // so leave the lock null and the indicator un-outlined.
    if (_navLockedId) _navLockedId = _virtualMsg.id;
    // Re-render the active path so existing branch counters pick up the new
    // sibling-count (otherwise the prior tail still shows N/N until the
    // next loadActiveChat).
    refreshMessages(chat, { preserveScroll: true });
    applyNavLockHighlight({ smooth: true });
    // Fire-and-forget — triggerGeneration's finally clears the virtual and
    // snaps the lock onto whatever ``selected_child`` resolves to. Skip
    // the hide-msg flow that ``regenerate`` uses: a keyboard reroll
    // *appends* a new sibling alongside the existing one rather than
    // replacing it, so the user can still see the prior tail (and curleft
    // back onto it) while the new one streams in below.
    triggerGeneration(chat, { parentId: msg.parent_id });
  }
  // Non-contact past-last → no-op (no wrap to first sibling, see the
  // comment above). Contact past-last while a reroll is in flight is
  // also a no-op so the user can't queue a second concurrent reroll.
}


async function sendUserMessage(chat, text, textareaEl) {
  if (state.generating) return;
  text = (text || '').trim();

  const pendingAtts = (textareaEl && textareaEl._pendingAttachments) || [];

  // Empty input + no attachments → just trigger a new generation
  // (continuation / greeting). Empty text but attachments → send the
  // attachments so a vision-capable model has something to look at.
  if (!text && !pendingAtts.length) {
    await triggerGeneration(chat);
    return;
  }

  if (!checkChatRefs(chat)) return;

  const allMessages = state.chatMessages || [];
  const path = state.activePathIds.map(id => allMessages.find(m => m.id === id)).filter(Boolean);
  const tail = path[path.length - 1];

  try {
    await api.createMessage(chat.id, {
      parent_id: tail ? tail.id : null,
      sender: 'user',
      body: [{ text: text || '', emotion: 'neutral' }],
      attachments: pendingAtts,
    });
    textareaEl.value = '';
    textareaEl.style.height = 'auto';
    if (textareaEl._pendingAttachments) {
      textareaEl._pendingAttachments.length = 0;
      if (typeof textareaEl._renderAttachmentChips === 'function') {
        textareaEl._renderAttachmentChips();
      }
      // Server atomically cleared the bound entries from
      // chat.pending_attachments inside the message-create transaction;
      // mirror that into the cached chatMap so a subsequent
      // renderChatView doesn't restore stale chips.
      _syncPendingAttachmentsToChatMap(chat.id, []);
    }
    clearChatDraft(chat.id);
    // ``anchorTip`` forces the virtualizer window to include the freshly-
    // persisted user bubble. Without it, a user who was scrolled away from
    // the bottom (window held back from the path tail) lands the new
    // bubble inside the bottom spacer; the typing indicator then appears
    // with an empty gap above it where the bubble should be, and the
    // bubble only materialises when the post-stream refresh re-renders.
    await loadActiveChat(chat.id, { anchorTip: true });
    await triggerGeneration(chat);
  } catch (e) {
    toast(`Could not send: ${e.message}`, 'error');
  }
}


async function regenerate(msg, chat) {
  // Reroll = generate sibling: parent is msg.parent_id. While the new sibling
  // is streaming we hide the original message so the typing indicator takes
  // its visual place.
  if (state.generating) return;
  await triggerGeneration(chat, { parentId: msg.parent_id, hideMsgId: msg.id });
}


// Surface dangling contact/user references before any network call so the
// toast points at the actual fix (re-assigning via the chat info modal)
// instead of a generic ``Stream error`` from the EventSource.
function checkChatRefs(chat) {
  const missing = [];
  if (!state.contacts.find(c => c.id === chat.contact_id)) missing.push('contact');
  if (!(state.users || []).find(u => u.id === chat.user_id)) missing.push('user');
  if (!missing.length) return true;
  toast(
    `Can't generate — chat is missing ${missing.join(' and ')}. ` +
    `Open the chat info button (ⓘ) to reassign.`,
    'error',
  );
  return false;
}


// Fire an Impersonate generation (the model writes the user's next turn).
// Shared by the Alt+Enter hotkey (both the in-input and document-level
// handlers) and the input menu. Mirrors the send path's guards so a press
// during streaming — or the brief settling window before the new message
// lands — is a no-op rather than a racing second generation.
function triggerImpersonate(chat) {
  if (!chat) return;
  if (state.generating || _virtPinnedUntilNextLoad) return;
  triggerGeneration(chat, { mode: 'impersonate' });
}


async function triggerGeneration(chat, opts = {}) {
  if (!checkChatRefs(chat)) return;

  setState({ generating: true });
  // Hold the IO-callback gate active until the post-stream loadActiveChat
  // settles — without this, scrolling that happens between stream end and
  // the refresh call could fire a window slide on stale path data.
  _virtPinnedUntilNextLoad = true;
  setSendBtnMode('cancel');

  // Esc cancels the in-flight generation — but only when no modal is open
  // (modals have their own Esc handler that we don't want to fight with).
  const escHandler = (e) => {
    if (e.key !== 'Escape') return;
    const modalRoot = document.getElementById('modal-root');
    if (modalRoot && !modalRoot.classList.contains('hidden')) return;
    e.preventDefault();
    api.cancelGeneration(chat.id).catch(() => {});
  };
  document.addEventListener('keydown', escHandler);

  // Hide the message being rerolled (and everything that visually follows
  // it: descendant messages on the active path + an undelete row at the
  // bottom). They'll be replaced after the new generation lands.
  const hiddenEls = [];
  if (opts.hideMsgId) {
    const target = document.querySelector(`.msg[data-msg-id="${opts.hideMsgId}"]`);
    if (target) {
      let cur = target;
      while (cur) {
        hiddenEls.push(cur);
        cur.style.display = 'none';
        cur = cur.nextElementSibling;
      }
    }
  }
  // Also hide the undelete row (if shown) — generating produces fresh content
  // so the soft-deleted siblings shouldn't be advertised mid-stream.
  const undeleteRow = document.querySelector('#chat-messages .undelete-row');
  if (undeleteRow && !hiddenEls.includes(undeleteRow)) {
    hiddenEls.push(undeleteRow);
    undeleteRow.style.display = 'none';
  }
  // Hide the "No messages yet" empty-state placeholder so the typing
  // indicator takes its place visually.
  const emptyState = document.querySelector('#chat-messages .list-empty');
  if (emptyState && !hiddenEls.includes(emptyState)) {
    hiddenEls.push(emptyState);
    emptyState.style.display = 'none';
  }

  // Inject a typing-indicator at the end. Bubbles will land in their own
  // rows (each with its own portrait) BEFORE this indicator as they arrive.
  const root = document.getElementById('chat-messages');
  // Full Contact / User (not the Summary projections in state.contacts /
  // state.users) — auto-TTS needs ``tts`` and the greeting fast-path
  // needs ``greeting``, neither of which the Summaries carry.
  const contact = state.activeChatContact;
  const user = state.activeChatUser;
  // Greeting fast-path: when the chat is empty and the contact has a
  // greeting string, the server returns ``contact.greeting`` as the
  // first bubble without a model call. The user asked for greetings
  // to stay silent — detect the case here and skip auto-TTS for
  // bubbles in this generation.
  const isGreeting = (!state.chatMessages || state.chatMessages.length === 0)
    && !!(contact && contact.greeting);

  // Group-continuation only if the message *immediately before this slot
  // in the eventual active path* is also a contact message. For:
  //  • Reroll via swipe (`_virtualMsg` set): the prior tail (old_msg) is
  //    being supplanted by the virtual sibling — once generation lands,
  //    new_msg's prev-in-path is `_virtualMsg.parent_id`, NOT old_msg.
  //    Compute against the parent so the indicator's layout matches the
  //    new_msg's eventual layout (avoids a ~30 px shift at the DOM swap).
  //  • Otherwise (e.g. plain `regenerate()` via the reroll button, which
  //    passes `hideMsgId`): fall back to the visible-DOM heuristic.
  let isGroupCont;
  if (_virtualMsg) {
    const allMsgs = state.chatMessages || [];
    const parent = allMsgs.find(m => m.id === _virtualMsg.parent_id);
    isGroupCont = !!(parent && parent.sender === 'contact');
  } else {
    const visibleMsgs = Array.from(root.querySelectorAll('.msg'))
      .filter(m => m.style.display !== 'none');
    const lastVisible = visibleMsgs[visibleMsgs.length - 1];
    isGroupCont = !!(lastVisible && lastVisible.classList.contains('contact'));
  }

  // Placeholder bubble-row: structurally identical to a regular bubble-row
  // (emotion-sprite + bubble), so it occupies the same layout space as the
  // real bubbles that will follow. On the first ``onBubble`` we update this
  // row's bubble text in place (no DOM swap) — the indicator's height
  // doesn't change at the transition. Subsequent bubbles append as new
  // rows. This is the "treat the typing indicator like a regular bubble"
  // model: no separate typing-row to remove, no height delta at stream
  // end, no bounce when refreshMessages atomically swaps indicator → msg.
  //
  // Generic-mode + contact with no avatar => the eventual bubble will be
  // rendered "bare" (no avatar slot), so the placeholder must match —
  // otherwise an empty ``no-image`` emotion-sprite shows up until the
  // first delta swaps the row.
  const isGenericMode = state.settings && state.settings.provider_mode === 'generic';
  const placeholderBare = isGenericMode && !(contact && contact.avatar);
  // ``typing`` styles the placeholder text italic + muted (the classic
  // "Typing…" look) for both AER and Generic modes; the class is stripped
  // when the first arriving bubble replaces the placeholder row entirely
  // via ``placeholderRow.replaceWith(newRow)`` below.
  const placeholderBubble = el('div', { class: 'bubble typing streaming' }, 'Typing…');
  const placeholderRow = placeholderBare
    ? el('div', {
        class: 'bubble-row contact bare', id: 'placeholder-row',
      }, placeholderBubble)
    : el('div', { class: 'bubble-row contact', id: 'placeholder-row' },
        el('div', { class: 'emotion-sprite no-image' }, '…'),
        placeholderBubble,
      );
  // The virtual sibling lives in this indicator. Stamp its id on the .msg
  // so applyNavLockHighlight can find it, and inject branch-nav so the
  // user can see + click between siblings while the new one streams in.
  const indicatorAttrs = {
    class: `msg contact ${isGroupCont ? 'group-cont' : 'group-start'}`,
    id: 'typing-indicator',
  };
  if (_virtualMsg) indicatorAttrs.dataset = { msgId: _virtualMsg.id };
  // Group-START indicator: meta-row in flex flow with sender-name +
  // (optional) virtual branch-nav. Matches `renderMessage`'s group-start
  // structure so the swap to the persisted message is layout-neutral.
  // Group-CONTINUATION indicator: virtual branch-nav lives in an
  // absolutely-positioned `.controls-overlay` (matching `renderMessage`'s
  // group-cont structure), so it doesn't take layout space — otherwise
  // the eventual DOM swap into a normal `.msg.group-cont` (no in-flow
  // meta-row) would shift everything above by the meta-row's height.
  let metaRow = null;
  if (!isGroupCont) {
    metaRow = el('div', { class: 'meta-row' },
      _senderNameEl(contact?.name || '', contact, 'contact'),
      el('span', { style: { flex: 1 } }),
      _virtualMsg ? _renderVirtualBranchNav(chat) : null,
    );
  } else if (_virtualMsg) {
    metaRow = el('div', {
      class: 'controls-overlay',
      // Transparent — the default `.controls-overlay` chrome (panel
      // background + border + radius) shouldn't render around the
      // virtual branch-nav counter on the typing indicator. Inline
      // overrides win over the class rule.
      style: {
        opacity: '1',
        background: 'transparent',
        border: 'none',
        boxShadow: 'none',
        padding: '0',
      },
    }, _renderVirtualBranchNav(chat));
  }
  const indicator = el('div', indicatorAttrs, metaRow, placeholderRow);
  root.appendChild(indicator);
  const _isTouchStream = window.matchMedia && window.matchMedia('(pointer: coarse)').matches;
  // Auto-pin to the streaming tail. ``active`` starts true — every
  // generation pins to the tail, wherever the user was when it began. The
  // first deliberate user scroll during the stream ``released``s the pin:
  // ``active`` goes false and STAYS false — we do NOT re-anchor when they
  // scroll back to the bottom, because a surprise yank at gen-end is exactly
  // what "I took control" should prevent. The post-stream scroll-to-reply
  // decision keys off ``released`` (see the finally block).
  //
  // ``toBottom`` is the pin target: desktop rides the very bottom; touch
  // caps at the new msg's first bubble pinned to the viewport top (see
  // pinScroll) so a long reply's start stays visible.
  _streamFollow = { active: true, released: false, toBottom: !_isTouchStream };
  function pinScroll() {
    if (!_streamFollow || !_streamFollow.active) return;
    if (!_isTouchStream) {
      root.scrollTop = root.scrollHeight;
      return;
    }
    let target = Math.max(0, root.scrollHeight - root.clientHeight);
    // Cap so the indicator's first bubble (i.e. the start of the new
    // msg) stays anchored at the top of the viewport — chasing later
    // bubbles past that point would push the new msg's beginning off
    // the screen. For long multi-bubble responses this keeps the meta-
    // row + first bubble visible while the rest streams in below.
    // ``.msg`` is position: relative, so firstBubble.offsetTop is
    // relative to the indicator, not to .chat-messages — sum both
    // offsets to get the absolute offset within the scroll container.
    // Skipped once the user has opted into bottom-follow.
    if (!_streamFollow.toBottom) {
      const indicator = document.getElementById('typing-indicator');
      const firstBubble = indicator && indicator.querySelector('.bubble-row');
      if (firstBubble) {
        const absTop = indicator.offsetTop + firstBubble.offsetTop;
        // 22 = .chat-messages's padding-top.
        const cap = Math.max(0, absTop - 22);
        if (target > cap) target = cap;
      }
    }
    root.scrollTo({ top: target, behavior: 'smooth' });
  }
  // Track follow intent off real user input — and only the user. We key
  // off touch-drag and a proximity-to-the-current-bottom test rather than
  // a remembered-scrollTop delta on purpose: a streaming bubble that
  // reflows shorter (font/image settle, the touch cap easing the viewport
  // up) makes the browser re-clamp scrollTop, and a delta heuristic reads
  // that clamp as a deliberate scroll — silently dropping follow mid-
  // stream. That false positive is what made the end-of-stream scroll-to-
  // view land only sometimes. Neither a programmatic ``scrollTop`` write
  // nor a layout re-clamp produces the signals watched below.
  const _nearBottom = () =>
    root.scrollHeight - (root.scrollTop + root.clientHeight) <= 48;
  let _onFollowScroll = null;
  let _onFollowTouchStart = null;
  let _onFollowTouchMove = null;
  if (_isTouchStream) {
    // Touch pins to a capped target (not the bottom), so a bottom-
    // proximity test alone can't tell our smooth easing apart from a drag.
    // Gate on an active finger instead: a touchmove owns scrollTop (it
    // pre-empts our in-flight smooth scroll), so a drag past a small
    // threshold is unambiguously the user taking control.
    let _touchY0 = null;
    _onFollowTouchStart = (e) => {
      _touchY0 = (e.touches && e.touches[0]) ? e.touches[0].clientY : null;
    };
    _onFollowTouchMove = (e) => {
      if (!_streamFollow || _touchY0 == null || !(e.touches && e.touches[0])) return;
      // Any deliberate drag hands control to the user for the rest of this
      // generation — ``released`` latches, so no reattach on return-to-bottom.
      if (Math.abs(e.touches[0].clientY - _touchY0) > 10) {
        _streamFollow.active = false;
        _streamFollow.released = true;
      }
    };
    root.addEventListener('touchstart', _onFollowTouchStart, { passive: true });
    root.addEventListener('touchmove', _onFollowTouchMove, { passive: true });
  } else {
    // Desktop pins to the very bottom, so a gap between the viewport and the
    // bottom can only be a real user scroll-up: our own instant pin lands at
    // gap 0, a layout re-clamp also lands at gap 0, and content growth emits
    // no scroll event (insertBubble + pinScroll run in one sync step). So a
    // non-near-bottom reading latches ``released`` — and we never clear it,
    // so scrolling back to the bottom does NOT reattach.
    _onFollowScroll = () => {
      if (_streamFollow && !_nearBottom()) {
        _streamFollow.active = false;
        _streamFollow.released = true;
      }
    };
    root.addEventListener('scroll', _onFollowScroll, { passive: true });
  }
  const _teardownFollowListeners = () => {
    if (_onFollowScroll) root.removeEventListener('scroll', _onFollowScroll);
    if (_onFollowTouchStart) root.removeEventListener('touchstart', _onFollowTouchStart);
    if (_onFollowTouchMove) root.removeEventListener('touchmove', _onFollowTouchMove);
  };
  // Apply the lock + visibility sync FIRST. _syncVirtualVisibility hides
  // the prior tail (old_msg) when a virtual swap is in flight, which
  // shrinks scrollHeight. If pinScroll runs before that, its target is
  // computed against the larger scrollHeight and the smooth-scroll eases
  // toward a position the browser then auto-clamps once the visibility
  // flip lands — visible as a "first higher up, then pop lower" bump.
  // Doing the visibility flip first lets pinScroll's smooth target match
  // the final layout.
  applyNavLockHighlight();
  pinScroll();

  let errorMsg = null;
  let errorPayload = null;
  // Captured from the SSE 'start' event so the virtual cleanup can snap
  // the lock onto the new message id directly, instead of inferring it
  // from selected_child_id (which can race with mid-reroll sibling-swaps
  // and even briefly point at the *prior* tail before the persist lands).
  let pendingNewMsgId = null;
  // The first bubble updates the placeholder in place; subsequent bubbles
  // append as new rows. ``placeholderConsumed`` tracks which path the
  // next ``onBubble`` takes.
  let placeholderConsumed = false;
  // Continue mode: append the new bubbles directly to the ORIGINAL
  // message's DOM so the user sees text growing in place rather than a
  // separate "typing" block appearing below the existing message. Found
  // by id at trigger time; the typing indicator stays in the tree as
  // a quiet "..." between bubble emissions.
  const continueTargetMsg = opts.continueAppendToId
    ? document.querySelector(`.msg[data-msg-id="${opts.continueAppendToId}"]`)
    : null;
  const insertBubble = (bubble) => {
    const newRow = renderBubbleRow(bubble, contact, true);
    const bubbleEl = newRow.querySelector('.bubble');
    if (bubbleEl) bubbleEl.classList.add('streaming');
    if (continueTargetMsg) {
      // Drop .streaming from the prior latest so only one bubble pulses.
      continueTargetMsg.querySelectorAll('.bubble.streaming').forEach(b => {
        if (b !== bubbleEl) b.classList.remove('streaming');
      });
      continueTargetMsg.appendChild(newRow);
      return;
    }
    if (!placeholderConsumed) {
      // First bubble: swap the placeholder row out for the real one.
      // Same row count, similar height (both bubble-rows with avatar +
      // bubble), so the indicator's height changes only by the (small)
      // text-content delta — far less than a full typing-row removal.
      placeholderRow.replaceWith(newRow);
      placeholderConsumed = true;
    } else {
      // Subsequent bubbles: drop .streaming from the previous latest so
      // only one bubble pulses at a time, then append the new row.
      indicator.querySelectorAll('.bubble.streaming').forEach(b => {
        if (b !== bubbleEl) b.classList.remove('streaming');
      });
      indicator.appendChild(newRow);
    }
  };

  // Generic-mode streaming state. Unlike AER where the parser emits
  // discrete bubbles, Generic fills one bubble incrementally from
  // ``delta`` events. The bubble + reasoning tray are mounted lazily
  // on the first relevant event; subsequent deltas re-render the
  // bubble's body from the full buffer (markdown re-parse is cheap
  // at message scale).
  //
  // The active generic provider's ``streaming`` flag gates whether the
  // bubble fills incrementally or all at once at ``done``. With it off
  // the deltas still accumulate, but the bubble materialises only when
  // generation completes — the placeholder ``…`` row stays put in the
  // meantime.
  const _genericStreamingEnabled = _activeGenericStreamingEnabled();
  let genericBuffer = '';
  let genericBubbleEl = null;
  let reasoningBuffer = '';
  let reasoningTrayEl = null;
  const renderGenericIntoBubble = () => {
    if (!genericBubbleEl) return;
    const sanitize = !(state.settings && state.settings.sanitize_generic_html === false);
    // Preserve the reasoning tray across content re-renders — it lives
    // as the bubble's first child and survives buffer updates.
    const tray = genericBubbleEl.querySelector(':scope > .reasoning-tray');
    while (genericBubbleEl.firstChild) genericBubbleEl.removeChild(genericBubbleEl.firstChild);
    if (tray) genericBubbleEl.appendChild(tray);
    genericBubbleEl.appendChild(renderGenericBubble(genericBuffer, { sanitizeHtml: sanitize }));
  };
  // Mount or re-home the reasoning tray inside the current bubble at the
  // top. Called when a new bubble appears (placeholder → real, or first
  // reasoning delta with the placeholder still in place).
  const mountReasoningInBubble = (bubbleEl) => {
    if (!reasoningTrayEl || !bubbleEl) return;
    if (reasoningTrayEl.parentNode === bubbleEl
        && bubbleEl.firstChild === reasoningTrayEl) return;
    bubbleEl.insertBefore(reasoningTrayEl, bubbleEl.firstChild);
  };
  const ensureGenericBubble = () => {
    if (genericBubbleEl) return;
    // Continue mode: reuse the ORIGINAL message's bubble so streamed
    // deltas extend the visible text in place. Pre-seed ``genericBuffer``
    // with the existing bubble's content so the next ``renderGenericIntoBubble``
    // call doesn't visually truncate.
    if (continueTargetMsg) {
      const existing = continueTargetMsg.querySelector('.bubble');
      if (existing) {
        genericBubbleEl = existing;
        if (!genericBuffer && state.chatMessages) {
          const orig = state.chatMessages.find(
            m => m.id === opts.continueAppendToId,
          );
          if (orig && orig.body && orig.body[0]) {
            genericBuffer = orig.body[0].text || '';
          }
        }
        if (_genericStreamingEnabled) {
          genericBubbleEl.classList.add('streaming');
        }
        return;
      }
    }
    const fakeBubble = { text: '', emotion: null };
    const newRow = renderBubbleRow(fakeBubble, contact, true, user, 'generic');
    genericBubbleEl = newRow.querySelector('.bubble');
    if (genericBubbleEl && _genericStreamingEnabled) {
      genericBubbleEl.classList.add('streaming');
    }
    if (!placeholderConsumed) {
      placeholderRow.replaceWith(newRow);
      placeholderConsumed = true;
    } else {
      indicator.appendChild(newRow);
    }
    mountReasoningInBubble(genericBubbleEl);
  };
  // Finalize the generic bubble at ``done`` — mounts the bubble and
  // renders the full buffer in one go when streaming-off, or just
  // renders the tail when streaming-on. Reasoning is folded in the
  // same way: mounted now if it wasn't already.
  const finalizeGenericBubble = () => {
    if (reasoningBuffer && !reasoningTrayEl) {
      reasoningTrayEl = renderReasoningTray(reasoningBuffer);
    }
    if (genericBuffer || reasoningTrayEl) {
      ensureGenericBubble();
      if (reasoningTrayEl) mountReasoningInBubble(genericBubbleEl);
      renderGenericIntoBubble();
    }
  };
  const insertGenericDelta = (text) => {
    genericBuffer += text || '';
    if (!_genericStreamingEnabled) return;
    // Hold the typing indicator until the buffer has something the user can
    // actually see. A reply that opens with an HTML comment (hidden
    // metadata) renders to nothing, so mounting now would swap "Typing…"
    // for a blank bubble until real text arrives. Once the bubble is
    // mounted we always re-render — later deltas extend it (and a trailing
    // comment after visible content is fine).
    if (!genericBubbleEl && !hasVisibleGenericContent(genericBuffer)) return;
    ensureGenericBubble();
    renderGenericIntoBubble();
  };
  const insertReasoningDelta = (text) => {
    reasoningBuffer += text || '';
    if (!_genericStreamingEnabled) return;
    if (!reasoningTrayEl) {
      reasoningTrayEl = renderReasoningTray(reasoningBuffer);
      if (reasoningTrayEl) {
        // Mount inside the current bubble (real, or the placeholder if
        // content hasn't started yet) at the top.
        const targetBubble = genericBubbleEl
          || placeholderRow.querySelector('.bubble');
        if (targetBubble) {
          mountReasoningInBubble(targetBubble);
        } else {
          indicator.appendChild(reasoningTrayEl);
        }
      }
    } else if (typeof reasoningTrayEl._setText === 'function') {
      reasoningTrayEl._setText(reasoningBuffer);
    }
  };
  // Snapshot the active path's tail at trigger time so the server can detect
  // a stale view (e.g. the same chat open in another tab that has since
  // generated). Empty string ⇒ "I see an empty chat".
  const _activePathIds = state.activePathIds || [];
  const _expectedTipId = _activePathIds.length
    ? _activePathIds[_activePathIds.length - 1]
    : '';
  _activeStream = startGenerationStream(chat.id, {
    parentId: opts.parentId,
    greeting: !!opts.greeting,
    expectedTipId: _expectedTipId,
    mode: opts.mode || 'normal',
    onStart: (data) => { pendingNewMsgId = data.message_id; },
    onContext: (data) => {
      setState({
        contextTokens: data.total_tokens,
        contextMsgsIn: data.messages_in_context ?? null,
        contextMsgsTotal: data.messages_total ?? null,
        contextOldestId: data.oldest_in_context_id ?? null,
        contextActiveBrains: data.active_brains || [],
        // SSE during generation: this is what's being used right now,
        // not a stored snapshot. The next /context-tokens fetch after
        // persist will read the same set back via message provenance
        // and flip this to true.
        contextActiveBrainsFromLastGen: !!data.active_brains_from_last_gen,
      });
    },
    onBubble: (bubble) => {
      insertBubble(bubble);
      autoplayBubble(bubble, chat, contact, user, isGreeting);
      pinScroll();
    },
    onDelta: (data) => {
      insertGenericDelta(data && data.text);
      pinScroll();
    },
    onReasoningDelta: (data) => {
      insertReasoningDelta(data && data.text);
      pinScroll();
    },
    onToken: () => {
      // Heartbeat — no DOM changes, but re-pinning keeps the smooth-
      // scroll alive in case the browser dropped it (or scrollHeight
      // settled differently after a layout-affecting microtask).
      pinScroll();
    },
    onError: (err) => {
      errorMsg = err.message || 'unknown error';
      errorPayload = err;
    },
    onDone: (payload) => {
      // Streaming-off path: materialise the bubble + reasoning all at
      // once now. Streaming-on path: the bubble's normally already there
      // from the first delta, and ``done`` only clears the pulse — but if
      // every delta was deferred (a reply that's nothing but an HTML
      // comment never produces visible content), mount it now so the final
      // state shows before the post-completion refresh.
      if (!_genericStreamingEnabled || !genericBubbleEl) finalizeGenericBubble();
      indicator.querySelectorAll('.bubble.streaming').forEach(b => {
        b.classList.remove('streaming');
      });
      pinScroll();
      const cancelled = !!(payload && payload.cancelled);
      // Auto-TTS for Generic mode. AER emits discrete ``bubble`` events and
      // speaks each in ``onBubble`` as it lands; Generic streams one buffer
      // via ``delta`` with no per-bubble boundary, so ``onBubble`` never fires
      // and the finished reply is the only speakable unit. ``autoplayBubble``
      // re-applies the global × contact gate and ``enqueue`` slices the text
      // internally, so playback is still chunked. Gated like the chime below —
      // a clean completion only — and on real visible content so a reply that
      // renders to nothing (e.g. an HTML-comment-only turn) stays silent.
      if (isGenericMode && !cancelled && !errorMsg
          && hasVisibleGenericContent(genericBuffer)) {
        autoplayBubble({ text: genericBuffer }, chat, contact, user, isGreeting);
      }
      // Settings-gated chime on successful completion only — cancel and
      // explicit errors stay silent (the user already knows they happened).
      if (!cancelled && !errorMsg
          && state.settings && state.settings.notify_on_complete) {
        playNotification();
      }
    },
  });

  try {
    await _activeStream.promise;
  } catch (e) {
    errorMsg = e.message;
  } finally {
    // Don't remove the indicator here — let ``refreshMessages``'s
    // replaceChildren wipe it atomically alongside the old DOM when the
    // post-completion ``loadActiveChat`` runs. Removing it now (sync) and
    // then awaiting the API roundtrip leaves a visible "no indicator, no
    // new message" gap that produces the wiggle on send.
    // Decide the post-generation scroll BEFORE the refresh wipes the DOM.
    // Land the user at the start of the new reply when they either never
    // took manual control (still following) OR scrolled up ABOVE the reply
    // (they went to check earlier content and would otherwise miss the
    // answer). If they scrolled around WITHIN the reply area, leave the
    // viewport exactly where they put it.
    const _followRoot = document.getElementById('chat-messages');
    const _released = !!(_streamFollow && _streamFollow.released);
    let _scrolledAbove = false;
    const _indicatorEl = document.getElementById('typing-indicator');
    if (_followRoot && _indicatorEl) {
      // "Above the reply" ⇔ its start sits below the viewport TOP — the user
      // scrolled up past the beginning of the new bubble to read earlier
      // content. (Scrolling DOWN into the reply puts its top above the
      // viewport top, which reads as "within" and keeps the position.) The
      // viewport-top reference is what makes a reply that only peeks into the
      // bottom of the viewport still count as "above" — a viewport-bottom
      // test missed that "parked just above the message" case.
      _scrolledAbove = _indicatorEl.offsetTop > _followRoot.scrollTop + 4;
      // Seed the new message's height from the just-streamed indicator so the
      // refresh doesn't virtualize the (never-rendered-as-a-.msg) tail into a
      // VIRT_ESTIMATED_HEIGHT spacer — which collapses scrollHeight and clamps
      // a kept viewport upward, above where the reply starts. The exact height
      // overwrites this the moment the message actually renders.
      if (pendingNewMsgId && _indicatorEl.offsetHeight > 0) {
        _virtHeightCache.set(pendingNewMsgId, _indicatorEl.offsetHeight);
      }
    }
    const _scrollToNewBubble = !_released || _scrolledAbove;
    _streamFollow = null;
    // Forward the streaming tray's expand state onto the about-to-be-
    // persisted msg id so renderMessage opens the re-rendered tray in
    // the same position the user left it. Without this the user clicks
    // Thoughts open mid-stream, generation ends, refreshMessages re-renders
    // the message from state, and the new tray comes back collapsed.
    if (reasoningTrayEl && pendingNewMsgId
        && typeof reasoningTrayEl._isExpanded === 'function'
        && reasoningTrayEl._isExpanded()) {
      _expandedReasoningIds.add(pendingNewMsgId);
    }
    setState({ generating: false });
    setSendBtnMode('send');
    document.removeEventListener('keydown', escHandler);
    _teardownFollowListeners();
    // Drop the virtual sibling BEFORE the refresh so ``renderControls``
    // doesn't paint a stale +1 sibling count (e.g. 43/43 instead of 42/42)
    // for the moments between gen-done and the next interaction. The lock
    // snaps onto the new message id captured from the SSE 'start' event
    // ONLY if the user was still parked on the virtual at completion —
    // otherwise (they navigated up, curlefted to the original, or
    // released via End/Esc) we respect their current position. The
    // post-refresh check below downgrades to ``selected_child`` if the
    // pending id never actually landed in storage.
    const _v = _virtualMsg;
    if (_v) {
      const wasOnVirtual = _navLockedId === _v.id;
      _virtualMsg = null;
      _virtualSlotShown = false;
      _virtualReplacingId = null;
      if (wasOnVirtual && pendingNewMsgId) _navLockedId = pendingNewMsgId;
    }
    if (errorMsg) {
      if (errorPayload && errorPayload.kind === 'brain_budget') {
        _showBrainBudgetToast(errorPayload);
      } else if (errorPayload && errorPayload.kind === 'no_provider') {
        toast('No provider configured for Generic mode. Pick one in Settings.', 'error');
      } else if (errorPayload && errorPayload.kind === 'no_preset') {
        toast('No Context Preset selected. Pick one in Settings.', 'error');
      } else if (errorPayload && errorPayload.kind === 'no_token') {
        toast('No API token configured for the active provider. Set one in Settings.', 'error');
      } else if (errorPayload && errorPayload.kind === 'no_aer_configured') {
        toast('Deletion response unavailable: AetherRoom isn\'t configured.', 'error');
      } else {
        toast(`Generation error: ${errorMsg}`, 'error');
      }
    }
    // ``preserveScroll`` when we're keeping the viewport fixed (the user
    // scrolled within the reply area) — otherwise refreshMessages' own
    // wasNearBottom heuristic could snap them to the bottom. The seeded tail
    // height above keeps that preserve from clamping upward.
    await loadActiveChat(chat.id, { preserveScroll: !_scrollToNewBubble });
    // Restore the hidden ``hideMsgId`` chain AFTER the refresh — by then
    // ``loadActiveChat`` has either replaced the DOM (the loop is a no-op
    // against detached nodes) or errored without re-rendering, in which
    // case we need to put the messages back. Doing this BEFORE the await
    // makes the hidden tail + descendants flash visible for the duration
    // of the API roundtrip just before the wholesale re-render wipes them.
    for (const e of hiddenEls) e.style.display = '';
    const all = await api.listChats();
    setState({ chats: all });
    if (_v) {
      // Only validate / fall back when there's still a lock to validate.
      // A null lock means the user explicitly released (End / Esc / nav
      // past the bottom) — respect that and don't snap onto the new
      // message just because the slot has a ``selected_child`` now.
      if (_navLockedId) {
        const inMsgs = (state.chatMessages || []).some(m => m.id === _navLockedId);
        if (!inMsgs) {
          const refreshed = getChatById(state, chat.id);
          const sel = refreshed?.selected_child_id?.[_v.parent_id || ''];
          _navLockedId = (sel && sel !== '__empty__') ? sel : null;
        }
      }
      applyNavLockHighlight();
    }
    // Land the user at the start of the new reply when they were following
    // or had scrolled above it. ``_smartScrollToMessageById`` materialises
    // the tail into the window if virtualization spacer'd it, then anchors
    // its top in view (its own "top visible if it fits, bottom gap above the
    // input bar" rules). When we're keeping the viewport fixed instead, the
    // preserveScroll refresh above already left it in place — nothing to do.
    if (_scrollToNewBubble && pendingNewMsgId) {
      const messagesRoot = document.getElementById('chat-messages');
      if (messagesRoot) _smartScrollToMessageById(pendingNewMsgId, messagesRoot);
    }
  }
}


// macOS shows ⌘ / ⌥ where other platforms show Ctrl / Alt. The key handlers
// accept the native modifier either way (Cmd is ``metaKey``, which the newline
// handler treats the same as Ctrl; Option is ``altKey``), so only the displayed
// legend needs translating. ``navigator.platform`` is deprecated but remains
// the most reliable desktop-OS signal — and the one tests can override.
const IS_MAC = /mac/i.test(navigator.platform || navigator.userAgent || '');
const MAC_KEY_LABELS = { Ctrl: '⌘', Alt: '⌥' };
function shortcutKeyLabel(key) {
  return IS_MAC ? (MAC_KEY_LABELS[key] || key) : key;
}


function emptyState() {
  function row(keys, desc) {
    return el('div', { class: 'shortcut-row' },
      el('div', { class: 'shortcut-keys' },
        ...keys.map(k => el('kbd', {}, shortcutKeyLabel(k))),
      ),
      el('div', { class: 'shortcut-desc' }, desc),
    );
  }
  return el('div', { class: 'content-empty' },
    el('div', {},
      el('h3', {}, 'Pick a chat — or start a new one'),
      el('div', {}, 'Your conversations live in the column on the left.'),
      el('div', { class: 'fiction-warning' },
        'Remember, everything here is strictly fictional roleplay created by ' +
        'a hallucination generator. Only an utter fool would believe anything ' +
        'written in these chats.',
      ),
      el('div', { class: 'shortcut-list' },
        el('div', { class: 'shortcut-title' }, 'Keyboard shortcuts'),
        row(['Enter'],            'Send — or edit the selection on empty input'),
        row(['Ctrl', 'Enter'],    'Insert a newline'),
        row(['Alt', 'Enter'],     'Impersonate — the model writes your next message'),
        row(['↑', '↓'],           'Select the previous / next message'),
        row(['←', '→'],           'Switch branches; reroll past the last'),
        row(['End'],              'Drop the selection, jump back to the input'),
        row(['Esc'],              'Drop the selection, or cancel a reply in progress'),
        row(['PgUp', 'PgDn'],     'Scroll the chat'),
      ),
    ),
  );
}
