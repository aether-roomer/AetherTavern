/* Thin wrappers over the FastAPI JSON endpoints + SSE. */


function _isMobile() {
  return (window.matchMedia && window.matchMedia('(pointer: coarse)').matches) ? 'true' : 'false';
}


/* Structured error thrown by ``request`` on non-2xx responses. Carries
 * the HTTP status code and the parsed JSON body (when available) so
 * callers can branch on specific cases — most importantly 409 conflicts,
 * which the autosaver and the conflict modal need to read the
 * ``current_version_id`` and ``current_updated_at`` fields out of. */
export class HttpError extends Error {
  constructor(status, message, body) {
    super(message);
    this.name = 'HttpError';
    this.status = status;
    this.body = body;
  }
}

async function request(method, url, body, fetchOpts) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers['content-type'] = 'application/json';
    opts.body = JSON.stringify(body);
  }
  // Optional ``{ signal }`` for AbortController support (paged list source).
  if (fetchOpts && fetchOpts.signal) opts.signal = fetchOpts.signal;
  let r;
  try {
    r = await fetch(url, opts);
  } catch (e) {
    // Network failure (server down, lost wifi, …). Surface as a
    // status-less HttpError so the autosaver's retry path treats it the
    // same as a 5xx — both are transient and worth retrying.
    throw new HttpError(0, e && e.message || 'Network error', null);
  }
  if (!r.ok) {
    let parsed = null;
    let detail = r.statusText;
    try {
      parsed = await r.json();
      // FastAPI's HTTPException(detail=str) → ``{detail: "..."}``;
      // detail=dict → ``{detail: {...}}``. Surface a string for the
      // Error message either way.
      if (parsed && typeof parsed.detail === 'string') detail = parsed.detail;
    } catch {}
    throw new HttpError(r.status, `${r.status} ${detail}`, parsed);
  }
  if (r.status === 204) return null;
  const ct = r.headers.get('content-type') || '';
  if (ct.includes('application/json')) return r.json();
  return r.text();
}

