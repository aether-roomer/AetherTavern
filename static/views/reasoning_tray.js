/**
 * Collapsible "Thoughts" tray that sits at the top of a bubble whose
 * message carries reasoning content. Bubble-mode-agnostic — works for
 * any origin (Generic emits reasoning today; AER might in the future).
 *
 * Renders reasoning through the same Generic markdown pipeline with
 * sanitize forced on, regardless of the user's HTML toggle. Reasoning
 * is upstream-emitted text the user didn't necessarily plan to read
 * as HTML; the safer default doesn't surprise anyone.
 *
 * Streaming: the tray exists from the first reasoning delta even when
 * collapsed. New deltas append into the body buffer and the body
 * re-renders cheaply (reasoning is typically short).
 */
import { el } from '../util.js';
import { icon } from '../ui.js';
import { renderGenericBubble } from '../render.js';


/**
 * Build a reasoning tray element for ``text``.
 *
 * @param {string} text - the accumulated reasoning content. Empty / falsy
 *   returns null so the caller can decide whether to mount.
 * @param {{startExpanded?: boolean, onToggle?: (expanded: boolean) => void}} opts
 *   - ``startExpanded``: open the tray on mount. Callers that want the
 *     user's expand state to survive a re-render (e.g. the post-stream
 *     refresh of a streaming message) pass ``true`` based on a tracked
 *     msg-id set.
 *   - ``onToggle``: invoked after every user-driven expand/collapse with
 *     the new expanded state. Lets callers keep an external "which msgs
 *     are expanded" set in sync with user interaction.
 * @returns {HTMLElement | null}
 */
export function renderReasoningTray(text, { startExpanded = false, onToggle } = {}) {
  if (!text) return null;
  let expanded = !!startExpanded;

  const arrowEl = el('span', { class: 'reasoning-arrow', 'aria-hidden': 'true' },
    expanded ? '▼' : '▶');
  const labelEl = el('span', { class: 'reasoning-label' }, 'Thoughts');
  const headerEl = el('button', {
    class: 'reasoning-header',
    type: 'button',
    'aria-expanded': expanded ? 'true' : 'false',
  }, arrowEl, labelEl);

  const bodyEl = el('div', { class: 'reasoning-body' });
  // Sanitize forced on for reasoning. Renderer returns a DocumentFragment.
  bodyEl.appendChild(renderGenericBubble(text, { sanitizeHtml: true }));
  if (!expanded) bodyEl.style.display = 'none';

  const applyExpanded = () => {
    arrowEl.textContent = expanded ? '▼' : '▶';
    headerEl.setAttribute('aria-expanded', expanded ? 'true' : 'false');
    bodyEl.style.display = expanded ? '' : 'none';
  };
  headerEl.addEventListener('click', () => {
    expanded = !expanded;
    applyExpanded();
    if (typeof onToggle === 'function') onToggle(expanded);
  });

  const wrap = el('div', { class: 'reasoning-tray' }, headerEl, bodyEl);
  // Stash the text on the element so streaming callers can update.
  wrap._setText = (newText) => {
    while (bodyEl.firstChild) bodyEl.removeChild(bodyEl.firstChild);
    bodyEl.appendChild(renderGenericBubble(newText, { sanitizeHtml: true }));
  };
  // Expose the current expand state so the streaming caller can read it
  // at stream-end and forward to whichever msg id the new bubble persists
  // under — otherwise refreshMessages re-renders the tray with the
  // default (collapsed) and the user's open state is lost.
  wrap._isExpanded = () => expanded;
  return wrap;
}
