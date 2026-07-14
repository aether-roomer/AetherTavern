/* "New chat" modal: pick contact (req) + user (req) + scenario (optional). */

import { api } from '../api.js';
import { state, setState } from '../state.js';
import { el, avatarEl, userAvatarEl, scenarioAvatarEl, placeholderAvatar } from '../util.js';
import { openModal, closeModal, toast } from '../ui.js';
import { makeAvatarPicker } from '../avatar_picker.js';
import { mountLibraryCascade } from './brain_libraries.js';
import { buildModelControls } from './model_controls.js';


export function openNewChatWizard(prefill = {}) {
  // Default-picking priority for each role:
  //   1. ``prefill`` — the caller knows best (e.g. wizard opened from a
  //      contact's "New chat" button supplies ``contactId``).
  //   2. Active entity in its own tab — if the user is sitting in the
  //      Contacts/Users/Scenarios tab on a specific entity, lead with it.
  //   3. Most recently used in any chat (by chat.updated_at). Lets the
  //      wizard converge on the user's working set without explicit input.
  //   4. First entity / no scenario.
  let contactId = prefill.contactId
    || _activeId(state.activeContactId, state.contacts)
    || _mostRecentField('contact_id')
    || (state.contacts[0] && state.contacts[0].id)
    || '';
  let userId = prefill.userId
    || _activeId(state.activeUserId, state.users)
    || _mostRecentField('user_id')
    || (state.users[0] && state.users[0].id)
    || '';
  // Tracks the full Contact for ``contactId`` for the scenario picker.
  // ``state.contacts`` (the Summary) already carries ``scenarios`` and
  // ``default_scenario_id``, so the initial render works without a fetch;
  // we still re-fetch fresh data on open in case the user just added a
  // scenario whose changes haven't propagated to ``state.contacts`` yet.
  let fullContact = null;

  // Selected scenario as a discriminated value: "" / "global:<id>" / "contact:<id>".
  let scenarioSel = prefill.scenarioId
    ? `global:${prefill.scenarioId}`
    : _defaultScenarioSel(contactId, fullContact);
  let brainLibraryIds = Array.isArray(prefill.brainLibraryIds)
    ? prefill.brainLibraryIds.slice()
    : [];
  let title = '';

  function autoTitle() {
    const c = state.contacts.find(x => x.id === contactId);
    return c ? `Chat with ${c.name}` : '';
  }

  function refreshTitle(input) {
    if (input.dataset.userTouched !== '1') input.value = autoTitle();
  }

  const titleInput = el('input', {
    type: 'text',
    placeholder: 'Title',
    value: autoTitle(),
    oninput: (e) => { e.target.dataset.userTouched = '1'; title = e.target.value; },
  });

  async function _hydrateAndRefreshScenarios(forContactId) {
    const loaded = await _loadFullContact(forContactId);
    // Drop if the user has since changed the contact selection.
    if (forContactId !== contactId) return;
    fullContact = loaded;
    // Re-derive the default scenario now that we know the contact's
    // character-scenarios — this catches the case where the wizard
    // opened with a contact whose default isn't a global scenario.
    if (!prefill.scenarioId) {
      scenarioSel = _defaultScenarioSel(contactId, fullContact);
    }
    scenarioPicker.setOptions(_scenarioOptions(contactId, fullContact));
    scenarioPicker.setValue(scenarioSel);
  }

  const contactPicker = makeAvatarPicker({
    value: contactId,
    options: _contactOptions(),
    onChange: (v) => {
      contactId = v;
      fullContact = null;
      refreshTitle(titleInput);
      // Switching contact resets the scenario to its default — the previous
      // contact's character-scenarios aren't valid for the new one anyway.
      scenarioSel = _defaultScenarioSel(contactId, fullContact);
      scenarioPicker.setOptions(_scenarioOptions(contactId, fullContact));
      scenarioPicker.setValue(scenarioSel);
      _hydrateAndRefreshScenarios(contactId);
    },
  });

  const userPicker = makeAvatarPicker({
    value: userId,
    options: _userOptions(),
    onChange: (v) => { userId = v; },
  });

  const scenarioPicker = makeAvatarPicker({
    value: scenarioSel,
    options: _scenarioOptions(contactId, fullContact),
    onChange: (v) => { scenarioSel = v; },
  });

  // Kick off the initial full-contact fetch so character-scenarios show
  // up as soon as they land — usually before the user notices.
  _hydrateAndRefreshScenarios(contactId);

  // Brain libraries are optional and only meaningful when some exist. With no
  // libraries to pick (and none pre-selected) the cascade renders nothing, so
  // the whole section is dropped rather than left as a lone headline.
  const showLibraries = (state.libraries || []).length > 0 || brainLibraryIds.length > 0;
  const libraryCascadeEl = el('div', { class: 'library-cascade' });
  if (showLibraries) {
    mountLibraryCascade(libraryCascadeEl, {
      selected: brainLibraryIds,
      onChange: (next) => { brainLibraryIds = next; },
    });
  }

  // Per-chat Provider / Model / Context-preset overrides (all optional).
  // Reuses the shared builder against a synthetic chat backed by locals; the
  // ``form-group`` chrome matches the wizard's other fields.
  let providerOverride = null;
  let modelOverrides = {};
  let contextPresetOverride = null;
  const modelControlRows = buildModelControls({
    getChat: () => ({
      provider_override: providerOverride,
      model_overrides: modelOverrides,
      context_preset_override: contextPresetOverride,
    }),
    settings: state.settings,
    layout: 'form-group',
    onChange: (field, value) => {
      if (field === 'provider_override') providerOverride = value;
      else if (field === 'model_overrides') modelOverrides = value;
      else if (field === 'context_preset_override') contextPresetOverride = value;
    },
  });

  const blockingErrors = [];
  if (state.contacts.length === 0) blockingErrors.push('You need at least one contact. Create or import one in the Contacts tab.');
  if (state.users.length === 0) blockingErrors.push('You need at least one user persona. Create or import one in the User personas tab.');

  const errorEl = blockingErrors.length
    ? el('div', { style: { color: 'var(--danger)', marginBottom: '12px', fontSize: '13px' } },
        ...blockingErrors.map(e => el('div', {}, e)))
    : null;

  async function create() {
    if (blockingErrors.length) return;
    if (!contactId || !userId) { toast('Pick a contact and persona', 'error'); return; }
    let scenario_id = null;
    let contact_scenario_id = null;
    if (scenarioSel && scenarioSel.startsWith('contact:')) {
      contact_scenario_id = scenarioSel.slice('contact:'.length);
    } else if (scenarioSel && scenarioSel.startsWith('global:')) {
      scenario_id = scenarioSel.slice('global:'.length);
    }
    try {
      const chat = await api.createChat({
        contact_id: contactId,
        user_id: userId,
        scenario_id,
        contact_scenario_id,
        brain_library_ids: brainLibraryIds,
        title: titleInput.value || autoTitle(),
        provider_override: providerOverride,
        model_overrides: modelOverrides,
        context_preset_override: contextPresetOverride,
      });
      const all = await api.listChats();
      setState({ chats: all, activeChatId: chat.id, activeTab: 'chats' });
      closeModal();
    } catch (e) {
      toast(`Could not create chat: ${e.message}`, 'error');
    }
  }

  const body = el('div', {},
    el('h3', {}, 'New chat'),
    errorEl,
    el('div', { class: 'form-group' },
      el('label', {}, 'Title'),
      titleInput,
    ),
    el('div', { class: 'form-group' },
      el('label', {}, 'Contact'),
      contactPicker,
    ),
    el('div', { class: 'form-group' },
      el('label', {}, 'User persona'),
      userPicker,
    ),
    el('div', { class: 'form-group' },
      el('label', {}, 'Scenario (optional)'),
      scenarioPicker,
    ),
    showLibraries
      ? el('div', { class: 'form-group' },
          el('label', {}, 'Brain libraries (optional)'),
          libraryCascadeEl,
        )
      : null,
    // The Provider / Model / Context-preset rows render as their own
    // ``.form-group``s; the wrapper just groups them for targeting.
    el('div', { class: 'model-overrides' }, ...modelControlRows),
    el('div', { class: 'modal-actions' },
      el('button', { class: 'btn ghost', onClick: () => closeModal() }, 'Cancel'),
      el('button', {
        class: 'btn primary',
        disabled: blockingErrors.length > 0,
        onClick: create,
      }, 'Create'),
    ),
  );

  // Enter anywhere in the modal triggers Create — saves a click for the
  // common case (defaults are already sensible). The picker's own keydown
  // handlers stopPropagation on Enter so navigating the popover doesn't
  // submit the form.
  body.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.ctrlKey && !e.metaKey) {
      e.preventDefault();
      create();
    }
  });

  openModal(body);
}