export const api = {
  // settings
  getSettings: () => request('GET', '/api/settings'),
  putSettings: (s) => request('PUT', '/api/settings', s),
  uploadNotificationSound: (file) => uploadFile('/api/settings/notification-sound', file),
  deleteNotificationSound: () => request('DELETE', '/api/settings/notification-sound'),
  uploadChatAttachment: (chatId, file) =>
    uploadFile(`/api/chats/${chatId}/attachments`, file),
  deleteChatAttachment: (chatId, attId) =>
    request('DELETE', `/api/chats/${chatId}/attachments/${attId}`),
  deleteMessageAttachment: (chatId, messageId, attId) =>
    request('DELETE', `/api/chats/${chatId}/messages/${messageId}/attachments/${attId}`),
  // presets
  listPresets: () => request('GET', '/api/presets'),
  createPreset: (p) => request('POST', '/api/presets', p),
  updatePreset: (id, p) => request('PUT', `/api/presets/${id}`, p),
  deletePreset: (id) => request('DELETE', `/api/presets/${id}`),
  // contacts
  listContacts: () => request('GET', '/api/contacts'),
  getContact: (id) => request('GET', `/api/contacts/${id}`),
  createContact: (c) => request('POST', '/api/contacts', c),
  updateContact: (id, c) => request('PUT', `/api/contacts/${id}`, c),
  deleteContact: (id) => request('DELETE', `/api/contacts/${id}`),
  duplicateContact: (id) => request('POST', `/api/contacts/${id}/duplicate`),
  uploadAvatar: (id, file) => uploadFile(`/api/contacts/${id}/avatar`, file),
  deleteAvatar: (id) => request('DELETE', `/api/contacts/${id}/avatar`),
  uploadEmotion: (id, emotion, file) => uploadFile(`/api/contacts/${id}/emotions/${emotion}`, file),
  deleteEmotion: (id, emotion) => request('DELETE', `/api/contacts/${id}/emotions/${emotion}`),
  uploadContactCard: (id, file) => uploadFile(`/api/contacts/${id}/card-image`, file),
  deleteContactCard: (id) => request('DELETE', `/api/contacts/${id}/card-image`),
  setContactFavorite: (id, favorite) =>
    request('PATCH', `/api/contacts/${id}/favorite`, { favorite }),
  // users
  listUsers: () => request('GET', '/api/users'),
  getUser: (id) => request('GET', `/api/users/${id}`),
  createUser: (u) => request('POST', '/api/users', u),
  updateUser: (id, u) => request('PUT', `/api/users/${id}`, u),
  deleteUser: (id) => request('DELETE', `/api/users/${id}`),
  duplicateUser: (id) => request('POST', `/api/users/${id}/duplicate`),
  uploadUserAvatar: (id, file) => uploadFile(`/api/users/${id}/avatar`, file),
  deleteUserAvatar: (id) => request('DELETE', `/api/users/${id}/avatar`),
  uploadUserCard: (id, file) => uploadFile(`/api/users/${id}/card-image`, file),
  deleteUserCard: (id) => request('DELETE', `/api/users/${id}/card-image`),
  setUserFavorite: (id, favorite) =>
    request('PATCH', `/api/users/${id}/favorite`, { favorite }),
  // scenarios
  listScenarios: () => request('GET', '/api/scenarios'),
  getScenario: (id) => request('GET', `/api/scenarios/${id}`),
  createScenario: (s) => request('POST', '/api/scenarios', s),
  updateScenario: (id, s) => request('PUT', `/api/scenarios/${id}`, s),
  deleteScenario: (id) => request('DELETE', `/api/scenarios/${id}`),
  duplicateScenario: (id) => request('POST', `/api/scenarios/${id}/duplicate`),
  uploadScenarioBackground: (id, file) => uploadFile(`/api/scenarios/${id}/background`, file),
  deleteScenarioBackground: (id) => request('DELETE', `/api/scenarios/${id}/background`),
  uploadScenarioCard: (id, file) => uploadFile(`/api/scenarios/${id}/card-image`, file),
  deleteScenarioCard: (id) => request('DELETE', `/api/scenarios/${id}/card-image`),
  setScenarioFavorite: (id, favorite) =>
    request('PATCH', `/api/scenarios/${id}/favorite`, { favorite }),
  // brain libraries
  listBrainLibraries: () => request('GET', '/api/libraries'),
  getBrainLibrary: (id) => request('GET', `/api/libraries/${id}`),
  createBrainLibrary: (l) => request('POST', '/api/libraries', l),
  updateBrainLibrary: (id, l) => request('PUT', `/api/libraries/${id}`, l),
  deleteBrainLibrary: (id) => request('DELETE', `/api/libraries/${id}`),
  duplicateBrainLibrary: (id) => request('POST', `/api/libraries/${id}/duplicate`),
  uploadLibraryAvatar: (id, file) => uploadFile(`/api/libraries/${id}/avatar`, file),
  deleteLibraryAvatar: (id) => request('DELETE', `/api/libraries/${id}/avatar`),
  uploadLibraryCard: (id, file) => uploadFile(`/api/libraries/${id}/card-image`, file),
  deleteLibraryCard: (id) => request('DELETE', `/api/libraries/${id}/card-image`),
  setLibraryFavorite: (id, favorite) =>
    request('PATCH', `/api/libraries/${id}/favorite`, { favorite }),
  // context presets — Generic-mode prompt templates
  listContextPresets: () => request('GET', '/api/context-presets'),
  getContextPreset: (id) => request('GET', `/api/context-presets/${id}`),
  createContextPreset: (p) => request('POST', '/api/context-presets', p),
  updateContextPreset: (id, p) => request('PUT', `/api/context-presets/${id}`, p),
  deleteContextPreset: (id) => request('DELETE', `/api/context-presets/${id}`),
  duplicateContextPreset: (id) =>
    request('POST', `/api/context-presets/${id}/duplicate`),
  previewContextPreset: (body) =>
    request('POST', '/api/context-presets/preview', body),
  setContextPresetFavorite: (id, favorite) =>
    request('PATCH', `/api/context-presets/${id}/favorite`, { favorite }),
  uploadContextPresetAvatar: (id, file) =>
    uploadFile(`/api/context-presets/${id}/avatar`, file),
  deleteContextPresetAvatar: (id) =>
    request('DELETE', `/api/context-presets/${id}/avatar`),
  uploadContextPresetCard: (id, file) =>
    uploadFile(`/api/context-presets/${id}/card-image`, file),
  deleteContextPresetCard: (id) =>
    request('DELETE', `/api/context-presets/${id}/card-image`),
  // macros help (introspection + virtual control-flow tokens)
  getMacros: () => request('GET', '/api/macros'),
  // chats
  // No args → legacy flat list[ChatSummary]. With params → server returns
  // a ``ChatListPage`` envelope ({items, total, offset, limit}).
  // Filterable params: q, contact_id, user_id, scenario_id, ref_library_id,
  // sort (updated_at|created_at|title), direction (desc|asc),
  // favorites_first, offset, limit.
  listChats: (params) => {
    if (!params) return request('GET', '/api/chats');
    const qp = new URLSearchParams();
    for (const [k, v] of Object.entries(params)) {
      if (v === undefined || v === null || v === '') continue;
      qp.set(k, String(v));
    }
    return request('GET', `/api/chats?${qp.toString()}`);
  },
  // Paged variant for the chat-list virtualizer — ``qs`` is pre-built by
  // ``createPagedList``; ``opts`` carries an AbortSignal.
  listChatsPage: (qs, opts) => request('GET', `/api/chats?${qs}`, undefined, opts),
  getChat: (id) => request('GET', `/api/chats/${id}`),
  createChat: (req) => request('POST', '/api/chats', req),
  updateChat: (id, c) => request('PUT', `/api/chats/${id}`, c),
  deleteChat: (id) => request('DELETE', `/api/chats/${id}`),
  setChatFavorite: (id, favorite) =>
    request('PATCH', `/api/chats/${id}/favorite`, { favorite }),
  listMessages: (id) => request('GET', `/api/chats/${id}/messages`),
  createMessage: (chatId, msg) => request('POST', `/api/chats/${chatId}/messages`, msg),
  updateMessage: (chatId, msgId, patch) =>
    request('PUT', `/api/chats/${chatId}/messages/${msgId}`, patch),
  deleteMessage: (chatId, msgId) =>
    request('DELETE', `/api/chats/${chatId}/messages/${msgId}`),
  restoreMessage: (chatId, msgId) =>
    request('POST', `/api/chats/${chatId}/messages/${msgId}/restore`),
  selectChild: (chatId, parentId, childId) =>
    request('POST', `/api/chats/${chatId}/select`, { parent_id: parentId, child_id: childId }),
  activePath: (id) => request('GET', `/api/chats/${id}/active-path`),
  cancelGeneration: (id) => request('POST', `/api/chats/${id}/cancel-generation`),
  contextTokens: (id, opts = {}) => {
    const params = new URLSearchParams();
    params.set('is_mobile', _isMobile());
    if (opts.mode && opts.mode !== 'normal') params.set('mode', opts.mode);
    return request('GET', `/api/chats/${id}/context-tokens?${params}`);
  },
  chatContextPreview: (id, mode = 'normal') => {
    const params = new URLSearchParams();
    params.set('is_mobile', _isMobile());
    if (mode && mode !== 'normal') params.set('mode', mode);
    return request('GET', `/api/chats/${id}/context-preview?${params}`);
  },
  rerollPicks: (id) => request('POST', `/api/chats/${id}/reroll-picks`),
  chatUsesPickMacro: (id) => request('GET', `/api/chats/${id}/uses-pick-macro`),
  imagePromptPreview: (chatId, continuing = false, anchorMessageId = null) => {
    const params = new URLSearchParams();
    if (continuing) params.set('continue', 'true');
    if (anchorMessageId) params.set('anchor_message_id', anchorMessageId);
    const query = params.toString();
    return request(
      'GET',
      `/api/chats/${chatId}/image-prompt-preview${query ? `?${query}` : ''}`,
    );
  },
  generateImage: (chatId, prompt, anchorMessageId, aspect) =>
    request('POST', `/api/chats/${chatId}/generate-image`, {
      prompt,
      anchor_message_id: anchorMessageId ?? null,
      aspect,
    }),
  // bookmarks
  listBookmarks: (chatId) => request('GET', `/api/chats/${chatId}/bookmarks`),
  createBookmark: (chatId, b) => request('POST', `/api/chats/${chatId}/bookmarks`, b),
  updateBookmark: (chatId, id, b) => request('PUT', `/api/chats/${chatId}/bookmarks/${id}`, b),
  deleteBookmark: (chatId, id) => request('DELETE', `/api/chats/${chatId}/bookmarks/${id}`),
  jumpBookmark: (chatId, bookmarkId) =>
    request('POST', `/api/chats/${chatId}/bookmarks/jump?bookmark_id=${encodeURIComponent(bookmarkId)}`),
  restorePath: (chatId, selectedChildId) =>
    request('POST', `/api/chats/${chatId}/restore-path`, { selected_child_id: selectedChildId }),
  // import / export
  importFile: (file, opts = {}) => importFileStream(file, opts),
  zipImportUpload: (file, opts = {}) => zipImportUpload(file, opts),
  zipImportToc: (token, opts = {}) => zipImportTocStream(token, opts),
  zipImportSelected: (token, selection, opts = {}) =>
    zipImportSelectedStream(token, selection, opts),
  zipImportCancel: (token) => zipImportCancel(token),
  exportContact: (id) => fetch(`/api/export/contact/${id}`),
  exportUser: (id) => fetch(`/api/export/user/${id}`),
  exportScenario: (id) => fetch(`/api/export/scenario/${id}`),
  exportLibrary: (id) => fetch(`/api/export/library/${id}`),
  exportContextPreset: (id) => fetch(`/api/export/context-preset/${id}`),
  exportChat: (id) => fetch(`/api/export/chat/${id}`),
  exportContactCard: (id) => fetch(`/api/export/contact/${id}/card`),
  exportUserCard: (id) => fetch(`/api/export/user/${id}/card`),
  exportScenarioCard: (id) => fetch(`/api/export/scenario/${id}/card`),
  exportLibraryCard: (id) => fetch(`/api/export/library/${id}/card`),
  exportContextPresetCard: (id) =>
    fetch(`/api/export/context-preset/${id}/card`),
  importBrains: (file) => {
    const fd = new FormData();
    fd.append('file', file);
    return fetch('/api/import-brains', { method: 'POST', body: fd }).then(async r => {
      if (!r.ok) {
        let detail = r.statusText;
        try { detail = (await r.json()).detail || detail; } catch {}
        throw new Error(`${r.status} ${detail}`);
      }
      return r.json();
    });
  },
  importPreset: (file) => {
    const fd = new FormData();
    fd.append('file', file);
    return fetch('/api/import-preset', { method: 'POST', body: fd }).then(async r => {
      if (!r.ok) throw new Error(`${r.status} ${(await r.json()).detail || r.statusText}`);
      return r.json();
    });
  },
  exportPreset: (id) => fetch(`/api/export/preset/${id}`),
  // tts discovery
  getOpenRouterTTSModels: ({ refresh = false } = {}) =>
    request('GET', refresh ? '/api/tts/openrouter/models?refresh=1' : '/api/tts/openrouter/models'),
  getNanoGPTTTSModels: ({ refresh = false } = {}) =>
    request('GET', refresh ? '/api/tts/nanogpt/models?refresh=1' : '/api/tts/nanogpt/models'),
  // generic-provider discovery
  getGenericModels: (provider, { refresh = false, customId = null } = {}) => {
    const q = new URLSearchParams({ provider });
    if (refresh) q.set('refresh', '1');
    if (customId) q.set('custom_id', customId);
    return request('GET', `/api/generic/models?${q}`);
  },
  getGenericProbe: (provider, { refresh = false } = {}) => {
    const q = new URLSearchParams({ provider });
    if (refresh) q.set('refresh', '1');
    return request('GET', `/api/generic/probe?${q}`);
  },
  getGenericCacheHints: (provider, model = '') => {
    const q = new URLSearchParams({ provider });
    if (model) q.set('model', model);
    return request('GET', `/api/generic/cache-hints?${q}`);
  },
};


