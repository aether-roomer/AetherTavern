/* Recursive editor for the brain "Advanced activation conditions" logic tree.
 *
 * One exported function — ``renderConditionNode(node, ctx)`` — returns a DOM
 * subtree representing ``node``. Mutations to ``node`` are surfaced via
 * ``ctx.onChange(newNode)``; the caller is responsible for re-rendering when
 * the type changes (we call ``onChange`` with the same node reference for
 * field-level edits, and with a brand-new node when the type changes).
 *
 * Mirrors the server-side discriminated union in ``server/models.py``:
 *   true | keyword | brain_active | relationship | style | length | japanese
 *   | random_chance | numeric_compare | string_compare | and | or | not
 */

import { el } from '../util.js';
import { makeSelect } from '../avatar_picker.js';
import { INTIMACIES, STYLES, RESPONSE_LENGTHS } from '../constants.js';
import { blankBrainKey } from './brain_row.js';


const NUMERIC_VARS = ['message_count', 'user_message_count', 'contact_message_count'];
const STRING_VARS  = ['story_text', 'memory_text', 'authors_note_text', 'tags'];

const NUMERIC_OPS = ['==', '!=', '<', '>', '<=', '>='];
const STRING_OPS  = ['equals', 'includes', 'starts_with', 'ends_with'];

const TYPE_OPTIONS = [
  ['true',             'Always true'],
  ['keyword',          'Keyword match'],
  ['brain_active',     'Brain entry active'],
  ['relationship',     'Relationship is'],
  ['style',            'Style is'],
  ['length',           'Length is'],
  ['japanese',         'Japanese flag'],
  ['tags',             'Tags include'],            // syntactic sugar — string_compare on tags
  ['random_chance',    'Random chance'],
  ['numeric_compare',  'Numeric comparison'],
  ['string_compare',   'String comparison'],
  ['and',              'AND group'],
  ['or',               'OR group'],
  ['not',              'NOT'],
];


function _defaultNode(type) {
  switch (type) {
    case 'true':            return { type: 'true' };
    case 'keyword':         return { type: 'keyword', keys: [blankBrainKey()] };
    case 'brain_active':    return { type: 'brain_active', brain_id: '' };
    case 'relationship':    return { type: 'relationship', relationship: INTIMACIES[0] };
    case 'style':           return { type: 'style', style: STYLES[0] };
    case 'length':          return { type: 'length', length: RESPONSE_LENGTHS.find(x => x) || 'medium' };
    case 'japanese':        return { type: 'japanese', expected: true };
    case 'tags':            return {
                              type: 'string_compare',
                              lhs: { kind: 'variable', variable: 'tags' },
                              op: 'includes',
                              rhs: { kind: 'literal', literal: '' },
                              case_sensitive: false,
                            };
    case 'random_chance':   return { type: 'random_chance', percent: 50 };
    case 'numeric_compare': return {
                              type: 'numeric_compare',
                              lhs: { kind: 'variable', variable: 'message_count' },
                              op: '>',
                              rhs: { kind: 'literal', literal: 0 },
                            };
    case 'string_compare':  return {
                              type: 'string_compare',
                              lhs: { kind: 'variable', variable: 'story_text' },
                              op: 'includes',
                              rhs: { kind: 'literal', literal: '' },
                              case_sensitive: false,
                            };
    case 'and':             return { type: 'and', children: [] };
    case 'or':              return { type: 'or',  children: [] };
    case 'not':             return { type: 'not', child: null };
    default:                return { type: 'true' };
  }
}


export function renderConditionNode(node, ctx) {
  const wrap = el('div', { class: 'cond-node', 'data-type': node?.type || 'true' });

  /* Top row: type selector */
  // String-compare with the "tags" variable on lhs is shown as the
  // syntactic-sugar "tags" type, but persisted as a string_compare.
  const displayType = _displayType(node);
  const typePicker = makeSelect({
    value: displayType,
    options: TYPE_OPTIONS.map(([v, l]) => ({ value: v, label: l })),
    onChange: (v) => { ctx.onChange(_defaultNode(v)); },
  });
  wrap.append(el('div', { class: 'cond-head' }, typePicker));

  /* Body */
  const body = el('div', { class: 'cond-body' });
  _renderBody(body, node, ctx);
  wrap.append(body);

  return wrap;
}


