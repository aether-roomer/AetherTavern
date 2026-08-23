/* Settings + presets editor (single combined page). */

import { api } from '../api.js';
import { state, setState, subscribe } from '../state.js';
import { el, downloadFromResponse } from '../util.js';
import { icon, helpDetails, confirmModal, toast } from '../ui.js';
import {
  THEMES,
  NAI_V1_PRESETS,
  NAI_V2_PRESETS,
  NAI_CUSTOM_SENTINEL,
} from '../constants.js';
import { makeSelect as pickerSelect, makeAvatarPicker } from '../avatar_picker.js';
import { buildGenericModelPicker, contextPresetOptions } from '../model_sources.js';
import { importWithProgress } from './import_progress.js';
import { markPending, clearPending } from '../save_queue.js';
import {
  getTTSModels,
  getCachedTTSModels,
  isCacheStale,
} from '../tts_discovery.js';
import { resolveTTSConfigFromGlobal, isProviderKeyConfigured } from '../tts_helpers.js';
import { renderTTSTestBlock } from './tts_test_block.js';
import { playNotification } from '../notification.js';


const TTS_KEY_SENTINEL = '__present__';

function naiLabel(p) { return `${p.name} (${p.gender})`; }


// Canonical inference targets surfaced as the dropdown options. Anything in
// the persisted settings that doesn't match one of these is appended to the
// options so the user can still see / re-select what they had configured.
const KNOWN_ENDPOINTS = ['https://text.novelai.net/oa'];
const KNOWN_MODELS = ['xialong-v1'];

function _selectWithDivergent(current, knownValues, onChange) {
  const options = [...knownValues];
  const c = (current || '').trim();
  if (c && !options.includes(c)) options.push(c);
  return pickerSelect({ value: c, options, onChange });
}


export async function renderSettingsTab(container) {
  container.replaceChildren();
  container.classList.add('full');

  const main = el('div', { class: 'content-pane' });
  const header = el('div', { class: 'page-header' }, el('h2', {}, 'Settings'));
  const scroll = el('div', { class: 'page-scroll' });
  const body = el('div', { class: 'page-body' });
  scroll.append(body);
  main.append(header, scroll);
  container.append(main);

  const settings = state.settings || await api.getSettings();
  const presets = state.presets || await api.listPresets();
  setState({ settings, presets });

  body.append(renderInferenceSection(settings));
  body.append(renderImageGenerationSection(settings));
  body.append(renderAppearanceSection(settings));
  body.append(renderNotificationsSection(settings));
  body.append(renderBudgetsSection(settings));
  body.append(renderTTSSection(settings));
  body.append(renderBulkImportSection());
  body.append(renderPresetsSection(presets, settings));
}


function renderImageGenerationSection(settings) {
  const draft = JSON.parse(JSON.stringify(settings.image_generation || {}));
  const save = debounce(() => patchSettings({ image_generation: draft }));
  const input = (key, opts = {}) => el(opts.tag || 'input', {
    ...(opts.tag === 'textarea' ? { rows: opts.rows || 4 } : { type: opts.type || 'text' }),
    value: draft[key] ?? '',
    placeholder: opts.placeholder || '',
    min: opts.min,
    max: opts.max,
    step: opts.step,
    onInput: (e) => {
      draft[key] = opts.number ? Number(e.target.value) : e.target.value;
      save();
    },
  });
  const field = (label, key, opts = {}) => el('div', { class: 'form-group' },
    el('label', {}, label),
    input(key, opts),
    opts.hint ? el('div', { class: 'hint' }, opts.hint) : null,
  );

  return el('div', { class: 'section' },
    el('h3', {}, 'Image generation'),
    el('p', { style: { color: 'var(--text-mute)', fontSize: '13px', margin: '0 0 14px' } },
      'The chat image workflow uses your NovelAI token. Prompt reasoning and image generation are separate, explicit actions.'),
    el('div', { class: 'form-grid-2' },
      field('Image API base URL', 'base_url', { type: 'url' }),
      field('Image model', 'model'),
      field('Prompt reasoning model', 'prompt_model', {
        hint: 'Blank inherits the configured NovelAI text model, then the AER default. An inherited xialong-v1 uses glm-4-6 for image prompting.',
      }),
      field('Sampler', 'sampler'),
    ),
    el('div', { class: 'form-grid-3', style: { marginTop: '18px' } },
      field('Width', 'width', { type: 'number', number: true, min: 64, max: 2048, step: 64 }),
      field('Height', 'height', { type: 'number', number: true, min: 64, max: 2048, step: 64 }),
      field('Steps', 'steps', { type: 'number', number: true, min: 1, max: 50, step: 1 }),
      field('Guidance scale', 'scale', { type: 'number', number: true, min: 0, max: 20, step: .1 }),
      field('Total reasoning output limit', 'prompt_max_tokens', {
        type: 'number', number: true, min: 128, max: 8192, step: 128,
        hint: 'Includes visible <think> output and the final prompt. The default leaves room for a final prompt up to 1,471 Qwen 3.5 tokens.',
      }),
    ),
    field('Image system prompt', 'system_prompt', {
      tag: 'textarea', rows: 12,
      hint: 'Controls scene analysis, continuity checks, and the <image_prompt> output contract.',
    }),
    field('Image user message', 'user_message', {
      tag: 'textarea', rows: 5,
      hint: 'Sent after the active scene context on the first reasoning request.',
    }),
    field('UC (undesired content)', 'negative_prompt', {
      tag: 'textarea', rows: 5,
      hint: 'Sent directly to NovelAI as both the v4 negative caption and negative_prompt.',
    }),
  );
}


function renderNotificationsSection(settings) {
  // Re-renders the whole section after upload / delete so the file
  // picker label + revert button reflect the new state.
  const section = el('div', { class: 'section' });

  function paint() {
    section.replaceChildren();
    const cur = state.settings || settings;
    section.append(el('h3', {}, 'Notifications'));

    section.append(el('div', { class: 'form-group checkbox' },
      el('input', {
        type: 'checkbox',
        id: 'notify-on-complete',
        checked: !!cur.notify_on_complete,
        onChange: (e) => patchSettings({ notify_on_complete: !!e.target.checked }),
      }),
      el('label', { for: 'notify-on-complete' }, 'Play notification sound'),
    ));

    const fileInputId = 'notify-sound-input';
    section.append(el('div', { class: 'form-group', style: { marginTop: '12px' } },
      el('label', {}, 'Sound (optional)'),
      el('div', { style: { display: 'flex', gap: '6px', alignItems: 'center', flexWrap: 'wrap' } },
        // A <button> that clicks the hidden file input, NOT a <label for=…>: a
        // <label> inside a .form-group inherits the uppercase / display:block /
        // margin headline styling from `.form-group label`, which clobbered
        // this trigger (uppercase text, taller than the sibling buttons). As a
        // plain .btn button it matches "Test" / "Revert" exactly.
        el('button', {
          class: 'btn',
          type: 'button',
          onClick: () => document.getElementById(fileInputId).click(),
        },
          icon('upload', 14),
          cur.notification_sound ? 'Replace…' : 'Choose audio…',
        ),
        el('input', {
          id: fileInputId,
          type: 'file',
          accept: 'audio/*',
          class: 'hidden',
          onChange: async (e) => {
            const f = e.target.files[0];
            if (!f) return;
            try {
              await api.uploadNotificationSound(f);
              const fresh = await api.getSettings();
              setState({ settings: fresh });
              toast('Notification sound saved.', 'success');
              paint();
            } catch (err) {
              toast(`Upload failed: ${err.message}`, 'error');
            }
            e.target.value = '';
          },
        }),
        el('button', {
          class: 'btn',
          onClick: () => playNotification(),
          title: 'Play the current sound',
        }, icon('play', 14), 'Test'),
        cur.notification_sound
          ? el('button', {
              class: 'btn ghost',
              onClick: async () => {
                try {
                  await api.deleteNotificationSound();
                  const fresh = await api.getSettings();
                  setState({ settings: fresh });
                  toast('Reverted to default chime.', 'success');
                  paint();
                } catch (err) {
                  toast(`Revert failed: ${err.message}`, 'error');
                }
              },
            }, 'Revert to default')
          : null,
      ),
    ));
  }

  paint();
  return section;
}


function renderBulkImportSection() {
  // Bulk imports — the same flow also runs from the Contacts tab's
  // Upload button when given a .zip. This section is the discoverable
  // entry point.
  return el('div', { class: 'section' },
    el('h3', {}, 'Bulk import'),
    el('p', { style: { color: 'var(--text-mute)', fontSize: '13px', margin: '0 0 12px' } },
      'Import a ', el('code', {}, '.zip'),
      ' archive of contact, user, scenario, and/or chat JSON exports —',
      ' many at once via a picker. Existing entries (matched by ID)',
      ' default to skip; you can opt to replace or import as a new copy',
      ' per row.',
    ),
    el('div', { style: { display: 'flex', gap: '6px', alignItems: 'center' } },
      el('label', { class: 'btn', for: 'bulk-import-input',
                    style: { cursor: 'pointer' } },
        icon('upload', 14), 'Choose .zip…'),
      el('input', {
        id: 'bulk-import-input', type: 'file', accept: '.zip', class: 'hidden',
        onChange: async (e) => {
          const f = e.target.files[0];
          if (!f) return;
          try {
            const r = await importWithProgress(f, { title: 'Bulk import' });
            if (r && r.imported) {
              const i = r.imported;
              toast(
                `Imported ${i.contacts} contact${i.contacts === 1 ? '' : 's'}, `
                + `${i.scenarios} scenario${i.scenarios === 1 ? '' : 's'}, `
                + `${i.chats} chat${i.chats === 1 ? '' : 's'}.`,
                'success',
              );
              // Refresh global lists so newly imported entities show up
              // immediately in the Contacts / Chats tabs.
              const [contacts, chats] = await Promise.all([
                api.listContacts(), api.listChats(),
              ]);
              setState({ contacts, chats });
            }
          } catch (err) {
            if (!err.cancelled) toast(`Import failed: ${err.message}`, 'error');
          }
          e.target.value = '';
        },
      }),
    ),
  );
}