/* ----- Option builders ----- */

function _contactOptions() {
  const list = state.contacts || [];
  const favs = list.filter(c => c.favorite);
  const rest = list.filter(c => !c.favorite);
  const opts = [];
  favs.forEach((c, i) => opts.push({
    value: c.id,
    label: c.name || '',
    getAvatar: () => avatarEl(c),
    favorite: true,
    groupAfter: i === favs.length - 1 && rest.length > 0,
  }));
  rest.forEach(c => opts.push({
    value: c.id,
    label: c.name || '',
    getAvatar: () => avatarEl(c),
  }));
  return opts;
}

function _userOptions() {
  const list = state.users || [];
  const favs = list.filter(u => u.favorite);
  const rest = list.filter(u => !u.favorite);
  const opts = [];
  const makeAv = (u) => () => userAvatarEl(u) || placeholderAvatar(u.name);
  favs.forEach((u, i) => opts.push({
    value: u.id,
    label: u.name || '',
    getAvatar: makeAv(u),
    favorite: true,
    groupAfter: i === favs.length - 1 && rest.length > 0,
  }));
  rest.forEach(u => opts.push({
    value: u.id,
    label: u.name || '',
    getAvatar: makeAv(u),
  }));
  return opts;
}

async function _loadFullContact(contactId) {
  // Always fetch fresh — the summary in ``state.contacts`` covers the
  // synchronous render path; this picks up server-side changes (e.g. a
  // scenario just added on another tab) that the summary doesn't yet
  // reflect.
  if (!contactId) return null;
  try {
    return await api.getContact(contactId);
  } catch {
    return null;
  }
}


