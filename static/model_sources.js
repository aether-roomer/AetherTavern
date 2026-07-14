/* Shared low-level primitives for LLM model selection, consumed by both the
 * global Settings inference editor and the per-chat model-override controls.
 *
 * The generic-models cache is a module singleton so a ``/v1/models`` list
 * fetched in one place (Settings) is instantly available in the other (the
 * chat model popover) and vice versa.
 */

import { api } from './api.js';
import { el } from './util.js';
import { icon, toast } from './ui.js';
import { state } from './state.js';
import { makeSelect as pickerSelect } from './avatar_picker.js';


// Canonical provider labels across the unified provider-slug space (AER plus
// every Generic sub-provider). Superset of the names the Settings provider
// dropdown shows.
export const PROVIDER_LABELS = {
  aetherroom: 'AetherRoom',
  novelai: 'NovelAI',
  openrouter: 'OpenRouter',
  nanogpt: 'NanoGPT',
  openai_compatible: 'OpenAI-compatible',
};


// In-memory mirror of the most recent ``/api/generic/models`` fetch, keyed by
// provider-kind plus (for ``openai_compatible``) the custom entry id — so each
// custom provider has its own model list and they don't bleed into one another.
// Named providers key by just the kind.
export const _genericModelsCache = {};

export function modelCacheKey(kind, customId = null) {
  if (kind === 'openai_compatible' && customId) return `openai_compatible:${customId}`;
  return kind;
}


/* Build a searchable model dropdown backed by the shared cache, with lazy
 * ``/v1/models`` discovery on first focus and an optional reload button.
 *
 * Returns ``{ wrap, picker, reloadBtn, refresh }``. ``onChange(value)`` fires
 * with the picked model id (``''`` when the leading empty option is chosen).
 * ``emptyLabel`` (when set) prepends an empty-value option — e.g.
 * ``"(provider's default)"`` for the per-chat override picker; the Settings
 * editor omits it. A stored value not present in the fetched catalog is kept as
 * a fallback option so switching away never silently drops it.
 */
export function buildGenericModelPicker({
  kind,
  customId = null,
  currentValue = '',
  emptyLabel = null,
  canRefresh = true,
  withReload = true,
  placeholder = null,
  onChange = null,
}) {
  const cacheKey = modelCacheKey(kind, customId);
  let current = currentValue || '';

  function modelOptions() {
    const lead = emptyLabel != null ? [{ value: '', label: emptyLabel }] : [];
    const cached = _genericModelsCache[cacheKey];
    if (cached) {
      const opts = cached.map(m => ({ value: m.id, label: m.name || m.id }));
      // Catalogs arrive in upstream order, rarely useful for picking — sort by
      // label so the user can scan.
      opts.sort((a, b) => (a.label || '').localeCompare(b.label || ''));
      if (current && !cached.find(m => m.id === current)) {
        opts.unshift({ value: current, label: current });
      }
      return [...lead, ...opts];
    }
    return [...lead, ...(current ? [{ value: current, label: current }] : [])];
  }

  const picker = pickerSelect({
    value: current,
    options: modelOptions(),
    placeholder: placeholder != null
      ? placeholder
      : (canRefresh ? 'Select model' : '(switch to this provider to fetch models)'),
    searchable: true,
    onChange: (v) => {
      current = v;
      if (onChange) onChange(v);
    },
  });

  let reloadBtn = null;

  async function refresh(force) {
    if (!canRefresh) return;
    if (reloadBtn) reloadBtn.classList.add('spinning');
    try {
      const opts = { refresh: !!force };
      if (kind === 'openai_compatible' && customId) opts.customId = customId;
      const data = await api.getGenericModels(kind, opts);
      _genericModelsCache[cacheKey] = data?.models || [];
      picker.setOptions(modelOptions());
    } catch (err) {
      toast(`Failed to refresh ${kind} models: ${err.message}`, 'error');
    } finally {
      if (reloadBtn) reloadBtn.classList.remove('spinning');
    }
  }

  if (withReload) {
    reloadBtn = el('button', {
      type: 'button',
      class: 'btn ghost tts-reload-btn',
      title: canRefresh ? 'Refresh model list' : 'Switch to this provider first',
      'aria-label': 'Refresh model list',
      onClick: () => refresh(true),
    }, icon('refresh', 14));
    if (!canRefresh) reloadBtn.setAttribute('disabled', '');
  }

  // Lazy fetch: opening the editor shouldn't trigger an upstream round-trip.
  // First focus (or an explicit Reload) is what fetches.
  picker.addEventListener('focusin', () => {
    if (canRefresh && !_genericModelsCache[cacheKey]) refresh(false);
  });

  const wrap = el('div', { class: 'tts-model-pickerwrap' },
    picker, ...(reloadBtn ? [reloadBtn] : []));

  return { wrap, picker, reloadBtn, refresh };
}


export function contextPresetOptions(emptyLabel = '(none)') {
  return [
    { value: '', label: emptyLabel },
    ...((state.contextPresets || []).map(p => ({
      value: p.id,
      label: p.name || '(unnamed)',
    }))),
  ];
}
