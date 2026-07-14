/* Edit local brains attached to a chat message. */

import { api } from '../api.js';
import { setState, state } from '../state.js';
import { el, downloadJson, brainsToExportJson } from '../util.js';
import { openModal, closeModal, confirmModal, toast, helpDetails } from '../ui.js';
import { scheduleContextRefresh, refreshActiveChatMessages } from './chat.js';
import { markPending, clearPending } from '../save_queue.js';
import { renderBrainRow, ensureBrainShape, brainCatalogEntries } from './brain_row.js';


function _importBrainsBtn(draft, rerender) {
  const fileInput = el('input', {
    type: 'file',
    accept: '.json,.png,application/json,image/png',
    style: { display: 'none' },
    onchange: async (e) => {
      const file = e.target.files?.[0];
      e.target.value = '';
      if (!file) return;
      try {
        const result = await api.importBrains(file);
        const incoming = Array.isArray(result?.brains) ? result.brains : [];
        if (!incoming.length) { toast('No brains found in import.', 'error'); return; }
        for (const b of incoming) {
          ensureBrainShape(b);
          draft.push(b);
        }
        rerender();
        const src = result?.source_name ? ` from ${result.source_name}` : '';
        toast(`Imported ${incoming.length} brain${incoming.length === 1 ? '' : 's'}${src}.`);
      } catch (err) {
        toast(`Import failed: ${err.message || err}`, 'error');
      }
    },
  });
  const btn = el('button', { class: 'btn ghost', onClick: () => fileInput.click() }, 'Import brains…');
  return el('span', {}, fileInput, btn);
}


export function openEditBrainsModal(chatId, msg) {
  // Deep-clone the brains so cancel really cancels (the row builder mutates in
  // place). Each clone gets its activation shape filled in.
  const draft = (msg.brains || []).map(b => {
    const clone = JSON.parse(JSON.stringify(b));
    ensureBrainShape(clone);
    return clone;
  });

  // Catalog of brains the user can reference from a "Brain entry active"
  // condition: every brain participating in this chat (its contact, user,
  // scenario, plus the message's own local brains). Rebuilt each row render
  // so a brain added during this session shows up in sibling pickers.
  function getBrainCatalog() {
    return _buildChatBrainCatalog(chatId, draft);
  }

  const stack = el('div', { class: 'brain-stack' });

  function rerender() {
    stack.replaceChildren();
    if (draft.length === 0) {
      stack.append(el('div', { class: 'list-empty', style: { padding: '20px 0' } },
        'No brains. Click + Add brain to create one.'));
    }
    for (const b of draft) {
      const row = renderBrainRow(b, {
        onChange: () => { /* save on close — no autosave */ },
        onDelete: () => {
          const i = draft.indexOf(b);
          if (i >= 0) draft.splice(i, 1);
          rerender();
        },
        // Function so sibling-brain edits propagate to the picker on focus.
        brainCatalog: getBrainCatalog,
      });
      stack.append(row);
    }
  }
  rerender();

  const body = el('div', {},
    el('h3', {},
      'Local brains ',
      helpDetails(
        'Always-on brains appear as a system message just before this message '
        + 'in the prompt; brains with activation keys or advanced conditions '
        + 'get relocated to a single block ~2048 tokens before the end of '
        + 'context when they activate.',
        { label: 'About local brains' },
      ),
    ),
    el('p', { style: { color: 'var(--text-mute)', fontSize: '13px' } },
      'These brains attach to this message.'),
    stack,
    el('div', { class: 'brain-editor-actions', style: { display: 'flex', gap: '6px', flexWrap: 'wrap' } },
      el('button', { class: 'btn ghost', onClick: () => {
        const fresh = { name: '', content: '' };
        ensureBrainShape(fresh);
        draft.push(fresh);
        rerender();
        const sections = stack.querySelectorAll(':scope > .section');
        const last = sections[sections.length - 1];
        const input = last && last.querySelector('input[type="text"]');
        if (input) input.focus();
      } }, '+ Add brain'),
      _importBrainsBtn(draft, rerender),
      el('button', { class: 'btn ghost', onClick: () => {
        if (!draft.length) { toast('No brains to export.', 'error'); return; }
        const payload = brainsToExportJson('Message brains', draft);
        downloadJson('message-brains.json', payload);
      } }, 'Export brains'),
    ),
    el('div', { class: 'modal-actions' },
      el('button', { class: 'btn ghost', onClick: () => closeModal() }, 'Cancel'),
      el('button', {
        class: 'btn primary',
        onClick: async () => {
          const cleaned = draft.filter(b => b.name.trim() && b.content.trim());
          const incomplete = draft.length - cleaned.length;
          if (incomplete > 0) {
            // Guard against losing a half-typed brain — common slip is to
            // type a paragraph of content but forget the name. Surface what
            // would be dropped and let the user back out to fix it.
            const ok = await confirmModal(
              incomplete === 1
                ? 'Discard 1 incomplete brain?'
                : `Discard ${incomplete} incomplete brains?`,
              'Each brain needs both a name and content. Anything missing either will not be saved.',
              { danger: true, confirmLabel: 'Discard and save', stack: true },
            );
            if (!ok) return;
          }
          try {
            const patch = { brains: cleaned };
            const queueId = `${chatId}:${msg.id}`;
            markPending('chatMessage', queueId, patch);
            await api.updateMessage(chatId, msg.id, patch);
            clearPending('chatMessage', queueId);
            const messages = await api.listMessages(chatId);
            setState({ chatMessages: messages });
            refreshActiveChatMessages();
            scheduleContextRefresh(chatId);
            closeModal();
          } catch (e) { toast(`Save failed: ${e.message}`, 'error'); }
        },
      }, 'Save'),
    ),
  );

  openModal(body, { size: 'large' });
}


/* Build the catalog from this chat's participants — the contact (with its
 * scenarios), the user, and the global scenario currently selected on the
 * chat (if any). Plus the message's local brains under a "(local)" label.
 * Anything outside this chat can't influence its prompt, so showing brains
 * from unrelated entities would only confuse the picker. */
function _buildChatBrainCatalog(chatId, localBrains) {
  const out = brainCatalogEntries(localBrains || [], '(local)');
  const chat = (state.chats || []).find(c => c.id === chatId);
  if (!chat) return out;

  const contact = (state.contacts || []).find(c => c.id === chat.contact_id);
  if (contact) {
    out.push(...brainCatalogEntries(contact.brains || [], contact.name));
    for (const cs of (contact.scenarios || [])) {
      out.push(...brainCatalogEntries(cs.brains || [],
        `${contact.name} / ${cs.name || 'Scenario'}`));
    }
  }
  const user = (state.users || []).find(u => u.id === chat.user_id);
  if (user) {
    out.push(...brainCatalogEntries(user.brains || [], user.name));
  }
  const scenario = chat.scenario_id
    ? (state.scenarios || []).find(s => s.id === chat.scenario_id)
    : null;
  if (scenario) {
    out.push(...brainCatalogEntries(scenario.brains || [], scenario.name));
  }
  return out;
}