/* =================================================================
 * Inference section — AER vs Generic, tabbed.
 *
 * One ``draft`` deep-cloned from ``state.settings`` covers BOTH tabs
 * (AER's endpoint_url + api_token + default_model, plus the entire
 * ``generic`` block). One debounced ``save`` flushes the whole section.
 * Token state lives in a ``pendingTokens`` map keyed by:
 *   - ``'aer'`` for ``Settings.api_token``
 *   - ``'generic:<provider>'`` for the three named generic configs
 *   - ``'generic:custom:<id>'`` for each custom OpenAI-compatible entry
 * The sentinel ``'__present__'`` round-trips for any token field whose
 * value isn't being explicitly changed. Mirrors the TTS section's
 * ``pendingKeys`` discipline; see ``buildTTSPayload`` for the canonical
 * pattern.
 * ================================================================= */


const GENERIC_PROVIDER_LABELS = {
  novelai: 'NovelAI',
  openrouter: 'OpenRouter',
  nanogpt: 'NanoGPT',
};


function renderInferenceSection(settings) {
  const root = el('div', { class: 'section' });

  // Deep clone so mutations (e.g. draft.generic.novelai.model_id = …)
  // don't leak into state.settings and contaminate other views. Mirrors
  // the TTS section's JSON-clone at settings.js:460. ``original`` is a
  // second, never-mutated clone the save diffs ``draft`` against to learn
  // which top-level fields actually changed.
  const live = state.settings || settings;
  const original = JSON.parse(JSON.stringify(live));
  const draft = JSON.parse(JSON.stringify(live));
  const pendingTokens = {};

  const save = debounce(async () => {
    await saveInferenceDraft(draft, original, pendingTokens);
    // After save, sync indicators back from the fresh state so
    // subsequent sentinel-preserve saves stay correct (mirror of
    // saveTTSDraft).
    const fresh = state.settings;
    if (fresh) {
      draft.api_token_indicator = fresh.api_token_indicator || '';
      const fg = fresh.generic || {};
      const dg = draft.generic;
      for (const p of ['novelai', 'openrouter', 'nanogpt']) {
        dg[p].api_token_indicator = fg[p]?.api_token_indicator || '';
      }
      const freshCustom = fg.openai_compatible?.custom_providers || [];
      for (const cur of freshCustom) {
        const local = (dg.openai_compatible?.custom_providers || []).find(c => c.id === cur.id);
        if (local) local.api_token_indicator = cur.api_token_indicator;
      }
    }
    for (const k of Object.keys(pendingTokens)) delete pendingTokens[k];
  }, 600);

  function rerender() {
    root.replaceChildren();

    // AER / Generic tab switcher — doubles as the section header. The
    // active button reads like an "Inference: AetherRoom" / "Inference:
    // Generic" title; clicking the other label switches modes and
    // re-renders the body.
    const tabs = el('div', { class: 'inference-tabs' });
    for (const m of ['aetherroom', 'generic']) {
      tabs.append(el('button', {
        type: 'button',
        class: 'inference-tab' + (draft.provider_mode === m ? ' active' : ''),
        onClick: () => {
          if (draft.provider_mode === m) return;
          draft.provider_mode = m;
          save();
          rerender();
        },
      }, m === 'aetherroom' ? 'AetherRoom' : 'Generic'));
    }
    root.append(tabs);

    if (draft.provider_mode === 'aetherroom') {
      renderAERTab(root, draft, pendingTokens, save, rerender);
    } else {
      renderGenericTab(root, draft, pendingTokens, save, rerender);
    }
  }

  rerender();
  return root;
}


function renderAERTab(root, draft, pendingTokens, save, rerender) {
  // Endpoint URL — canonical option + the user's prior value when it
  // diverges, so changing the field to "" doesn't lose the previous URL.
  root.append(el('div', { class: 'form-group' },
    el('label', {}, 'Endpoint URL'),
    _selectWithDivergent(draft.endpoint_url, KNOWN_ENDPOINTS, v => {
      draft.endpoint_url = v; save();
    }),
    el('div', { class: 'hint' }, 'No trailing /v1/completions — just the base URL.'),
  ));

  // API token — sentinel-aware. Reuse ttsKeyInput for the password
  // input + X clear button.
  const tokenRow = ttsKeyInput(
    draft.api_token_indicator,
    'Bearer token',
    (v) => { pendingTokens.aer = v; save(); },
    () => {
      pendingTokens.aer = '';
      draft.api_token_indicator = '';
      save();
      rerender();
    },
  );
  root.append(el('div', { class: 'form-group' },
    el('label', {}, 'API token'),
    tokenRow,
  ));

  root.append(el('div', { class: 'form-group' },
    el('label', {}, 'Default model'),
    _selectWithDivergent(draft.default_model, KNOWN_MODELS, v => {
      draft.default_model = v; save();
    }),
  ));
}


function renderGenericTab(root, draft, pendingTokens, save, rerender) {
  const g = draft.generic;

  // Auto-pick the single custom entry when openai_compatible is the
  // active provider but no entry is selected. Same shortcut TTS uses
  // for the bare-generic state.
  if (
    g.provider === 'openai_compatible'
    && !g.openai_compatible.active_id
    && g.openai_compatible.custom_providers?.length === 1
  ) {
    g.openai_compatible.active_id = g.openai_compatible.custom_providers[0].id;
    save();
  }

  // Active-provider dropdown — bare "OpenAI-compatible (custom)" + each
  // custom entry nested below as ``↳ <label>``. Same encoding scheme as
  // the TTS section's ``generic:<id>`` (here ``openai_compatible:<id>``).
  const providerOptions = [
    { value: 'novelai',                label: 'NovelAI' },
    { value: 'openrouter',             label: 'OpenRouter' },
    { value: 'nanogpt',                label: 'NanoGPT' },
    { value: 'openai_compatible:',     label: 'OpenAI-compatible (custom)' },
    ...(g.openai_compatible.custom_providers || []).map(c => ({
      value: `openai_compatible:${c.id}`,
      label: `↳ ${c.label || 'Custom provider'}`,
    })),
  ];
  let activeValue;
  if (g.provider === 'openai_compatible') {
    activeValue = `openai_compatible:${g.openai_compatible.active_id || ''}`;
  } else {
    activeValue = g.provider;
  }
  const activeSel = pickerSelect({
    value: activeValue,
    options: providerOptions,
    onChange: (v) => {
      if (v.startsWith('openai_compatible:')) {
        g.provider = 'openai_compatible';
        g.openai_compatible.active_id = v.slice('openai_compatible:'.length) || null;
      } else {
        g.provider = v;
      }
      save();
      rerender();
    },
  });
  root.append(el('div', { class: 'form-group' },
    el('label', {}, 'Active provider'),
    activeSel,
  ));

  if (g.provider !== 'openai_compatible') {
    renderNamedProviderFields(root, draft, g.provider, pendingTokens, save, rerender);
  } else {
    renderCustomProvidersList(root, draft, pendingTokens, save, rerender);
  }

  // HTML sanitizer toggle. Applies to Generic-mode assistant messages
  // only — AER's renderer is hardwired escape-then-emit. When off, raw
  // HTML the model emits (links, images, scripts) reaches the DOM; same
  // risk profile as opening a random web page from the model.
  const sanitizeOn = draft.sanitize_generic_html !== false;
  const sanitizeCheckbox = el('input', {
    type: 'checkbox',
    id: 'sanitize-generic-html',
    checked: sanitizeOn,
    onChange: (e) => {
      draft.sanitize_generic_html = !!e.target.checked;
      save();
    },
  });
  sanitizeCheckbox.checked = sanitizeOn;
  root.append(el('div', { class: 'form-group checkbox' },
    sanitizeCheckbox,
    el('label', { for: 'sanitize-generic-html' }, 'Sanitize HTML'),
    helpDetails(
      el('div', {},
        'When on (default), the markdown renderer escapes HTML before parsing so '
        + 'no raw ', el('code', {}, '<script>'), ' / ', el('code', {}, '<a>'),
        ' / ', el('code', {}, '<img>'),
        ' outside markdown-generated ones reach the DOM. Turn off only if you trust the model to emit safe HTML.',
      ),
      { label: 'About HTML sanitization' },
    ),
  ));

  // Image compression (Generic multimodal). When enabled, image
  // attachments are re-encoded as JPEG at the chosen quality before being
  // sent to the provider; the quality box reveals only while it's on.
  const compressOn = draft.compress_images === true;

  const qualityInput = el('input', {
    type: 'number',
    lang: 'en-US',
    min: '1',
    max: '100',
    step: '1',
    value: String(draft.image_compression_quality ?? 85),
    // Tolerate intermediate empty / out-of-range states (CLAUDE.md): only
    // commit a finite, in-range value on input; clamp on commit (blur).
    oninput: (e) => {
      const n = parseInt(e.target.value, 10);
      if (!Number.isFinite(n) || n < 1 || n > 100) return;
      draft.image_compression_quality = n;
      save();
    },
    onChange: (e) => {
      let n = parseInt(e.target.value, 10);
      if (!Number.isFinite(n)) n = 85;
      n = Math.max(1, Math.min(100, n));
      draft.image_compression_quality = n;
      e.target.value = String(n);
      save();
    },
  });
  const qualityRow = el('div', { class: 'form-group', style: { marginTop: '8px' } },
    el('label', {}, 'JPEG quality'),
    qualityInput,
  );
  qualityRow.style.display = compressOn ? '' : 'none';

  const compressCheckbox = el('input', {
    type: 'checkbox',
    id: 'compress-images',
    checked: compressOn,
    onChange: (e) => {
      const on = !!e.target.checked;
      draft.compress_images = on;
      qualityRow.style.display = on ? '' : 'none';
      save();
    },
  });
  compressCheckbox.checked = compressOn;
  root.append(el('div', { class: 'form-group checkbox' },
    compressCheckbox,
    el('label', { for: 'compress-images' }, 'Compress images'),
    helpDetails(
      el('div', {},
        'When on, image attachments are re-encoded as JPEG at the quality '
        + 'set below (default 85) before being sent to the model — shrinking '
        + 'large photo uploads substantially. If the original file is already '
        + 'smaller (e.g. a simple PNG), it is sent unchanged. Originals are '
        + 'always kept on disk; compressed copies are cached, regenerated as '
        + 'needed, and never exported.',
      ),
      { label: 'About image compression' },
    ),
  ));
  root.append(qualityRow);
}