/* Build the full GET URL the <audio> element consumes. The browser
 * does the streaming HTTP fetch itself; we just need a URL.
 *
 * `cfg` matches the shape from `tts_helpers.js#resolveTTSConfig`:
 *   - api: 'novelai' | 'openrouter' | 'nanogpt' | 'generic'
 *   - novelai:    version, voice, custom_seed
 *   - openrouter: model, voice, speed
 *   - nanogpt:    model, voice, speed
 *   - generic:    custom_id, model, voice, speed
 *
 * Fields that don't apply to the chosen api are dropped so URLs stay
 * minimal. */
export function ttsSpeakUrl(text, cfg) {
  const params = new URLSearchParams();
  params.set('api', cfg.api);
  params.set('text', text);
  if (cfg.api === 'novelai') {
    if (cfg.version) params.set('version', cfg.version);
    if (cfg.voice) params.set('voice', cfg.voice);
    if (cfg.custom_seed) params.set('custom_seed', cfg.custom_seed);
  } else if (cfg.api === 'generic') {
    if (cfg.custom_id) params.set('custom_id', cfg.custom_id);
    if (cfg.model) params.set('model', cfg.model);
    if (cfg.voice) params.set('voice', cfg.voice);
    if (cfg.speed != null && cfg.speed !== 1.0) params.set('speed', String(cfg.speed));
  } else {
    // openrouter / nanogpt
    if (cfg.model) params.set('model', cfg.model);
    if (cfg.voice) params.set('voice', cfg.voice);
    if (cfg.speed != null && cfg.speed !== 1.0) params.set('speed', String(cfg.speed));
  }
  return `/api/tts/speak?${params.toString()}`;
}

