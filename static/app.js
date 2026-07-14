/* AetherRoom — main entrypoint. Boots state and dispatches to view renderers. */

import { api } from './api.js';
import { state, setState, subscribe } from './state.js';
import { toast } from './ui.js';
import { installRouter, parsePath } from './router.js';
import { TOGGLEABLE_THEMES } from './constants.js';

import { renderChatList } from './views/chat_list.js';
import { renderChatView } from './views/chat.js';
import { renderContactsTab, setupContactsTab } from './views/contacts.js';
import { renderUsersTab, setupUsersTab } from './views/users.js';
import { renderScenariosTab, setupScenariosTab } from './views/scenarios.js';
import { renderBrainLibrariesTab, setupBrainLibrariesTab } from './views/brain_libraries.js';
import { renderContextPresetsTab, setupContextPresetsTab } from './views/context_presets.js';
import { renderSettingsTab } from './views/settings.js';
import { drainSaveQueue } from './save_queue.js';


/* ---------- Boot ---------- */

async function boot() {
  // Restore list-sort prefs from localStorage before any view first renders.
  try {
    const sortPatch = {};
    for (const k of [
      'contactSort', 'userSort', 'scenarioSort', 'librarySort',
      'contextPresetSort', 'chatSort',
    ]) {
      const v = localStorage.getItem(k);
      if (!v) continue;
      try {
        const parsed = JSON.parse(v);
        if (parsed && typeof parsed === 'object' && parsed.mode) sortPatch[k] = parsed;
      } catch {}
    }
    if (Object.keys(sortPatch).length) setState(sortPatch);
  } catch {}
  try {
    if (localStorage.getItem('sidebarCollapsed') === '1') {
      setState({ sidebarCollapsed: true });
    }
  } catch {}

  // Tab-scoped boot: only fetch what the URL-derived active tab needs
  // for its first paint. Settings + presets are always fetched (settings
  // because the theme + font CSS vars must apply before render; presets
  // because they're tiny and the settings page uses them). The other
  // lists go to the background after first paint via ``_hydrateRemaining``.
  const parsedAtBoot = parsePath(location.pathname);
  try {
    const required = {
      settings: api.getSettings(),
      presets: api.listPresets(),
    };
    // The chats tab joins on contact / user / scenario for row rendering
    // and filter chips, so all three must be loaded before paint when
    // the user lands here. Otherwise the row "contact name" briefly
    // shows blank until the background fetch lands.
    if (parsedAtBoot.tab === 'chats') {
      required.contacts = api.listContacts();
      required.users = api.listUsers();
      required.scenarios = api.listScenarios();
      // For ``/chats/{slug}`` we also need state.chats to resolve the
      // slug → id mapping in ``applyUrl``. ``api.listChats`` returns the
      // legacy flat list (no params) — first paint uses these rows; the
      // paged adapter kicks in for further pagination as the user scrolls.
      required.chats = api.listChats();
    } else if (parsedAtBoot.tab === 'contacts') {
      required.contacts = api.listContacts();
    } else if (parsedAtBoot.tab === 'users') {
      required.users = api.listUsers();
    } else if (parsedAtBoot.tab === 'scenarios') {
      required.scenarios = api.listScenarios();
    } else if (parsedAtBoot.tab === 'libraries') {
      required.libraries = api.listBrainLibraries();
    } else if (parsedAtBoot.tab === 'contextPresets') {
      required.contextPresets = api.listContextPresets();
    }
    // /settings: presets is already required.

    const entries = await Promise.all(
      Object.entries(required).map(async ([k, p]) => [k, await p])
    );
    const patch = {};
    for (const [k, v] of entries) patch[k] = v;
    setState(patch);
    const settings = patch.settings;
    document.documentElement.dataset.theme = settings.theme || 'dark';
    document.documentElement.style.setProperty(
      '--ui-font-size', `${settings.font_size || 14}px`);
    document.documentElement.style.setProperty(
      '--content-font-size', `${settings.content_font_size || settings.font_size || 14}px`);
    // null / undefined ⇒ unlimited. ``100%`` (not ``none``) so the value stays
    // usable inside the `.msg.contact` controls-alignment calc; for the bubble
    // itself `100%` and `none` are equivalent.
    document.documentElement.style.setProperty(
      '--bubble-max-width',
      settings.max_bubble_width_em == null ? '100%' : `${settings.max_bubble_width_em}em`);
    refreshThemeIcon();
  } catch (e) {
    toast(`Failed to load: ${e.message}`, 'error');
  }
  setupRail();
  setupMobileNav();
  setupTabs();
  installRouter();
  render();
  // Background-fetch the lists that weren't required for first paint.
  // The chat-list pane / detail editors stay responsive — they read
  // ``state.{kind}`` and re-render when the background fetch lands.
  _hydrateRemaining(parsedAtBoot.tab);
  // Flush any saves that were queued by a prior session but didn't make
  // it to the server (page closed mid-retry, transient outage, …). Done
  // after render so the user sees the app first; the drain pops a
  // conflict modal on top if any of those queued payloads collide with
  // a remote change.
  drainSaveQueue().catch(e => console.error('drainSaveQueue:', e));
}