function renderNamedProviderFields(root, draft, providerKind, pendingTokens, save, rerender) {
  const g = draft.generic;
  const cfg = g[providerKind];
  const keyId = `generic:${providerKind}`;

  const wrap = el('div', { class: 'tts-provider-config' });
  root.append(wrap);

  // Base URL row. NovelAI exposes a divergent dropdown so users can
  // point at an alternate endpoint without losing the canonical one;
  // OpenRouter and NanoGPT have no alternate endpoints, so the row is
  // omitted entirely rather than rendered as a read-only badge.
  if (providerKind === 'novelai') {
    wrap.append(el('div', { class: 'form-group' },
      el('label', {}, 'Base URL'),
      _selectWithDivergent(
        cfg.base_url,
        ['https://text.novelai.net/oa'],
        v => { cfg.base_url = v; save(); },
      ),
    ));
  }

  // API token. NAI gets a smarter placeholder reflecting the fallback chain.
  let tokenPlaceholder = `${GENERIC_PROVIDER_LABELS[providerKind]} API token`;
  if (providerKind === 'novelai' && cfg.api_token_indicator !== TTS_KEY_SENTINEL) {
    if (draft.api_token_indicator === TTS_KEY_SENTINEL) {
      tokenPlaceholder = '(using AER token — leave blank to keep)';
    } else {
      tokenPlaceholder = '(not set — generation will fail)';
    }
  }
  const tokenRow = ttsKeyInput(
    cfg.api_token_indicator,
    tokenPlaceholder,
    (v) => { pendingTokens[keyId] = v; save(); },
    () => {
      pendingTokens[keyId] = '';
      cfg.api_token_indicator = '';
      save();
      rerender();
    },
  );
  wrap.append(el('div', { class: 'form-group' },
    el('label', {}, 'API token'),
    tokenRow,
  ));

  renderGenericProviderBodyFields(wrap, draft, cfg, providerKind, save);
}


function renderCustomProvidersList(root, draft, pendingTokens, save, rerender) {
  const g = draft.generic;
  const list = g.openai_compatible.custom_providers || [];

  const wrap = el('div', { class: 'tts-custom-list' });
  root.append(wrap);

  wrap.append(el('div', { class: 'tts-custom-list-header' },
    el('h4', { style: { margin: 0 } }, 'OpenAI-compatible endpoints'),
    el('button', { class: 'btn primary', onClick: () => {
      const id = `custom_${Math.random().toString(36).slice(2, 10)}`;
      g.openai_compatible.custom_providers.push({
        id,
        label: 'Custom provider',
        base_url: '',
        api_token_indicator: '',
        model_id: '',
        cache_minutes: null,
        streaming: true,
        brain_message_role: 'system',
        context_preset_id: null,
      });
      // First entry auto-picks (parallel of TTS's ``custom_apis.length === 1``).
      if (g.openai_compatible.custom_providers.length === 1) {
        g.openai_compatible.active_id = id;
      }
      save();
      rerender();
    } }, icon('plus', 14), 'Add custom provider'),
  ));

  if (!list.length) {
    wrap.append(el('p', {
      style: { color: 'var(--text-mute)', fontSize: '13px', margin: '8px 0 0' },
    }, 'Point your own OpenAI-compatible LLM endpoint here.'));
    return;
  }

  for (const entry of list) {
    wrap.append(renderCustomProviderCard(entry, draft, pendingTokens, save, rerender));
  }
}


function renderCustomProviderCard(entry, draft, pendingTokens, save, rerender) {
  const g = draft.generic;
  const isActive = g.openai_compatible.active_id === entry.id;
  const keyId = `generic:custom:${entry.id}`;

  const nameInput = el('input', {
    class: 'preset-card-name',
    type: 'text',
    value: entry.label || '',
    placeholder: 'Provider name',
    // ``onInput`` rerenders would tear down this very input on every
    // keystroke (the surrounding card lives in the same subtree). Defer
    // the rerender to ``onChange`` (blur / commit) so the active-provider
    // dropdown picks up the new ``↳ <label>`` when the user is done.
    onInput: (e) => { entry.label = e.target.value; save(); },
    onChange: () => { rerender(); },
  });

  const urlInput = el('input', {
    type: 'text',
    value: entry.base_url || '',
    placeholder: 'https://example.com',
    onInput: (e) => { entry.base_url = e.target.value; save(); },
  });

  const tokenRow = ttsKeyInput(
    entry.api_token_indicator,
    'API token',
    (v) => { pendingTokens[keyId] = v; save(); },
    () => {
      pendingTokens[keyId] = '';
      entry.api_token_indicator = '';
      save();
      rerender();
    },
  );

  const headerActions = el('div', { class: 'preset-card-actions' });
  if (isActive) {
    headerActions.append(el('span', {
      class: 'chip preset-default-chip',
      title: 'Currently the active LLM provider',
    }, '✓ active'));
  } else {
    headerActions.append(el('button', { class: 'btn', onClick: () => {
      g.openai_compatible.active_id = entry.id;
      save();
      rerender();
    } }, 'Set as active'));
  }
  headerActions.append(el('button', {
    class: 'btn ghost danger',
    title: 'Delete custom provider',
    onClick: async () => {
      const hasToken = entry.api_token_indicator === TTS_KEY_SENTINEL
        || (keyId in pendingTokens && pendingTokens[keyId]);
      if (hasToken) {
        const ok = await confirmModal(
          'Delete custom provider?',
          `${entry.label || 'This entry'} and its saved API token will be removed.`,
          { danger: true },
        );
        if (!ok) return;
      }
      const i = g.openai_compatible.custom_providers.indexOf(entry);
      if (i >= 0) g.openai_compatible.custom_providers.splice(i, 1);
      // If the deleted entry was active, clear active_id so the next
      // render doesn't try to resolve against a missing entry. Mirrors
      // the TTS section's pattern.
      if (g.openai_compatible.active_id === entry.id) {
        g.openai_compatible.active_id = null;
      }
      delete pendingTokens[keyId];
      save();
      rerender();
    },
  }, icon('trash', 14)));

  const card = el('div', {
    class: 'section preset-card' + (isActive ? ' preset-card-default' : ''),
  },
    el('div', { class: 'preset-card-header' },
      el('div', { class: 'preset-card-meta' }, nameInput),
      headerActions,
    ),
    el('div', { class: 'form-group' },
      el('label', {}, 'Base URL'),
      urlInput,
    ),
    el('div', { class: 'form-group' },
      el('label', {}, 'API token'),
      tokenRow,
    ),
  );

  renderGenericProviderBodyFields(card, draft, entry, 'openai_compatible', save);
  return card;
}


/* Shared body fields between named-provider configs and custom-provider
 * cards. Renders Model + Cache + Streaming + Brain role + Context Preset.
 *
 * ``providerKind`` is the value passed to ``api.getGenericModels`` etc.;
 * the model-discovery route only works against the *active* provider
 * (since it reads from the currently-selected token / base URL on the
 * server), so non-active cards just show their existing model_id as
 * the single dropdown option.
 */
