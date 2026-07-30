/* Generic helpers — DOM building, escaping, time formatting. */

export function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v == null || v === false) continue;
    if (k === 'class') node.className = v;
    else if (k === 'dataset') Object.assign(node.dataset, v);
    else if (k === 'style') Object.assign(node.style, v);
    else if (k.startsWith('on') && typeof v === 'function') node.addEventListener(k.slice(2).toLowerCase(), v);
    else if (k in node) {
      try { node[k] = v; }
      catch { node.setAttribute(k, v); }
    }
    else node.setAttribute(k, v);
  }
  for (const c of children.flat()) {
    if (c == null || c === false) continue;
    node.append(c.nodeType ? c : document.createTextNode(String(c)));
  }
  return node;
}

/* Set a textarea's height to exactly fit its content. border-box (global) makes
 * scrollHeight omit the border, so re-add it via offsetHeight - clientHeight,
 * else the box sits a couple px short and clips the last line. No-op while
 * detached (scrollHeight reads 0). */
export function fitTextareaHeight(ta) {
  if (!ta.isConnected) return;
  ta.style.height = 'auto';
  ta.style.height = ta.scrollHeight + (ta.offsetHeight - ta.clientHeight) + 'px';
}

/* Auto-grow a textarea to fit its content — but ONLY inside a modal, where the
 * textarea should be the single scroll surface (growing it is what avoids the
 * double-scroll — modal scrollbar + textarea scrollbar — in the Edit Message
 * modal). On a ``.page-scroll`` page it deliberately does nothing: the textarea
 * keeps its CSS / ``rows`` fixed height (resizable, with internal scroll for
 * overflow). Auto-growing page fields balloons long content — e.g. a Context
 * Preset block (min-height 120px) holding a big prompt — into a page that's
 * cumbersome to scroll.
 *
 * The enclosing ``.modal`` is the discriminator, re-checked on each fit (the
 * element must be mounted to detect it — hence the queueMicrotask, which runs
 * after the synchronous render appends it, before paint). So a shared editor
 * (e.g. a brain row) grows when opened in a modal yet stays fixed on its page.
 * Returns ``fit`` for callers that mutate ``.value`` programmatically (no input
 * event fires then).
 *
 * Not for the chat input bar or read-only previews: those manage their own
 * height — don't run this on them. */
export function autoGrowTextarea(ta) {
  const fit = () => {
    if (!ta.isConnected) return;
    if (ta.closest('.modal')) {
      ta.style.resize = 'none';
      ta.style.overflowY = 'hidden';
      fitTextareaHeight(ta);
    } else {
      // Page: clear any auto-grow inline styles so CSS / rows sizing governs.
      ta.style.height = '';
      ta.style.resize = '';
      ta.style.overflowY = '';
    }
  };
  ta.addEventListener('input', fit);
  queueMicrotask(fit);
  return fit;
}

export function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g, ch => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[ch]));
}

export function formatRelative(epochSeconds) {
  if (!epochSeconds) return '';
  const now = Date.now() / 1000;
  const dt = now - epochSeconds;
  if (dt < 60) return 'now';
  if (dt < 3600) return `${Math.round(dt / 60)}m`;
  if (dt < 86400) return `${Math.round(dt / 3600)}h`;
  if (dt < 86400 * 7) return `${Math.round(dt / 86400)}d`;
  const d = new Date(epochSeconds * 1000);
  return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}

export function shortId(id) {
  return (id || '').slice(0, 8);
}

export function clamp(n, lo, hi) { return Math.max(lo, Math.min(hi, n)); }

