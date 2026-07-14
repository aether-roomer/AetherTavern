/* TTS helpers: markup stripping, NAI slicing, autoplay gate, config resolution.
 *
 * Resolution lives on the client so the URL bakes in the exact provider
 * config the active tab thinks is right. The server is a thin proxy and
 * only resolves the API key (which the frontend never sees).
 */

import { NAI_CUSTOM_SENTINEL } from './constants.js';


/* ------------------------------------------------------------------- *
 * stripForTTS — clean bubble text for speech synthesis.
 *
 * - HTML comments (``<!-- ... -->``): dropped entirely. They render
 *   invisibly in prose (hidden metadata / "chain-of-thought at home"),
 *   so speaking their text would read out content the user never sees.
 * - Triple-backtick fences: drop the backticks (and any language tag
 *   on the opening fence) but keep the inner content. The TTS reads
 *   the code; only the markers are silent.
 * - **bold**, *italic*, _italic_: same pair-matching logic as
 *   aer_render.js — markers stay literal when unbalanced.
 * - Collapse whitespace runs and trim.
 * ------------------------------------------------------------------- */
export function stripForTTS(text) {
  if (!text) return '';
  let s = text;
  // HTML comments first: they're invisible on screen, so nothing inside
  // should be spoken. Doing this before the fence / emphasis passes also
  // keeps a comment that happens to hold ``` or * / _ from perturbing them.
  s = s.replace(/<!--[\s\S]*?-->/g, '');
  // Triple-backtick fences. ``` optionally followed by a language tag
  // on the same line, then content, then closing ```. Non-greedy.
  s = s.replace(/```[^\n`]*\n?([\s\S]*?)\n?```/g, '$1');
  // Strip bold/italic markers (pair-matching, non-greedy).
  s = s.replace(/\*\*([^\*\n][\s\S]*?)\*\*/g, '$1');
  s = s.replace(/\*([^\*\n][\s\S]*?)\*/g, '$1');
  s = s.replace(/_([^_\n][\s\S]*?)_/g, '$1');
  // Collapse whitespace runs to a single space.
  s = s.replace(/\s+/g, ' ').trim();
  return s;
}


/* ------------------------------------------------------------------- *
 * Slicing — split text on sentence / newline boundaries.
 *
 * The algorithm walks once: for each slice, search forward from
 * cursor+target to cursor+cap for the first sentence / newline
 * boundary; if none, fall back to the last word boundary in that
 * window; if still none, hard-cut at cap (pathological no-whitespace
 * case).
 * ------------------------------------------------------------------- */
function sliceText(text, { target, cap }) {
  if (!text) return [];
  if (text.length <= cap) return [text];

  const out = [];
  let i = 0;
  while (i < text.length) {
    if (text.length - i <= cap) {
      const tail = text.slice(i).trim();
      if (tail) out.push(tail);
      break;
    }
    const targetIdx = i + target;
    const capIdx = Math.min(i + cap, text.length);

    // First sentence/newline boundary in [target, cap)
    let cut = -1;
    for (let j = targetIdx; j < capIdx; j++) {
      const ch = text[j];
      if (ch === '\n') { cut = j + 1; break; }
      if (
        (ch === '.' || ch === '!' || ch === '?') &&
        (j + 1 >= text.length || /\s/.test(text[j + 1]))
      ) {
        cut = j + 1;
        break;
      }
    }

    // Last word boundary fallback
    if (cut === -1) {
      for (let j = capIdx - 1; j >= targetIdx; j--) {
        if (/\s/.test(text[j])) { cut = j + 1; break; }
      }
    }

    // Hard cut
    if (cut === -1) cut = capIdx;

    const piece = text.slice(i, cut).trim();
    if (piece) out.push(piece);
    i = cut;
    while (i < text.length && /\s/.test(text[i])) i++;
  }
  return out;
}

export function sliceForNAI(text) {
  // NAI's hard text limit is 1000 chars; we aim for 300 so the first
  // audio bubble comes back fast.
  return sliceText(text, { target: 300, cap: 1000 });
}

export function sliceForUrlSafety(text) {
  // OR / NGPT / Generic don't have NAI's hard cap but the URL needs to
  // stay under ~8KB. 3000 chars + ~200 of params is well under after
  // percent-encoding.
  return sliceText(text, { target: 2000, cap: 3000 });
}

export function sliceForApi(text, api) {
  return api === 'novelai' ? sliceForNAI(text) : sliceForUrlSafety(text);
}


/* ------------------------------------------------------------------- *
 * shouldAutoplayTTS — apply the global × entity mode gate.
 *
 * Speaker-button clicks BYPASS this gate (they always play); only the
 * auto-TTS path during generation calls this.
 * ------------------------------------------------------------------- */
export function shouldAutoplayTTS(sender, contact, user, settings) {
  const globalMode = settings?.tts?.mode || 'off';
  if (globalMode === 'off') return false;

  const entity = sender === 'contact' ? contact : user;
  // Per-kind defaults: contact → 'default' (follow global), user →
  // 'disabled' (don't auto-play user messages).
  const entityDefault = sender === 'contact' ? 'default' : 'disabled';
  const mode = entity?.tts?.mode || entityDefault;

  if (mode === 'enabled') return true;
  if (mode === 'disabled') return false;
  // 'default' — follow global
  return globalMode === 'default_on';
}


/* ------------------------------------------------------------------- *
 * resolveTTSConfig — pick the effective provider config.
 *
 * Returns an object the URL builder can consume directly. Never
 * returns null — there's always *some* provider config we can build
 * (manual speaker-click works even when the gate would deny auto-TTS,
 * provided the resolved key is configured server-side).
 *
 * Fields by api:
 *   novelai:    { api, version, voice, custom_seed }
 *   openrouter: { api, model, voice, speed }
 *   nanogpt:    { api, model, voice, speed }
 *   generic:    { api, custom_id, model, voice, speed }
 * ------------------------------------------------------------------- */
export function resolveTTSConfig(sender, contact, user, settings) {
  const entity = sender === 'contact' ? contact : user;
  const tts = settings?.tts || {};
  const useCustom = !!entity?.tts?.use_custom;
  const ov = entity?.tts?.override || {};

  const kind = useCustom
    ? (ov.kind || 'novelai')
    : (tts.active_kind || 'novelai');

  if (kind === 'novelai') {
    if (useCustom) {
      const ver = ov.novelai_version || 'v2';
      return {
        api: 'novelai',
        version: ver,
        voice: ver === 'v1'
          ? (ov.novelai_voice_v1 || 'Cyllene')
          : (ov.novelai_voice_v2 || 'Aini'),
        custom_seed: ver === 'v1'
          ? (ov.novelai_custom_seed_v1 || '')
          : (ov.novelai_custom_seed_v2 || ''),
      };
    }
    const g = tts.novelai || {};
    const ver = g.version || 'v2';
    return {
      api: 'novelai',
      version: ver,
      voice: ver === 'v1' ? (g.voice_v1 || 'Cyllene') : (g.voice_v2 || 'Aini'),
      custom_seed: ver === 'v1' ? (g.custom_seed_v1 || '') : (g.custom_seed_v2 || ''),
    };
  }

  if (kind === 'openrouter') {
    if (useCustom) {
      return {
        api: 'openrouter',
        model: ov.openrouter_model || '',
        voice: ov.openrouter_voice || '',
        speed: ov.openrouter_speed ?? 1.0,
      };
    }
    const g = tts.openrouter || {};
    return {
      api: 'openrouter',
      model: g.model || '',
      voice: g.voice || '',
      speed: g.speed ?? 1.0,
    };
  }

  if (kind === 'nanogpt') {
    if (useCustom) {
      return {
        api: 'nanogpt',
        model: ov.nanogpt_model || '',
        voice: ov.nanogpt_voice || '',
        speed: ov.nanogpt_speed ?? 1.0,
      };
    }
    const g = tts.nanogpt || {};
    return {
      api: 'nanogpt',
      model: g.model || '',
      voice: g.voice || '',
      speed: g.speed ?? 1.0,
    };
  }

  if (kind === 'generic') {
    let customId, model, voice, speed;
    if (useCustom) {
      customId = ov.custom_id || '';
      model = ov.generic_model || '';
      voice = ov.generic_voice || '';
      speed = ov.generic_speed ?? 1.0;
    } else {
      customId = tts.active_custom_id || '';
      const entry = (tts.custom_apis || []).find(e => e.id === customId);
      model = entry?.default_model || '';
      voice = entry?.default_voice || '';
      speed = entry?.speed ?? 1.0;
    }
    return { api: 'generic', custom_id: customId, model, voice, speed };
  }

  // Unknown kind — fall back to NAI defaults so callers can build *something*.
  return { api: 'novelai', version: 'v2', voice: 'Aini', custom_seed: '' };
}


/* Build a TTS config from a *global TTSSettings draft* alone — no
 * per-entity override. Used by the Settings-page voice tester. */
export function resolveTTSConfigFromGlobal(draftTTS) {
  const t = draftTTS || {};
  const kind = t.active_kind || 'novelai';
  if (kind === 'novelai') {
    const g = t.novelai || {};
    const ver = g.version || 'v2';
    return {
      api: 'novelai',
      version: ver,
      voice: ver === 'v1' ? (g.voice_v1 || 'Cyllene') : (g.voice_v2 || 'Aini'),
      custom_seed: ver === 'v1' ? (g.custom_seed_v1 || '') : (g.custom_seed_v2 || ''),
    };
  }
  if (kind === 'openrouter') {
    const g = t.openrouter || {};
    return {
      api: 'openrouter',
      model: g.model || '',
      voice: g.voice || '',
      speed: g.speed ?? 1.0,
    };
  }
  if (kind === 'nanogpt') {
    const g = t.nanogpt || {};
    return {
      api: 'nanogpt',
      model: g.model || '',
      voice: g.voice || '',
      speed: g.speed ?? 1.0,
    };
  }
  if (kind === 'generic') {
    const id = t.active_custom_id || '';
    const entry = (t.custom_apis || []).find(c => c.id === id);
    return {
      api: 'generic',
      custom_id: id,
      model: entry?.default_model || '',
      voice: entry?.default_voice || '',
      speed: entry?.speed ?? 1.0,
    };
  }
  return { api: 'novelai', version: 'v2', voice: 'Aini', custom_seed: '' };
}


/* True when the global Settings view has everything needed to talk
 * to the resolved provider — an API key indicator is set, and (for
 * generic / OpenAI-compatible custom endpoints) the base URL is
 * non-empty. The frontend doesn't see raw keys — it sees the
 * ``api_key_indicator`` which equals ``"__present__"`` when set.
 * Used by the Test-voice button to disable when the upstream can't
 * actually be reached. */
export function isProviderKeyConfigured(cfg, settings) {
  if (!cfg || !settings) return false;
  const tts = settings.tts || {};
  // The server-side TTS token resolver falls back to the matching
  // generic-LLM token for NAI / OR / NGPT, so the readiness check has
  // to mirror that cascade or the chip lies. NAI gets a four-tier
  // chain (own → AER → generic-NAI); OR / NGPT get (own → generic-cousin).
  const generic = settings.generic || {};
  const SENT = '__present__';
  if (cfg.api === 'novelai') {
    return tts.novelai?.api_key_indicator === SENT
        || settings.api_token_indicator === SENT
        || generic.novelai?.api_token_indicator === SENT;
  }
  if (cfg.api === 'openrouter') {
    return tts.openrouter?.api_key_indicator === SENT
        || generic.openrouter?.api_token_indicator === SENT;
  }
  if (cfg.api === 'nanogpt') {
    return tts.nanogpt?.api_key_indicator === SENT
        || generic.nanogpt?.api_token_indicator === SENT;
  }
  if (cfg.api === 'generic') {
    const entry = (tts.custom_apis || []).find(c => c.id === cfg.custom_id);
    if (!entry) return false;
    // Custom TTS endpoints need BOTH a key AND a base URL to be
    // reachable. No LLM-side fallback for generic-custom TTS — its
    // upstream is tied to the entry's own credentials.
    return entry.api_key_indicator === SENT && !!entry.base_url;
  }
  return false;
}


/* Convenience: does a resolved config look complete enough to send?
 * Used by the chat view to decide whether to show speaker buttons /
 * pre-flight the TTS path. The server still does the authoritative
 * check (412 on missing key, 400 on missing model/voice), but this
 * helps the UI surface friendly inline state. */
export function isTTSConfigComplete(cfg) {
  if (!cfg) return false;
  if (cfg.api === 'novelai') {
    if (cfg.voice === NAI_CUSTOM_SENTINEL && !cfg.custom_seed) return false;
    return !!cfg.voice;
  }
  if (cfg.api === 'generic') return !!cfg.custom_id && !!cfg.model && !!cfg.voice;
  // openrouter, nanogpt
  return !!cfg.model && !!cfg.voice;
}