function renderGenericProviderBodyFields(parent, draft, cfg, providerKind, save) {
  const g = draft.generic;
  const isActive = providerKind !== 'openai_compatible'
    ? g.provider === providerKind
    : (g.provider === 'openai_compatible'
       && g.openai_compatible.active_id
       && g.openai_compatible.custom_providers.find(c => c.id === g.openai_compatible.active_id) === cfg);
  // Custom providers can be queried by id, so refresh works without
  // having to flip the active entry first. Named providers still only
  // resolve via the active selection.
  const isCustom = providerKind === 'openai_compatible';
  const canRefresh = isActive || isCustom;
  // ---- Model dropdown + reload button ---------------------------------
  // Shared builder (also drives the per-chat model-override popover); the
  // generic-models cache is a module singleton so a list fetched here is
  // instantly available in the chat picker and vice versa.
  const { wrap: modelWrap } = buildGenericModelPicker({
    kind: providerKind,
    customId: isCustom ? cfg.id : null,
    currentValue: cfg.model_id || '',
    canRefresh,
    onChange: (v) => {
      cfg.model_id = v;
      save();
      // Cache options may yield a different per-model option set —
      // repaint the cache row when model changes.
      paintCache();
    },
  });

  parent.append(el('div', { class: 'form-group' },
    el('label', {}, 'Model'),
    modelWrap,
  ));

  // ---- Cache dropdown -------------------------------------------------
  //
  // Only OR and NGPT expose provider-side prompt caching. NovelAI
  // generic and bare openai-compatible (where we don't know the
  // upstream's capabilities) skip the row entirely. For OR, the row
  // mounts only once hints come back AND the selected model is known
  // cache-capable; for NGPT, it always mounts (static tiers).

  const showsCache = providerKind === 'openrouter' || providerKind === 'nanogpt';
  let paintCache = () => {};
  if (showsCache) {
    const cacheRow = el('div', { class: 'form-group', style: { display: 'none' } });
    parent.append(cacheRow);
    paintCache = async () => {
      let hints = { options: [{ value: null, label: 'No cache' }], default: null };
      try {
        hints = await api.getGenericCacheHints(providerKind, cfg.model_id || '');
      } catch { /* keep the safe fallback */ }
      const realCacheChoice = hints.options.some(o => o.value !== null);
      if (!realCacheChoice) {
        cacheRow.replaceChildren();
        cacheRow.style.display = 'none';
        return;
      }
      cacheRow.style.display = '';
      cacheRow.replaceChildren(
        el('label', {}, 'Cache duration'),
        pickerSelect({
          value: cfg.cache_minutes === null ? '__none__' : String(cfg.cache_minutes),
          options: hints.options.map(o => ({
            value: o.value === null ? '__none__' : String(o.value),
            label: o.label,
          })),
          onChange: (v) => {
            cfg.cache_minutes = v === '__none__' ? null : parseInt(v, 10);
            save();
          },
        }),
      );
    };
    paintCache();
  }

  // ---- Streaming toggle ----------------------------------------------

  const streamSeg = el('div', { class: 'tts-segmented' });
  for (const [val, lbl] of [[true, 'On'], [false, 'Off']]) {
    streamSeg.append(el('button', {
      type: 'button',
      class: 'tts-seg-btn' + (!!cfg.streaming === val ? ' active' : ''),
      onClick: () => {
        cfg.streaming = val;
        save();
        // Refresh just this toggle visually.
        for (const b of streamSeg.children) b.classList.remove('active');
        streamSeg.children[val ? 0 : 1].classList.add('active');
      },
    }, lbl));
  }
  parent.append(el('div', { class: 'form-group' },
    el('label', {}, 'Streaming'),
    streamSeg,
  ));

  // ---- Brain message role --------------------------------------------

  parent.append(el('div', { class: 'form-group' },
    el('label', {}, 'Reminder / dynamic brain message role'),
    pickerSelect({
      value: cfg.brain_message_role || 'system',
      options: [
        { value: 'system',    label: 'System' },
        { value: 'user',      label: 'User' },
        { value: 'assistant', label: 'Assistant' },
      ],
      onChange: (v) => { cfg.brain_message_role = v; save(); },
    }),
  ));

  // ---- Context preset ------------------------------------------------

  parent.append(el('div', { class: 'form-group' },
    el('label', {}, 'Context preset'),
    pickerSelect({
      value: cfg.context_preset_id || '',
      options: contextPresetOptions('(none)'),
      placeholder: 'None',
      onChange: (v) => { cfg.context_preset_id = v || null; save(); },
    }),
  ));
}


/* Translate a SettingsView's `generic` block (where api_tokens are
 * indicators) into a GenericSettings PUT payload that preserves tokens
 * via sentinel. Mirrors ``buildTTSPayload``. */
function buildGenericPayload(draft, pendingTokens) {
  function resolveToken(indicator, keyId) {
    if (keyId in pendingTokens) return pendingTokens[keyId];
    return indicator === TTS_KEY_SENTINEL ? TTS_KEY_SENTINEL : '';
  }
  const g = draft.generic;
  function named(cfg, providerKind) {
    return {
      base_url: cfg.base_url || '',
      api_token: resolveToken(cfg.api_token_indicator, `generic:${providerKind}`),
      model_id: cfg.model_id || '',
      cache_minutes: cfg.cache_minutes ?? null,
      streaming: !!cfg.streaming,
      brain_message_role: cfg.brain_message_role || 'system',
      context_preset_id: cfg.context_preset_id || null,
    };
  }
  return {
    provider: g.provider,
    novelai: named(g.novelai, 'novelai'),
    openrouter: named(g.openrouter, 'openrouter'),
    nanogpt: named(g.nanogpt, 'nanogpt'),
    openai_compatible: {
      active_id: g.openai_compatible.active_id || null,
      custom_providers: (g.openai_compatible.custom_providers || []).map(e => ({
        id: e.id,
        label: e.label || '',
        base_url: e.base_url || '',
        api_token: resolveToken(e.api_token_indicator, `generic:custom:${e.id}`),
        model_id: e.model_id || '',
        cache_minutes: e.cache_minutes ?? null,
        streaming: !!e.streaming,
        brain_message_role: e.brain_message_role || 'system',
        context_preset_id: e.context_preset_id || null,
      })),
    },
  };
}


async function saveInferenceDraft(draft, original, pendingTokens) {
  // ``generic`` is always sent: the backend replaces the block as a unit,
  // and buildGenericPayload re-seals unchanged tokens via the
  // ``__present__`` sentinel, so re-sending it is idempotent.
  const patch = { generic: buildGenericPayload(draft, pendingTokens) };

  // Persist whichever plain top-level fields diverged from the snapshot
  // taken when this editor opened. Diffing — rather than naming each
  // field — means a control added to the AER / Generic tabs later
  // persists with no extra wiring here, and fields owned by other
  // settings sections (theme, fonts, tts…) are never clobbered: they
  // don't change within this draft, so they never enter the patch.
  // Nested blocks (generic / tts) and view-only secret indicators are
  // excluded — tokens flow through pendingTokens / buildGenericPayload.
  for (const [k, v] of Object.entries(draft)) {
    if (v !== null && typeof v === 'object') continue;
    if (k.endsWith('_indicator')) continue;
    if (v !== original[k]) patch[k] = v;
  }

  // ``api_token`` is only included when the user has explicitly edited
  // the AER token in this session. ``''`` means clear; anything else
  // sets; absence means preserve.
  if ('aer' in pendingTokens) patch.api_token = pendingTokens.aer;
  markPending('settings', 'settings', patch);
  try {
    const updated = await api.putSettings(patch);
    clearPending('settings', 'settings');
    setState({ settings: updated });
  } catch (e) {
    toast(`Save failed: ${e.message}`, 'error');
  }
}


function applySettingsToDom(s) {
  document.documentElement.dataset.theme = s.theme;
  document.documentElement.style.setProperty('--ui-font-size', `${s.font_size}px`);
  document.documentElement.style.setProperty('--content-font-size', `${s.content_font_size}px`);
  // null / undefined ⇒ unlimited (no cap). ``100%`` (not ``none``) keeps the
  // value usable in the `.msg.contact` controls-alignment calc; for the bubble
  // itself the two are equivalent.
  document.documentElement.style.setProperty(
    '--bubble-max-width',
    s.max_bubble_width_em == null ? '100%' : `${s.max_bubble_width_em}em`,
  );
}


async function patchSettings(patch) {
  markPending('settings', 'settings', patch);
  try {
    const updated = await api.putSettings(patch);
    clearPending('settings', 'settings');
    setState({ settings: updated });
    applySettingsToDom(updated);
  } catch (e) { toast(`Save failed: ${e.message}`, 'error'); }
}


function renderAppearanceSection(settings) {
  function sizeSelect(key, current) {
    return pickerSelect({
      value: String(current),
      options: [12, 13, 14, 15, 16, 18, 20].map(n => ({ value: String(n), label: `${n}px` })),
      onChange: (v) => patchSettings({ [key]: +v }),
    });
  }

  return el('div', { class: 'section' },
    el('h3', {}, 'Appearance'),
    el('div', { class: 'form-group' },
      el('label', {}, 'Theme'),
      themePicker(settings),
    ),
    el('div', { class: 'form-grid', style: { marginTop: '14px' } },
      el('div', { class: 'form-group' },
        el('label', {}, 'UI font size'),
        sizeSelect('font_size', settings.font_size),
      ),
      el('div', { class: 'form-group' },
        el('label', {}, 'Chat font size'),
        sizeSelect('content_font_size', settings.content_font_size),
      ),
    ),
    bubbleWidthControl(settings),
  );
}


// Max chat-bubble width slider. The track runs 20–140 em with one extra tick
// past 140 for "Unlimited" (no cap — the default and prior behaviour). em is
// relative to the chat font size, so the cap tracks the line length the reader
// actually sees. Persist on release (change); update the readout live (input).
const BUBBLE_WIDTH_UNLIMITED = 141;

function bubbleWidthControl(settings) {
  const fmt = (v) => (v >= BUBBLE_WIDTH_UNLIMITED ? 'Unlimited' : `${v} em`);
  const initial = settings.max_bubble_width_em == null
    ? BUBBLE_WIDTH_UNLIMITED
    : settings.max_bubble_width_em;

  const readout = el('span', { class: 'range-value' }, fmt(initial));
  const slider = el('input', {
    type: 'range',
    min: '20',
    max: String(BUBBLE_WIDTH_UNLIMITED),
    step: '1',
    value: String(initial),
    'aria-label': 'Maximum chat bubble width',
    oninput: (e) => { readout.textContent = fmt(+e.target.value); },
    onChange: (e) => {
      const v = +e.target.value;
      patchSettings({
        max_bubble_width_em: v >= BUBBLE_WIDTH_UNLIMITED ? null : v,
      });
    },
  });

  return el('div', { class: 'form-group', style: { marginTop: '14px' } },
    el('label', {}, 'Max chat bubble width',
      helpDetails(
        'Caps how wide chat bubbles grow, in em (relative to the chat font '
        + 'size). Long lines are easier to read when bubbles do not stretch '
        + 'across a wide window. Slide fully right for Unlimited — no cap, '
        + 'the default.',
      ),
    ),
    el('div', { class: 'range-row' }, slider, readout),
  );
}


