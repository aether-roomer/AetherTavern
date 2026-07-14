/* Windowed virtualizer for long list panes.
 *
 * Only rows whose estimated / measured top lies within the visible
 * viewport (plus a small buffer) are mounted in the DOM. The container
 * height is the cumulative sum of all row heights so the native
 * scrollbar matches reality. Heights are estimated on first mount and
 * cached per index after measurement, so subsequent passes don't shift.
 *
 * Shared by the chat list (server-paged), the zip-import picker, and the
 * four non-chat list panes (full client fetch).
 *
 * API
 * ---
 *
 *   const virt = createVirtList(scrollContainer, {
 *     count,                  // total item count (drives scrollbar height)
 *     getItem,                // (i) => item | null   (null = placeholder)
 *     renderItem,             // (item | null, i) => HTMLElement
 *     estimatedHeight,        // starting guess per row, in px
 *     gap = 8,                // px gap between rows, ignored after the last
 *     observeRows = true,     // per-row ResizeObserver (chat rows are
 *                             // fixed-shape; opt out to save observers).
 *     footerEl = null,        // optional element appended below the list
 *                             // (mobile disclaimer / spacer).
 *   });
 *
 * Returned methods:
 *   virt.setCount(n, { preserveScroll = false })
 *       Replace the item count. Resets cached heights. Resets scrollTop
 *       to 0 by default; pass ``preserveScroll`` for paged extension
 *       cases where the user has scrolled into the new range.
 *
 *   virt.invalidate(rangeOrAll = 'all')
 *       Unmount and re-render rows whose data has changed. Passing
 *       ``'all'`` re-renders every currently-mounted row; passing
 *       ``[start, end]`` re-renders rows in that half-open range that
 *       are currently mounted.
 *
 *   virt.refresh()
 *       Re-run visibility + reflow without changing count. Use after
 *       state shared with renderItem changes (e.g. activeChatId).
 *
 *   virt.scrollToIndex(i)
 *       Scroll the container so row ``i`` is at the top of the viewport.
 *       Used for per-tab scroll snapshot restore.
 *
 *   virt.mountedCount()
 *       For tests / debugging.
 *
 *   virt.destroy()
 *       Detach all listeners + observers. Leaves the container empty.
 *
 * Row mutations: rows can dispatch a bubbling ``virtRowResize``
 * CustomEvent to force a re-measure on the same frame. The
 * ResizeObserver catches most cases automatically; the event is
 * belt-and-suspenders for browsers that lag a frame.
 */

import { el } from './util.js';


