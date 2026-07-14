/* Custom dropdown picker — used in place of native ``<select>`` everywhere.
 * The popover is appended to ``document.body`` and ``position: fixed`` so
 * it isn't clipped by the surrounding modal's ``overflow-y: auto`` scroll
 * container.
 *
 * Two entry points:
 *   - ``makeAvatarPicker`` for entity pickers (avatars, favorite stars,
 *     type-to-filter). Used by the new chat wizard and the chat info modal.
 *   - ``makeSelect`` for plain value/label dropdowns (no search, no
 *     avatars). Used for emotion / style / preset / context-size /
 *     font-size / etc.
 *
 * Option shape (full):
 *   {
 *     value:        unique string,
 *     label:        display text,
 *     getAvatar?:   () => HTMLElement (rebuilt on each repaint — no
 *                   shared-element / cloneNode footguns),
 *     favorite?:    boolean — filled star at the right edge,
 *     groupAfter?:  boolean — draws a divider below this row
 *                   (ignored when filter has hidden adjacent rows),
 *   }
 *
 * The returned wrapper exposes ``setValue(v)``, ``setOptions(o)``,
 * ``getValue()`` for controlled-component patterns.
 */

import { el } from './util.js';


/* Plain dropdown — no avatars, no favourites. Search is off by default
 * (most call sites are short fixed lists); pass ``searchable: true`` for
 * long lists like the Generic model picker. ``options`` accepts three
 * shapes: ``['v1', 'v2']``, ``[['v', 'label'], …]``, or full option
 * objects. Capitalisation / formatting is the caller's responsibility. */
export function makeSelect({
  value = '', options = [], onChange, placeholder, searchable = false,
} = {}) {
  const normalized = (options || []).map(o => {
    if (typeof o === 'string') return { value: o, label: o };
    if (Array.isArray(o)) return { value: o[0], label: o[1] != null ? o[1] : String(o[0]) };
    return o;
  });
  return makeAvatarPicker({
    value, options: normalized, onChange, placeholder,
    searchable,
  });
}