/* The region a body-portalled ``position: fixed`` popover has to stay inside,
 * expressed in the coordinate space ``getBoundingClientRect`` reports (the
 * layout viewport) so anchor rects and this box are directly comparable.
 *
 * On a phone the two viewports diverge: the on-screen keyboard shrinks the
 * visual viewport, and on iOS it also offsets it, while the layout viewport
 * that ``window.inner*`` describes stays put. Clamping to ``window.inner*``
 * there lets a popover settle behind the keyboard. Desktop: the offsets are
 * 0 and the sizes match ``window.inner*``, so this is a no-op.
 *
 * Popovers that reposition themselves should listen on ``visualViewport``
 * (resize + scroll) as well as ``window`` resize — the keyboard fires only
 * the former on iOS. */
export function visibleViewport() {
  const vv = window.visualViewport;
  if (!vv) {
    return { left: 0, top: 0, width: window.innerWidth, height: window.innerHeight };
  }
  return { left: vv.offsetLeft, top: vv.offsetTop, width: vv.width, height: vv.height };
}

export function debounce(fn, ms) {
  let t;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}

export function uuid8() {
  return Array.from({ length: 8 }, () => Math.floor(Math.random() * 16).toString(16)).join('');
}

/* Emit a brain-library-shape JSON payload from a list of brains. Used
 * by the "Export brains" button on every brain editor surface (contact,
 * user, scenario, library, per-message); the receiving "Import brains"
 * button reads the ``brains`` array and ignores the rest of the
 * brain-library wrapper. */
export function brainsToExportJson(name, brains) {
  return {
    kind: 'brain_library',
    name: name || 'Brains',
    description: '',
    tags: '',
    favorite: false,
    avatarUri: '',
    brains: (brains || []).map(b => {
      const out = { id: b.id, name: b.name, content: b.content };
      if (b.keys && b.keys.length) out.keys = b.keys;
      if (b.cascades) out.cascades = true;
      if (b.blocks_recursion) out.blocks_recursion = true;
      if (b.disabled) out.disabled = true;
      if (b.advanced) out.advanced = b.advanced;
      return out;
    }),
  };
}


export function downloadJson(filename, data) {
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export function downloadFromResponse(response, fallbackName) {
  const cd = response.headers.get('content-disposition') || '';
  const m = cd.match(/filename="?([^";]+)"?/);
  const name = m ? m[1] : fallbackName;
  return response.blob().then(blob => {
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = name;
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  });
}

export function slugify(name) {
  return String(name || '')
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '') || 'untitled';
}


/* ============== Cropped images ============== */

/* Mark an element as a cropped image wrapper. The element should already
 * have overflow:hidden + a fixed (or aspect-ratio'd) size. The image
 * inside it gets sized and shifted so that only the requested rect is
 * visible. Pass ``null`` to clear the crop. */
export function applyCrop(wrapperEl, crop) {
  if (!wrapperEl) return;
  if (!crop) {
    wrapperEl.classList.remove('cropped');
    wrapperEl.style.removeProperty('--crop-x');
    wrapperEl.style.removeProperty('--crop-y');
    wrapperEl.style.removeProperty('--crop-w');
    wrapperEl.style.removeProperty('--crop-h');
    return;
  }
  wrapperEl.classList.add('cropped');
  wrapperEl.style.setProperty('--crop-x', crop.x ?? 0);
  wrapperEl.style.setProperty('--crop-y', crop.y ?? 0);
  wrapperEl.style.setProperty('--crop-w', crop.w ?? 1);
  wrapperEl.style.setProperty('--crop-h', crop.h ?? 1);
}


/* Build an avatar wrapper for a scenario. The ``/display`` derivative is
 * the same source image as the chat-pane background, cropped square via
 * the scenario's ``avatar_crop``. Falls back to the first letter of the
 * scenario name when no background image is set. */
export function scenarioAvatarEl(scenario, opts = {}) {
  const extra = opts.class ? ` ${opts.class}` : '';
  const div = el('div', { class: `avatar${extra}` });
  if (scenario && scenario.background_image) {
    const cb = scenario.updated_at ? `?v=${Math.floor(scenario.updated_at)}` : '';
    div.append(el('img', {
      src: `/api/files/scenarios/${scenario.id}/background/display${cb}`,
      alt: scenario.name || '',
    }));
  } else {
    div.textContent = (scenario?.name || '?').slice(0, 1).toUpperCase();
  }
  return div;
}


