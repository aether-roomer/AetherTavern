/* URL ↔ state sync.
 *
 * URLs are bookmarkable: directly navigating to e.g. ``/contacts/alice-1438d31b``
 * loads the app with that contact's edit page already open.
 *
 *   /                                          → chats list, no chat open
 *   /chats                                     → same as /
 *   /chats/alice-anon-d2319c91                 → chat open (contact-user-uuid8)
 *   /chats/alice-anon-cafe-d2319c91            → with scenario in slug
 *   /contacts                                  → contacts list, none selected
 *   /contacts/alice-1438d31b                   → that contact's edit page
 *   /users/anon-11fd7328                       → user persona edit
 *   /scenarios/cafe-c0fe7801                   → scenario edit
 *   /settings                                  → settings page
 */

import { state, setState, subscribe } from './state.js';
import { slugify } from './util.js';


/* ----- slug builders & resolvers, per tab ----- */

function chatSlug(chat, ctx) {
  const parts = [];
  const contact = ctx.contacts.find(c => c.id === chat.contact_id);
  const user = ctx.users.find(u => u.id === chat.user_id);
  const scenario = chat.scenario_id ? ctx.scenarios.find(s => s.id === chat.scenario_id) : null;
  if (contact) parts.push(slugify(contact.name));
  if (user) parts.push(slugify(user.name));
  if (scenario) parts.push(slugify(scenario.name));
  parts.push((chat.id || '').slice(0, 8));
  return parts.join('-');
}

function entitySlug(entity) {
  const name = entity.name || entity.title || 'untitled';
  return `${slugify(name)}-${(entity.id || '').slice(0, 8)}`;
}

function findById8(list, slugStr) {
  if (!slugStr) return null;
  const id8 = String(slugStr).toLowerCase().split('-').pop();
  return list.find(e => (e.id || '').slice(0, 8) === id8) || null;
}


const TABS = {
  chats:          { activeKey: 'activeChatId',          list: 'chats',          buildSlug: chatSlug },
  contacts:       { activeKey: 'activeContactId',       list: 'contacts',       buildSlug: entitySlug },
  users:          { activeKey: 'activeUserId',          list: 'users',           buildSlug: entitySlug },
  scenarios:      { activeKey: 'activeScenarioId',      list: 'scenarios',       buildSlug: entitySlug },
  libraries:      { activeKey: 'activeLibraryId',       list: 'libraries',       buildSlug: entitySlug },
  contextPresets: { activeKey: 'activeContextPresetId', list: 'contextPresets',  buildSlug: entitySlug, urlSegment: 'context-presets' },
};
const KNOWN_TABS = new Set([...Object.keys(TABS), 'settings']);

// Map the URL segment back to the tab name for parsing. The
// ``context-presets`` segment is dash-cased per URL convention; the tab
// itself is camelCase to keep parity with the state key.
const URL_SEGMENT_TO_TAB = (() => {
  const out = {};
  for (const [tab, cfg] of Object.entries(TABS)) {
    out[cfg.urlSegment || tab] = tab;
  }
  return out;
})();


export function urlForState() {
  const tab = state.activeTab || 'chats';
  if (tab === 'settings') return '/settings';
  const cfg = TABS[tab];
  if (!cfg) return '/';
  const segment = cfg.urlSegment || tab;
  const activeId = state[cfg.activeKey];
  if (!activeId) return `/${segment}`;
  const ent = (state[cfg.list] || []).find(x => x.id === activeId);
  if (!ent) return `/${segment}`;
  return `/${segment}/${cfg.buildSlug(ent, state)}`;
}


export function parsePath(pathname) {
  const parts = String(pathname || '').split('/').filter(Boolean);
  if (parts.length === 0) return { tab: 'chats', slug: null };
  const segment = parts[0];
  if (segment === 'settings') return { tab: 'settings', slug: null };
  const tab = URL_SEGMENT_TO_TAB[segment];
  if (!tab) return { tab: 'chats', slug: null };
  return { tab, slug: parts.slice(1).join('/') || null };
}


export function applyUrl() {
  const parsed = parsePath(location.pathname);
  const patch = { activeTab: parsed.tab };
  if (parsed.tab in TABS) {
    const cfg = TABS[parsed.tab];
    if (parsed.slug) {
      const ent = findById8(state[cfg.list] || [], parsed.slug);
      patch[cfg.activeKey] = ent ? ent.id : null;
    } else {
      patch[cfg.activeKey] = null;
    }
  }
  setState(patch);
}


let _lastPushedUrl = null;
let _suspended = false;

export function pushUrlForState() {
  if (_suspended) return;
  const url = urlForState();
  if (url !== _lastPushedUrl) {
    if (_lastPushedUrl == null) history.replaceState(null, '', url);
    else history.pushState(null, '', url);
    _lastPushedUrl = url;
  }
}


export function installRouter() {
  _suspended = true;
  applyUrl();
  _suspended = false;
  _lastPushedUrl = location.pathname || '/';

  subscribe(() => pushUrlForState());

  window.addEventListener('popstate', () => {
    _suspended = true;
    try {
      applyUrl();
    } finally {
      _suspended = false;
      _lastPushedUrl = location.pathname || '/';
    }
  });
}