function themePicker(settings) {
  const grid = el('div', { class: 'theme-picker' });
  const tilesById = new Map();

  function setSelected(themeId) {
    for (const [id, tile] of tilesById) {
      const on = id === themeId;
      tile.classList.toggle('selected', on);
      tile.setAttribute('aria-pressed', on ? 'true' : 'false');
    }
  }

  function buildTile(t) {
    const tile = el('button', {
      type: 'button',
      class: 'theme-tile' + (settings.theme === t.id ? ' selected' : ''),
      'data-theme': t.id,
      title: t.name,
      'aria-label': `${t.name} theme`,
      'aria-pressed': settings.theme === t.id ? 'true' : 'false',
      onClick: async () => {
        if ((state.settings && state.settings.theme) === t.id) return;
        await patchSettings({ theme: t.id });
        setSelected(t.id);
      },
    },
      el('div', { class: 'theme-tile-preview' },
        el('div', { class: 'theme-tile-header' },
          el('span', { class: 'theme-tile-dot' }),
          el('span', { class: 'theme-tile-title' }, t.name),
        ),
        el('div', { class: 'theme-tile-bubble contact' }, 'Aa'),
        el('div', { class: 'theme-tile-bubble user' }, 'Aa'),
      ),
    );
    tilesById.set(t.id, tile);
    return tile;
  }

  for (const t of THEMES) grid.append(buildTile(t));

  // Keep the selection ring in sync when the theme is changed elsewhere
  // (toolbar sun/moon button). The handler self-detaches once the grid is
  // unmounted so we don't pile up subscribers across settings re-renders.
  const unsub = subscribe(() => {
    if (!grid.isConnected) { unsub(); return; }
    const t = state.settings && state.settings.theme;
    if (t) setSelected(t);
  });

  return grid;
}


function renderBudgetsSection(settings) {
  const PRESET_OPTIONS = [
    { value: 'opus',   label: 'Opus — 28k + 8k rollover' },
    { value: 'scroll', label: 'Scroll — 12k + 4k rollover' },
    { value: 'tablet', label: 'Tablet — 8k + 4k rollover' },
  ];
  const sel = pickerSelect({
    value: settings.context_preset,
    options: PRESET_OPTIONS,
    onChange: (v) => patchSettings({ context_preset: v }),
  });
  return el('div', { class: 'section' },
    el('h3', {}, 'Context size'),
    sel,
  );
}


function renderPresetsSection(presets, settings) {
  const list = el('div', {});

  // Rerender when ``provider_mode`` flips so the generic-only sizing
  // knobs appear / disappear without a tab switch. Subscribers are
  // freed when the Settings tab is rebuilt (closure-bound). Triggers
  // only on provider_mode change to avoid thrashing the editor on
  // every save (which bumps state.settings).
  let lastMode = (state.settings || settings).provider_mode;
  subscribe((s) => {
    const m = s.settings?.provider_mode;
    if (m && m !== lastMode) {
      lastMode = m;
      rerender(state.presets || presets);
    }
  });

  function rerender(latest) {
    list.replaceChildren();
    list.append(el('div', { style: { display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '8px' } },
      el('h3', { style: { margin: 0 } }, 'Generation presets'),
      el('div', { style: { display: 'flex', gap: '6px' } },
        el('label', { class: 'btn', for: 'preset-import-input', style: { cursor: 'pointer' } }, icon('upload', 14), 'Import'),
        el('input', {
          id: 'preset-import-input', type: 'file', accept: '.json', class: 'hidden',
          onChange: async (e) => {
            const f = e.target.files[0];
            if (!f) return;
            try {
              const r = await api.importPreset(f);
              toast(`Imported preset ${r.name}`, 'success');
              const ps = await api.listPresets();
              setState({ presets: ps });
              rerender(ps);
            } catch (err) { toast(`Import failed: ${err.message}`, 'error'); }
            e.target.value = '';
          },
        }),
        el('button', { class: 'btn primary', onClick: async () => {
          const p = await api.createPreset({ id: '', name: 'New preset' });
          const ps = await api.listPresets();
          setState({ presets: ps });
          rerender(ps);
        } }, icon('plus', 14), 'New'),
      ),
    ));
    list.append(el('p', { style: { color: 'var(--text-mute)', fontSize: '13px', margin: '0 0 16px' } },
      'The preset marked as ', el('strong', {}, 'default'), ' is used by every chat unless that chat selects a different one in its header row.',
    ));
    list.append(el('div', { style: { height: '4px' } }));

    for (const p of latest) {
      // Use the current store snapshot, not the (stale) ``settings`` captured
      // when this section first rendered — otherwise "Set as default" doesn't
      // update the chip until the page reloads.
      list.append(renderPresetCard(p, state.settings || settings, async () => {
        const ps = await api.listPresets();
        const s = await api.getSettings();
        setState({ presets: ps, settings: s });
        rerender(ps);
      }));
    }
  }
  rerender(presets);
  return el('div', { class: 'section' }, list);
}


function renderPresetCard(preset, settings, onChange) {
  let draft = { ...preset };

  const save = debounce(async () => {
    markPending('preset', draft.id, draft);
    try {
      await api.updatePreset(draft.id, draft);
      clearPending('preset', draft.id);
      // Sync the global presets list (so the active-chat selector and
      // other consumers see the latest values), but DON'T trigger a
      // section rerender — the user may still be typing in this card's
      // inputs, and a full ``list.replaceChildren()`` would steal focus
      // mid-edit after every debounced save pulse. Structural actions
      // (set-as-default, delete) still use ``onChange`` below.
      const ps = await api.listPresets();
      setState({ presets: ps });
    } catch (e) { toast(`Save failed: ${e.message}`, 'error'); }
  }, 400);

  // ``<input type="number">`` would localise display and parsing on a
  // non-en-US browser (e.g. ``0,5`` instead of ``0.5``), but our wire format
  // is always ``.``-decimal. The ``lang="en-US"`` attribute pins the input's
  // locale so the spinner buttons still work and the displayed / parsed
  // value stays canonical regardless of the browser's locale.
  function nf(label, key, step = 0.01) {
    return el('div', { class: 'form-group' },
      el('label', {}, label),
      el('input', {
        type: 'number',
        lang: 'en-US',
        value: draft[key],
        step,
        oninput: e => {
          const n = +e.target.value;
          if (!Number.isFinite(n)) return;
          draft[key] = n;
          save();
        },
      }),
    );
  }

  const isDefault = settings.default_preset_id === preset.id;

  const isGeneric = (state.settings || settings).provider_mode === 'generic';

  // Generic-mode-only sizing knobs. Persisted on every preset; AER
  // mode just doesn't surface them. Step 1 because tokens are
  // integers — the existing ``nf`` helper already pins ``lang="en-US"``
  // and tolerates intermediate non-numeric states.
  const genericFields = isGeneric ? el('div', { class: 'form-grid-2' },
    nf('Max context tokens', 'max_context_tokens', 1),
    nf('Rollover window tokens', 'rollover_window_tokens', 1),
    nf('Max new tokens', 'max_new_tokens', 1),
  ) : null;
  // Generic-only repetition penalties — OpenAI-style ``presence_penalty``
  // and ``frequency_penalty``. AER mode hides them; the upstream client
  // omits them when zero so the default preset stays a no-op.
  const genericPenalties = isGeneric ? el('div', { class: 'form-grid-2' },
    nf('Presence penalty', 'presence_penalty'),
    nf('Frequency penalty', 'frequency_penalty'),
  ) : null;

  return el('div', {
    class: 'section preset-card' + (isDefault ? ' preset-card-default' : ''),
  },
    el('div', { class: 'preset-card-header' },
      el('div', { class: 'preset-card-meta' },
        el('input', {
          class: 'preset-card-name',
          type: 'text', value: draft.name,
          oninput: e => { draft.name = e.target.value; save(); }
        }),
      ),
      el('div', { class: 'preset-card-actions' },
        isDefault
          ? el('span', {
              class: 'chip preset-default-chip',
              title: 'Used by every chat unless the chat selects a different preset',
            }, '✓ default')
          : el('button', { class: 'btn', onClick: async () => {
              const patch = { default_preset_id: preset.id };
              markPending('settings', 'settings', patch);
              await api.putSettings(patch);
              clearPending('settings', 'settings');
              onChange();
            } }, 'Set as default'),
        el('button', { class: 'btn ghost', title: 'Export preset', onClick: async () => {
          const r = await api.exportPreset(preset.id);
          await downloadFromResponse(r, `${preset.name}-preset.json`);
        } }, icon('download', 14)),
        el('button', { class: 'btn ghost danger', title: 'Delete preset', onClick: async () => {
          const ok = await confirmModal('Delete preset?', `${preset.name} will be removed.`, { danger: true });
          if (!ok) return;
          try {
            await api.deletePreset(preset.id);
            onChange();
          } catch (e) { toast(e.message, 'error'); }
        } }, icon('trash', 14)),
      ),
    ),
    el('div', { class: 'form-grid-2' },
      nf('Temperature', 'temperature'),
      nf('top_p', 'top_p'),
      nf('top_k', 'top_k', 1),
      nf('min_p', 'min_p'),
    ),
    ...(genericPenalties ? [genericPenalties] : []),
    ...(genericFields ? [genericFields] : []),
  );
}


/* ================================================================
 * Text-to-Speech section
 * ================================================================ */