async function uploadFile(url, file) {
  const fd = new FormData();
  fd.append('file', file);
  const r = await fetch(url, { method: 'POST', body: fd });
  if (!r.ok) {
    let detail = r.statusText;
    try { detail = (await r.json()).detail || detail; } catch {}
    throw new Error(`${r.status} ${detail}`);
  }
  return r.json();
}


/* User-initiated cancellation flag. Catch sites that surface import
 * errors to the user can check ``err.cancelled`` to suppress the
 * toast — clicking "Cancel" already gave the user feedback. */
function _cancelledError() {
  const e = new Error('Import cancelled');
  e.cancelled = true;
  return e;
}


/* ====== Streaming import (SSE over a POST) ====== */

async function importFileStream(file, opts = {}) {
  // ``mode`` defaults to "ask" — the server surfaces a ``conflict`` event on
  // any top-level UUID collision (handled via ``onConflict`` → 'replace' or
  // 'copy') and a ``name_matches`` event on composite sidecars that match
  // existing entities by name (handled via ``onNameMatches`` → per-role map
  // of either an existing UUID or the literal "new"). We transparently
  // re-issue the upload with the resolved fields each time.
  let mode = opts.mode || 'ask';
  let resolutions = opts.resolutions || null;

  while (true) {
    const fd = new FormData();
    fd.append('file', file);
    fd.append('mode', mode);
    if (resolutions) fd.append('resolutions', JSON.stringify(resolutions));

    const r = await fetch('/api/import', { method: 'POST', body: fd, signal: opts.signal });
    if (!r.ok) {
      let detail = r.statusText;
      try { detail = (await r.json()).detail || detail; } catch {}
      throw new Error(`${r.status} ${detail}`);
    }
    const reader = r.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';
    let result = null;
    let conflict = null;
    let nameMatches = null;
    let errored = null;

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let sep;
      while ((sep = buf.indexOf('\n\n')) !== -1) {
        const block = buf.slice(0, sep);
        buf = buf.slice(sep + 2);
        let event = 'message';
        let data = '';
        for (const line of block.split('\n')) {
          if (line.startsWith('event: ')) event = line.slice(7).trim();
          else if (line.startsWith('data: ')) data += line.slice(6);
        }
        if (!data) continue;
        let payload;
        try { payload = JSON.parse(data); } catch { continue; }
        if (event === 'progress') opts.onProgress && opts.onProgress(payload);
        else if (event === 'done') result = payload;
        else if (event === 'conflict') conflict = payload;
        else if (event === 'name_matches') nameMatches = payload;
        else if (event === 'error') errored = new Error(payload.message || 'Import failed');
      }
    }
    if (errored) throw errored;
    if (conflict) {
      if (!opts.onConflict) {
        throw new Error(`UUID conflict (${conflict.kind} ${conflict.id})`);
      }
      const next = await opts.onConflict(conflict);  // 'replace' | 'copy' | null
      if (!next) throw _cancelledError();
      mode = next;
      continue;
    }
    if (nameMatches) {
      if (!opts.onNameMatches) {
        throw new Error('Composite has name-matched sidecars but no resolver provided');
      }
      const decisions = await opts.onNameMatches(nameMatches);  // {role: id|"new"} | null
      if (!decisions) throw _cancelledError();
      // Merge with prior resolutions so multi-round prompts (rare) accumulate.
      resolutions = { ...(resolutions || {}), ...decisions };
      continue;
    }
    if (!result) throw new Error('Import ended unexpectedly');
    return result;
  }
}