function _scenarioOptions(contactId, fullContact = null) {
  const contact = fullContact
    || (state.contacts || []).find(c => c.id === contactId);
  const cscens = (contact && contact.scenarios) || [];
  const sortedCscens = [...cscens].sort((a, b) => (a.name || '').localeCompare(b.name || ''));
  const globals = state.scenarios || [];
  const favGlobals = globals.filter(s => s.favorite);
  const restGlobals = globals.filter(s => !s.favorite);

  const hasOthers = sortedCscens.length > 0 || favGlobals.length > 0 || restGlobals.length > 0;
  const opts = [];
  // Synthetic "(none)" option — empty value clears the scenario for this chat.
  opts.push({
    value: '',
    label: '(none)',
    groupAfter: hasOthers,
  });

  // Contact-scoped scenarios — share the contact's avatar so the user sees
  // they're tied to this character. ``(default)`` annotation matches the
  // previous text-only selector behaviour.
  sortedCscens.forEach((s, i) => {
    const isDefault = s.id === (contact && contact.default_scenario_id);
    opts.push({
      value: `contact:${s.id}`,
      label: `${s.name || '(unnamed)'}${isDefault ? '  (default)' : ''}`,
      getAvatar: () => avatarEl(contact),
      groupAfter: i === sortedCscens.length - 1
        && (favGlobals.length > 0 || restGlobals.length > 0),
    });
  });

  favGlobals.forEach((s, i) => opts.push({
    value: `global:${s.id}`,
    label: s.name || '',
    getAvatar: () => scenarioAvatarEl(s),
    favorite: true,
    groupAfter: i === favGlobals.length - 1 && restGlobals.length > 0,
  }));

  restGlobals.forEach(s => opts.push({
    value: `global:${s.id}`,
    label: s.name || '',
    getAvatar: () => scenarioAvatarEl(s),
  }));

  return opts;
}


/* ----- Default-picking helpers ----- */

function _activeId(id, list) {
  // Whichever entity the user last clicked into in its own list — regardless
  // of which tab is currently visible. The active state is sticky across
  // tab switches, so "I last looked at Natasha + Anon" carries over when
  // the user opens the wizard from elsewhere.
  if (!id) return '';
  if (!(list || []).some(x => x.id === id)) return '';
  return id;
}

function _mostRecentField(field) {
  // Sort chats by updated_at desc, return the first non-empty value of
  // ``field``. ``field`` is one of ``contact_id`` / ``user_id``.
  const chats = state.chats || [];
  if (!chats.length) return '';
  const sorted = [...chats].sort((a, b) => (b.updated_at || 0) - (a.updated_at || 0));
  for (const c of sorted) {
    if (c[field]) return c[field];
  }
  return '';
}

function _defaultScenarioSel(contactId, fullContact = null) {
  // Order:
  //   1. Active scenario if the user is editing one on the Scenarios tab.
  //   2. Contact's ``default_scenario_id`` if it resolves to one of its
  //      character-scenarios.
  //   3. (none).
  // Don't fall back to "last scenario used in a chat with this contact" —
  // it surprises more often than it helps.
  const activeScen = _activeId(state.activeScenarioId, state.scenarios);
  if (activeScen) return `global:${activeScen}`;

  const contact = fullContact
    || (state.contacts || []).find(x => x.id === contactId);
  const cscens = (contact && contact.scenarios) || [];
  if (contact && contact.default_scenario_id
      && cscens.some(s => s.id === contact.default_scenario_id)) {
    return `contact:${contact.default_scenario_id}`;
  }
  return '';
}
