/* Shared Text-to-Speech section used by both the contact edit page
 * and the user persona edit page. Wraps the mode + use_custom toggle
 * + per-provider override in a collapsible <details> block. Holds NO
 * API key fields — those live only on the Settings page. The block
 * surfaces a small "key set / not set" indicator that links to
 * Settings instead.
 *
 * Call: ``renderEntityTTSSection(draft, save, { entityKind, settings, onTabSwitch })``
 *   - draft: the contact/user draft (must have a ``.tts`` field shaped
 *     like server.models.EntityTTSConfig)
 *   - save: the autosaver from the parent view; called on every mutation
 *   - opts.entityKind: 'contact' or 'user' (affects tooltip wording +
 *     default option order)
 *   - opts.settings: the SettingsView from /api/settings (for the
 *     active-provider + key-indicator readout)
 *   - opts.onTabSwitch: optional () => void, fired when the user
 *     clicks the "configure on Settings" link
 */

import { el } from '../util.js';
import { icon, helpDetails } from '../ui.js';
import { makeSelect, makeAvatarPicker } from '../avatar_picker.js';
import {
  NAI_V1_PRESETS,
  NAI_V2_PRESETS,
  NAI_CUSTOM_SENTINEL,
  TTS_PROVIDER_KINDS,
} from '../constants.js';
import { resolveTTSConfig, isProviderKeyConfigured } from '../tts_helpers.js';
import { getTTSModels, getCachedTTSModels, isCacheStale } from '../tts_discovery.js';
import { renderTTSTestBlock } from './tts_test_block.js';
import { state } from '../state.js';


/* Format a NAI preset's dropdown label: "Cyllene (F)". */
function naiLabel(preset) {
  return `${preset.name} (${preset.gender})`;
}


function defaultEntityTTS() {
  return {
    mode: 'default',
    use_custom: false,
    override: defaultOverride(),
  };
}

function defaultOverride() {
  return {
    kind: 'novelai',
    custom_id: '',
    novelai_version: 'v2',
    novelai_voice: 'Aini',
    novelai_custom_seed: '',
    openrouter_model: '',
    openrouter_voice: '',
    openrouter_speed: 1.0,
    nanogpt_model: '',
    nanogpt_voice: '',
    nanogpt_speed: 1.0,
    generic_model: '',
    generic_voice: '',
    generic_speed: 1.0,
  };
}


