/* TTS model-catalog client shim. The load-bearing cache lives
 * server-side (server/discovery_cache.py); this file just calls the
 * route and keeps the last response around for synchronous reads.
 *
 * Why the mirror exists: paintVoice / paintSpeed in settings.js and
 * entity_tts_section.js read the model list synchronously from
 * onChange handlers — they need the latest result in-process, not a
 * round-trip away. The mirror has no TTL of its own; the server
 * decides when to refresh upstream.
 */

import { api } from './api.js';


// provider -> models[]  (memoization for synchronous reads).
const lastFetched = new Map();
const everFetched = new Set();


export async function getTTSModels(provider, { force = false } = {}) {
  const fetcher = provider === 'openrouter'
    ? api.getOpenRouterTTSModels
    : api.getNanoGPTTTSModels;
  const data = await fetcher({ refresh: force });
  const models = data?.models || [];
  lastFetched.set(provider, models);
  everFetched.add(provider);
  return models;
}


/* Return the last fetched model list synchronously, or null if we
 * have never fetched for this provider in this session. The server
 * is the authoritative store; this is a per-tab mirror. */
export function getCachedTTSModels(provider) {
  return lastFetched.get(provider) || null;
}


/* True until the first successful fetch for this provider in this
 * session. Used by focusin handlers to fetch on initial open but
 * not on every subsequent focus event. The Reload button always
 * passes force=true and bypasses this check. */
export function isCacheStale(provider) {
  return !everFetched.has(provider);
}
