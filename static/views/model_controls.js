/* Per-chat Provider / Model / Context-Preset override controls.
 *
 * Shared by the chat settings bar's "Model" popover, the mobile chat-settings
 * modal, and the new-chat wizard. The three overrides live on the Chat as
 * ``provider_override`` (a provider slug, or null = inherit global),
 * ``model_overrides`` ({provider_slug -> model_id}, per-provider memory), and
 * ``context_preset_override`` (a ContextPreset id, or null = provider default).
 *
 * When a chat resolves to AetherRoom only the Provider picker shows (so you can
 * switch out of AER) — AER has one model and no context-preset concept. Model +
 * Context-preset pickers appear only for a Generic provider.
 */

import { el } from '../util.js';
import { state } from '../state.js';
import { makeSelect as pickerSelect } from '../avatar_picker.js';
import {
  PROVIDER_LABELS,
  buildGenericModelPicker,
  contextPresetOptions,
} from '../model_sources.js';


// --- slug helpers ---------------------------------------------------------

export function customIdOf(slug) {
  return (slug && slug.startsWith('openai_compatible:'))
    ? slug.slice('openai_compatible:'.length)
    : null;
}

export function genericKindOf(slug) {
  if (!slug || slug === 'aetherroom') return null;
  if (slug.startsWith('openai_compatible:')) return 'openai_compatible';
  if (slug === 'novelai' || slug === 'openrouter' || slug === 'nanogpt') return slug;
  return null;
}


/* The canonical resolved provider slug for a chat: its override if set, else the
 * global selection. Mirrors server/generation_target.py. Returns null when the
 * global generic provider is ``openai_compatible`` with no active entry. */
export function resolveChatProvider(chat, settings) {
  const ov = chat && chat.provider_override;
  if (ov) return ov;
  const s = settings || {};
  if (s.provider_mode !== 'generic') return 'aetherroom';
  const g = s.generic || {};
  if (g.provider === 'novelai' || g.provider === 'openrouter' || g.provider === 'nanogpt') {
    return g.provider;
  }
  if (g.provider === 'openai_compatible') {
    const id = g.openai_compatible && g.openai_compatible.active_id;
    return id ? `openai_compatible:${id}` : null;
  }
  return 'aetherroom';
}


export function hasModelOverride(chat) {
  if (!chat) return false;
  if (chat.provider_override) return true;
  if (chat.context_preset_override) return true;
  const mo = chat.model_overrides || {};
  return Object.keys(mo).length > 0;
}


function providerOptions(settings, currentOverride) {
  const g = (settings && settings.generic) || {};
  const customs = (g.openai_compatible && g.openai_compatible.custom_providers) || [];
  const opts = [
    { value: '', label: '(currently selected)' },
    { value: 'aetherroom', label: PROVIDER_LABELS.aetherroom },
    { value: 'novelai', label: PROVIDER_LABELS.novelai },
    { value: 'openrouter', label: PROVIDER_LABELS.openrouter },
    { value: 'nanogpt', label: PROVIDER_LABELS.nanogpt },
    ...customs.map(c => ({
      value: `openai_compatible:${c.id}`,
      label: `↳ ${c.label || 'Custom provider'}`,
    })),
  ];
  // Stale override (e.g. a deleted custom provider): surface it so the user can
  // see and fix it rather than silently showing the inherit option.
  if (currentOverride && !opts.some(o => o.value === currentOverride)) {
    const id = customIdOf(currentOverride) || currentOverride;
    opts.push({ value: currentOverride, label: `↳ (missing) ${id}` });
  }
  return opts;
}


function ctxPresetOptions(current) {
  const opts = contextPresetOptions("(provider's default)");
  if (current && !opts.some(o => o.value === current)) {
    opts.push({ value: current, label: '(deleted)' });
  }
  return opts;
}