function _hydrateRemaining(activeTab) {
  // Schedule on requestIdleCallback so the background fetches don't
  // compete with the first-paint frame. Each fetch is a small list
  // endpoint (Summary projections) so they typically complete in <50 ms.
  const sched = window.requestIdleCallback
    ? (fn) => window.requestIdleCallback(fn, { timeout: 1000 })
    : (fn) => setTimeout(fn, 0);
  sched(async () => {
    const tasks = [];
    // Anything NOT in the required set for ``activeTab``. The chats tab
    // pulled contacts/users/scenarios as required, so they're already
    // fetched and we skip them here.
    if (activeTab !== 'chats' && activeTab !== 'contacts') {
      tasks.push(['contacts', api.listContacts]);
    }
    if (activeTab !== 'chats' && activeTab !== 'users') {
      tasks.push(['users', api.listUsers]);
    }
    if (activeTab !== 'chats' && activeTab !== 'scenarios') {
      tasks.push(['scenarios', api.listScenarios]);
    }
    if (activeTab !== 'libraries') {
      tasks.push(['libraries', api.listBrainLibraries]);
    }
    if (activeTab !== 'contextPresets') {
      tasks.push(['contextPresets', api.listContextPresets]);
    }
    if (activeTab !== 'chats') {
      tasks.push(['chats', api.listChats]);
    }
    for (const [key, fn] of tasks) {
      try { setState({ [key]: await fn() }); }
      catch (e) { console.warn(`background fetch ${key}:`, e); }
    }
  });
}


/* ---------- Rail ---------- */

function setupRail() {
  const rail = document.getElementById('rail');
  rail.addEventListener('click', (e) => {
    const btn = e.target.closest('.rail-btn');
    if (!btn) return;
    if (btn.id === 'theme-toggle') {
      toggleTheme();
      return;
    }
    if (btn.id === 'sidebar-toggle') {
      const next = !state.sidebarCollapsed;
      setState({ sidebarCollapsed: next });
      try { localStorage.setItem('sidebarCollapsed', next ? '1' : '0'); } catch {}
      return;
    }
    if (btn.dataset.tab) {
      // Tab clicks always restore the sidebar — that's how the user "brings
      // it back" after a collapse. Bundle into one setState so subscribers
      // only re-render once.
      setState({ activeTab: btn.dataset.tab, sidebarCollapsed: false });
      try { localStorage.setItem('sidebarCollapsed', '0'); } catch {}
    }
  });
}


/* ---------- Mobile nav toggle (hamburger ↔ back) ---------- */

const MOBILE_MQ = window.matchMedia ? window.matchMedia('(max-width: 720px)') : null;