/* ====== Zip import (bulk archive flows) ======
 *
 * Three-phase API to keep memory bounded for multi-GB uploads:
 *   1. ``zipImportUpload(file, {onProgress})`` — XHR (so we can drive an
 *      upload-progress bar), streams the multipart body to disk under a
 *      fresh server-side token. Resolves to ``{token}``.
 *   2. ``zipImportTocStream(token, {onProgress})`` — opens an SSE on
 *      the ToC endpoint; each ``progress`` event ticks the picker's
 *      "Reading archive" bar; the final ``manifest`` event resolves the
 *      promise with the picker payload.
 *   3. ``zipImportSelectedStream(token, selection, {onProgress})`` —
 *      submits the user's picks and consumes the bulk-import SSE stream.
 *
 * Plus ``zipImportCancel(token)`` for the picker's Cancel path. */


function zipImportUpload(file, opts = {}) {
  // XHR (not fetch) so ``upload.onprogress`` fires during the body upload
  // — fetch doesn't expose that hook today. Resolves to ``{token}``.
  return new Promise((resolve, reject) => {
    const fd = new FormData();
    fd.append('file', file);
    const xhr = new XMLHttpRequest();
    xhr.open('POST', '/api/import-zip-upload');
    xhr.responseType = 'json';
    if (opts.onProgress) {
      xhr.upload.addEventListener('progress', (e) => {
        opts.onProgress({
          loaded: e.loaded,
          total: e.lengthComputable ? e.total : 0,
          fraction: e.lengthComputable && e.total ? e.loaded / e.total : 0,
        });
      });
    }
    xhr.addEventListener('load', () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve(xhr.response || {});
      } else {
        const detail = (xhr.response && xhr.response.detail)
          || `${xhr.status} ${xhr.statusText}`;
        reject(new Error(detail));
      }
    });
    xhr.addEventListener('error', () => reject(new Error('Upload network error')));
    xhr.addEventListener('abort', () => reject(new Error('Upload aborted')));
    if (opts.signal) {
      opts.signal.addEventListener('abort', () => xhr.abort());
    }
    xhr.send(fd);
  });
}