function _displayType(node) {
  if (!node) return 'true';
  if (node.type === 'string_compare'
      && node.lhs?.kind === 'variable'
      && node.lhs?.variable === 'tags'
      && node.op === 'includes'
      && node.rhs?.kind === 'literal') {
    return 'tags';
  }
  return node.type;
}


function _renderBody(body, node, ctx) {
  body.replaceChildren();
  switch (node.type) {
    case 'true':
      // No body.
      break;
    case 'keyword':
      _renderKeywordBody(body, node, ctx);
      break;
    case 'brain_active':
      _renderBrainActiveBody(body, node, ctx);
      break;
    case 'relationship':
      _renderEnumBody(body, node, ctx, 'relationship', INTIMACIES);
      break;
    case 'style':
      _renderEnumBody(body, node, ctx, 'style', STYLES);
      break;
    case 'length':
      _renderEnumBody(body, node, ctx, 'length',
        RESPONSE_LENGTHS.filter(x => x));   // drop the empty-string placeholder
      break;
    case 'japanese':
      _renderJapaneseBody(body, node, ctx);
      break;
    case 'random_chance':
      _renderRandomBody(body, node, ctx);
      break;
    case 'numeric_compare':
      _renderCompareBody(body, node, ctx, /* isString */ false);
      break;
    case 'string_compare':
      _renderCompareBody(body, node, ctx, /* isString */ true);
      break;
    case 'and':
    case 'or':
      _renderGroupBody(body, node, ctx);
      break;
    case 'not':
      _renderNotBody(body, node, ctx);
      break;
  }
}


function _renderKeywordBody(body, node, ctx) {
  if (!Array.isArray(node.keys)) node.keys = [blankBrainKey()];
  const list = el('div', { class: 'brain-keys cond-keys' });
  function rerender() {
    list.replaceChildren();
    node.keys.forEach((k, i) => {
      const patternInput = el('input', {
        type: 'text', value: k.pattern || '',
        placeholder: k.is_regex ? 'regex pattern' : 'word or phrase',
        oninput: e => { k.pattern = e.target.value; ctx.onChange(node); },
      });
      const regexBtn = el('button', {
        class: 'btn ghost toggle' + (k.is_regex ? ' on' : ''),
        title: 'Treat pattern as regex',
        onClick: () => { k.is_regex = !k.is_regex; ctx.onChange(node); rerender(); },
      }, '/.*/');
      const caseBtn = el('button', {
        class: 'btn ghost toggle' + (k.case_sensitive ? ' on' : ''),
        title: 'Case-sensitive',
        onClick: () => { k.case_sensitive = !k.case_sensitive; ctx.onChange(node); rerender(); },
      }, 'Aa');
      const rangeInput = el('input', {
        type: 'number', min: '0', placeholder: 'all',
        // ``lang="en-US"`` pins the input's locale so the decimal separator
        // stays ``.`` regardless of the browser locale; otherwise typing
        // ``,`` parses fine for a German user but our parseFloat (below /
        // elsewhere) only accepts ``.`` and silently zeroes the value.
        lang: 'en-US',
        value: k.search_range == null ? '' : String(k.search_range),
        title: 'Search range in characters (blank = whole context)',
        oninput: e => {
          const v = e.target.value.trim();
          k.search_range = v === '' ? null : Math.max(0, parseInt(v, 10) || 0);
          ctx.onChange(node);
        },
      });
      rangeInput.classList.add('brain-key-range');
      const delBtn = el('button', {
        class: 'btn ghost danger brain-key-del', title: 'Remove key',
        onClick: () => { node.keys.splice(i, 1); ctx.onChange(node); rerender(); },
      }, '✕');
      const row = el('div', { class: 'brain-key-row' }, patternInput, regexBtn);
      if (!k.is_regex) row.append(caseBtn);
      row.append(rangeInput, delBtn);
      list.append(row);
    });
    list.append(el('button', {
      class: 'btn ghost',
      onClick: () => { node.keys.push(blankBrainKey()); ctx.onChange(node); rerender(); },
    }, '+ Add key'));
  }
  rerender();
  body.append(list);
}