function _activeIdKeyForTab(tab) {
  if (tab === 'chats') return 'activeChatId';
  if (tab === 'contacts') return 'activeContactId';
  if (tab === 'users') return 'activeUserId';
  if (tab === 'scenarios') return 'activeScenarioId';
  if (tab === 'libraries') return 'activeLibraryId';
  if (tab === 'contextPresets') return 'activeContextPresetId';
  return null;
}

function isMobileDetailOpen() {
  if (!MOBILE_MQ || !MOBILE_MQ.matches) return false;
  const key = _activeIdKeyForTab(state.activeTab);
  return !!(key && state[key]);
}

function applyMobileDetailOpen() {
  document.body.classList.toggle('mobile-detail-open', isMobileDetailOpen());
}

function setupMobileNav() {
  const navBtn = document.getElementById('mobile-nav-toggle');
  if (navBtn) {
    navBtn.addEventListener('click', () => {
      // The mobile-nav-toggle is back-only (visible only in detail mode).
      const key = _activeIdKeyForTab(state.activeTab);
      if (key) setState({ [key]: null });
    });
  }
  // Re-evaluate body.mobile-detail-open when the user crosses the breakpoint.
  if (MOBILE_MQ && typeof MOBILE_MQ.addEventListener === 'function') {
    MOBILE_MQ.addEventListener('change', applyMobileDetailOpen);
  }
  setupKbInset();
  setupEdgeSwipe();
}


/* ---------- Edge-swipe back gesture ----------
 *
 * Touch users can drag from the left edge of the viewport to interactively
 * back out of a detail pane (chat, contact, user, scenario). The detail
 * pane translates with the finger; the list pane peeks underneath. Past
 * a threshold the swipe commits and the active entity is cleared; before
 * the threshold it snaps back.
 *
 * Gated three ways: pointerType === 'touch' (desktop mice can't trigger),
 * matchMedia(max-width:720px) (only phones), and body.mobile-detail-open
 * (only when there's something to back out of). */
let _edgeSwipeState = null;
function setupEdgeSwipe() {
  document.addEventListener('pointerdown', (e) => {
    if (e.pointerType !== 'touch') return;
    if (!MOBILE_MQ || !MOBILE_MQ.matches) return;
    if (!document.body.classList.contains('mobile-detail-open')) return;
    // Only fire for swipes starting near the left edge.
    if (e.clientX > 20) return;
    // Don't start if the user is touching an interactive control — a
    // tap on the back button at the edge shouldn't get hijacked.
    if (e.target.closest('button, a, input, textarea, select, [contenteditable]')) return;
    _edgeSwipeState = {
      startX: e.clientX,
      startY: e.clientY,
      pointerId: e.pointerId,
      locked: false,
      abandoned: false,
    };
  });

  document.addEventListener('pointermove', (e) => {
    if (!_edgeSwipeState || e.pointerId !== _edgeSwipeState.pointerId) return;
    if (_edgeSwipeState.abandoned) return;
    const dx = e.clientX - _edgeSwipeState.startX;
    const dy = e.clientY - _edgeSwipeState.startY;
    if (!_edgeSwipeState.locked) {
      // Vertical wins → user is scrolling, not edge-swiping.
      if (Math.abs(dy) > 10 && Math.abs(dy) > Math.abs(dx)) {
        _edgeSwipeState.abandoned = true;
        return;
      }
      // Right-going horizontal motion crosses the lock threshold.
      if (dx > 30) {
        _edgeSwipeState.locked = true;
        document.body.classList.add('edge-swiping');
      }
    }
    if (_edgeSwipeState.locked) {
      const tx = Math.max(0, dx);
      document.documentElement.style.setProperty('--edge-tx', `${tx}px`);
      e.preventDefault();
    }
  });

  document.addEventListener('pointerup', (e) => {
    if (!_edgeSwipeState || e.pointerId !== _edgeSwipeState.pointerId) return;
    const swipe = _edgeSwipeState;
    _edgeSwipeState = null;
    if (!swipe.locked) {
      document.body.classList.remove('edge-swiping');
      document.documentElement.style.removeProperty('--edge-tx');
      return;
    }
    const dx = e.clientX - swipe.startX;
    const threshold = window.innerWidth / 3;
    document.body.classList.add('edge-swipe-anim');
    if (dx > threshold) {
      // Commit: animate the detail pane fully off-screen, then clear the
      // active entity. The mobile-detail-open class flips next render and
      // the list pane takes over.
      document.documentElement.style.setProperty('--edge-tx', `${window.innerWidth}px`);
      setTimeout(() => {
        document.body.classList.remove('edge-swipe-anim');
        document.body.classList.remove('edge-swiping');
        document.documentElement.style.removeProperty('--edge-tx');
        const key = _activeIdKeyForTab(state.activeTab);
        if (key) setState({ [key]: null });
      }, 200);
    } else {
      // Cancel: animate back to 0.
      document.documentElement.style.setProperty('--edge-tx', '0px');
      setTimeout(() => {
        document.body.classList.remove('edge-swipe-anim');
        document.body.classList.remove('edge-swiping');
        document.documentElement.style.removeProperty('--edge-tx');
      }, 200);
    }
  });

  document.addEventListener('pointercancel', (e) => {
    if (!_edgeSwipeState || e.pointerId !== _edgeSwipeState.pointerId) return;
    _edgeSwipeState = null;
    document.body.classList.remove('edge-swiping');
    document.body.classList.remove('edge-swipe-anim');
    document.documentElement.style.removeProperty('--edge-tx');
  });
}