function zipImportTocStream(token, opts = {}) {
  // EventSource auto-reconnects if the connection drops, which we don't
  // want here — read the ToC once. Resolves on the ``manifest`` event;
  // surfaces ``progress`` events to the caller in the meantime.
  return new Promise((resolve, reject) => {
    const url = `/api/import-zip-toc/${encodeURIComponent(token)}`;
    const es = new EventSource(url);
    let settled = false;
    const settle = (fn, v) => { if (!settled) { settled = true; es.close(); fn(v); } };

    es.addEventListener('progress', (e) => {
      try { opts.onProgress && opts.onProgress(JSON.parse(e.data)); } catch {}
    });
    es.addEventListener('manifest', (e) => {
      try {
        settle(resolve, JSON.parse(e.data).manifest);
      } catch (err) {
        settle(reject, err);
      }
    });
    es.addEventListener('error', (e) => {
      let payload = null;
      try { payload = JSON.parse(e.data); } catch {}
      if (payload && payload.message) {
        settle(reject, new Error(payload.message));
      } else if (!settled) {
        settle(reject, new Error('ToC stream failed'));
      }
    });
    if (opts.signal) {
      opts.signal.addEventListener('abort', () => settle(reject, new Error('ToC aborted')));
    }
  });
}


async function zipImportSelectedStream(token, selection, opts = {}) {
  // POSTs JSON ``{token, selection}`` and consumes SSE in the same shape
  // as ``/api/import``. No ``conflict`` / ``name_matches`` events — the
  // picker resolved everything up-front.
  const r = await fetch('/api/import-zip', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ token, selection }),
    signal: opts.signal,
  });
  if (!r.ok) {
    let detail = r.statusText;
    try { detail = (await r.json()).detail || detail; } catch {}
    throw new Error(`${r.status} ${detail}`);
  }
  const reader = r.body.getReader();
  const decoder = new TextDecoder();
  let buf = '';
  let result = null;
  let errored = null;

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let sep;
    while ((sep = buf.indexOf('\n\n')) !== -1) {
      const block = buf.slice(0, sep);
      buf = buf.slice(sep + 2);
      let event = 'message';
      let data = '';
      for (const line of block.split('\n')) {
        if (line.startsWith('event: ')) event = line.slice(7).trim();
        else if (line.startsWith('data: ')) data += line.slice(6);
      }
      if (!data) continue;
      let payload;
      try { payload = JSON.parse(data); } catch { continue; }
      if (event === 'progress') opts.onProgress && opts.onProgress(payload);
      else if (event === 'done') result = payload;
      else if (event === 'error') errored = new Error(payload.message || 'Import failed');
    }
  }
  if (errored) throw errored;
  if (!result) throw new Error('Import ended unexpectedly');
  return result;
}