function renderTTSSection(settings) {
  const root = el('div', { class: 'section tts-section' });

  // Persistent across rerenders so the in-flight typed key + the
  // debounce timer survive structural redraws (e.g. toggling the
  // active provider). The draft mutates in place; rerender() only
  // rebuilds the DOM, not the data.
  const initial = (state.settings || settings).tts || defaultTTS();
  const draft = JSON.parse(JSON.stringify(initial));
  const pendingKeys = {};   // keyId -> raw user-typed key

  const save = debounce(async () => {
    await saveTTSDraft(draft, pendingKeys);
    // After save the server returns fresh indicators. Sync them into
    // the draft so subsequent saves with sentinel preserve correctly.
    const fresh = state.settings?.tts;
    if (fresh) {
      draft.novelai.api_key_indicator = fresh.novelai?.api_key_indicator || '';
      draft.openrouter.api_key_indicator = fresh.openrouter?.api_key_indicator || '';
      draft.nanogpt.api_key_indicator = fresh.nanogpt?.api_key_indicator || '';
      for (const cur of fresh.custom_apis || []) {
        const local = (draft.custom_apis || []).find(c => c.id === cur.id);
        if (local) local.api_key_indicator = cur.api_key_indicator;
      }
    }
    // Clear pending keys; they've been committed and the server now
    // knows them via the indicator.
    for (const k of Object.keys(pendingKeys)) delete pendingKeys[k];
  }, 500);

  function rerender() {
    root.replaceChildren();

    // If the user has picked the bare Generic option (or is in generic
    // mode with no specific entry selected) AND there's exactly one
    // custom API configured, auto-pick it as the active entry. Saves
    // the user a second dropdown click after creating their first
    // custom endpoint. Idempotent — re-runs on every rerender.
    if (
      draft.active_kind === 'generic'
      && !draft.active_custom_id
      && draft.custom_apis?.length === 1
    ) {
      draft.active_custom_id = draft.custom_apis[0].id;
      save();
    }

    root.append(el('h3', {}, 'Text-to-Speech'));

    // Global mode goes first — the master "is TTS on?" toggle.
    const modeSelect = pickerSelect({
      value: draft.mode,
      options: [
        { value: 'off',          label: 'Off' },
        { value: 'default_off',  label: 'On (default off)' },
        { value: 'default_on',   label: 'On (default on)' },
      ],
      onChange: (v) => { draft.mode = v; save(); },
    });
    root.append(el('div', { class: 'form-group' },
      el('label', {},
        'Global mode ',
        helpDetails(
          '“On (default off)” means TTS only plays for contacts whose own '
          + 'setting is Enabled. “On (default on)” means TTS plays for every '
          + 'contact unless the contact’s own setting is Disabled.',
          { label: 'Global mode help' },
        ),
      ),
      modeSelect,
    ));

    // Active provider dropdown. A plain "Custom" entry is always
    // offered so users can switch to Generic mode and bootstrap the
    // custom-APIs list even when no entries exist yet. Each existing
    // entry then nests below it.
    const providerOptions = [
      { value: 'novelai',    label: 'NovelAI' },
      { value: 'openrouter', label: 'OpenRouter' },
      { value: 'nanogpt',    label: 'NanoGPT' },
      { value: 'generic:',   label: 'Custom (OpenAI-compatible)' },
      ...(draft.custom_apis || []).map(c => ({
        value: `generic:${c.id}`,
        label: `↳ ${c.name || 'Custom API'}`,
      })),
    ];
    let activeValue;
    if (draft.active_kind === 'generic') {
      activeValue = `generic:${draft.active_custom_id || ''}`;
    } else {
      activeValue = draft.active_kind || 'novelai';
    }
    const activeSelect = pickerSelect({
      value: activeValue,
      options: providerOptions,
      onChange: (v) => {
        if (v.startsWith('generic:')) {
          draft.active_kind = 'generic';
          draft.active_custom_id = v.slice('generic:'.length);
        } else {
          draft.active_kind = v;
          draft.active_custom_id = '';
        }
        save();
        rerender();  // provider-specific block below depends on this
      },
    });
    root.append(el('div', { class: 'form-group' },
      el('label', {}, 'Active provider'),
      activeSelect,
    ));

    // Two-tier provider config: "above-mode" (API key + model /
    // version — the connection-level concerns) and "below-mode"
    // (voice picker + speed — the per-turn rendering concerns).
    // Mode dropdown sits between them.
    const blockAbove = el('div', { class: 'tts-provider-config' });
    const blockBelow = el('div', { class: 'tts-provider-config' });
    root.append(blockAbove);

    const live = state.settings || settings;
    if (draft.active_kind === 'novelai') {
      renderNAIConfig(blockAbove, blockBelow, draft, save, pendingKeys, live, rerender);
    } else if (draft.active_kind === 'openrouter') {
      renderOpenAIConfig(blockAbove, blockBelow, draft, save, pendingKeys, 'openrouter', live, rerender);
    } else if (draft.active_kind === 'nanogpt') {
      renderOpenAIConfig(blockAbove, blockBelow, draft, save, pendingKeys, 'nanogpt', live, rerender);
    }
    // Generic: the per-entry editor lives inside the Custom TTS APIs
    // list further down — no separate provider config block here.

    root.append(blockBelow);

    // Custom APIs list — only visible when Generic is the active
    // kind. Sits ABOVE the Test block so the per-entry editor (where
    // the user actually configures the voices being tested) lives
    // next to its provider config section, not below the tester.
    if (draft.active_kind === 'generic') {
      root.append(renderCustomAPIsList(draft, save, pendingKeys, rerender));
    }

    // "Test voice" — always last, so it tests whatever was just
    // configured above. Uses the current draft's effective config and
    // whatever API key the server has on disk. (If the user just
    // typed a new key without saving, hit Test once after the 500ms
    // autosave to pick up the fresh key.) The Play button is
    // disabled when the resolved provider has no API key configured.
    root.append(renderTTSTestBlock(
      () => resolveTTSConfigFromGlobal(draft),
      {
        getReady: () => isProviderKeyConfigured(
          resolveTTSConfigFromGlobal(draft),
          state.settings || settings,
        ),
      },
    ));
  }

  rerender();
  return root;
}


function defaultTTS() {
  return {
    mode: 'off',
    active_kind: 'novelai',
    active_custom_id: '',
    novelai: {
      api_key_indicator: '',
      version: 'v2',
      voice_v1: 'Cyllene',
      voice_v2: 'Aini',
      custom_seed_v1: '',
      custom_seed_v2: '',
    },
    openrouter: { api_key_indicator: '', model: '', voice: '', speed: 1.0 },
    nanogpt: { api_key_indicator: '', model: '', voice: '', speed: 1.0 },
    custom_apis: [],
  };
}


/* Translate a SettingsView's `tts` block (where api_keys are
 * indicators) into a TTSSettings PUT payload that preserves keys via
 * sentinel. Used everywhere we save TTS state.
 *
 * `pendingKeys` records the user's *explicit* intent for each key —
 * keys present in the object override the indicator-based default.
 * The presence check uses ``in`` rather than truthiness, so an
 * explicit empty string ("user typed then cleared") is treated as a
 * clear, not as "preserve existing".
 */
function buildTTSPayload(draft, pendingKeys = {}) {
  function resolveKey(indicator, pending, pendingKey) {
    if (pendingKey in pending) return pending[pendingKey];
    if (indicator === TTS_KEY_SENTINEL) return TTS_KEY_SENTINEL;
    return '';
  }
  return {
    mode: draft.mode,
    active_kind: draft.active_kind,
    active_custom_id: draft.active_custom_id || '',
    novelai: {
      api_key: resolveKey(draft.novelai?.api_key_indicator, pendingKeys, 'novelai'),
      version: draft.novelai?.version || 'v2',
      voice_v1: draft.novelai?.voice_v1 || 'Cyllene',
      voice_v2: draft.novelai?.voice_v2 || 'Aini',
      custom_seed_v1: draft.novelai?.custom_seed_v1 || '',
      custom_seed_v2: draft.novelai?.custom_seed_v2 || '',
    },
    openrouter: {
      api_key: resolveKey(draft.openrouter?.api_key_indicator, pendingKeys, 'openrouter'),
      model: draft.openrouter?.model || '',
      voice: draft.openrouter?.voice || '',
      speed: draft.openrouter?.speed ?? 1.0,
    },
    nanogpt: {
      api_key: resolveKey(draft.nanogpt?.api_key_indicator, pendingKeys, 'nanogpt'),
      model: draft.nanogpt?.model || '',
      voice: draft.nanogpt?.voice || '',
      speed: draft.nanogpt?.speed ?? 1.0,
    },
    custom_apis: (draft.custom_apis || []).map(c => ({
      id: c.id,
      name: c.name || '',
      base_url: c.base_url || '',
      api_key: resolveKey(c.api_key_indicator, pendingKeys, `custom:${c.id}`),
      models: [...(c.models || [])],
      voices: { ...(c.voices || {}) },
      default_model: c.default_model || '',
      default_voice: c.default_voice || '',
      speed: c.speed ?? 1.0,
    })),
  };
}


async function saveTTSDraft(draft, pendingKeys = {}) {
  const patch = { tts: buildTTSPayload(draft, pendingKeys) };
  markPending('settings', 'settings', patch);
  try {
    const updated = await api.putSettings(patch);
    clearPending('settings', 'settings');
    setState({ settings: updated });
  } catch (e) {
    toast(`Save failed: ${e.message}`, 'error');
  }
}