/* ---------- Mobile keyboard inset ----------
 *
 * Tracks the height of the on-screen keyboard via the visualViewport API
 * and exposes it as a CSS custom property `--kb-inset`. The chat input
 * area's `padding-bottom: max(env(safe-area-inset-bottom), var(--kb-inset))`
 * keeps the textarea visible above the keyboard on iOS Safari (where
 * `100dvh` doesn't always shrink to exclude the keyboard).
 *
 * Desktop: visualViewport.height === window.innerHeight always, so the
 * inset stays 0 px and the rule that reads it (gated by the mobile media
 * query) never runs anyway. */
function setupKbInset() {
  if (!window.visualViewport) return;
  const apply = () => {
    const inset = Math.max(0, window.innerHeight - window.visualViewport.height);
    document.documentElement.style.setProperty('--kb-inset', `${inset}px`);
  };
  window.visualViewport.addEventListener('resize', apply);
  apply();
}

// Two distinct SVG bodies so the sidebar-toggle visibly swaps icons rather
// than just changing colour: panel-with-content + arrow-into-it when expanded
// (click to tuck away), narrow rail + arrow-out-of-it when collapsed (click
// to bring back).
const SIDEBAR_ICON_PATHS = {
  expanded:
    '<rect x="3" y="4" width="18" height="16" rx="2"/>' +
    '<path d="M9 4v16"/>' +
    '<path d="M16 9l-3 3 3 3"/>',
  collapsed:
    '<rect x="3" y="4" width="18" height="16" rx="2"/>' +
    '<path d="M9 4v16"/>' +
    '<path d="M13 9l3 3-3 3"/>',
};

function applySidebarCollapsedToDom() {
  document.body.classList.toggle('sidebar-collapsed', !!state.sidebarCollapsed);
  const btn = document.getElementById('sidebar-toggle');
  if (btn) {
    btn.classList.toggle('active', !!state.sidebarCollapsed);
    btn.title = state.sidebarCollapsed ? 'Expand sidebar' : 'Collapse sidebar';
    // No sidebar on the settings tab — hide the toggle so it doesn't
    // pretend it has work to do there.
    btn.hidden = state.activeTab === 'settings';
    const ic = document.getElementById('sidebar-toggle-icon');
    if (ic) {
      ic.innerHTML = state.sidebarCollapsed
        ? SIDEBAR_ICON_PATHS.collapsed
        : SIDEBAR_ICON_PATHS.expanded;
    }
  }
}