function zipImportCancel(token) {
  // Best-effort token cleanup. Used by the picker's Cancel path so an
  // abandoned upload doesn't sit on disk until the next server restart.
  if (!token) return Promise.resolve();
  return fetch(`/api/import-zip-token/${encodeURIComponent(token)}`, {
    method: 'DELETE',
  }).catch(() => {});
}


/* ====== SSE deletion-response streaming ====== */

export function startContactDeletionResponseStream(contactId, options = {}) {
  const url = `/api/contacts/${contactId}/deletion-response?is_mobile=${_isMobile()}`;
  const es = new EventSource(url);
  let resolved = false;
  function close() { if (!resolved) { resolved = true; es.close(); } }

  es.addEventListener('bubble', (e) => {
    options.onBubble && options.onBubble(JSON.parse(e.data));
  });
  es.addEventListener('done', (e) => {
    let payload = null; try { payload = JSON.parse(e.data); } catch {}
    options.onDone && options.onDone(payload || { cancelled: false });
    close();
  });
  es.addEventListener('error', (e) => {
    let payload = null; try { payload = JSON.parse(e.data); } catch {}
    if (payload && options.onError) options.onError(payload);
    close();
  });
  es.onerror = () => close();
  return { close };
}


/* ====== SSE generation streaming ====== */

export function startGenerationStream(chatId, options = {}) {
  /*
   * Returns an object: { close(), promise }.
   * options: { parentId?: string|null, greeting?: bool, expectedTipId?: string,
   *            mode?: 'normal'|'swipe'|'continue'|'impersonate',
   *            onStart, onContext, onBubble, onError, onDone }
   */
  const params = new URLSearchParams();
  // ``parentId`` distinguishes three cases: undefined ⇒ "extend the chat",
  // a non-null string ⇒ "regenerate as a sibling of this parent", and an
  // explicit null ⇒ "regenerate at the root" (used by greeting rerolls).
  // We send an empty string for the third case so the server can tell it
  // apart from the omitted/extend case. ``in`` would treat an explicitly-
  // passed ``undefined`` as "set", so we check for ``!== undefined``.
  if (options.parentId !== undefined) {
    params.set('parent_id', options.parentId === null ? '' : options.parentId);
  }
  if (options.greeting) params.set('greeting', 'true');
  // ``expectedTipId`` is the active path's tail message id from the caller's
  // POV (or '' for an empty chat). The server compares it to its own active
  // tip and rejects if they differ — guard against two tabs of the same chat
  // stepping on each other when their views have drifted apart.
  if (options.expectedTipId !== undefined) {
    params.set('expected_tip_id', options.expectedTipId);
  }
  if (options.mode && options.mode !== 'normal') {
    params.set('mode', options.mode);
  }
  params.set('is_mobile', _isMobile());
  const url = `/api/chats/${chatId}/generate?${params}`;

  const es = new EventSource(url);
  let resolved = false;
  let resolve, reject;
  const promise = new Promise((res, rej) => { resolve = res; reject = rej; });

  function finish(payload) {
    if (resolved) return;
    resolved = true;
    es.close();
    console.groupEnd();
    resolve(payload);
  }
  function fail(err) {
    if (resolved) return;
    resolved = true;
    es.close();
    console.error('%c[stream] failed', 'color: #f7768e', err);
    console.groupEnd();
    reject(err);
  }

  const debugLabel = `gen ${chatId.slice(0, 8)}`;
  console.groupCollapsed(`%c[${debugLabel}] streaming generation`, 'color: #7aa2f7');

  es.addEventListener('start', e => {
    const d = JSON.parse(e.data);
    console.log('%cstart', 'color: #7aa2f7', d);
    options.onStart && options.onStart(d);
  });
  es.addEventListener('context', e => {
    const d = JSON.parse(e.data);
    console.log('%ccontext', 'color: #9ece6a', d);
    options.onContext && options.onContext(d);
  });
  es.addEventListener('prompt', e => {
    const d = JSON.parse(e.data);
    console.groupCollapsed(`%cprompt (${d.tokens} tokens)`, 'color: #e0af68');
    // AER mode sends a rendered string (``d.text``); Generic mode sends
    // a structured message list (``d.messages``). Log whichever the
    // server emitted so both flows surface the inputs to the model.
    if (d.text != null) {
      console.log(d.text);
    }
    if (Array.isArray(d.messages)) {
      console.log(d.messages);
    }
    console.groupEnd();
  });
  es.addEventListener('bubble', e => {
    const d = JSON.parse(e.data);
    console.log(`%cbubble [${d.emotion}]`, 'color: #bb9af7', d.text);
    options.onBubble && options.onBubble(d);
  });
  // Generic-mode streaming: ``delta`` carries plain text chunks for the
  // current assistant turn; ``reasoning_delta`` carries reasoning text
  // for the (collapsed-by-default) Thoughts tray.
  es.addEventListener('delta', e => {
    const d = JSON.parse(e.data);
    options.onDelta && options.onDelta(d);
  });
  es.addEventListener('reasoning_delta', e => {
    const d = JSON.parse(e.data);
    options.onReasoningDelta && options.onReasoningDelta(d);
  });
  es.addEventListener('token', () => {
    options.onToken && options.onToken();
  });
  es.addEventListener('completion', e => {
    const d = JSON.parse(e.data);
    console.groupCollapsed(`%ccompletion (${d.text.length} chars)`, 'color: #bb9af7');
    console.log(d.text);
    console.groupEnd();
  });
  es.addEventListener('parse_summary', e => {
    const d = JSON.parse(e.data);
    const colour = d.format_error ? '#f7768e' : '#9ece6a';
    console.log(`%cparse_summary`, `color: ${colour}`, d);
  });
  es.addEventListener('error', e => {
    let payload = null;
    try { payload = JSON.parse(e.data); } catch {}
    if (payload) {
      console.error('%cerror', 'color: #f7768e', payload);
      options.onError && options.onError(payload);
      finish({ cancelled: false, error: payload });
    } else {
      fail(new Error('Stream error'));
    }
  });
  es.addEventListener('done', e => {
    const payload = JSON.parse(e.data);
    console.log('%cdone', 'color: #9ece6a', payload);
    options.onDone && options.onDone(payload);
    finish(payload);
  });
  es.onerror = () => {
    if (!resolved) fail(new Error('Stream connection lost'));
  };

  return {
    close: () => { es.close(); resolved = true; },
    promise,
  };
}