function ttsKeyInput(currentIndicator, placeholder, onCommit, onClear) {
  const input = el('input', {
    type: 'password',
    placeholder: currentIndicator === TTS_KEY_SENTINEL
      ? '(key saved — leave blank to keep)'
      : placeholder,
    autocomplete: 'off',
  });
  input.addEventListener('input', () => onCommit(input.value));
  const wrap = el('div', { class: 'tts-key-wrap' }, input);
  if (currentIndicator === TTS_KEY_SENTINEL && onClear) {
    wrap.append(el('button', {
      type: 'button',
      class: 'btn ghost tts-key-clear',
      title: 'Clear API key',
      'aria-label': 'Clear API key',
      onClick: () => onClear(),
    }, icon('x', 14)));
  }
  return wrap;
}


function renderNAIConfig(above, below, draft, save, pendingKeys, settings, rerender) {
  const ov = draft.novelai;
  const voiceField = () => ov.version === 'v1' ? 'voice_v1' : 'voice_v2';
  const seedField = () => ov.version === 'v1' ? 'custom_seed_v1' : 'custom_seed_v2';

  // ----- above mode: API key only -------------------------------------

  // NAI TTS key cascades to AER, then to the generic-NAI token. The
  // placeholder reflects which tier the server will end up using when
  // the field is left blank.
  const usesAer = ov.api_key_indicator !== TTS_KEY_SENTINEL
    && settings.api_token_indicator === TTS_KEY_SENTINEL;
  const usesGenericNAI = ov.api_key_indicator !== TTS_KEY_SENTINEL
    && !usesAer
    && settings.generic?.novelai?.api_token_indicator === TTS_KEY_SENTINEL;
  const naiKeyPlaceholder = usesAer
    ? '(using AER token from above — leave blank to keep)'
    : usesGenericNAI
      ? '(using generic NovelAI token — leave blank to keep)'
      : 'NovelAI API key';
  const keyInput = ttsKeyInput(ov.api_key_indicator, naiKeyPlaceholder, (v) => {
    pendingKeys.novelai = v;
    save();
  }, () => {
    pendingKeys.novelai = '';
    ov.api_key_indicator = '';
    save();
    rerender();   // toggle the X button off, refresh placeholder
  });
  above.append(el('div', { class: 'form-group' },
    el('label', {}, 'API key'),
    keyInput,
  ));

  // ----- below mode: V1/V2 segmented + voice + optional custom seed ---

  // Version segmented — NAI's "model" equivalent.
  const versionRow = el('div', { class: 'form-group' },
    el('label', {}, 'Voice version'),
  );
  const seg = el('div', { class: 'tts-segmented' });
  for (const v of ['v1', 'v2']) {
    seg.append(el('button', {
      type: 'button',
      class: 'tts-seg-btn' + (ov.version === v ? ' active' : ''),
      onClick: () => {
        ov.version = v;
        save();
        repaint();
      },
    }, v.toUpperCase()));
  }
  versionRow.append(seg);
  below.append(versionRow);

  const voiceRow = el('div', { class: 'form-group' });
  below.append(voiceRow);
  let customSeedRow = null;

  function repaint() {
    voiceRow.replaceChildren();
    if (customSeedRow) { customSeedRow.remove(); customSeedRow = null; }

    const presets = ov.version === 'v1' ? NAI_V1_PRESETS : NAI_V2_PRESETS;
    const options = [
      ...presets.map(p => ({ value: p.name, label: naiLabel(p) })),
      { value: NAI_CUSTOM_SENTINEL, label: '(custom)' },
    ];
    const vField = voiceField();
    const sField = seedField();
    const sel = pickerSelect({
      value: ov[vField],
      options,
      onChange: (v) => { ov[vField] = v; save(); repaint(); },
    });
    voiceRow.append(el('label', {}, 'Voice'), sel);

    if (ov[vField] === NAI_CUSTOM_SENTINEL) {
      customSeedRow = el('div', { class: 'form-group' },
        el('label', {}, 'Voice seed'),
        el('input', {
          type: 'text',
          value: ov[sField] || '',
          placeholder: 'Free-form seed',
          onInput: (e) => { ov[sField] = e.target.value; save(); },
        }),
      );
      below.append(customSeedRow);
    }

    for (const b of seg.querySelectorAll('.tts-seg-btn')) {
      b.classList.toggle('active', b.textContent.toLowerCase() === ov.version);
    }
  }
  repaint();
}


function renderOpenAIConfig(above, below, draft, save, pendingKeys, kind, settings, rerender) {
  const cfg = draft[kind];

  // ----- above mode: API key only -------------------------------------

  // OR / NGPT TTS keys cascade to the matching generic-LLM token when
  // blank. Surface that fallback in the placeholder so a user with
  // only the LLM token configured doesn't think their TTS is broken.
  const labelByKind = { openrouter: 'OpenRouter', nanogpt: 'NanoGPT' };
  const usesGenericLLM = cfg.api_key_indicator !== TTS_KEY_SENTINEL
    && settings?.generic?.[kind]?.api_token_indicator === TTS_KEY_SENTINEL;
  const placeholder = usesGenericLLM
    ? `(using generic ${labelByKind[kind]} token — leave blank to keep)`
    : `${labelByKind[kind]} API key`;
  const keyInput = ttsKeyInput(cfg.api_key_indicator, placeholder, (v) => {
    pendingKeys[kind] = v;
    save();
  }, () => {
    pendingKeys[kind] = '';
    cfg.api_key_indicator = '';
    save();
    rerender && rerender();
  });
  above.append(el('div', { class: 'form-group' },
    el('label', {}, 'API key'),
    keyInput,
  ));

  // ----- below mode: model + voice + speed ----------------------------

  const modelPicker = makeAvatarPicker({
    value: cfg.model || '',
    options: cachedAsOptions(kind),
    placeholder: 'Select model',
    onChange: (v) => {
      cfg.model = v;
      const m = (getCachedTTSModels(kind) || []).find(x => x.id === v);
      if (m && !m.voices.includes(cfg.voice)) cfg.voice = m.voices[0] || '';
      save();
      paintVoice();
      paintSpeed();
    },
  });

  const reloadBtn = el('button', {
    type: 'button',
    class: 'btn ghost tts-reload-btn',
    title: 'Refresh model list',
    'aria-label': 'Refresh model list',
    onClick: () => refresh(true),
  }, icon('refresh', 14));

  modelPicker.addEventListener('focusin', () => refresh(false));
  async function refresh(force) {
    if (!force && !isCacheStale(kind)) return;
    reloadBtn.classList.add('spinning');
    try {
      const models = await getTTSModels(kind, { force });
      modelPicker.setOptions(models.map(m => ({ value: m.id, label: m.name || m.id })));
      paintVoice();
      paintSpeed();
    } catch (err) {
      toast(`Failed to refresh ${kind} models: ${err.message}`, 'error');
    } finally {
      reloadBtn.classList.remove('spinning');
    }
  }
  refresh(false);  // initial population

  above.append(el('div', { class: 'form-group' },
    el('label', {}, 'Model'),
    el('div', { class: 'tts-model-pickerwrap' }, modelPicker, reloadBtn),
  ));

  // ----- below mode: voice + speed ------------------------------------

  const voiceRow = el('div', { class: 'form-group' });
  const speedRow = el('div', { class: 'form-group' });
  below.append(voiceRow);
  below.append(speedRow);

  function paintVoice() {
    voiceRow.replaceChildren();
    const m = (getCachedTTSModels(kind) || []).find(x => x.id === cfg.model);
    const voices = m?.voices || [];
    if (!cfg.voice && voices.length) cfg.voice = voices[0];
    voiceRow.append(
      el('label', {}, 'Voice'),
      pickerSelect({
        value: cfg.voice || '',
        options: voices.map(v => ({ value: v, label: v })),
        placeholder: voices.length ? 'Select voice' : 'Pick a model first',
        onChange: (v) => { cfg.voice = v; save(); },
      }),
    );
  }

  function paintSpeed() {
    speedRow.replaceChildren();
    const m = (getCachedTTSModels(kind) || []).find(x => x.id === cfg.model);
    const show = kind === 'nanogpt' ? true : (m?.speed_supported ?? false);
    if (!show) return;
    speedRow.append(speedSlider(cfg, 'speed', save));
  }

  paintVoice();
  paintSpeed();
}


function cachedAsOptions(kind) {
  const models = getCachedTTSModels(kind);
  if (!models) return [];
  return models.map(m => ({ value: m.id, label: m.name || m.id }));
}


function speedSlider(target, field, save) {
  const slider = el('input', {
    type: 'range',
    min: '0.25',
    max: '4.0',
    step: '0.05',
    value: target[field] ?? 1.0,
    lang: 'en-US',
    oninput: (e) => {
      const n = parseFloat(e.target.value);
      if (Number.isFinite(n)) {
        target[field] = n;
        readout.textContent = `${n.toFixed(2)}×`;
      }
    },
    onchange: () => save(),
  });
  const readout = el('span', { class: 'tts-speed-readout' }, `${(target[field] ?? 1.0).toFixed(2)}×`);
  return el('div', { class: 'tts-speed-controls' },
    el('label', {}, 'Speed'),
    slider,
    readout,
  );
}


/* ================================================================
 * Custom TTS APIs — add / edit / remove / set-default
 * ================================================================ */