function highlightRail() {
  // Generic-only rail entries (e.g. Context Presets) hide entirely when
  // the user is in AER mode — the tab makes no sense there and the
  // entity isn't reachable from generation. Drives ``data-provider-mode``
  // on the document root so CSS can also key off it if needed.
  const providerMode =
    (state.settings && state.settings.provider_mode) || 'aetherroom';
  document.documentElement.dataset.providerMode = providerMode;
  const showGenericOnly = providerMode === 'generic';
  for (const btn of document.querySelectorAll('.rail-btn')) {
    btn.classList.toggle('active', btn.dataset.tab === state.activeTab);
    if (btn.classList.contains('rail-generic-only')) {
      btn.hidden = !showGenericOnly;
    }
  }
}

const THEME_ICON_PATHS = {
  // Shown in dark mode → click to switch to light → use sun icon.
  dark: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M5 5l1.4 1.4M17.6 17.6L19 19M2 12h2M20 12h2M5 19l1.4-1.4M17.6 6.4L19 5"/>',
  // Shown in light mode → click to switch to dark → use moon icon.
  light: '<path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/>',
};

function refreshThemeIcon() {
  // Trust the reactive store, not dataset.theme — the latter can lag if
  // setState fires subscribers before applySettingsToDom runs.
  const theme = (state.settings && state.settings.theme)
    || document.documentElement.dataset.theme
    || 'dark';
  const icon = document.getElementById('theme-icon');
  const btn = document.getElementById('theme-toggle');
  // Sun/moon toggle only makes sense for the light/dark pair; for any other
  // theme we hide it (the Settings picker is the source of truth).
  if (btn) btn.hidden = !TOGGLEABLE_THEMES.has(theme);
  if (icon) icon.innerHTML = THEME_ICON_PATHS[theme] || THEME_ICON_PATHS.dark;
}

async function toggleTheme() {
  const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
  document.documentElement.dataset.theme = next;
  refreshThemeIcon();
  try {
    const updated = await api.putSettings({ theme: next });
    setState({ settings: updated });
  } catch (e) { toast(e.message, 'error'); }
}


/* ---------- Tab dispatch ---------- */

function setupTabs() {
  setupContactsTab();
  setupUsersTab();
  setupScenariosTab();
  setupBrainLibrariesTab();
  setupContextPresetsTab();
}

const _renderedTab = { tab: null };

// Per-tab scroll snapshot so switching tabs (rail click / back-button) lands
// the user where they left off. Covers the left ``.list-body`` and the right
// ``.page-scroll`` of every list+detail tab; the chats tab's ``.chat-messages``
// is intentionally left alone — its own virtualized scroll logic owns that.
const _scrollByTab = {};

// Observer that watches for ``.page-scroll`` to appear after an async
// ``refreshDetail`` resolves. Cancelled if the user switches tabs again
// before it fires, to avoid restoring scroll on a tab the user has left.
let _pendingPageScrollRestore = null;

function _snapshotTabScroll(tabName) {
  if (!tabName) return;
  const main = document.getElementById('main');
  if (!main) return;
  const listBody = main.querySelector('.list-body');
  const pageScroll = main.querySelector('.page-scroll');
  // If the element isn't in the DOM yet (user switched away before the
  // async detail fetch resolved), keep the previously saved value rather
  // than blowing it away with a misleading 0.
  const prev = _scrollByTab[tabName] || { listBody: 0, pageScroll: 0 };
  _scrollByTab[tabName] = {
    listBody: listBody ? listBody.scrollTop : prev.listBody,
    pageScroll: pageScroll ? pageScroll.scrollTop : prev.pageScroll,
  };
}