export function createVirtList(scrollContainer, opts) {
  const {
    count: initialCount,
    getItem,
    renderItem,
    estimatedHeight,
    gap = 8,
    observeRows = true,
    footerEl = null,
  } = opts;

  const inner = el('div', { class: 'virt-list-inner' });
  scrollContainer.replaceChildren(inner);
  if (footerEl) scrollContainer.appendChild(footerEl);

  let count = initialCount;
  let heights = new Map();   // index → measured height (px)
  let tops = [];             // cumulative top offsets per index
  const mounted = new Map(); // index → row element
  const BUFFER = 200;        // px above/below visible for pre-mount

  function getHeight(i) { return heights.get(i) || estimatedHeight; }

  function recomputeTops() {
    tops.length = 0;
    let acc = 0;
    for (let i = 0; i < count; i += 1) {
      tops.push(acc);
      acc += getHeight(i);
      // Gap goes between rows but not after the final one.
      if (i < count - 1) acc += gap;
    }
    inner.style.height = `${acc}px`;
  }

  function visibleRange() {
    const scroll = scrollContainer.scrollTop;
    // Container clientHeight is 0 until the parent lays out (modal mount,
    // tab activate). Fall back to a generous default so the initial mount
    // covers what a typical viewport would; subsequent reflows driven by
    // the ResizeObserver trim back to the real measured size.
    const viewport = scrollContainer.clientHeight || 600;
    if (!count) return [0, 0];
    let start = 0;
    while (start < count && tops[start] + getHeight(start) < scroll - BUFFER) {
      start += 1;
    }
    let end = start;
    while (end < count && tops[end] < scroll + viewport + BUFFER) {
      end += 1;
    }
    return [start, end];
  }

  function reflow() {
    recomputeTops();
    const [start, end] = visibleRange();

    // Unmount out-of-window rows.
    for (const [i, row] of [...mounted]) {
      if (i < start || i >= end) {
        const h = row.offsetHeight;
        if (h > 0) heights.set(i, h);
        if (observeRows) rowResize.unobserve(row);
        row.remove();
        mounted.delete(i);
      }
    }

    // Mount in-window rows. Don't write ``top`` yet — we'll set it once
    // after measuring everyone, so a height change in one row pushes
    // the others down on the same paint.
    for (let i = start; i < end; i += 1) {
      if (mounted.has(i)) continue;
      const item = getItem(i);
      const row = renderItem(item, i);
      row._virtIndex = i;
      row.dataset.virtRow = '';
      row.style.position = 'absolute';
      row.style.left = '0';
      row.style.right = '0';
      inner.appendChild(row);
      mounted.set(i, row);
      if (observeRows) rowResize.observe(row);
    }

    // Re-measure every mounted row — their heights may differ from the
    // cached estimate (mobile layout settling, font / image load,
    // collapsible content). Only-when-changed updates aren't enough:
    // a skip-if-same branch would leave following rows pinned to the
    // wrong ``top`` after a row shrunk.
    let dirty = false;
    for (const [i, row] of mounted) {
      const h = row.offsetHeight;
      if (h > 0 && heights.get(i) !== h) {
        heights.set(i, h);
        dirty = true;
      }
    }
    if (dirty) recomputeTops();
    for (const [i, row] of mounted) row.style.top = `${tops[i]}px`;
  }

  let pending = false;
  function scheduleReflow() {
    if (pending) return;
    pending = true;
    requestAnimationFrame(() => { pending = false; reflow(); });
  }

  function onScroll() { scheduleReflow(); }
  scrollContainer.addEventListener('scroll', onScroll);

  // Modal / tab layout happens after mount: the container's clientHeight
  // is 0 at first reflow, which would mount only the buffer. The
  // observer re-flows once the real height settles, then on any later
  // resize.
  const containerResize = new ResizeObserver(() => scheduleReflow());
  containerResize.observe(scrollContainer);

  // Per-row resize observer: when a mounted row's height changes (e.g.
  // the user collapses a collapsible child list), update the cached
  // height and reflow so following rows shift up. Throttled via the
  // same ``pending`` flag.
  const rowResize = observeRows ? new ResizeObserver((entries) => {
    let dirtyRows = false;
    for (const entry of entries) {
      const row = entry.target;
      const idx = row._virtIndex;
      if (idx == null) continue;
      const h = row.offsetHeight;
      if (h > 0 && heights.get(idx) !== h) {
        heights.set(idx, h);
        dirtyRows = true;
      }
    }
    if (dirtyRows) scheduleReflow();
  }) : null;

  // Belt-and-suspenders: rows can dispatch ``virtRowResize`` after a
  // known mutation. ResizeObserver should catch this too, but in some
  // browsers the resize entry can lag a frame — the explicit event
  // lets us re-measure on the same frame the user clicked.
  function onVirtRowResize(e) {
    const row = e.target.closest && e.target.closest('[data-virt-row]');
    if (!row || row.parentElement !== inner) return;
    const idx = row._virtIndex;
    if (idx == null) return;
    requestAnimationFrame(() => {
      const h = row.offsetHeight;
      if (h > 0 && heights.get(idx) !== h) {
        heights.set(idx, h);
        reflow();
      }
    });
  }
  inner.addEventListener('virtRowResize', onVirtRowResize);

  reflow();

  return {
    setCount(n, { preserveScroll = false } = {}) {
      count = n;
      heights = new Map();
      for (const row of mounted.values()) {
        if (observeRows) rowResize.unobserve(row);
        row.remove();
      }
      mounted.clear();
      if (!preserveScroll) scrollContainer.scrollTop = 0;
      reflow();
    },

    invalidate(rangeOrAll = 'all') {
      const [start, end] = Array.isArray(rangeOrAll)
        ? rangeOrAll
        : [0, count];
      for (const [i, row] of [...mounted]) {
        if (i < start || i >= end) continue;
        if (observeRows) rowResize.unobserve(row);
        row.remove();
        mounted.delete(i);
      }
      scheduleReflow();
    },

    refresh() { scheduleReflow(); },

    scrollToIndex(i) {
      recomputeTops();
      if (i < 0 || i >= count) return;
      scrollContainer.scrollTop = tops[i];
    },

    mountedCount() { return mounted.size; },

    destroy() {
      scrollContainer.removeEventListener('scroll', onScroll);
      containerResize.disconnect();
      if (rowResize) rowResize.disconnect();
      inner.removeEventListener('virtRowResize', onVirtRowResize);
      for (const row of mounted.values()) row.remove();
      mounted.clear();
      inner.remove();
      if (footerEl && footerEl.parentElement === scrollContainer) {
        footerEl.remove();
      }
    },
  };
}
