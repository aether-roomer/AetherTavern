/* Shared brain editor row + activation UI primitives.
 *
 * A "brain" is `{ id, name, content, keys, cascades, blocks_recursion, advanced }`.
 * The first three are always-visible; everything else lives inside a
 * collapsible "Activation" section. Inside that section is a nested
 * collapsible "Advanced conditions" sub-section that hosts the logic-tree
 * editor. Default-collapsed for newly-added brains; default-open for any
 * brain that already has activation configured.
 *
 * Two consumers share this primitive:
 *   - ``contacts.js#renderBrainsEditor`` (auto-saved inline editor used by
 *     Contact / User / Scenario / ContactScenario).
 *   - ``edit_brains_modal.js`` (per-message brains; explicit save on close).
 */

import { el, autoGrowTextarea } from '../util.js';
import { confirmModal, helpDetails, icon } from '../ui.js';
import { renderConditionNode } from './brain_condition_editor.js';


export function newBrainId() {
  if (window.crypto?.randomUUID) {
    return crypto.randomUUID().replace(/-/g, '');
  }
  return Array.from({ length: 32 }, () => Math.floor(Math.random() * 16).toString(16)).join('');
}


export function blankBrainKey() {
  return {
    pattern: '',
    is_regex: false,
    case_sensitive: false,
    match_whole_words: false,
    search_range: null,
    search_messages: null,
  };
}


export function ensureBrainShape(b) {
  if (!b.id) b.id = newBrainId();
  if (!Array.isArray(b.keys)) b.keys = [];
  if (typeof b.cascades !== 'boolean') b.cascades = false;
  if (typeof b.blocks_recursion !== 'boolean') b.blocks_recursion = false;
  if (typeof b.disabled !== 'boolean') b.disabled = false;
  if (b.advanced === undefined) b.advanced = null;
  // Normalize each BrainKey with the new optional fields so saving an
  // older brain doesn't trip Pydantic on the way back.
  for (const k of b.keys) {
    if (typeof k.match_whole_words !== 'boolean') k.match_whole_words = false;
    if (k.search_messages === undefined) k.search_messages = null;
  }
  return b;
}


/* Convert a list of brain objects into ``{ id, label }`` catalog entries for
 * the "Brain entry active" condition. ``ownerLabel`` prefixes each brain's
 * name so users can disambiguate brains that share a name across entities. */
export function brainCatalogEntries(brains, ownerLabel = '') {
  const out = [];
  for (const b of (brains || [])) {
    if (!b || !b.id || !b.name) continue;
    out.push({
      id: b.id,
      label: ownerLabel ? `${ownerLabel} / ${b.name}` : b.name,
    });
  }
  return out;
}


export function isConditional(b) {
  return (Array.isArray(b.keys) && b.keys.length > 0) || b.advanced != null;
}


/* Render one brain row.
 *
 * - ``brain``        — the mutable brain object; mutations flow back through onChange.
 * - ``onChange()``   — called after every mutation so the parent can autosave.
 * - ``onDelete()``   — called when the user clicks Remove.
 * - ``brainCatalog`` — optional array of `{id, label}` exposed to the advanced
 *                       "Brain Entry Active" condition picker.
 */