function _restoreTabScroll(tabName) {
  // Cancel any in-flight page-scroll observer from the previous tab switch —
  // its target tab is no longer current.
  if (_pendingPageScrollRestore) {
    _pendingPageScrollRestore.cancel();
    _pendingPageScrollRestore = null;
  }
  const saved = _scrollByTab[tabName];
  if (!saved) return;
  const main = document.getElementById('main');
  if (!main) return;
  const listBody = main.querySelector('.list-body');
  if (listBody) listBody.scrollTop = saved.listBody;

  // ``.page-scroll`` is appended by the detail view's async ``refreshDetail``
  // (it awaits ``api.getX(id)`` before the editor DOM is built); on top of
  // that, content can keep growing as late-rendering tiles land (e.g. the
  // Context Preset tab's preview cards). A one-shot scrollTop assignment
  // gets clamped while scrollHeight is small and stays clamped after the
  // late content lands. Keep watching subtree mutations until either the
  // saved position is fully reachable, the user manually scrolls, or the
  // deadline expires.
  let lastSet = -1;
  const deadline = performance.now() + 2000;
  const apply = () => {
    const pageScroll = main.querySelector('.page-scroll');
    if (!pageScroll) return 'wait';
    // User-scroll detection: respect manual scroll once we've made at
    // least one successful apply.
    if (lastSet >= 0 && Math.abs(pageScroll.scrollTop - lastSet) > 3) {
      return 'done';
    }
    pageScroll.scrollTop = saved.pageScroll;
    lastSet = pageScroll.scrollTop;
    return lastSet >= saved.pageScroll ? 'done' : 'partial';
  };
  if (apply() === 'done') return;
  const observer = new MutationObserver(() => {
    if (performance.now() > deadline) {
      observer.disconnect();
      if (_pendingPageScrollRestore && _pendingPageScrollRestore.observer === observer) {
        _pendingPageScrollRestore = null;
      }
      return;
    }
    if (apply() === 'done') {
      observer.disconnect();
      if (_pendingPageScrollRestore && _pendingPageScrollRestore.observer === observer) {
        _pendingPageScrollRestore = null;
      }
    }
  });
  observer.observe(main, { childList: true, subtree: true });
  const safetyTimer = setTimeout(() => {
    observer.disconnect();
    if (_pendingPageScrollRestore && _pendingPageScrollRestore.observer === observer) {
      _pendingPageScrollRestore = null;
    }
  }, 2500);
  _pendingPageScrollRestore = {
    observer,
    cancel: () => { observer.disconnect(); clearTimeout(safetyTimer); },
  };
}

function render() {
  highlightRail();
  applySidebarCollapsedToDom();
  const main = document.getElementById('main');
  const tab = state.activeTab;

  // The settings tab uses a single-column layout (no list pane); other tabs
  // use the standard list+content split. Sync this on every render so a
  // popstate / direct navigation to /settings always lays out correctly.
  main.classList.toggle('full', tab === 'settings');

  if (tab !== _renderedTab.tab) {
    _snapshotTabScroll(_renderedTab.tab);
    _renderedTab.tab = tab;
    if (tab === 'chats') {
      renderChatsTab(main);
    } else if (tab === 'contacts') {
      renderContactsTab(main);
    } else if (tab === 'users') {
      renderUsersTab(main);
    } else if (tab === 'scenarios') {
      renderScenariosTab(main);
    } else if (tab === 'libraries') {
      renderBrainLibrariesTab(main);
    } else if (tab === 'contextPresets') {
      renderContextPresetsTab(main);
    } else if (tab === 'settings') {
      renderSettingsTab(main);
    }
    _restoreTabScroll(tab);
  } else if (tab === 'chats') {
    // Same tab, but active chat may have changed — re-render right pane.
    refreshChatRightPane();
  }
}


/* ---------- Chats tab: list pane (persistent) + content pane (active chat) ---------- */