function _renderBrainActiveBody(body, node, ctx) {
  const hint = el('div', { class: 'brain-hint' },
    'No other brains available to reference in this scope. ' +
    'Add brains to the relevant entity (or chat participant) first.');

  function entriesToOptions(catalog) {
    return catalog.map(e => ({
      value: e.id,
      label: e.label || e.name || e.id,
    }));
  }

  const picker = makeSelect({
    value: node.brain_id || '',
    options: entriesToOptions(_resolveCatalog(ctx.brainCatalog)),
    placeholder: '— pick brain —',
    onChange: (v) => { node.brain_id = v; ctx.onChange(node); },
  });

  // Refresh catalog right before the popover opens so sibling brains added
  // or renamed mid-session show up live. Capture-phase click runs before
  // avatar-picker's own bubble-phase click handler — by then ``setOptions``
  // has updated currentOptions, and the subsequent ``open()`` / ``paintList()``
  // see the fresh list.
  const trigger = picker.querySelector('.avatar-picker-trigger');
  trigger.addEventListener('click', () => {
    const catalog = _resolveCatalog(ctx.brainCatalog);
    picker.setOptions(entriesToOptions(catalog));
    // Stale-ref clear is deferred via microtask — synchronously calling
    // ctx.onChange would tear down the trigger before the bubble-phase
    // click handler runs.
    if (node.brain_id && !catalog.some(e => e.id === node.brain_id)) {
      node.brain_id = '';
      picker.setValue('');
      queueMicrotask(() => ctx.onChange(node));
    }
  }, true);  // capture phase

  const initialCatalog = _resolveCatalog(ctx.brainCatalog);
  if (initialCatalog.length === 0) {
    body.append(hint);
  } else {
    body.append(picker);
  }
}


function _resolveCatalog(catalog) {
  if (typeof catalog === 'function') {
    try { return catalog() || []; } catch { return []; }
  }
  return catalog || [];
}


function _renderEnumBody(body, node, ctx, field, values) {
  body.append(makeSelect({
    value: node[field],
    options: values.map(v => ({ value: v, label: _titleCase(v) })),
    onChange: (v) => { node[field] = v; ctx.onChange(node); },
  }));
}


function _titleCase(s) {
  return (s || '').replace(/\b\w/g, c => c.toUpperCase());
}


function _renderJapaneseBody(body, node, ctx) {
  const cb = el('input', {
    type: 'checkbox',
    oninput: e => { node.expected = e.target.checked; ctx.onChange(node); },
  });
  cb.checked = node.expected !== false;
  body.append(el('label', { class: 'cond-aside' }, cb, ' Chat has the Japanese flag set'));
}


function _renderRandomBody(body, node, ctx) {
  const input = el('input', {
    type: 'number', min: '0', max: '100', step: '0.1',
    // See ``rangeInput`` above — pin the locale so ``.`` is the decimal
    // separator regardless of the browser's locale.
    lang: 'en-US',
    value: String(node.percent ?? 50),
    oninput: e => {
      const raw = e.target.value;
      // While the user is partway through typing (e.g. ``.``, ``0.``,
      // ``-``), ``parseFloat`` returns 0 / NaN. Clamping to 0 every
      // keystroke makes small numbers like ``0.0001`` impossible to type
      // because each intermediate character resets the model. Treat an
      // unparseable intermediate as "no value yet" and leave the model
      // untouched until the input parses to a finite number.
      const v = parseFloat(raw);
      if (raw === '' || !Number.isFinite(v)) return;
      node.percent = Math.max(0, Math.min(100, v));
      ctx.onChange(node);
    },
  });
  input.classList.add('cond-random-input');
  body.append(el('div', { class: 'cond-random-row' },
    input,
    el('span', {}, '% chance per turn'),
  ));
}


function _renderCompareBody(body, node, ctx, isString) {
  const ops = isString ? STRING_OPS : NUMERIC_OPS;
  const lhs = _renderValueEditor(node.lhs, ctx, isString, () => { ctx.onChange(node); });
  const opPicker = makeSelect({
    value: node.op,
    options: ops.map(o => ({ value: o, label: isString ? o.replace(/_/g, ' ') : o })),
    onChange: (v) => { node.op = v; ctx.onChange(node); },
  });
  opPicker.classList.add('cond-op-picker');
  const rhs = _renderValueEditor(node.rhs, ctx, isString, () => { ctx.onChange(node); });
  body.append(el('div', { class: 'cond-compare' }, lhs, opPicker, rhs));
  if (isString) {
    const cb = el('input', {
      type: 'checkbox',
      oninput: e => { node.case_sensitive = e.target.checked; ctx.onChange(node); },
    });
    cb.checked = !!node.case_sensitive;
    body.append(el('label', { class: 'cond-aside' }, cb, ' Case-sensitive'));
  }
}