/* Build an avatar wrapper for a contact. By default loads the pre-cropped
 * ``/display`` derivative — small WebP, crop already baked in, so no CSS
 * crop is applied. Resolution order: contact's avatar → neutral emotion
 * sprite → first letter of the name. */
export function avatarEl(contact, opts = {}) {
  const extra = opts.class ? ` ${opts.class}` : '';
  const div = el('div', { class: `avatar${extra}` });
  const cb = opts.cacheBust ? `?v=${opts.cacheBust}` : '';
  if (contact && contact.avatar) {
    div.append(el('img', {
      src: `/api/files/contacts/${contact.id}/avatar/display${cb}`,
      alt: contact.name || '',
    }));
  } else if (contact && contact.emotions && contact.emotions.neutral) {
    div.append(el('img', {
      src: `/api/files/contacts/${contact.id}/emotions/neutral/display${cb}`,
      alt: contact.name || '',
    }));
  } else {
    div.textContent = (contact?.name || '?').slice(0, 1).toUpperCase();
  }
  return div;
}


/* Sort an array of entities by one of the supported modes:
 *   - ``added``:  by ``created_at``
 *   - ``edited``: by ``updated_at``
 *   - ``used``:   by ``lastUsedById`` lookup, falling back to ``created_at``
 *   - ``name``:   alphabetical
 *
 * ``direction`` is ``'desc'`` (default) for larger-first / Z→A names, or
 * ``'asc'`` for smaller-first / A→Z names. ``lastUsedById`` is an optional
 * ``{ [entityId]: timestamp }`` map; entities with no chat usage fall back
 * to ``created_at``. Returns a new array; the input is never mutated. */
export function sortEntities(items, mode, lastUsedById, direction, opts) {
  const arr = [...(items || [])];
  const sign = direction === 'asc' ? -1 : 1;
  // localeCompare: a<b → negative. For "Z→A first" (desc), we want b first
  // when a<b → positive → so negate. With sign=1 (desc), -sign*neg = +pos.
  // With sign=-1 (asc), -sign*neg = -neg = neg → a first (A→Z). ✓
  // Chats use ``title`` instead of ``name``; fall back so this works for both.
  const labelOf = (e) => e.name || e.title || '';
  const cmpNameRaw = (a, b) => labelOf(a).localeCompare(labelOf(b),
    undefined, { sensitivity: 'base' });
  const cmpAdded  = (a, b) => (b.created_at || b.updated_at || 0) - (a.created_at || a.updated_at || 0);
  const cmpEdited = (a, b) => (b.updated_at || 0) - (a.updated_at || 0);
  const usedKey   = (e) => (lastUsedById && lastUsedById[e.id]) || e.created_at || e.updated_at || 0;
  const cmpUsed   = (a, b) => usedKey(b) - usedKey(a);
  let cmp;
  switch (mode) {
    case 'edited': cmp = (a, b) => sign * cmpEdited(a, b); break;
    case 'used':   cmp = (a, b) => sign * cmpUsed(a, b); break;
    case 'name':   cmp = (a, b) => -sign * cmpNameRaw(a, b); break;
    case 'added':
    default:       cmp = (a, b) => sign * cmpAdded(a, b); break;
  }
  // Favourites-first: stable two-pass sort, partition then concat. The
  // mode/direction sort applies *within* each partition so the user's
  // chosen ordering is preserved among favourites and among the rest.
  if (opts && opts.favoritesFirst) {
    const favs = arr.filter(x => x && x.favorite).sort(cmp);
    const rest = arr.filter(x => !(x && x.favorite)).sort(cmp);
    return [...favs, ...rest];
  }
  arr.sort(cmp);
  return arr;
}