function renderChatsTab(container) {
  container.replaceChildren();
  const listSlot = document.createElement('div');
  // .chat-list-slot { display: contents } so the inner .list-pane is the
  // effective grid item of .main; otherwise this wrapper sits in the grid
  // with min-height: auto and a long chat list grows the row past the
  // viewport, pushing the chat-view's header/messages above the visible
  // area. Use a class (not inline style) so the mobile-detail-open hide
  // rule in styles.css can override it — inline ``display`` would win
  // over class rules.
  listSlot.className = 'chat-list-slot';
  const detailSlot = document.createElement('div');
  detailSlot.id = 'chat-content-pane';
  detailSlot.className = 'content-pane';
  container.append(listSlot, detailSlot);

  renderChatList(listSlot);
  refreshChatRightPane();
}

function refreshChatRightPane() {
  const slot = document.getElementById('chat-content-pane');
  if (!slot) return;
  renderChatView(slot);
}


/* ---------- Reactive ---------- */

let _lastActiveChat = null;
let _lastTheme = null;
let _lastSidebarCollapsed = null;
let _lastMobileDetailKey = null;
let _lastDocTitle = null;

function _computeDocTitle(s) {
  const base = 'AetherTavern';
  const tab = s.activeTab;
  if (tab === 'settings') return `Settings — ${base}`;
  let name = null;
  if (tab === 'chats' && s.activeChatId) {
    const full = s.chatMap && s.chatMap.get(s.activeChatId);
    const summary = !full && (s.chats || []).find(c => c.id === s.activeChatId);
    const chat = full || summary;
    name = chat ? (chat.title || 'Untitled chat') : null;
  } else if (tab === 'contacts' && s.activeContactId) {
    const e = (s.contacts || []).find(c => c.id === s.activeContactId);
    name = e ? e.name : null;
  } else if (tab === 'users' && s.activeUserId) {
    const e = (s.users || []).find(c => c.id === s.activeUserId);
    name = e ? e.name : null;
  } else if (tab === 'scenarios' && s.activeScenarioId) {
    const e = (s.scenarios || []).find(c => c.id === s.activeScenarioId);
    name = e ? e.name : null;
  } else if (tab === 'libraries' && s.activeLibraryId) {
    const e = (s.libraries || []).find(c => c.id === s.activeLibraryId);
    name = e ? e.name : null;
  } else if (tab === 'contextPresets' && s.activeContextPresetId) {
    const e = (s.contextPresets || []).find(c => c.id === s.activeContextPresetId);
    name = e ? e.name : null;
  }
  return name ? `${name} — ${base}` : base;
}

function _refreshDocTitle() {
  const next = _computeDocTitle(state);
  if (next !== _lastDocTitle) {
    _lastDocTitle = next;
    document.title = next;
  }
}

subscribe(() => {
  // Tab switch
  if (state.activeTab !== _renderedTab.tab) {
    render();
  }
  // Active chat change within chats tab
  if (state.activeTab === 'chats' && state.activeChatId !== _lastActiveChat) {
    _lastActiveChat = state.activeChatId;
    refreshChatRightPane();
  }
  // Theme switch (rail-toggle visibility + icon).
  const theme = state.settings && state.settings.theme;
  if (theme !== _lastTheme) {
    _lastTheme = theme;
    refreshThemeIcon();
  }
  // Sidebar collapsed toggle.
  if (state.sidebarCollapsed !== _lastSidebarCollapsed) {
    _lastSidebarCollapsed = state.sidebarCollapsed;
    applySidebarCollapsedToDom();
  }
  // Mobile detail-open toggle: any change in tab or per-tab active id might
  // flip whether the body should be in mobile-detail-open mode. Cheap check
  // — gated by isMobileDetailOpen() which short-circuits on desktop widths.
  const mobileKey = `${state.activeTab}|${state.activeChatId || ''}|${state.activeContactId || ''}|${state.activeUserId || ''}|${state.activeScenarioId || ''}|${state.activeLibraryId || ''}|${state.activeContextPresetId || ''}`;
  if (mobileKey !== _lastMobileDetailKey) {
    _lastMobileDetailKey = mobileKey;
    applyMobileDetailOpen();
  }
  // Highlight rail
  highlightRail();
  _refreshDocTitle();
});

boot();
_refreshDocTitle();