/* Build the three override pickers as a stack of labelled rows. ``getChat``
 * returns the chat to seed from; ``onChange(field, value)`` fires whenever an
 * override changes (the bar persists via ``patch``, the wizard writes local
 * state). Returns an array of DOM nodes for the caller to spread into its
 * container.
 *
 * ``layout`` selects the row chrome:
 *   - default / ``'inline'`` — bare ``<label>`` rows (headline text + control on
 *     one line), tuned for the ``.chat-config.stacked`` mobile gear modal so
 *     they sit flush with its sibling Intimacy / Style / … selects.
 *   - ``'form-group'`` — ``.form-group`` rows (uppercase headline label above a
 *     full-width control), matching the new-chat wizard's other fields and the
 *     desktop Model popover. */
export function buildModelControls({ getChat, settings, onChange, layout } = {}) {
  const chat0 = getChat() || {};
  const liveSettings = settings || state.settings || {};
  const formGroup = layout === 'form-group';
  // Local working copy so provider switches re-derive the model/context rows
  // synchronously, without waiting for the async save to land back in state.
  const draft = {
    provider_override: chat0.provider_override || null,
    model_overrides: { ...(chat0.model_overrides || {}) },
    context_preset_override: chat0.context_preset_override || null,
  };

  const emit = (field, value) => { if (onChange) onChange(field, value); };

  // One labelled row in either chrome. ``mount(control)`` (re)places the control
  // next to / under a persistent headline label; the model + context rows swap
  // their control whenever the provider changes, leaving the label untouched.
  function makeRow(labelText) {
    if (formGroup) {
      const labelEl = el('label', {}, labelText);
      const row = el('div', { class: 'form-group' }, labelEl);
      return {
        row,
        mount: (control) => row.replaceChildren(labelEl, ...(control ? [control] : [])),
        setHidden: (h) => { row.style.display = h ? 'none' : ''; },
      };
    }
    const row = el('label', {});
    return {
      row,
      mount: (control) => row.replaceChildren(labelText, ...(control ? [control] : [])),
      setHidden: (h) => { row.style.display = h ? 'none' : ''; },
    };
  }

  const providerPicker = pickerSelect({
    value: draft.provider_override || '',
    options: providerOptions(liveSettings, draft.provider_override),
    onChange: (v) => {
      draft.provider_override = v || null;
      emit('provider_override', draft.provider_override);
      // Per-provider model memory: don't clear model_overrides — just re-read
      // the row for whichever provider the chat now resolves to.
      renderModelAndContext();
    },
  });

  const providerRow = makeRow('Provider');
  providerRow.mount(providerPicker);
  const modelRow = makeRow('Model');
  const ctxRow = makeRow('Context preset');

  function renderModelAndContext() {
    const slug = resolveChatProvider({ provider_override: draft.provider_override }, liveSettings);
    const generic = !!slug && slug !== 'aetherroom';
    modelRow.setHidden(!generic);
    ctxRow.setHidden(!generic);
    if (!generic) {
      // AER (or no resolvable generic provider): only the Provider picker
      // applies — drop any stale model / context control + its listeners.
      modelRow.mount(null);
      ctxRow.mount(null);
      return;
    }

    const kind = genericKindOf(slug);
    const customId = customIdOf(slug);
    const { wrap: modelWrap } = buildGenericModelPicker({
      kind,
      customId,
      currentValue: draft.model_overrides[slug] || '',
      emptyLabel: "(provider's default)",
      onChange: (v) => {
        if (v) draft.model_overrides[slug] = v;
        else delete draft.model_overrides[slug];
        emit('model_overrides', { ...draft.model_overrides });
      },
    });
    modelRow.mount(modelWrap);

    const ctxPicker = pickerSelect({
      value: draft.context_preset_override || '',
      options: ctxPresetOptions(draft.context_preset_override),
      onChange: (v) => {
        draft.context_preset_override = v || null;
        emit('context_preset_override', draft.context_preset_override);
      },
    });
    ctxRow.mount(ctxPicker);
  }

  renderModelAndContext();

  return [providerRow.row, modelRow.row, ctxRow.row];
}