function renderCustomAPIsList(draft, save, pendingKeys, rerenderAll) {
  const wrap = el('div', { class: 'tts-custom-list' });

  function rerender() {
    wrap.replaceChildren();
    wrap.append(el('div', { class: 'tts-custom-list-header' },
      el('h4', { style: { margin: 0 } }, 'Custom TTS APIs'),
      el('button', { class: 'btn primary', onClick: () => {
        const id = `custom_${Math.random().toString(36).slice(2, 10)}`;
        draft.custom_apis = draft.custom_apis || [];
        draft.custom_apis.push({
          id,
          name: 'Custom TTS',
          base_url: '',
          api_key_indicator: '',
          models: [],
          voices: {},
          default_model: '',
          default_voice: '',
          speed: 1.0,
        });
        save();
        rerender();
        rerenderAll();   // active-provider dropdown needs the new entry
      } }, icon('plus', 14), 'Add custom API'),
    ));

    if (!draft.custom_apis?.length) {
      wrap.append(el('p', {
        style: { color: 'var(--text-mute)', fontSize: '13px', margin: '8px 0 0' },
      }, 'Point your own OpenAI-compatible TTS endpoint here.'));
      return;
    }

    for (const entry of draft.custom_apis) {
      wrap.append(renderCustomAPICard(entry, draft, save, pendingKeys, rerender, rerenderAll));
    }
  }
  rerender();
  return wrap;
}


function renderCustomAPICard(entry, draft, save, pendingKeys, rerenderList, rerenderAll) {
  const isActive = draft.active_kind === 'generic' && draft.active_custom_id === entry.id;

  // Name
  const nameInput = el('input', {
    type: 'text',
    value: entry.name || '',
    placeholder: 'Name',
    onInput: (e) => { entry.name = e.target.value; save(); rerenderAll(); },
  });

  // Base URL
  const urlInput = el('input', {
    type: 'text',
    value: entry.base_url || '',
    placeholder: 'https://example.com/v1',
    onInput: (e) => { entry.base_url = e.target.value; save(); },
  });

  // API key
  const keyInput = ttsKeyInput(entry.api_key_indicator, 'API key', (v) => {
    pendingKeys[`custom:${entry.id}`] = v;
    save();
  }, () => {
    pendingKeys[`custom:${entry.id}`] = '';
    entry.api_key_indicator = '';
    save();
    rerenderList();
  });

  // Models list editor + per-model voices editor
  const modelsBlock = el('div', { class: 'tts-custom-models' });
  function paintModels() {
    modelsBlock.replaceChildren();
    modelsBlock.append(el('div', { class: 'tts-models-header' },
      el('label', { style: { margin: 0 } }, 'Models & voices'),
      el('button', {
        type: 'button',
        class: 'btn ghost',
        onClick: () => {
          entry.models = entry.models || [];
          entry.models.push('');
          paintModels();
          save();
        },
      }, icon('plus', 12), 'Add model'),
    ));

    if (!entry.models?.length) {
      modelsBlock.append(el('p', {
        style: { color: 'var(--text-mute)', fontSize: '12px', margin: '4px 0 0' },
      }, 'Add at least one model + voice to use this API.'));
    }

    (entry.models || []).forEach((_unused, idx) => {
      // Read the model name fresh from `entry.models[idx]` on every
      // interaction so handlers don't capture a stale closure value
      // when the user renames the model.
      const getName = () => entry.models[idx] || '';
      const getVoices = () => {
        const n = getName();
        if (!entry.voices) entry.voices = {};
        if (!entry.voices[n]) entry.voices[n] = [];
        return entry.voices[n];
      };

      const row = el('div', { class: 'tts-model-row-edit' });
      const modelInput = el('input', {
        type: 'text',
        value: getName(),
        placeholder: 'tts-1, kokoro-82m, …',
        onInput: (e) => {
          const newName = e.target.value;
          const oldName = entry.models[idx];
          entry.models[idx] = newName;
          // Re-key voices map so the voices the user has already
          // typed migrate to the new model name. Skip the no-op
          // (oldName === newName) and the from-empty case (let the
          // empty-named voices stay parked under '' until the user
          // commits a real name — that's the only branch where
          // newName-as-key may not exist yet).
          if (entry.voices) {
            if (oldName && oldName !== newName) {
              entry.voices[newName] = entry.voices[oldName] || [];
              delete entry.voices[oldName];
            } else if (!oldName && newName && entry.voices['']) {
              entry.voices[newName] = entry.voices[''];
              delete entry.voices[''];
            }
          }
          if (entry.default_model === oldName) entry.default_model = newName;
          save();
          paintDefaults();   // model dropdown reflects the new name immediately
        },
      });

      const removeBtn = el('button', {
        type: 'button', class: 'btn ghost danger',
        title: 'Remove this model',
        onClick: async () => {
          const cur = getName();
          const ok = await confirmModal(
            'Remove model?',
            `${cur || 'This model'} and its voices will be removed.`,
            { danger: true },
          );
          if (!ok) return;
          const removed = entry.models.splice(idx, 1)[0];
          if (removed && entry.voices) delete entry.voices[removed];
          if (entry.default_model === removed) entry.default_model = '';
          save();
          paintModels();
          paintDefaults();
          rerenderAll();
        },
      }, icon('trash', 12));

      const voicesEditor = el('div', { class: 'tts-voices-list' });

      function paintVoices() {
        voicesEditor.replaceChildren();
        const voices = getVoices();
        for (let vi = 0; vi < voices.length; vi++) {
          voicesEditor.append(el('div', { class: 'tts-voice-row' },
            el('input', {
              type: 'text',
              value: voices[vi],
              placeholder: 'voice name',
              onInput: (e) => {
                const v = getVoices();
                v[vi] = e.target.value;
                save();
                paintDefaults();
              },
            }),
            el('button', {
              type: 'button', class: 'btn ghost danger',
              title: 'Remove voice',
              onClick: () => {
                const v = getVoices();
                v.splice(vi, 1);
                if (entry.default_voice && !v.includes(entry.default_voice)) {
                  entry.default_voice = '';
                }
                save();
                paintVoices();
                paintDefaults();
              },
            }, icon('trash', 12)),
          ));
        }
        voicesEditor.append(el('button', {
          type: 'button', class: 'btn ghost',
          onClick: () => {
            const v = getVoices();
            v.push('');
            save();
            paintVoices();
            paintDefaults();
          },
        }, icon('plus', 12), 'Add voice'));
      }
      paintVoices();

      row.append(
        el('div', { class: 'tts-model-row-controls' }, modelInput, removeBtn),
        voicesEditor,
      );
      modelsBlock.append(row);
    });
  }
  paintModels();

  // Default model + voice
  const defaultsRow = el('div', { class: 'form-grid-2' });
  function paintDefaults() {
    defaultsRow.replaceChildren();
    const modelOpts = (entry.models || []).filter(m => m).map(m => ({ value: m, label: m }));
    // Auto-pick the default model when exactly one is configured and
    // nothing has been picked yet, so the user doesn't have to click
    // through the dropdown for the obvious case.
    if (!entry.default_model && modelOpts.length === 1) {
      entry.default_model = modelOpts[0].value;
    }
    defaultsRow.append(el('div', { class: 'form-group' },
      el('label', {}, 'Default model'),
      pickerSelect({
        value: entry.default_model || '',
        options: modelOpts,
        placeholder: modelOpts.length ? '—' : 'No models',
        onChange: (v) => {
          entry.default_model = v;
          // Reset default_voice if not in voices[new model].
          const list = entry.voices?.[v] || [];
          if (entry.default_voice && !list.includes(entry.default_voice)) {
            entry.default_voice = '';
          }
          save();
          paintDefaults();
        },
      }),
    ));
    const voiceList = (entry.voices?.[entry.default_model] || []).filter(v => v);
    // Same auto-pick for default voice — when the model has exactly
    // one configured voice, surface it as the default.
    if (!entry.default_voice && voiceList.length === 1) {
      entry.default_voice = voiceList[0];
    }
    defaultsRow.append(el('div', { class: 'form-group' },
      el('label', {}, 'Default voice'),
      pickerSelect({
        value: entry.default_voice || '',
        options: voiceList.map(v => ({ value: v, label: v })),
        placeholder: voiceList.length ? '—' : 'No voices',
        onChange: (v) => { entry.default_voice = v; save(); },
      }),
    ));
  }
  paintDefaults();

  return el('div', {
    class: 'section preset-card' + (isActive ? ' preset-card-default' : ''),
  },
    el('div', { class: 'preset-card-header' },
      el('div', { class: 'preset-card-meta' }, nameInput),
      el('div', { class: 'preset-card-actions' },
        isActive
          ? el('span', {
              class: 'chip preset-default-chip',
              title: 'Currently the active TTS provider',
            }, '✓ active')
          : el('button', { class: 'btn', onClick: async () => {
              draft.active_kind = 'generic';
              draft.active_custom_id = entry.id;
              save();
              rerenderAll();
            } }, 'Set as active'),
        el('button', { class: 'btn ghost danger', title: 'Delete custom API', onClick: async () => {
          const ok = await confirmModal('Delete custom TTS API?', `${entry.name || 'This entry'} will be removed.`, { danger: true });
          if (!ok) return;
          const i = draft.custom_apis.indexOf(entry);
          if (i >= 0) draft.custom_apis.splice(i, 1);
          // If this entry was active, drop the selection but keep
          // active_kind=generic so the user stays in the custom
          // section (the rerender's auto-pick will reassign if a
          // single other entry remains).
          if (draft.active_kind === 'generic' && draft.active_custom_id === entry.id) {
            draft.active_custom_id = '';
          }
          save();
          rerenderAll();
        } }, icon('trash', 14)),
      ),
    ),
    el('div', { class: 'form-group' },
      el('label', {}, 'Endpoint base URL'),
      urlInput,
    ),
    el('div', { class: 'form-group' },
      el('label', {}, 'API key'),
      keyInput,
    ),
    modelsBlock,
    defaultsRow,
    speedSlider(entry, 'speed', save),
  );
}


function debounce(fn, ms) {
  let t;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}