export function renderEntityTTSSection(draft, save, opts = {}) {
  const entityKind = opts.entityKind || 'contact';
  const settings = opts.settings || {};
  const onTabSwitch = opts.onTabSwitch || null;

  // Lazy-init: older entities loaded from YAML may not have the tts
  // block at all. Fill the default in-memory so the form bindings have
  // something to read from.
  if (!draft.tts) draft.tts = defaultEntityTTS();
  if (!draft.tts.override) draft.tts.override = defaultOverride();

  const section = el('div', { class: 'section tts-entity' });
  section.append(el('h3', {}, 'Text-to-Speech'));

  const body = el('div', { class: 'tts-entity-body' });
  section.append(body);

  // --- Mode dropdown -----------------------------------------------------

  const modeOptions = entityKind === 'user'
    ? [
        { value: 'disabled', label: 'Disabled' },
        { value: 'default',  label: 'Default (follow global)' },
        { value: 'enabled',  label: 'Enabled' },
      ]
    : [
        { value: 'default',  label: 'Default (follow global)' },
        { value: 'enabled',  label: 'Enabled' },
        { value: 'disabled', label: 'Disabled' },
      ];

  const modeSelect = makeSelect({
    value: draft.tts.mode || (entityKind === 'user' ? 'disabled' : 'default'),
    options: modeOptions,
    onChange: (v) => {
      draft.tts.mode = v;
      save();
    },
  });

  const modeHelp = entityKind === 'user'
    ? 'Default follows the global TTS toggle. Enabled always voices this persona’s messages. Disabled keeps them silent.'
    : 'Default follows the global TTS toggle. Enabled always plays this contact’s replies. Disabled keeps them silent.';

  body.append(
    el('div', { class: 'form-group tts-row' },
      el('label', { class: 'tts-row-label' }, 'Mode ', helpDetails(modeHelp, { label: 'Mode help' })),
      modeSelect,
    ),
  );

  // --- Use custom settings toggle ---------------------------------------

  const customToggle = el('label', { class: 'tts-checkbox-row' },
    el('input', {
      type: 'checkbox',
      checked: !!draft.tts.use_custom,
      onChange: (e) => {
        draft.tts.use_custom = e.target.checked;
        save();
        renderCustomBlock();
      },
    }),
    el('span', {}, 'Use custom voice settings for this ' + entityKind),
  );
  body.append(customToggle);

  // --- Per-kind config block (only when use_custom is on) ---------------

  const customBlock = el('div', { class: 'tts-custom-block' });
  body.append(customBlock);

  // "Test voice" — always present below the override block. Uses the
  // draft's *current* (unsaved) tts settings so the user can hear
  // their changes immediately instead of having to save first. The
  // Play button is disabled when the resolved provider has no API
  // key configured on Settings.
  const getCfg = () => {
    if (entityKind === 'contact') {
      return resolveTTSConfig('contact', draft, null, state.settings || settings);
    }
    return resolveTTSConfig('user', null, draft, state.settings || settings);
  };
  body.append(renderTTSTestBlock(getCfg, {
    getReady: () => isProviderKeyConfigured(getCfg(), state.settings || settings),
  }));

  function renderCustomBlock() {
    customBlock.replaceChildren();
    if (!draft.tts.use_custom) return;

    // Filter the kind dropdown to providers that have a key
    // configured on Settings — there's no point letting the user pick
    // a provider they can't authenticate against.
    const s = settings || {};
    const kindOptions = [];
    if (s.tts?.novelai?.api_key_indicator === '__present__'
        || s.api_token_indicator === '__present__') {
      kindOptions.push({ value: 'novelai',    label: 'NovelAI' });
    }
    if (s.tts?.openrouter?.api_key_indicator === '__present__') {
      kindOptions.push({ value: 'openrouter', label: 'OpenRouter' });
    }
    if (s.tts?.nanogpt?.api_key_indicator === '__present__') {
      kindOptions.push({ value: 'nanogpt',    label: 'NanoGPT' });
    }
    for (const c of s.tts?.custom_apis || []) {
      // Custom endpoints need a key AND a base URL to be reachable;
      // an entry missing either isn't a valid pick.
      if (c.api_key_indicator === '__present__' && c.base_url) {
        kindOptions.push({ value: `generic:${c.id}`, label: c.name || 'Custom API' });
      }
    }

    const ov = draft.tts.override;

    if (kindOptions.length === 0) {
      customBlock.append(el('div', {
        class: 'tts-no-keys-hint',
        style: {
          color: 'var(--text-mute)',
          fontSize: '13px',
          padding: '8px 0',
        },
      }, 'No providers have an API key configured yet — set one up on the Settings page first.'));
      return;
    }

    // If the previously-chosen kind no longer has a key, fall back to
    // the first available option so the dropdown shows something
    // valid. The override fields stay intact — switching back later
    // restores the picks for that kind.
    let kindValue;
    if (ov.kind === 'generic') {
      kindValue = `generic:${ov.custom_id || ''}`;
    } else {
      kindValue = ov.kind || 'novelai';
    }
    if (!kindOptions.find(o => o.value === kindValue)) {
      kindValue = kindOptions[0].value;
      if (kindValue.startsWith('generic:')) {
        ov.kind = 'generic';
        ov.custom_id = kindValue.slice('generic:'.length);
      } else {
        ov.kind = kindValue;
        ov.custom_id = '';
      }
    }

    const kindSelect = makeSelect({
      value: kindValue,
      options: kindOptions,
      onChange: (v) => {
        if (v.startsWith('generic:')) {
          ov.kind = 'generic';
          ov.custom_id = v.slice('generic:'.length);
        } else {
          ov.kind = v;
          ov.custom_id = '';
        }
        save();
        renderProviderConfig();
      },
    });

    customBlock.append(
      el('div', { class: 'form-group tts-row' },
        el('label', { class: 'tts-row-label' }, 'Provider'),
        kindSelect,
      ),
    );

    const providerConfigBlock = el('div', { class: 'tts-provider-config' });
    customBlock.append(providerConfigBlock);
    function renderProviderConfig() {
      providerConfigBlock.replaceChildren();
      const k = ov.kind;
      if (k === 'novelai') {
        renderNAIBlock(providerConfigBlock, ov, save, settings);
      } else if (k === 'openrouter') {
        renderOpenAIBlock(providerConfigBlock, ov, save, settings, 'openrouter');
      } else if (k === 'nanogpt') {
        renderOpenAIBlock(providerConfigBlock, ov, save, settings, 'nanogpt');
      } else if (k === 'generic') {
        renderGenericBlock(providerConfigBlock, ov, save, settings, onTabSwitch);
      }
    }
    renderProviderConfig();
  }

  renderCustomBlock();

  return section;
}


/* ====================================================================
 * Per-kind config blocks
 * ==================================================================== */