/* Per-mode default direction. Time-based modes default to descending
 * (newest first); name defaults to ascending (A→Z). */
export function defaultSortDirection(mode) {
  return mode === 'name' ? 'asc' : 'desc';
}


/* Return the full ``Chat`` for ``id`` from the in-memory ``chatMap``,
 * falling back to the (Summary-shaped) ``state.chats`` row if the full
 * chat hasn't been fetched yet. Callers that need fields off the
 * chat-tree state (``selected_child_id``, ``last_deleted_child``, the
 * rollover cache, ``pick_reroll_nonce``) must use the chatMap result;
 * the Summary fallback only carries identity + display fields.
 *
 * Consumers expecting the full Chat AND who can wait for it (e.g. a
 * modal opening on click) should ``await api.getChat(id)`` instead —
 * this helper is for sync render-time lookups that tolerate a brief
 * Summary view until ``loadActiveChat`` lands. */
export function getChatById(state, id) {
  if (!id) return null;
  if (state.chatMap && state.chatMap.has(id)) return state.chatMap.get(id);
  return (state.chats || []).find(c => c.id === id) || null;
}


/* Build ``{ [entityId]: max(chat.updated_at) }`` for chats that reference
 * the entity through ``key`` (``contact_id`` / ``user_id`` / ``scenario_id``).
 *
 * ``state.chats`` can be sparse — the paged adapter fills indices as the
 * user scrolls; not-yet-loaded slots are ``undefined``. Skip those rather
 * than dereferencing them. List views that read ``last_used_at`` directly
 * off the Summary don't need this helper; it's the fallback for the rest. */
export function lastUsedByEntity(chats, key) {
  const map = Object.create(null);
  for (const c of chats || []) {
    if (!c) continue;
    const id = c[key];
    if (!id) continue;
    const t = c.updated_at || 0;
    if (!(id in map) || map[id] < t) map[id] = t;
  }
  return map;
}


const DEFAULT_SORT_OPTIONS = [
  ['added', 'Added'],
  ['used', 'Used'],
  ['edited', 'Edited'],
  ['name', 'Name'],
];

/* Sort dropdown + direction toggle. ``current`` is ``{ mode, direction }``;
 * ``onChange`` is called with the new pair when either control changes.
 * Picking a new mode resets ``direction`` to that mode's natural default
 * so the toggle stays meaningful. The toggle re-paints its own arrow
 * in-place — the surrounding list only needs to re-render its body.
 * ``options`` is an optional ``[[value, label], ...]`` to override the
 * default mode set (e.g. chats, which collapse used/edited into one). */
