/* Tiny pub-sub state container. Exposes a single `state` object plus
 * setState/subscribe for reactive view updates. */

export const state = {
  activeTab: 'chats',          // chats | contacts | users | scenarios | libraries | contextPresets | settings
  activeChatId: null,
  activeContactId: null,
  activeUserId: null,
  activeScenarioId: null,
  activeLibraryId: null,
  activeContextPresetId: null,
  filterContactId: null,        // chat list filter chip
  filterUserId: null,
  filterScenarioId: null,

  chats: [],
  // Full ``Chat`` objects keyed by id — populated by ``loadActiveChat``
  // and any code path that fetches a full chat via ``api.getChat``.
  // ``state.chats`` holds ``ChatSummary`` rows for the *list pane*;
  // consumers that need the chat's tree state (``selected_child_id``,
  // ``last_deleted_child``, ``rollover_*``, ``pick_reroll_nonce``) read
  // from ``chatMap`` instead. Mutate via ``setChatInMap`` below so the
  // pub-sub fires (a bare ``chatMap.set`` is invisible to subscribers
  // and leaves the dynamic window title / list-row title stale).
  chatMap: new Map(),
  // Bumped by ``setChatInMap`` whenever the underlying Map mutates.
  // The dynamic ``document.title`` updater in app.js reads chatMap for
  // the active chat's title — without this version counter, a chat-info
  // edit that updates chatMap directly would leave the tab title stale.
  chatMapVersion: 0,
  contacts: [],
  users: [],
  scenarios: [],
  libraries: [],
  // Context-preset summaries for the list pane + the settings dropdown.
  // The detail editor fetches the full preset via ``api.getContextPreset``
  // on row click. Only visible in Generic mode (see ``app.js``).
  contextPresets: [],
  presets: [],
  settings: null,

  // Active chat data
  chatMessages: [],            // ChatMessage[] from server (full tree)
  activePathIds: [],           // [msg.id, ...] derived from server's active-path
  // Full Contact / User / Scenario for the active chat. ``state.contacts``
  // / ``state.users`` / ``state.scenarios`` hold *Summary* projections
  // for the list pane (TTS, persona, background blur/dim/brighten/tint,
  // brains, etc. stripped). Anything in the chat view that depends on
  // those fields — auto-TTS gate, speaker-button resolve, greeting
  // detection, scenario background — reads these full entities instead.
  // Refreshed by ``loadActiveChat``.
  activeChatContact: null,
  activeChatUser: null,
  activeChatScenario: null,
  // Server-computed flag: does anything feeding this chat's prompt
  // header reference ``{{pick``? Drives the reroll-pick dice button's
  // visibility. Computed server-side because the detection needs full
  // contact / user / scenario / library entities, and pulling all four
  // to the client just to scan strings would be wasteful.
  chatUsesPickMacro: false,

  // Generation state
  generating: false,
  contextTokens: null,         // tokens of last built context
  contextActiveBrains: [],     // per-brain breakdown for the "brains: N (i)" stat + modal
  contextActiveBrainsFromLastGen: false,  // true = provenance of last gen, false = preview for next

  // Sidebar collapse — hides the list pane to give the chat-pane (and its
  // scenario background) the full content area. Persisted to localStorage
  // by app.js. Clicking a rail tab always restores the sidebar.
  sidebarCollapsed: false,

  // List sort: ``mode`` ∈ added|used|edited|name; ``direction`` ∈ desc|asc.
  // Persisted as JSON to localStorage in the views — these defaults only
  // apply on a fresh load (no stored prefs). Favourites float to the top
  // by default; the user can disable via the toggle next to the sort icon.
  contactSort:        { mode: 'added', direction: 'desc', favoritesFirst: true },
  userSort:           { mode: 'added', direction: 'desc', favoritesFirst: true },
  scenarioSort:       { mode: 'added', direction: 'desc', favoritesFirst: true },
  librarySort:        { mode: 'added', direction: 'desc', favoritesFirst: true },
  contextPresetSort:  { mode: 'added', direction: 'desc', favoritesFirst: true },
  // Chats default to "last activity, newest first" since that's the most
  // useful default for picking up an in-progress conversation. Favourites
  // float to the top so pinned chats stay visible as activity stacks up.
  chatSort:     { mode: 'edited', direction: 'desc', favoritesFirst: true },
};

const subs = new Set();
export function subscribe(fn) { subs.add(fn); return () => subs.delete(fn); }

export function setState(patch) {
  Object.assign(state, patch);
  for (const fn of subs) fn(state);
}


/** Mutate ``state.chatMap`` and notify subscribers atomically.
 *
 * The chatMap is a Map (not a plain object), so ``setState`` on its own
 * doesn't help — Object.assign on the Map reference is a no-op.
 * Callers that mutated it directly used to follow up with an unrelated
 * ``setState({ chats: ... })`` to incidentally fire the pub-sub; this
 * helper makes the fire explicit so a future path that doesn't refresh
 * the list pane still triggers subscribers (e.g. document.title). */
export function setChatInMap(chatId, chat) {
  state.chatMap.set(chatId, chat);
  setState({ chatMapVersion: (state.chatMapVersion || 0) + 1 });
}