export function renderBrainRow(brain, { onChange, onDelete, brainCatalog }) {
  ensureBrainShape(brain);
  // ``brainCatalog`` is either an array or a function returning an array.
  // Wrap it in a getter that re-evaluates on every call so newly-added /
  // renamed sibling brains show up live in the "Brain entry active" picker
  // without forcing the whole brain list to re-render (which would yank
  // focus out of whatever input the user is currently typing in).
  // Self is excluded — a brain can't reference itself (it isn't in the
  // activated set until its own condition fires).
  const getCatalog = () => {
    const raw = typeof brainCatalog === 'function' ? brainCatalog() : (brainCatalog || []);
    return raw.filter(entry => entry && entry.id !== brain.id);
  };

  const nameInput = el('input', {
    type: 'text',
    value: brain.name || '',
    oninput: e => { brain.name = e.target.value; onChange(); },
  });
  const disabledBtn = el('button', {
    type: 'button',
    class: 'icon-btn brain-disabled-toggle' + (brain.disabled ? ' active' : ''),
    title: brain.disabled ? 'Enable brain' : 'Disable brain',
    'aria-label': brain.disabled ? 'Enable brain' : 'Disable brain',
    onClick: () => {
      brain.disabled = !brain.disabled;
      onChange();
      root.classList.toggle('disabled', !!brain.disabled);
      disabledBtn.classList.toggle('active', !!brain.disabled);
      const iconEl = disabledBtn.querySelector('svg');
      if (iconEl) iconEl.replaceWith(icon(brain.disabled ? 'eye-off' : 'eye', 16));
      disabledBtn.title = brain.disabled ? 'Enable brain' : 'Disable brain';
    },
  }, icon(brain.disabled ? 'eye-off' : 'eye', 16));
  const contentTa = el('textarea', {
    rows: 4,
    oninput: e => { brain.content = e.target.value; onChange(); },
  });
  contentTa.value = brain.content || '';
  autoGrowTextarea(contentTa);

  /* Activation section */
  const activationDetails = el('details', { class: 'brain-activation' });
  if (isConditional(brain)) activationDetails.setAttribute('open', '');
  activationDetails.append(el('summary', {}, 'Activation'));

  /* Keys list */
  const keysBlock = el('div', { class: 'brain-keys' });
  function rerenderKeys() {
    keysBlock.replaceChildren();
    const keysHelp = helpDetails([
      el('div', {},
        'Brain activates when any key is found in the chat context. Keys are OR‑ed; the brain also activates if Advanced conditions match.'),
      el('ul', {},
        el('li', {},
          el('code', {}, '/.*/'),
          ' — treat the pattern as a regular expression instead of a literal substring.'),
        el('li', {},
          el('code', {}, 'Aa'),
          ' — require exact case (literal keys only; regex keys carry their own ',
          el('code', {}, '(?i)'),
          ' flag).'),
        el('li', {},
          el('code', {}, 'all'),
          ' — search range in characters from the end of context. Leave blank to scan the whole context; set a number (e.g. ',
          el('code', {}, '500'),
          ') to match only the trailing N characters.'),
      ),
    ], { label: 'About activation keys' });
    keysBlock.append(el('div', { class: 'help-row' },
      el('label', {}, 'Activation keys'),
      keysHelp,
    ));
    if (!brain.keys.length) {
      // The brain is unconditional (always on) when BOTH keys and advanced
      // are empty — anything else is misleading. Reflect the real state so
      // the user doesn't think "no keys" means "won't fire".
      const hasAdvanced = brain.advanced != null;
      keysBlock.append(el('div', {
        class: 'brain-hint',
      }, hasAdvanced
        ? 'No keys — brain fires through the Advanced conditions below.'
        : 'No keys and no Advanced conditions — brain is always on. Add a key or an Advanced condition to make it fire only when wanted.'));
    }
    brain.keys.forEach((k, i) => {
      const patternInput = el('input', {
        type: 'text',
        value: k.pattern || '',
        placeholder: k.is_regex ? 'regex pattern' : 'word or phrase',
        oninput: e => {
          k.pattern = e.target.value;
          _validatePattern(patternInput, k);
          onChange();
        },
      });
      _validatePattern(patternInput, k);

      const regexBtn = _toggleButton('/.*/', !!k.is_regex, 'Treat pattern as regular expression', () => {
        k.is_regex = !k.is_regex;
        onChange();
        rerenderKeys();
      });
      const caseBtn = _toggleButton('Aa', !!k.case_sensitive, 'Case-sensitive match', () => {
        k.case_sensitive = !k.case_sensitive;
        onChange();
        rerenderKeys();
      });
      const wholeWordsBtn = _toggleButton('·w·', !!k.match_whole_words, 'Whole-word match only', () => {
        k.match_whole_words = !k.match_whole_words;
        onChange();
        rerenderKeys();
      });
      const rangeInput = el('input', {
        type: 'number',
        min: '0',
        // ``lang="en-US"`` pins the input's decimal separator to ``.`` so
        // non-en-US browsers don't display / parse ``0,5`` etc. Integer
        // input here, but consistency across all numeric inputs matters
        // (and Firefox quirks aside).
        lang: 'en-US',
        placeholder: 'chars',
        value: k.search_range == null ? '' : String(k.search_range),
        title: 'Search the last N characters of context (blank = unbounded)',
        oninput: e => {
          const v = e.target.value.trim();
          k.search_range = v === '' ? null : Math.max(0, parseInt(v, 10) || 0);
          onChange();
        },
      });
      rangeInput.classList.add('brain-key-range');
      const messagesInput = el('input', {
        type: 'number',
        min: '0',
        lang: 'en-US',
        placeholder: 'msgs',
        value: k.search_messages == null ? '' : String(k.search_messages),
        title: 'Search the last N messages of context (blank = unbounded)',
        oninput: e => {
          const v = e.target.value.trim();
          k.search_messages = v === '' ? null : Math.max(0, parseInt(v, 10) || 0);
          onChange();
        },
      });
      messagesInput.classList.add('brain-key-messages');
      const delBtn = el('button', {
        class: 'btn ghost danger brain-key-del',
        title: 'Remove key',
        onClick: () => { brain.keys.splice(i, 1); onChange(); rerenderKeys(); },
      }, '✕');

      const toggles = el('div', { class: 'brain-key-toggles' }, regexBtn);
      if (!k.is_regex) toggles.append(caseBtn, wholeWordsBtn);
      const numerics = el('div', { class: 'brain-key-numerics' }, rangeInput, messagesInput);
      const row = el('div', { class: 'brain-key-row' },
        patternInput, toggles, delBtn, numerics);
      keysBlock.append(row);
    });
    keysBlock.append(el('button', {
      class: 'btn ghost',
      onClick: () => {
        brain.keys.push(blankBrainKey());
        onChange();
        rerenderKeys();
        // Focus the just-added pattern input.
        const rows = keysBlock.querySelectorAll(':scope > .brain-key-row');
        const lastInput = rows[rows.length - 1]?.querySelector('input[type="text"]');
        if (lastInput) lastInput.focus();
      },
    }, '+ Add key'));
  }
  rerenderKeys();
  activationDetails.append(keysBlock);

  /* Cascading + block-recursion flags */
  const cascadeCb = el('input', {
    type: 'checkbox',
    oninput: e => { brain.cascades = e.target.checked; onChange(); },
  });
  cascadeCb.checked = !!brain.cascades;
  const blockCb = el('input', {
    type: 'checkbox',
    oninput: e => { brain.blocks_recursion = e.target.checked; onChange(); },
  });
  blockCb.checked = !!brain.blocks_recursion;

  activationDetails.append(el('div', { class: 'brain-flags' },
    el('div', { class: 'help-row brain-flag' },
      el('label', {}, cascadeCb, ' Cascading'),
      helpDetails(
        "This brain's content is added to the search text when it activates, so other brains can react to it.",
        { label: 'About cascading' },
      ),
    ),
    el('div', { class: 'help-row brain-flag' },
      el('label', {}, blockCb, ' Block recursion'),
      helpDetails(
        'This brain only matches the original context, never cascade-augmented text.',
        { label: 'About block recursion' },
      ),
    ),
  ));

  /* Advanced conditions. The schema stores a single ``BrainCondition``
   * (or null), but the UI presents the root as a flat OR'd list so users
   * can add/remove top-level alternatives independently. 2+ entries are
   * stashed under an implicit ``CondOr`` on save; nested OR groups (built
   * via the type selector inside the tree) are unaffected. */
  const advancedDetails = el('details', { class: 'brain-advanced' });
  if (brain.advanced) advancedDetails.setAttribute('open', '');
  // The (?) is a portalled-popover button (see ``helpDetails`` in ui.js).
  // Click on the button calls preventDefault + stopPropagation so it
  // doesn't bubble up to toggle the parent ``<details>`` section.
  const advancedSummary = el('summary', {},
    el('span', { class: 'help-row' },
      el('span', {}, 'Advanced conditions'),
      helpDetails(
        "Add one or more top-level conditions. Multiple top-level entries are "
        + "OR-ed: the brain activates if any of them matches. Each entry can "
        + "be a comparison, a NOT, or an AND/OR group for nested logic. "
        + "If activation keys are also set on the brain, those are OR-ed in too.",
        { label: 'About advanced conditions' },
      ),
    ),
  );
  advancedDetails.append(advancedSummary);
  const advancedBody = el('div', { class: 'brain-advanced-body' });

  function _readAdvancedList() {
    if (!brain.advanced) return [];
    if (brain.advanced.type === 'or' && Array.isArray(brain.advanced.children)) {
      return brain.advanced.children.slice();
    }
    return [brain.advanced];
  }
  function _writeAdvancedList(list) {
    if (!list.length) brain.advanced = null;
    else if (list.length === 1) brain.advanced = list[0];
    else brain.advanced = { type: 'or', children: list };
  }

  function rerenderAdvanced() {
    advancedBody.replaceChildren();
    const list = _readAdvancedList();
    list.forEach((child, i) => {
      const nodeEl = renderConditionNode(child, {
        onChange: (newChild) => {
          const oldChild = list[i];
          if (newChild == null) {
            list.splice(i, 1);
          } else {
            list[i] = newChild;
          }
          _writeAdvancedList(list);
          onChange();
          // Only rebuild the whole conditions tree when the node was
          // *replaced* (type change → new node from ``_defaultNode``, or
          // removal). Field-level edits mutate the existing node in
          // place — the DOM is already up to date, and rebuilding on
          // every keystroke would replace the input element the user is
          // typing into, losing focus and any in-progress text (e.g. the
          // ``.`` in ``0.0001`` parses to NaN mid-keystroke; a forced
          // rebuild would slam the input back to the last finite value).
          if (newChild !== oldChild) {
            rerenderAdvanced();
          }
        },
        brainCatalog: getCatalog,
      });
      const delBtn = el('button', {
        class: 'btn ghost danger cond-del',
        title: 'Remove this condition',
        onClick: () => {
          list.splice(i, 1);
          _writeAdvancedList(list);
          onChange();
          rerenderAdvanced();
        },
      }, '✕');
      advancedBody.append(el('div', { class: 'cond-child-row' }, nodeEl, delBtn));
    });
    advancedBody.append(el('button', {
      class: 'btn ghost',
      onClick: () => {
        list.push({ type: 'true' });
        _writeAdvancedList(list);
        onChange();
        rerenderAdvanced();
      },
    }, '+ Add condition'));
  }
  rerenderAdvanced();
  advancedDetails.append(advancedBody);
  activationDetails.append(advancedDetails);

  const nameRow = el('div', { class: 'form-group brain-name-row' },
    el('label', {}, 'Name'),
    el('div', { class: 'brain-name-input-row', style: { display: 'flex', gap: '6px', alignItems: 'center' } },
      nameInput, disabledBtn,
    ),
  );
  const root = el('div', { class: 'section brain-row' + (brain.disabled ? ' disabled' : '') },
    nameRow,
    el('div', { class: 'form-group' },
      el('label', {}, 'Content'),
      contentTa,
    ),
    activationDetails,
  );
  if (onDelete) {
    // Confirm via the stacked-modal variant so this dialog layers on top
    // of any outer modal (e.g. the per-message brain editor) instead of
    // replacing it and wiping the draft.
    root.append(el('button', {
      class: 'btn ghost danger brain-row-del',
      onClick: async () => {
        const label = (brain.name || '').trim() || 'this brain';
        const ok = await confirmModal(
          `Remove ${label}?`,
          'The brain (and its activation keys + advanced conditions) will be permanently removed.',
          { danger: true, confirmLabel: 'Remove', stack: true },
        );
        if (ok) onDelete();
      },
    }, 'Remove'));
  }
  return root;
}


function _toggleButton(label, on, title, onClick) {
  return el('button', {
    class: 'btn ghost toggle' + (on ? ' on' : ''),
    title,
    onClick,
  }, label);
}


/* Set the ``.invalid`` class on a regex pattern input when the pattern fails
 * ``new RegExp`` construction. Visual hint only — server is the final
 * authority on pattern acceptance. */
function _validatePattern(inputEl, key) {
  if (!key.is_regex || !key.pattern) {
    inputEl.classList.remove('invalid');
    return;
  }
  try { new RegExp(key.pattern); inputEl.classList.remove('invalid'); }
  catch { inputEl.classList.add('invalid'); }
}