/* ====== User-stepped scene → image-prompt streaming ====== */

export function startImagePromptStream(chatId, options = {}) {
  const params = new URLSearchParams();
  if (options.continue) params.set('continue', 'true');
  if (options.anchorMessageId) {
    params.set('anchor_message_id', options.anchorMessageId);
  }
  const query = params.toString();
  const suffix = query ? `?${query}` : '';
  const controller = new AbortController();
  let settled = false;
  const handlers = {
    start: options.onStart,
    delta: options.onDelta,
    state: options.onState,
  };

  // A one-shot fetch is intentional here. EventSource may reconnect on its
  // own, which could turn one user click into a second NovelAI request.
  const promise = (async () => {
    let response;
    try {
      response = await fetch(`/api/chats/${chatId}/image-prompt${suffix}`, {
        method: 'POST',
        signal: controller.signal,
      });
    } catch (error) {
      if (error && error.name === 'AbortError') return { cancelled: true };
      throw new HttpError(0, error && error.message || 'Network error', null);
    }
    if (!response.ok) {
      let parsed = null;
      let detail = response.statusText;
      try {
        parsed = await response.json();
        if (parsed && typeof parsed.detail === 'string') detail = parsed.detail;
      } catch {}
      throw new HttpError(response.status, `${response.status} ${detail}`, parsed);
    }
    if (!response.body) throw new Error('Image-prompt stream has no response body');

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    while (true) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
      let separator = buffer.match(/\r?\n\r?\n/);
      while (separator && separator.index !== undefined) {
        const block = buffer.slice(0, separator.index);
        buffer = buffer.slice(separator.index + separator[0].length);
        let event = 'message';
        const data = [];
        for (const line of block.split(/\r?\n/)) {
          if (line.startsWith('event:')) event = line.slice(6).trim();
          if (line.startsWith('data:')) data.push(line.slice(5).replace(/^ /, ''));
        }
        if (data.length) {
          const payload = JSON.parse(data.join('\n'));
          if (handlers[event]) handlers[event](payload);
          if (event === 'error') {
            if (options.onError) options.onError(payload);
            settled = true;
            return { error: payload };
          }
          if (event === 'done') {
            if (options.onDone) options.onDone(payload);
            settled = true;
            return payload;
          }
        }
        separator = buffer.match(/\r?\n\r?\n/);
      }
      if (done) break;
    }
    throw new Error('Image-prompt stream ended before completion');
  })();

  return {
    close: () => {
      if (settled) return;
      settled = true;
      controller.abort();
    },
    promise,
  };
}