function renderNAIBlock(parent, ov, save, settings) {
  const voiceField = () => ov.novelai_version === 'v1'
    ? 'novelai_voice_v1' : 'novelai_voice_v2';
  const seedField = () => ov.novelai_version === 'v1'
    ? 'novelai_custom_seed_v1' : 'novelai_custom_seed_v2';

  // Version toggle (V1 / V2 segmented button).
  const versionRow = el('div', { class: 'form-group tts-row' },
    el('label', { class: 'tts-row-label' }, 'Voice version'),
  );
  const segWrap = el('div', { class: 'tts-segmented' });
  for (const v of ['v1', 'v2']) {
    const btn = el('button', {
      type: 'button',
      class: 'tts-seg-btn' + (ov.novelai_version === v ? ' active' : ''),
      onClick: () => {
        ov.novelai_version = v;
        save();
        repaint();
      },
    }, v.toUpperCase());
    segWrap.append(btn);
  }
  versionRow.append(segWrap);
  parent.append(versionRow);

  const voiceRow = el('div', { class: 'form-group tts-row' });
  const customSeedRow = el('div', { class: 'form-group tts-row' });

  function repaint() {
    voiceRow.replaceChildren();
    customSeedRow.replaceChildren();

    const presets = ov.novelai_version === 'v1' ? NAI_V1_PRESETS : NAI_V2_PRESETS;
    const voiceOptions = [
      ...presets.map(p => ({ value: p.name, label: naiLabel(p) })),
      { value: NAI_CUSTOM_SENTINEL, label: '(custom)' },
    ];
    const vField = voiceField();
    const sField = seedField();
    const voiceSelect = makeSelect({
      value: ov[vField],
      options: voiceOptions,
      onChange: (v) => {
        ov[vField] = v;
        save();
        repaint();
      },
    });
    voiceRow.append(
      el('label', { class: 'tts-row-label' }, 'Voice'),
      voiceSelect,
    );

    if (ov[vField] === NAI_CUSTOM_SENTINEL) {
      customSeedRow.append(
        el('label', { class: 'tts-row-label' }, 'Voice seed'),
        el('input', {
          type: 'text',
          value: ov[sField] || '',
          placeholder: 'Free-form seed',
          onInput: (e) => {
            ov[sField] = e.target.value;
            save();
          },
        }),
      );
    }

    // Re-rebuild segmented buttons' active state — version may have changed.
    for (const b of segWrap.querySelectorAll('.tts-seg-btn')) {
      b.classList.toggle('active', b.textContent.toLowerCase() === ov.novelai_version);
    }
  }

  parent.append(voiceRow);
  parent.append(customSeedRow);

  repaint();
}


function renderOpenAIBlock(parent, ov, save, settings, kind) {
  const modelField = `${kind}_model`;
  const voiceField = `${kind}_voice`;
  const speedField = `${kind}_speed`;

  // Voice picker + speed slider — rebuilt whenever the model changes.
  const voiceRow = el('div', { class: 'form-group tts-row' });
  const speedRow = el('div', { class: 'form-group tts-row' });

  const modelPicker = makeAvatarPicker({
    value: ov[modelField] || '',
    options: cachedModelsAsOptions(kind),
    placeholder: cachedModelsAsOptions(kind).length ? 'Select model' : 'Loading models…',
    onChange: (v) => {
      ov[modelField] = v;
      // Reset voice if the new model doesn't carry the old one.
      const models = getCachedTTSModels(kind) || [];
      const m = models.find(mm => mm.id === v);
      if (m && !m.voices.includes(ov[voiceField])) {
        ov[voiceField] = m.voices[0] || '';
      }
      save();
      paintVoice();
      paintSpeed();
    },
  });

  // Refresh on dropdown open if stale; manual reload button is always live.
  modelPicker.addEventListener('focusin', () => maybeRefresh(false));
  const reloadBtn = el('button', {
    type: 'button',
    class: 'btn ghost tts-reload-btn',
    title: 'Refresh model list',
    'aria-label': 'Refresh model list',
    onClick: () => maybeRefresh(true),
  }, icon('refresh', 14));

  let refreshSpinner = null;
  async function maybeRefresh(force) {
    if (!force && !isCacheStale(kind)) return;
    reloadBtn.classList.add('spinning');
    try {
      const models = await getTTSModels(kind, { force });
      modelPicker.setOptions(models.map(m => ({ value: m.id, label: m.name || m.id })));
      if (ov[modelField] && !models.find(m => m.id === ov[modelField])) {
        // Picked model is gone — leave value in place (proxy will reject)
        // but flag the voice list as empty.
      }
      paintVoice();
      paintSpeed();
    } catch (err) {
      console.error(`Failed to refresh ${kind} models:`, err);
    } finally {
      reloadBtn.classList.remove('spinning');
    }
  }

  // Kick off an initial fetch (non-forcing) so the dropdown isn't empty.
  maybeRefresh(false);

  parent.append(
    el('div', { class: 'form-group tts-row tts-model-row' },
      el('label', { class: 'tts-row-label' }, 'Model'),
      el('div', { class: 'tts-model-pickerwrap' }, modelPicker, reloadBtn),
    ),
  );

  parent.append(voiceRow);
  parent.append(speedRow);

  function paintVoice() {
    voiceRow.replaceChildren();
    const models = getCachedTTSModels(kind) || [];
    const m = models.find(mm => mm.id === ov[modelField]);
    const voices = m?.voices || [];
    if (!ov[voiceField] && voices.length) ov[voiceField] = voices[0];
    const voiceSelect = makeSelect({
      value: ov[voiceField] || '',
      options: voices.map(v => ({ value: v, label: v })),
      placeholder: voices.length ? 'Select voice' : 'Pick a model first',
      onChange: (v) => {
        ov[voiceField] = v;
        save();
      },
    });
    voiceRow.append(
      el('label', { class: 'tts-row-label' }, 'Voice'),
      voiceSelect,
    );
  }

  function paintSpeed() {
    speedRow.replaceChildren();
    const models = getCachedTTSModels(kind) || [];
    const m = models.find(mm => mm.id === ov[modelField]);
    // OR: only show speed when supported. NGPT: always show — most TTS
    // models support it and the upstream silently ignores it where
    // they don't (per OpenAI spec).
    const show = kind === 'nanogpt' ? true : (m?.speed_supported ?? false);
    if (!show) return;
    speedRow.append(speedSliderRow(ov, speedField, save));
  }

  paintVoice();
  paintSpeed();
}


