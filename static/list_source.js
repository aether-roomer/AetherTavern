/* Paged data adapter that backs a virtualized list with a server endpoint.
 *
 * The chat list is the only pane that needs this — contacts / users /
 * scenarios / brain libraries fit comfortably in memory at hundreds-
 * of-items scale, so they wire the virtualizer directly against a
 * full client-fetched array. Chats are the scaling concern: a user
 * may eventually accumulate thousands.
 *
 * Behaviour
 * ---------
 *
 *   - Loads one ``pageSize`` page at a time, keyed by ``offset``.
 *   - On a near-bottom-of-current-page scroll, eagerly prefetches the
 *     next page so the scrollbar feels continuous.
 *   - ``setParams`` diffs the incoming params against the current set;
 *     anything that changes the filter / sort / search query invalidates
 *     every cached page and refetches page 0, aborting whichever fetch
 *     is currently in flight. The visible rows render as skeletons
 *     during the brief gap.
 *   - ``getItem(i)`` returns ``null`` for slots that haven't been loaded
 *     yet — the consumer renders a placeholder. Calling ``ensureLoaded``
 *     fires the page fetch covering that index; subsequent renders see
 *     a real item.
 *
 * API
 * ---
 *
 *   const paged = createPagedList({
 *     endpoint,         // (queryString: string) =>
 *                       //   Promise<{ items, total, offset, limit }>
 *     pageSize = 50,
 *     params = {},      // query-shaped params: { q, sort, dir, ... }
 *     onChange = null,  // () => void  fires when getTotal or some
 *                       //   covered range becomes valid; consumer
 *                       //   calls virt.setCount / virt.invalidate.
 *   });
 *
 *   paged.getItem(i)
 *   paged.getTotal()         // null until first response
 *   paged.ensureLoaded(i)
 *   paged.setParams(newParams)
 *   paged.firstPagePromise   // resolves on first response (any params).
 *   paged.abort()
 *
 * The consumer (chat_list.js) wires ``onChange`` to recompute the
 * virtualizer's ``count`` from ``getTotal()`` and invalidate the
 * newly-loaded range so its skeleton rows get replaced with real ones.
 */


export function createPagedList(opts) {
  const {
    endpoint,
    pageSize = 50,
    params: initialParams = {},
    onChange = null,
  } = opts;

  let params = { ...initialParams };
  // Keyed by ``offset`` of the first item on that page.
  const pages = new Map();   // offset → items[]
  let total = null;
  let inflight = null;       // { offset, controller }
  let firstResolve;
  let firstReject;
  const firstPagePromise = new Promise((res, rej) => {
    firstResolve = res;
    firstReject = rej;
  });
  let firstResolved = false;

  function _paramKey(p) {
    // Stable string key over the param keys we actually send. Skipping
    // offset/limit because they're not "filter-changing" — they move
    // within the same logical result set.
    const skip = new Set(['offset', 'limit']);
    const keys = Object.keys(p).filter(k => !skip.has(k)).sort();
    return keys.map(k => `${k}=${p[k]}`).join('&');
  }

  function _abortInflight() {
    if (inflight) {
      inflight.controller.abort();
      inflight = null;
    }
  }

  function _queryString(offset) {
    const qp = new URLSearchParams();
    for (const [k, v] of Object.entries(params)) {
      if (v === undefined || v === null || v === '') continue;
      qp.set(k, String(v));
    }
    qp.set('offset', String(offset));
    qp.set('limit', String(pageSize));
    return qp.toString();
  }

  async function _fetchPage(offset) {
    if (pages.has(offset)) return;
    if (inflight && inflight.offset === offset) return;
    _abortInflight();
    const controller = new AbortController();
    inflight = { offset, controller };
    try {
      const resp = await endpoint(_queryString(offset), { signal: controller.signal });
      if (controller.signal.aborted) return;
      // Server may cap ``limit`` lower than requested — trust the
      // returned ``items.length`` for slot accounting.
      pages.set(offset, resp.items || []);
      total = resp.total;
      if (!firstResolved) {
        firstResolved = true;
        firstResolve(resp);
      }
      if (onChange) onChange({ offset, length: (resp.items || []).length });
    } catch (err) {
      if (controller.signal.aborted) return;
      if (!firstResolved) {
        firstResolved = true;
        firstReject(err);
      }
      throw err;
    } finally {
      if (inflight && inflight.controller === controller) inflight = null;
    }
  }

  function _offsetFor(index) {
    return Math.floor(index / pageSize) * pageSize;
  }

  return {
    getItem(i) {
      const off = _offsetFor(i);
      const page = pages.get(off);
      if (!page) return null;
      return page[i - off] ?? null;
    },

    getTotal() { return total; },

    ensureLoaded(i) {
      if (total === null) {
        // First-fetch case — load page 0 regardless of ``i`` so the
        // virtualizer gets a total to size the scrollbar.
        _fetchPage(0).catch(() => {});
        return;
      }
      const off = _offsetFor(i);
      if (!pages.has(off)) _fetchPage(off).catch(() => {});
      // Eager prefetch when the requested index sits in the back half
      // of its page — keeps scrolling smooth without firing on every
      // visible mount.
      const within = i - off;
      if (within >= pageSize * 0.6) {
        const nextOff = off + pageSize;
        if (nextOff < (total ?? Infinity) && !pages.has(nextOff)) {
          _fetchPage(nextOff).catch(() => {});
        }
      }
    },

    setParams(newParams) {
      const merged = { ...params, ...newParams };
      const before = _paramKey(params);
      const after = _paramKey(merged);
      params = merged;
      if (before === after) return;
      // Filter / sort / search shifted — every cached page is now
      // potentially wrong, drop them and refetch from 0.
      _abortInflight();
      pages.clear();
      total = null;
      _fetchPage(0).catch(() => {});
    },

    refresh() {
      // External mutations (entity created / deleted / favourite
      // toggled outside the page-cache mirror) make every cached
      // page potentially stale. Drop them and refetch page 0.
      _abortInflight();
      pages.clear();
      total = null;
      _fetchPage(0).catch(() => {});
    },

    abort() { _abortInflight(); },

    firstPagePromise,
  };
}