function _renderValueEditor(value, ctx, isString, onMutate) {
  const vars = isString ? STRING_VARS : NUMERIC_VARS;
  if (!value) value = { kind: 'literal', literal: isString ? '' : 0 };
  const wrap = el('div', { class: 'cond-value' });

  const kindPicker = makeSelect({
    value: value.kind,
    options: [
      { value: 'literal',  label: 'value' },
      { value: 'variable', label: 'variable' },
    ],
    onChange: (v) => {
      value.kind = v;
      if (value.kind === 'literal') {
        value.literal = isString ? (value.literal ?? '') : (value.literal ?? 0);
      } else {
        value.variable = value.variable || vars[0];
      }
      onMutate();
      rerender();
    },
  });
  kindPicker.classList.add('cond-value-kind');

  function rerender() {
    wrap.replaceChildren(kindPicker);
    let inner;
    if (value.kind === 'variable') {
      inner = makeSelect({
        value: value.variable || vars[0],
        options: vars.map(v => ({ value: v, label: v })),
        onChange: (v) => { value.variable = v; onMutate(); },
      });
    } else if (isString) {
      inner = el('input', {
        type: 'text', value: String(value.literal ?? ''),
        oninput: e => { value.literal = e.target.value; onMutate(); },
      });
    } else {
      inner = el('input', {
        // Pin locale (see other numeric inputs in this file). Treat
        // intermediate unparseable values as "still typing" so users can
        // enter fractional values without each keystroke zeroing the model.
        type: 'number', lang: 'en-US', value: String(value.literal ?? 0),
        oninput: e => {
          const raw = e.target.value;
          const v = parseFloat(raw);
          if (raw === '' || !Number.isFinite(v)) return;
          value.literal = v;
          onMutate();
        },
      });
    }
    wrap.append(inner);
  }
  rerender();
  return wrap;
}


function _renderGroupBody(body, node, ctx) {
  if (!Array.isArray(node.children)) node.children = [];
  const list = el('div', { class: 'cond-children' });
  function rerender() {
    list.replaceChildren();
    if (!node.children.length) {
      list.append(el('div', { class: 'brain-hint' }, 'No conditions in this group.'));
    }
    node.children.forEach((child, i) => {
      const childWrap = renderConditionNode(child, {
        onChange: (newChild) => {
          if (newChild == null) {
            node.children.splice(i, 1);
          } else {
            node.children[i] = newChild;
          }
          ctx.onChange(node);
          rerender();
        },
        brainCatalog: ctx.brainCatalog,
      });
      const delBtn = el('button', {
        class: 'btn ghost danger cond-del',
        title: 'Remove condition',
        onClick: () => { node.children.splice(i, 1); ctx.onChange(node); rerender(); },
      }, '✕');
      list.append(el('div', { class: 'cond-child-row' }, childWrap, delBtn));
    });
    list.append(el('button', {
      class: 'btn ghost',
      onClick: () => {
        node.children.push(_defaultNode('true'));
        ctx.onChange(node);
        rerender();
      },
    }, '+ Add condition'));
  }
  rerender();
  body.append(list);
}


function _renderNotBody(body, node, ctx) {
  const slot = el('div', { class: 'cond-slot' });
  function rerender() {
    slot.replaceChildren();
    if (!node.child) {
      slot.append(el('button', {
        class: 'btn ghost',
        onClick: () => {
          node.child = _defaultNode('true');
          ctx.onChange(node);
          rerender();
        },
      }, '+ Add condition to negate'));
      return;
    }
    slot.append(renderConditionNode(node.child, {
      onChange: (newChild) => {
        node.child = newChild;
        ctx.onChange(node);
        rerender();
      },
      brainCatalog: ctx.brainCatalog,
    }));
    slot.append(el('button', {
      class: 'btn ghost danger cond-del',
      title: 'Clear',
      onClick: () => { node.child = null; ctx.onChange(node); rerender(); },
    }, 'Clear'));
  }
  rerender();
  body.append(slot);
}