export function makeAvatarPicker({
  value = '', options = [], onChange,
  placeholder = 'Select…',
  searchable = true,
} = {}) {
  const wrap = el('div', {
    // ``with-avatars`` is the styling hook for triggers that may contain
    // an avatar — keeps the height stable across "with image" and "(none)"
    // selections so the picker doesn't visibly shrink between values.
    class: 'avatar-picker' + (searchable ? ' with-avatars' : ''),
  });
  const trigger = el('button', { type: 'button', class: 'avatar-picker-trigger' });
  const popover = el('div', {
    class: 'avatar-picker-popover hidden' + (searchable ? '' : ' no-search'),
  });
  const search = searchable
    ? el('input', { type: 'text', class: 'avatar-picker-search', placeholder: 'Search…' })
    : null;
  const listEl = el('div', { class: 'avatar-picker-list' });
  if (search) popover.append(search);
  popover.append(listEl);
  wrap.append(trigger);

  let currentValue = value;
  let currentOptions = options;
  let isOpen = false;
  let highlightedValue = null;
  let filterText = '';
  // Mouse-hover row highlighting is gated on this flag — set true only after
  // an actual ``mousemove`` inside the popover. Without the gate, opening
  // the picker with the cursor already parked over an option fires a
  // synthetic ``mouseenter`` that yanks the keyboard highlight away from
  // the originally-selected value. Reset to ``false`` on every keyboard
  // navigation step so the user can scroll the list past their cursor
  // without the mouse repeatedly stealing back the highlight.
  let _mouseMoved = false;
  // Native ``<select>`` lets you type letters while focused-and-closed to
  // jump to / select an option. Mirror that here: accumulate keystrokes
  // into a buffer that resets after a brief idle gap.
  let _typeBuffer = '';
  let _typeTimer = null;
  function _typeAppend(ch) {
    _typeBuffer += ch.toLowerCase();
    clearTimeout(_typeTimer);
    _typeTimer = setTimeout(() => { _typeBuffer = ''; }, 700);
    return currentOptions.find(o => (o.label || '').toLowerCase().startsWith(_typeBuffer));
  }

  function paintTrigger() {
    trigger.replaceChildren();
    const opt = currentOptions.find(o => o.value === currentValue);
    if (opt && opt.getAvatar) {
      const av = opt.getAvatar();
      av.classList.add('avatar-picker-trigger-avatar');
      trigger.append(av);
    }
    const labelText = opt ? (opt.label || '') : placeholder;
    trigger.append(el('span', {
      class: 'avatar-picker-label' + (opt ? '' : ' placeholder')
    }, labelText));
    trigger.append(el('span', { class: 'avatar-picker-chevron' }, '▾'));
  }

  function visibleOptions() {
    if (!filterText) return currentOptions;
    const f = filterText.toLowerCase();
    return currentOptions.filter(o => (o.label || '').toLowerCase().includes(f));
  }

  function paintList() {
    listEl.replaceChildren();
    const visible = visibleOptions();
    // Only reserve the leading avatar slot in rows when at least one option
    // in the list has an avatar — otherwise plain ``makeSelect`` dropdowns
    // (intimacy, style, font-size, …) would show a blank gutter on every
    // row. Computed against the FULL option list, not the filtered one, so
    // alignment stays consistent as the user types.
    const hasAnyAvatar = currentOptions.some(o => o.getAvatar);
    visible.forEach((opt, i) => {
      const row = el('button', {
        type: 'button',
        class: 'avatar-picker-option'
          + (opt.value === currentValue ? ' selected' : '')
          + (opt.value === highlightedValue ? ' kb-active' : ''),
        dataset: { value: opt.value },
        onClick: (e) => { e.preventDefault(); selectValue(opt.value); },
        // Update via class toggle (``setHighlight``), not a full ``paintList``
        // rebuild — rebuilding tears down + recreates buttons, which fires
        // a fresh ``mouseenter`` on whatever option the cursor happens to
        // be over and lets the mouse undo every keyboard arrow press.
        onMouseEnter: () => {
          if (!_mouseMoved) return;
          setHighlight(opt.value);
        },
      });
      if (opt.getAvatar) {
        const av = opt.getAvatar();
        av.classList.add('avatar-picker-option-avatar');
        row.append(av);
      } else if (hasAnyAvatar) {
        row.append(el('div', { class: 'avatar avatar-picker-option-avatar avatar-picker-spacer' }));
      }
      row.append(el('span', { class: 'avatar-picker-option-label' }, opt.label || ''));
      if (opt.favorite) row.append(_filledStar());
      listEl.append(row);
      if (opt.groupAfter && i < visible.length - 1) {
        listEl.append(el('div', { class: 'avatar-picker-divider' }));
      }
    });
    if (visible.length === 0) {
      listEl.append(el('div', { class: 'avatar-picker-empty' }, 'No matches'));
    }
  }

  function _filledStar() {
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('width', '14');
    svg.setAttribute('height', '14');
    svg.setAttribute('fill', 'currentColor');
    svg.classList.add('avatar-picker-option-star');
    const poly = document.createElementNS('http://www.w3.org/2000/svg', 'polygon');
    poly.setAttribute('points', '12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2');
    svg.appendChild(poly);
    return svg;
  }

  function positionPopover() {
    const rect = trigger.getBoundingClientRect();
    // Match the trigger's font so option rows in compact contexts (chat-config
    // bar with its 0.857rem font) don't render with a larger 1rem typeface
    // than the dropdown they spawned from.
    const cs = window.getComputedStyle(trigger);
    popover.style.fontSize = cs.fontSize;
    popover.style.fontFamily = cs.fontFamily;
    popover.style.left = `${rect.left}px`;
    // Trigger width is the lower bound; let the popover grow to fit its
    // widest option (capped at the remaining viewport width minus a small
    // gutter) so longer labels don't ellipsis-truncate inside the list.
    popover.style.minWidth = `${rect.width}px`;
    popover.style.width = 'max-content';
    const maxAvail = Math.max(160, window.innerWidth - rect.left - 16);
    popover.style.maxWidth = `${maxAvail}px`;

    // Vertical placement: prefer below the trigger; flip above if the popover
    // would overflow the viewport bottom (long lists like the 24-emotion
    // picker would otherwise be unreadable from a chat-pane bottom edge).
    // If neither side fits at full height, use whichever side has more room
    // and cap maxHeight so the popover stays fully visible.
    const margin = 8;
    popover.style.maxHeight = '';   // reset before measuring
    const naturalHeight = popover.offsetHeight;
    const spaceBelow = window.innerHeight - rect.bottom - 4 - margin;
    const spaceAbove = rect.top - 4 - margin;
    if (naturalHeight <= spaceBelow) {
      popover.style.top = `${rect.bottom + 4}px`;
    } else if (naturalHeight <= spaceAbove) {
      popover.style.top = `${rect.top - 4 - naturalHeight}px`;
    } else if (spaceBelow >= spaceAbove) {
      popover.style.top = `${rect.bottom + 4}px`;
      popover.style.maxHeight = `${Math.max(120, spaceBelow)}px`;
    } else {
      popover.style.top = `${margin}px`;
      popover.style.maxHeight = `${Math.max(120, spaceAbove)}px`;
    }
  }

  function open() {
    if (isOpen) return;
    isOpen = true;
    _mouseMoved = false;
    document.body.appendChild(popover);
    popover.classList.remove('hidden');
    filterText = '';
    if (search) search.value = '';
    highlightedValue = currentValue;
    // Paint list BEFORE positioning so ``offsetHeight`` reflects the real
    // popover height — otherwise the very first open measures an empty
    // popover (~50 px), decides "fits below" even with 24 emotion rows
    // queued up, and only future opens flip correctly.
    paintList();
    positionPopover();
    requestAnimationFrame(() => {
      if (search) search.focus();
      const row = listEl.querySelector('.avatar-picker-option.kb-active')
        || listEl.querySelector('.avatar-picker-option.selected');
      // ``center`` so a long alphabetical list (e.g. the Generic model
      // picker) lands with the user's current pick visible — ``nearest``
      // only nudges enough to bring the row to the closest edge, which
      // for a list opened with the selected row already in the rough
      // middle does nothing visible.
      if (row) row.scrollIntoView({ block: 'center' });
    });
    // Capture-phase scroll listener catches scrolls in any ancestor (modal,
    // body, …). Resize close avoids reflow drift; in-popover scroll is
    // explicitly excluded so the options list itself stays scrollable.
    document.addEventListener('scroll', _onAnyScroll, true);
    window.addEventListener('resize', close);
    document.addEventListener('click', _onDocClick);
    // Capture-phase keydown so Escape closes the picker BEFORE the modal's
    // bubble-phase Escape handler (registered by openModal) sees it. Without
    // this, Escape pressed before focus settles on the search input would
    // close the host modal instead of just the popover.
    document.addEventListener('keydown', _onDocKeydown, true);
  }

  function close() {
    if (!isOpen) return;
    isOpen = false;
    popover.classList.add('hidden');
    if (popover.parentNode) popover.parentNode.removeChild(popover);
    filterText = '';
    if (search) search.value = '';
    document.removeEventListener('scroll', _onAnyScroll, true);
    window.removeEventListener('resize', close);
    document.removeEventListener('click', _onDocClick);
    document.removeEventListener('keydown', _onDocKeydown, true);
  }

  // All keyboard navigation is handled at document capture phase so it
  // works whether or not a search input has focus, and so Escape always
  // closes the picker BEFORE any host modal's Escape listener fires.
  function _onDocKeydown(e) {
    if (!isOpen) return;
    if (e.key === 'Escape') {
      e.preventDefault(); e.stopPropagation();
      close(); trigger.focus();
    } else if (e.key === 'Tab') {
      // Close the picker and restore focus to the trigger; the default
      // Tab action then continues naturally from there to the next /
      // previous focusable element in source order. No preventDefault.
      close();
      trigger.focus();
    } else if (e.key === 'ArrowDown') {
      e.preventDefault(); e.stopPropagation();
      moveHighlight(1);
    } else if (e.key === 'ArrowUp') {
      e.preventDefault(); e.stopPropagation();
      moveHighlight(-1);
    } else if (e.key === 'Enter') {
      e.preventDefault(); e.stopPropagation();
      if (highlightedValue !== null) selectValue(highlightedValue);
    } else if (
      !searchable && e.key.length === 1
      && !e.ctrlKey && !e.altKey && !e.metaKey && e.key !== ' '
    ) {
      // Type-to-jump while the popover is open (non-searchable picker).
      // Searchable pickers route printable keys to their own search input.
      e.preventDefault(); e.stopPropagation();
      const match = _typeAppend(e.key);
      if (match) {
        _mouseMoved = false;
        setHighlight(match.value);
        const row = listEl.querySelector(`.avatar-picker-option[data-value="${CSS.escape(highlightedValue)}"]`);
        if (row) row.scrollIntoView({ block: 'nearest' });
      }
    }
  }

  function _onAnyScroll(e) {
    if (popover.contains(e.target)) return;
    close();
  }

  function _onDocClick(e) {
    if (wrap.contains(e.target)) return;
    if (popover.contains(e.target)) return;
    close();
  }

  function selectValue(v) {
    currentValue = v;
    paintTrigger();
    close();
    // Restore focus to the trigger so the user can keyboard-cycle / re-open
    // the same picker without first having to Tab back to it.
    trigger.focus();
    if (onChange) onChange(v);
  }

  function moveHighlight(dir) {
    const visible = visibleOptions();
    if (visible.length === 0) return;
    const idx = visible.findIndex(o => o.value === highlightedValue);
    let next;
    if (idx < 0) next = dir > 0 ? 0 : visible.length - 1;
    else next = (idx + dir + visible.length) % visible.length;
    // Reset hover-engaged flag — without it, scrolling the list past the
    // user's stationary cursor would fire a stream of mouseenter events
    // that yank the highlight back to whatever happens to be under the
    // cursor after each scroll step. The user has to actually move the
    // mouse again to re-engage hover.
    _mouseMoved = false;
    setHighlight(visible[next].value);
    const row = listEl.querySelector(`.avatar-picker-option[data-value="${CSS.escape(highlightedValue)}"]`);
    if (row) row.scrollIntoView({ block: 'nearest' });
  }

  // In-place class swap so the cursor's resting position over the rebuilt
  // listEl can't fire a synthetic ``mouseenter`` and override the keyboard.
  function setHighlight(newValue) {
    if (highlightedValue === newValue) return;
    if (highlightedValue !== null) {
      const old = listEl.querySelector(`.avatar-picker-option[data-value="${CSS.escape(highlightedValue)}"]`);
      if (old) old.classList.remove('kb-active');
    }
    if (newValue !== null) {
      const nextRow = listEl.querySelector(`.avatar-picker-option[data-value="${CSS.escape(newValue)}"]`);
      if (nextRow) nextRow.classList.add('kb-active');
    }
    highlightedValue = newValue;
  }

  trigger.addEventListener('click', (e) => {
    e.preventDefault();
    isOpen ? close() : open();
  });

  // Trigger-level keydown so Enter / Space open the picker without bubbling
  // to the surrounding modal's "Enter submits" handler. Modifier-Enter
  // (Ctrl/Cmd/Alt) is left to bubble — callers like the example-message
  // row use Ctrl+Enter to add another row, and shouldn't be intercepted
  // when the user happens to be focused on a sender / emotion picker.
  trigger.addEventListener('keydown', (e) => {
    const plainEnter = (e.key === 'Enter' || e.key === ' ')
      && !e.ctrlKey && !e.metaKey && !e.altKey;
    if (plainEnter) {
      e.preventDefault();
      e.stopPropagation();
      isOpen ? close() : open();
    } else if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      e.stopPropagation();
      if (!isOpen) open();
    } else if (
      !isOpen && e.key.length === 1
      && !e.ctrlKey && !e.altKey && !e.metaKey && e.key !== ' '
    ) {
      // Type-to-select while the picker is closed — matches native
      // ``<select>`` UX. Selects (no opening) the first option whose label
      // starts with the typed prefix; the buffer resets after 700 ms idle.
      e.preventDefault();
      e.stopPropagation();
      const match = _typeAppend(e.key);
      if (match && match.value !== currentValue) {
        currentValue = match.value;
        paintTrigger();
        if (onChange) onChange(currentValue);
      }
    }
  });

  if (search) {
    search.addEventListener('input', () => {
      filterText = search.value;
      const visible = visibleOptions();
      highlightedValue = visible.length ? visible[0].value : null;
      paintList();
    });
  }

  popover.addEventListener('mousemove', () => { _mouseMoved = true; });

  paintTrigger();

  wrap.setValue = (v) => {
    currentValue = v;
    paintTrigger();
    if (isOpen) paintList();
  };
  wrap.setOptions = (newOpts) => {
    currentOptions = newOpts || [];
    paintTrigger();
    if (isOpen) paintList();
  };
  wrap.getValue = () => currentValue;

  return wrap;
}