export function sortControls(current, onChange, options, opts) {
  options = options || DEFAULT_SORT_OPTIONS;
  // ``opts.hideFavorites`` lets list panes that don't carry a favourite
  // flag (chats today) opt out of the floats-to-top toggle.
  const hideFavorites = !!(opts && opts.hideFavorites);
  let mode = current.mode || options[0][0];
  let direction = current.direction || defaultSortDirection(mode);
  let favoritesFirst = !!current.favoritesFirst;

  function labelFor(m) {
    const found = options.find(([v]) => v === m);
    return found ? found[1] : m;
  }

  // Sort-mode trigger — an icon-only button matching the direction +
  // favourites toggles in width. Tooltip carries the current mode; click
  // opens the popover menu where the active mode is highlighted.
  const modeBtn = el('button', {
    class: 'list-sort-mode',
    type: 'button',
    onClick: (e) => { e.stopPropagation(); toggleMenu(); },
  });
  const menu = el('div', { class: 'list-sort-menu hidden' });

  function paintMode() {
    modeBtn.title = `Sort by ${labelFor(mode).toLowerCase()}`;
    // Three descending-width bars — a generic "sort order" affordance.
    modeBtn.innerHTML = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 6h16M4 12h10M4 18h6"/></svg>`;
  }

  function rebuildMenu() {
    menu.replaceChildren();
    for (const [val, lbl] of options) {
      menu.append(el('button', {
        class: 'list-sort-menu-item' + (val === mode ? ' active' : ''),
        type: 'button',
        onClick: (e) => {
          e.stopPropagation();
          mode = val;
          direction = defaultSortDirection(mode);
          paintMode();
          paintToggle();
          closeMenu();
          onChange({ mode, direction, favoritesFirst });
        },
      }, lbl));
    }
  }

  function openMenu() {
    rebuildMenu();
    menu.classList.remove('hidden');
    setTimeout(() => document.addEventListener('mousedown', dismiss), 0);
  }
  function closeMenu() {
    menu.classList.add('hidden');
    document.removeEventListener('mousedown', dismiss);
  }
  function toggleMenu() {
    if (menu.classList.contains('hidden')) openMenu(); else closeMenu();
  }
  function dismiss(e) {
    if (!menu.contains(e.target) && !modeBtn.contains(e.target)) closeMenu();
  }

  const toggle = el('button', {
    class: 'list-sort-dir',
    type: 'button',
    onClick: () => {
      direction = direction === 'desc' ? 'asc' : 'desc';
      paintToggle();
      onChange({ mode, direction, favoritesFirst });
    },
  });

  // Filled-vs-outline star — toggling promotes favourited entities to the
  // top of the list while preserving the chosen mode/direction *within*
  // each partition (favourites, then the rest).
  const favBtn = el('button', {
    class: 'list-sort-fav',
    type: 'button',
    onClick: () => {
      favoritesFirst = !favoritesFirst;
      paintFav();
      onChange({ mode, direction, favoritesFirst });
    },
  });

  function paintToggle() {
    // ↓ = desc (newest first / Z→A); ↑ = asc.
    const arrowPath = direction === 'desc'
      ? 'M12 4v16m0 0l-6-6m6 6l6-6'
      : 'M12 20V4m0 0l-6 6m6-6l6 6';
    toggle.title = direction === 'desc'
      ? (mode === 'name' ? 'Z → A (click for A → Z)' : 'Newest first (click for oldest)')
      : (mode === 'name' ? 'A → Z (click for Z → A)' : 'Oldest first (click for newest)');
    toggle.innerHTML = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="${arrowPath}"/></svg>`;
  }
  function paintFav() {
    favBtn.title = favoritesFirst
      ? 'Favourites floated to top (click to disable)'
      : 'Float favourites to top';
    favBtn.classList.toggle('active', favoritesFirst);
    const fill = favoritesFirst ? 'currentColor' : 'none';
    favBtn.innerHTML = `<svg width="14" height="14" viewBox="0 0 24 24" fill="${fill}" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"/></svg>`;
  }
  paintMode();
  paintToggle();
  paintFav();

  const modeWrap = el('div', { class: 'list-sort-mode-wrap' }, modeBtn, menu);
  const group = el('div', { class: 'list-sort-group' }, modeWrap, toggle);
  if (!hideFavorites) group.append(favBtn);
  return group;
}



/* Backwards-compat: simple sort dropdown that returns just the mode. */
export function sortSelect(current, onChange) {
  return sortControls({ mode: current }, ({ mode }) => onChange(mode));
}


/* Count badge with a stable element + an in-place ``refresh()`` hook. Use
 * when the surrounding section won't re-render on every mutation but the
 * count next to the heading still needs to track the underlying array /
 * object size live (emotions, example chats, brains, …). */
export function liveBadge(getText) {
  const span = el('span', { class: 'badge' });
  span.refresh = () => { span.textContent = getText(); };
  span.refresh();
  return span;
}


/* Inline star button used next to entity names in list rows. ``onToggle``
 * receives the new (flipped) value; the caller fires the actual API write
 * and state refresh. The button stops propagation so clicking the star
 * doesn't also trigger the surrounding row's onClick. */