function renderGenericBlock(parent, ov, save, settings, onTabSwitch) {
  const customApis = settings.tts?.custom_apis || [];
  const entry = customApis.find(e => e.id === ov.custom_id);

  // The Provider dropdown above already filters to entries that have
  // both a key and a base URL — a missing entry here means the user
  // hasn't picked one yet. The Test button takes care of its own
  // "ready / not ready" affordance, so we just bail quietly.
  if (!entry) return;

  const modelOptions = entry.models.map(m => ({ value: m, label: m }));
  if (!ov.generic_model && entry.default_model) ov.generic_model = entry.default_model;

  const modelSelect = makeSelect({
    value: ov.generic_model || '',
    options: modelOptions,
    placeholder: modelOptions.length ? 'Select model' : 'No models configured',
    onChange: (v) => {
      ov.generic_model = v;
      // Reset voice if the new model doesn't carry the old one.
      const voices = entry.voices[v] || [];
      if (!voices.includes(ov.generic_voice)) {
        ov.generic_voice = voices[0] || '';
      }
      save();
      paintVoice();
    },
  });

  parent.append(
    el('div', { class: 'form-group tts-row' },
      el('label', { class: 'tts-row-label' }, 'Model'),
      modelSelect,
    ),
  );

  const voiceRow = el('div', { class: 'form-group tts-row' });
  parent.append(voiceRow);
  function paintVoice() {
    voiceRow.replaceChildren();
    const voices = entry.voices[ov.generic_model] || [];
    if (!ov.generic_voice && voices.length) ov.generic_voice = voices[0];
    const sel = makeSelect({
      value: ov.generic_voice || '',
      options: voices.map(v => ({ value: v, label: v })),
      placeholder: voices.length ? 'Select voice' : 'No voices configured for this model',
      onChange: (v) => {
        ov.generic_voice = v;
        save();
      },
    });
    voiceRow.append(
      el('label', { class: 'tts-row-label' }, 'Voice'),
      sel,
    );
  }
  paintVoice();

  parent.append(speedSliderRow(ov, 'generic_speed', save));
}


function speedSliderRow(ov, field, save) {
  const row = el('div', { class: 'form-group tts-row tts-speed-row' });
  const slider = el('input', {
    type: 'range',
    min: '0.25',
    max: '4.0',
    step: '0.05',
    value: ov[field] ?? 1.0,
    lang: 'en-US',
    oninput: (e) => {
      const n = parseFloat(e.target.value);
      if (Number.isFinite(n)) {
        ov[field] = n;
        readout.textContent = `${n.toFixed(2)}×`;
      }
    },
    onchange: () => save(),
  });
  const readout = el('span', { class: 'tts-speed-readout' }, `${(ov[field] ?? 1.0).toFixed(2)}×`);
  row.append(
    el('label', { class: 'tts-row-label' }, 'Speed'),
    el('div', { class: 'tts-speed-controls' }, slider, readout),
  );
  return row;
}


function cachedModelsAsOptions(kind) {
  const models = getCachedTTSModels(kind);
  if (!models) return [];
  return models.map(m => ({ value: m.id, label: m.name || m.id }));
}
