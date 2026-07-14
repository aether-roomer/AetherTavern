/* Reminder brain editor — a single optional panel attached only to
 * contacts. ST cards' ``post_history_instructions`` imports here as a
 * ``Notes``-named row at depth 0.
 *
 * The reminder is structurally simpler than a regular brain: no
 * keys, no advanced condition, no cascade/recursion flags. The only
 * positional knob is ``depth`` — how many messages from the end of the
 * prompt the system message lands (0 = directly above the AER style
 * marker; capped at 10). The cap is enforced silently to avoid
 * surfacing the underlying rationale (cache stability) in the UI.
 */
import { el } from '../util.js';
import { icon, helpDetails } from '../ui.js';


/* renderReminderBrainEditor(reminderOrNull, onChange) returns a
 * container element. When ``reminderOrNull`` is null, an "[+ Add
 * reminder]" affordance is shown. When set, the editing controls
 * appear. ``onChange`` fires with the latest reminder (or null) on
 * every mutation so the surrounding autosaver picks it up.
 *
 * Field-level edits (name, content, depth) mutate the reminder object
 * in place — the DOM is already up to date, and tearing the panel down
 * mid-keystroke would yank focus and drop characters. Only structural
 * changes (null ↔ object, disabled toggle that flips the row's class)
 * trigger a rebuild.
 */
export function renderReminderBrainEditor(reminder, onChange) {
  const wrapper = el('div', { class: 'reminder-brain-panel' });
  refresh();
  return wrapper;

  function refresh() {
    wrapper.innerHTML = '';
    wrapper.append(_renderPanel(reminder, {
      onFieldChange: () => {
        // ``reminder`` mutated in place; just notify the autosaver.
        onChange(reminder);
      },
      onDisabledToggle: () => {
        reminder.disabled = !reminder.disabled;
        onChange(reminder);
        // Visual class flip happens via refresh below — cheap; no
        // input being typed in here.
        refresh();
      },
      onAdd: () => {
        reminder = {
          name: '',
          content: '',
          depth: 0,
          disabled: false,
        };
        onChange(reminder);
        refresh();
      },
      onDelete: () => {
        reminder = null;
        onChange(null);
        refresh();
      },
    }));
  }
}


function _renderPanel(reminder, h) {
  if (!reminder) {
    const addBtn = el('button', {
      type: 'button',
      class: 'btn btn-secondary',
      onclick: h.onAdd,
    }, '+ Add reminder');

    return el('div', { class: 'reminder-brain-empty' }, [
      el('div', { class: 'reminder-brain-empty-text' },
        'No reminder brain. Inject a short tail instruction (notes, format reminder, …) towards the end of context.'),
      addBtn,
    ]);
  }

  const root = el('div', {
    class: 'reminder-brain-row' + (reminder.disabled ? ' disabled' : ''),
  });

  const header = el('div', { class: 'reminder-brain-header' });
  const nameInput = el('input', {
    type: 'text',
    class: 'reminder-brain-name',
    value: reminder.name || '',
    placeholder: 'Untitled reminder',
    oninput: (e) => { reminder.name = e.target.value; h.onFieldChange(); },
  });
  const disabledBtn = el('button', {
    type: 'button',
    class: 'icon-btn brain-disabled-toggle' + (reminder.disabled ? ' active' : ''),
    title: reminder.disabled ? 'Enable' : 'Disable',
    'aria-label': reminder.disabled ? 'Enable' : 'Disable',
    onclick: h.onDisabledToggle,
  }, icon(reminder.disabled ? 'eye-off' : 'eye', 16));
  const deleteBtn = el('button', {
    type: 'button',
    class: 'icon-btn',
    title: 'Remove reminder',
    'aria-label': 'Remove reminder',
    onclick: h.onDelete,
  }, icon('trash', 16));
  header.append(nameInput, disabledBtn, deleteBtn);

  const depthRow = el('div', { class: 'reminder-brain-depth' });
  const depthLabel = el('label', { for: 'reminder-depth-input' }, 'Depth');
  const depthInput = el('input', {
    type: 'number',
    id: 'reminder-depth-input',
    min: '0',
    max: '10',
    step: '1',
    lang: 'en-US',
    value: String(Math.max(0, Math.min(10, Number(reminder.depth || 0)))),
    oninput: (e) => {
      const raw = e.target.value;
      if (raw === '') return;
      const parsed = parseInt(raw, 10);
      if (!Number.isFinite(parsed)) return;
      reminder.depth = Math.max(0, Math.min(10, parsed));
      h.onFieldChange();
    },
  });
  const depthHelp = helpDetails(
    'Number of user / assistant turns to keep between the reminder and the ' +
    'end of the chat (max 10). 0 places the reminder just before the model ' +
    'generates; larger values push it earlier in history. System messages do ' +
    'not count toward depth.',
    { label: 'Depth' },
  );
  depthRow.append(depthLabel, depthInput, depthHelp);

  const contentTextarea = el('textarea', {
    class: 'reminder-brain-content',
    rows: '4',
    placeholder: 'Tail instruction to inject towards the end of context…',
    oninput: (e) => { reminder.content = e.target.value; h.onFieldChange(); },
  }, reminder.content || '');

  root.append(header, depthRow, contentTextarea);
  return root;
}