export function favoriteStar(entity, onToggle) {
  const isFav = !!(entity && entity.favorite);
  const fill = isFav ? 'currentColor' : 'none';
  const btn = el('button', {
    class: 'favorite-star' + (isFav ? ' active' : ''),
    type: 'button',
    title: isFav ? 'Unfavorite' : 'Favorite',
    onClick: (e) => { e.stopPropagation(); onToggle(!isFav); },
  });
  btn.innerHTML = `<svg width="14" height="14" viewBox="0 0 24 24" fill="${fill}" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"/></svg>`;
  return btn;
}


/* Text input with a ``<datalist>`` of suggestions — the user can pick a
 * preset value or type whatever they want. Returns a wrapper element so
 * the input + datalist live together in the DOM. ``listId`` should be
 * unique per occurrence (typically suffixed with the entity id). */
export function suggestionInput(value, listId, options, onChange, opts = {}) {
  const input = el('input', {
    type: 'text',
    value: value || '',
    placeholder: opts.placeholder || '',
    list: listId,
    oninput: (e) => onChange(e.target.value),
  });
  const datalist = el('datalist', { id: listId });
  for (const opt of options) {
    datalist.append(el('option', { value: opt }));
  }
  return el('span', { style: { display: 'contents' } }, input, datalist);
}


/* First-letter monogram avatar — used as a fallback when an entity has
 * no image (e.g. a user persona that hasn't uploaded an avatar). The
 * shape matches ``avatarEl`` / ``userAvatarEl`` outputs so it slots into
 * the same layouts. */
export function placeholderAvatar(name) {
  const div = el('div', { class: 'avatar' });
  div.textContent = (name || '?').slice(0, 1).toUpperCase();
  return div;
}


/* User-persona avatar wrapper. Returns ``null`` when the user has no
 * avatar set so callers can fall back to the bubble-only layout instead
 * of rendering a first-letter monogram. Uses the pre-cropped
 * ``/display`` derivative — no CSS crop needed, crop is baked in. */
export function userAvatarEl(user, opts = {}) {
  if (!user || !user.avatar) return null;
  const extra = opts.class ? ` ${opts.class}` : '';
  const div = el('div', { class: `avatar${extra}` });
  const cb = opts.cacheBust ? `?v=${opts.cacheBust}` : '';
  div.append(el('img', {
    src: `/api/files/users/${user.id}/avatar/display${cb}`,
    alt: user.name || '',
  }));
  return div;
}


/* Brain-library avatar wrapper. Returns a square wrapper with the library's
 * ``/display`` derivative when an avatar is set, else a first-letter
 * monogram. Mirrors :func:`avatarEl` / :func:`userAvatarEl`. */
export function libraryAvatarEl(library, opts = {}) {
  const extra = opts.class ? ` ${opts.class}` : '';
  const div = el('div', { class: `avatar${extra}` });
  const cb = opts.cacheBust ? `?v=${opts.cacheBust}` : '';
  if (library && library.avatar) {
    div.append(el('img', {
      src: `/api/files/libraries/${library.id}/avatar/display${cb}`,
      alt: library.name || '',
    }));
  } else {
    div.textContent = (library?.name || '?').slice(0, 1).toUpperCase();
  }
  return div;
}


/* Dirty-state dot for edit headers. Lives next to the page title and
 * lights up while there are unsaved changes (or a save in flight).
 * The element reserves its own space at all times so flipping state
 * doesn't shift the surrounding text. */
export function dirtyDot() {
  return el('span', { class: 'dirty-dot', title: 'Saved' });
}


