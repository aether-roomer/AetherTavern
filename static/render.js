/**
 * Per-origin bubble rendering dispatch.
 *
 * AER and Manual messages render through the existing line-highlighter in
 * `aer_render.js` (bold/italic/subdued/code-fence, escape-then-emit).
 * Generic-origin messages render through a markdown pipeline: escape the
 * five HTML entities first (when sanitize is on), then `marked.parse`
 * with a small renderer override that reverses the escape inside code
 * spans + fenced code blocks so marked's own re-escape produces the
 * right visible output. With sanitize off, raw HTML passes through and a
 * `<|RAWHTML|>…<|/RAWHTML|>` region is spliced in verbatim, skipping markdown so
 * inline HTML/JS isn't mangled (see parseRawHtmlBlocks).
 *
 * Returns DocumentFragments (markdown side) or HTML strings (AER side)
 * to match each path's existing caller contract.
 *
 * Permissive mode also activates `<script>` tags the model emits: the
 * `executeScripts` option clone-replaces the inert parser-created scripts
 * with executable ones (see activateScripts), so a committed static render
 * runs them. Streaming re-renders and the reasoning tray leave it off.
 */
import { renderBubble } from './aer_render.js';

export { renderBubble };

// The five entities we use everywhere — chained `replace` is faster than a
// regex callback at our message sizes and keeps the function inlineable.
function escapeHtml(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

// Reverse the five-entity escape — used inside code/codespan renderer
// overrides so marked's own re-escape isn't applied on top of ours.
function decodeFiveEntities(s) {
  return String(s)
    .replace(/&#39;/g, "'")
    .replace(/&quot;/g, '"')
    .replace(/&gt;/g, '>')
    .replace(/&lt;/g, '<')
    .replace(/&amp;/g, '&');
}

// marked v18 uses the token-object renderer signature: each method
// receives `{type, raw, lang, text, ...}` and returns an HTML string.
// We override `code` and `codespan` so the pre-escape we apply on the
// caller side gets reversed inside code blocks before marked's
// builtin escape runs — otherwise marked sees `&lt;div&gt;` as literal
// content and re-escapes the `&`, producing `&amp;lt;div&amp;gt;`.
function makeRenderer(M) {
  const renderer = new M.Renderer();
  renderer.code = (token) => {
    const text = decodeFiveEntities(token.text || '');
    const lang = (token.lang || '').split(/\s+/, 1)[0] || '';
    const escaped = escapeHtml(text);
    const cls = lang ? ` class="language-${lang}"` : '';
    return `<pre><code${cls}>${escaped}\n</code></pre>`;
  };
  renderer.codespan = (token) => {
    const text = decodeFiveEntities(token.text || '');
    return `<code>${escapeHtml(text)}</code>`;
  };
  return renderer;
}

// Lazily-built reusable renderer instance.
let _renderer = null;
function getRenderer() {
  const M = (typeof window !== 'undefined' && window.marked) ? window.marked : null;
  if (!M) return null;
  if (_renderer === null) _renderer = makeRenderer(M);
  return { marked: M, renderer: _renderer };
}

// After marked parses, restore HTML comments in non-code-block content
// so the model can use them for hidden metadata without the markers
// showing up as visible `<!--` / `-->` text. Both ``<pre>...</pre>``
// (fenced code blocks) AND inline ``<code>...</code>`` spans keep the
// strict escape — the entities live as content the user is meant to
// see verbatim. The combined alternation in the regex matches a code
// region OR the entity, and the callback returns the region unchanged
// while rewriting bare entities.
//
// Regex-parsing-HTML caveat: the input here is HTML that marked itself
// produced from our escape-then-parse pipeline, so it's well-formed by
// construction; code-region content can never contain raw ``</pre>`` /
// ``</code>`` (marked's escape would have entity-encoded them inside
// our code/codespan renderer override). We're only locating regions to
// skip, not parsing their content — a regex is sufficient.
const _COMMENT_SKIP_OR_LT = /(<pre[^>]*>[\s\S]*?<\/pre>|<code[^>]*>[\s\S]*?<\/code>)|&lt;!--/g;
const _COMMENT_SKIP_OR_GT = /(<pre[^>]*>[\s\S]*?<\/pre>|<code[^>]*>[\s\S]*?<\/code>)|--&gt;/g;
function restoreHtmlComments(html) {
  return html.replace(_COMMENT_SKIP_OR_LT, (m, skip) => {
    if (skip) return skip;
    return '<!--';
  }).replace(_COMMENT_SKIP_OR_GT, (m, skip) => {
    if (skip) return skip;
    return '-->';
  });
}

/**
 * Does ``text`` contain anything the Generic renderer would actually paint?
 *
 * HTML comments render invisibly — ``renderGenericBubble`` restores them as
 * real ``<!-- -->`` nodes (sanitize on) and marked passes them through
 * verbatim (sanitize off) — so a reply that opens with ``<!-- meta -->``
 * produces a blank bubble until real content follows. Streaming uses this
 * to keep the typing indicator up instead of swapping in an empty bubble.
 *
 * Strips complete comments, then a trailing comment that hasn't closed yet
 * — including a bare partial opener (``<``, ``<!``, ``<!-``) that the next
 * delta might still turn into ``<!--``. Without that, a stream whose first
 * delta is just ``<`` would render a stray ``<`` and consume the indicator
 * before the comment even forms. What's left is checked for a non-whitespace
 * remainder: only comments + whitespace count as "nothing yet"; any other
 * text (a bare image, a link, prose) is visible. ``<!-x`` / ``<div`` keep
 * their non-comment content — the opener pattern only matches a true prefix
 * of ``<!--`` sitting at the very end.
 */
export function hasVisibleGenericContent(text) {
  if (!text) return false;
  const s = String(text)
    .replace(/<!--[\s\S]*?-->/g, '')
    .replace(/<(?:!(?:-(?:-[\s\S]*)?)?)?$/, '');
  return s.trim().length > 0;
}

// Escape hatch for the sanitize-OFF path: a `<|RAWHTML|>…<|/RAWHTML|>` region
// has its inner content spliced into the output verbatim, bypassing markdown
// entirely — so backticks, asterisks, underscores and other markdown-significant
// characters inside literal HTML/JS survive untouched. The `<|RAWHTML|>` /
// `<|/RAWHTML|>` delimiters themselves are dropped; surrounding text is still
// parsed as markdown. The distinctive `<|…|>` sentinel (rather than a bare
// `<html>`) can't collide with markup the region itself contains — a script
// that writes a document holding `</html>` no longer closes the region early,
// dropping its tail back into the markdown parser.
//
// The carve's second alternative catches an unterminated trailing
// `<|RAWHTML|>` — the streaming state where the opener has arrived but
// `<|/RAWHTML|>` hasn't yet — and treats the rest of the buffer as raw. Without
// it, a half-streamed region would flash through the markdown parser (mangling
// its `*`/`` ` ``/`_`) until the close landed, then snap to verbatim. Ordered
// after the closed-block alternative, so a real `<|/RAWHTML|>` later in the
// buffer still wins.
const RAW_HTML_BLOCK_RE = /<\|RAWHTML\|>([\s\S]*?)<\|\/RAWHTML\|>|<\|RAWHTML\|>([\s\S]*)$/g;
const RAW_HTML_OPEN_LEN = '<|RAWHTML|>'.length;
const RAW_HTML_CLOSE_LEN = '<|/RAWHTML|>'.length;

// Code-region protection. The carve must NOT fire inside markdown code: a
// fenced block or inline span the model uses to SHOW the sentinel as literal
// text (documenting the escape hatch) should keep `<|RAWHTML|>` visible as
// code, not have it eaten as a delimiter. We blank code regions to
// equal-length filler before locating the delimiters, so the carve regex
// can't match inside them; the inner content is then sliced from the ORIGINAL
// text (offsets preserved by the equal-length mask) and the surrounding
// segments — code regions intact — go to marked, which renders the fenced /
// inline sentinel as visible code through the renderer override.
//
// Fence/codespan detection approximates CommonMark (exact-length close
// fences; no 4-space indented code blocks). It errs generous: over-masking
// only declines to carve a delimiter (the pre-feature behaviour), whereas a
// miss would eat a code sample. An unterminated fence masks to end-of-buffer
// — matching CommonMark (an unclosed fence runs to EOF) and keeping a
// mid-stream fenced block's content protected.
const FENCE_BLOCK_RE = /^[ \t]{0,3}(`{3,}|~{3,})[^\n]*\n[\s\S]*?(?:^[ \t]{0,3}\1[ \t]*$|(?![\s\S]))/gm;
const INLINE_CODE_RE = /(`+)[\s\S]+?\1(?!`)/g;
function maskCodeRegions(text) {
  // Mask fences first so their backticks can't seed a spurious codespan
  // match. `\0` filler is never emitted — only its offsets and the carve
  // regex run against the masked copy.
  return text
    .replace(FENCE_BLOCK_RE, (m) => '\0'.repeat(m.length))
    .replace(INLINE_CODE_RE, (m) => '\0'.repeat(m.length));
}

function parseRawHtmlBlocks(text, marked, parseOpts) {
  const masked = maskCodeRegions(text);
  let out = '';
  let lastIndex = 0;
  let m;
  RAW_HTML_BLOCK_RE.lastIndex = 0;
  while ((m = RAW_HTML_BLOCK_RE.exec(masked)) !== null) {
    out += marked.parse(text.slice(lastIndex, m.index), parseOpts);
    // Slice the inner from the ORIGINAL text — the masked match may carry
    // `\0` filler where code lived inside the block. m[1] defined = closed
    // block (drop the trailing `<|/RAWHTML|>`); else an unterminated opener to end.
    const innerStart = m.index + RAW_HTML_OPEN_LEN;
    const innerEnd = m[1] !== undefined
      ? m.index + m[0].length - RAW_HTML_CLOSE_LEN
      : m.index + m[0].length;
    out += text.slice(innerStart, innerEnd);
    lastIndex = m.index + m[0].length;
  }
  out += marked.parse(text.slice(lastIndex), parseOpts);
  return out;
}

// Make `<script>` tags inside `root` executable. A script element the HTML
// parser produces (which is what `innerHTML = html` does) is flagged
// "already started" by the spec and never runs. Swapping each for a freshly
// created element clears that flag: a fresh script runs when it becomes
// connected to the document — and that holds even when the swap happens on a
// still-detached subtree, because the bubble is built detached in
// `renderBubbleRow` and only connected later when `refreshMessages` appends
// the row. `async = false` keeps external (`src`) scripts in source order;
// inline scripts run in tree order on connect regardless. The `_aerActivated`
// guard makes a repeat call on the same element a no-op (we never re-run the
// clones we just created).
//
// Re-execution is intentional: the static render path runs on every (re)mount
// — post-stream rebuild, reload, branch nav, and virtualization slide — so a
// bubble's scripts re-run each time it renders, matching "the bubble is a
// little web page" semantics. Scripts with global side effects (timers,
// document/window listeners, network) therefore re-run on scroll-back; the
// permissive (Sanitize HTML off) opt-in already carries that risk.
// A script the browser would execute as classic JS from its own source (no
// `src`, and a type that's empty or a JS MIME). Module / external / data
// scripts fall outside — Function() can't vet a module's `import`/`export`,
// and a `src` script has no inline source to vet or wrap.
function isClassicInlineScript(el) {
  if (el.src) return false;
  const t = (el.getAttribute('type') || '').trim().toLowerCase();
  return t === '' || t === 'text/javascript' || t === 'application/javascript'
      || t === 'text/ecmascript' || t === 'application/ecmascript';
}

// Wrap a classic inline script's body so a synchronous throw is reported to
// the console with a clear prefix, rather than surfacing as an uncaught error
// the browser pins on whatever connected the bubble (refreshMessages' append).
// `try {CODE` (no leading newline) keeps the body's line numbers aligned in
// the error's stack; the newline before `}` guards a trailing line comment in
// CODE. The body runs sloppy-mode (a leading "use strict" becomes a plain
// expression inside the block) — which suits the inline-handler global-function
// pattern; cross-script state should live on a window-scoped namespace, not
// top-level let/const (block-scoped in here). Only synchronous throws are
// caught — an error inside a later callback or timer surfaces uncaught, as on
// any page.
function wrapForRuntimeReport(code) {
  return 'try {' + code
    + '\n} catch (err) {'
    + ' console.error("A <script> in a message bubble threw at runtime:", err);'
    + ' }';
}

function activateScripts(root) {
  for (const old of root.querySelectorAll('script')) {
    if (old._aerActivated) continue;
    const code = old.textContent || '';
    const fresh = document.createElement('script');
    for (const attr of old.attributes) fresh.setAttribute(attr.name, attr.value);
    fresh.async = false;
    fresh._aerActivated = true;

    // Module / external scripts can't be syntax-checked or wrapped — run them
    // verbatim. Their errors surface uncaught, as on any web page.
    if (!isClassicInlineScript(old)) {
      fresh.textContent = code;
      old.replaceWith(fresh);
      continue;
    }

    // Syntax-check first. A classic inline script the model emitted can be
    // malformed — an unterminated string, or a `</script>` the HTML parser
    // split out of a string mid-token. Executing it throws an uncaught
    // SyntaxError (and a syntax error can't be wrapped — the wrapper would
    // fail to parse too), so Function() makes it catchable: we log the parse
    // error WITH its source so it can be fixed, and skip the script. Only a
    // genuine SyntaxError skips — any other failure (e.g. a CSP eval block)
    // isn't evidence the code is bad, so we fall through and run it.
    try {
      new Function(code);
    } catch (e) {
      if (e instanceof SyntaxError) {
        console.error(
          'Skipped a malformed <script> in a message bubble:',
          (e.message || e) + '\n— source —\n' + code,
        );
        old.remove();
        continue;
      }
    }

    fresh.textContent = wrapForRuntimeReport(code);
    old.replaceWith(fresh);
  }
}

/**
 * Render a Generic-origin bubble's body as a DocumentFragment.
 *
 * @param {string} text - raw message text as the model emitted it.
 * @param {{sanitizeHtml?: boolean, executeScripts?: boolean}} opts - when
 *   sanitizeHtml is truthy (default true), the five HTML entities are
 *   escaped before marked parses, so no raw `<script>` / `<a>` / `<img>`
 *   reaches the DOM outside markdown-generated tags. When false, raw HTML
 *   passes through and `<|RAWHTML|>…<|/RAWHTML|>` regions skip markdown entirely
 *   (see parseRawHtmlBlocks). executeScripts (default false) activates `<script>`
 *   tags in the permissive path — pass it only from a committed static
 *   render, never from a streaming re-render (see activateScripts).
 * @returns {DocumentFragment} ready to insert into a `.bubble.generic`.
 */
export function renderGenericBubble(text, { sanitizeHtml = true, executeScripts = false } = {}) {
  const fragment = document.createDocumentFragment();
  if (!text) return fragment;
  const got = getRenderer();
  if (!got) {
    // marked not loaded — defensive fallback: render as plain text in a
    // <pre> so the bubble at least shows something coherent.
    const pre = document.createElement('pre');
    pre.textContent = String(text);
    fragment.appendChild(pre);
    return fragment;
  }
  const { marked, renderer } = got;
  // ``breaks: true`` — a lone newline becomes a ``<br>``. In a chat bubble a
  // newline is intentional: a model that writes one line then the next means
  // two lines, not one reflowed paragraph. GFM's default (``breaks: false``)
  // collapses a single newline to a space, which reads as dropped formatting
  // here. Blank-line paragraph breaks and block structure are unaffected.
  const parseOpts = { gfm: true, breaks: true, renderer };
  let html = '';
  try {
    if (sanitizeHtml) {
      // Escape the five entities first so no raw tag reaches the DOM, then
      // restore the HTML comments marked emitted from our escaped markers.
      html = restoreHtmlComments(marked.parse(escapeHtml(String(text)), parseOpts));
    } else {
      // Permissive mode: raw HTML passes through, and `<|RAWHTML|>…<|/RAWHTML|>`
      // regions bypass the markdown parser entirely (see parseRawHtmlBlocks).
      html = parseRawHtmlBlocks(String(text), marked, parseOpts);
    }
  } catch (e) {
    // Defensive: marked shouldn't throw on well-formed input but a buggy
    // upstream payload could trip an edge case. Fall back to plain text.
    const pre = document.createElement('pre');
    pre.textContent = String(text);
    fragment.appendChild(pre);
    return fragment;
  }
  const wrapper = document.createElement('div');
  wrapper.innerHTML = html;
  // Permissive mode only: turn the inert parser-created `<script>` tags into
  // executable ones so they fire when this fragment lands in the connected
  // bubble. Gated on executeScripts so streaming's per-delta re-render and
  // the (sanitize-on) reasoning tray never trip it.
  if (executeScripts && !sanitizeHtml) activateScripts(wrapper);
  while (wrapper.firstChild) {
    fragment.appendChild(wrapper.firstChild);
  }
  return fragment;
}