/* Wrap an async save function with debounce + dirty-dot wiring + retry.
 *
 * The returned ``trigger()`` is called synchronously on every input
 * change: it lights the dot, schedules the save after ``ms`` of quiet,
 * and clears the dot once the save resolves. On transient failure
 * (network error, 5xx) the dot flips to ``error`` and a retry is
 * scheduled with exponential backoff (2 s → 4 s → 8 s, capped at 60 s);
 * the dot's tooltip surfaces the live "Retrying in Ns…" countdown so
 * the user can tell the queue is alive.
 *
 * Non-retryable failures (4xx other than 408 / 429) park the dot in the
 * ``error`` state and stop retrying — typically these are conflicts
 * (which the asyncSaveFn is expected to handle internally via a
 * confirmation modal) or validation errors the user has to fix in the
 * UI. A subsequent edit (next ``trigger()``) restarts the cycle. */
const RETRY_BACKOFFS = [2000, 4000, 8000, 16000, 30000, 60000];

function _isRetryable(err) {
  if (!err) return false;
  // Network error / unreachable server — the api.js HttpError wrapper
  // gives these status === 0.
  if (err.status === 0) return true;
  // 5xx: server error worth retrying.
  if (err.status >= 500 && err.status < 600) return true;
  // 408 Request Timeout, 429 Too Many Requests: explicitly retryable.
  if (err.status === 408 || err.status === 429) return true;
  return false;
}

export function makeAutoSaver(asyncSaveFn, dot, ms = 400) {
  let debounceTimer;
  let retryTimer;
  let countdownTimer;
  let attempt = 0;
  let inFlight = 0;
  let lastError = null;

  const setDot = (cls, title) => {
    if (!dot) return;
    dot.classList.remove('dirty', 'saving', 'error');
    if (cls) dot.classList.add(cls);
    dot.title = title;
  };

  function _clearTimers() {
    clearTimeout(debounceTimer);
    clearTimeout(retryTimer);
    clearInterval(countdownTimer);
    debounceTimer = retryTimer = countdownTimer = undefined;
  }

  async function _attempt() {
    inFlight++;
    setDot('saving', attempt > 0 ? `Retrying… (attempt ${attempt + 1})` : 'Saving…');
    try {
      await asyncSaveFn();
      attempt = 0;
      lastError = null;
      if (--inFlight === 0) setDot(null, 'Saved');
    } catch (e) {
      inFlight--;
      lastError = e;
      if (_isRetryable(e)) {
        const delay = RETRY_BACKOFFS[Math.min(attempt, RETRY_BACKOFFS.length - 1)];
        attempt++;
        const until = Date.now() + delay;
        const tick = () => {
          const remaining = Math.max(0, Math.round((until - Date.now()) / 1000));
          setDot('error', `Save failed (${e.message}). Retrying in ${remaining}s…`);
        };
        tick();
        countdownTimer = setInterval(tick, 1000);
        retryTimer = setTimeout(() => {
          clearInterval(countdownTimer);
          countdownTimer = undefined;
          _attempt();
        }, delay);
      } else {
        // Permanent failure — stop the retry loop. The next user edit
        // restarts via trigger(). asyncSaveFn is responsible for
        // surfacing 409 conflicts to the user (overwrite prompt) before
        // the rejection bubbles here.
        attempt = 0;
        setDot('error', `Save failed: ${e && e.message || e}`);
      }
    }
  }

  let queued = false;

  async function _drain() {
    await _attempt();
    if (queued) {
      queued = false;
      _clearTimers();
      // Use a 0-ms timer rather than recursing so the call stack stays
      // shallow if the user is hammering the keyboard.
      debounceTimer = setTimeout(() => { attempt = 0; _drain(); }, 0);
    }
  }

  return function trigger() {
    // A fresh edit invalidates any pending retry — the new payload
    // supersedes the old one. If a save is already running (e.g. its
    // conflict modal is open awaiting the user), don't fire another in
    // parallel — coalesce into a single follow-up after the current one
    // resolves. The shared ``draft`` reference means whatever the user
    // typed is included in that follow-up automatically.
    setDot('dirty', 'Unsaved changes');
    if (inFlight > 0) { queued = true; return; }
    _clearTimers();
    debounceTimer = setTimeout(() => { attempt = 0; _drain(); }, ms);
  };
}
