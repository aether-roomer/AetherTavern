/* AER message rendering. Triple-backtick fences become plain code blocks
 * (no language tag, no syntax highlighting). The rest is split per line
 * and run through an inline highlighter that recognises **, *, and _
 * as bold / italic+subdued / italic markers. Markers are consumed when a
 * pair is recognised on the same line, and degrade to literal characters
 * when unbalanced. Text is HTML-escaped before emission.
 *
 * One exception applies to inline (non-code-block) content: ``<!--`` and
 * ``-->`` are restored after escaping so the model can use HTML comments
 * (e.g. for hidden metadata) without them showing up as visible text.
 * The contents of the resulting comment stay HTML-escaped, so nothing
 * inside can be parsed as a tag. Code blocks keep the strict escape so
 * literal ``<!--`` / ``-->`` characters render as visible code.
 *
 * Comment-only blocks (one or more ``<!--...-->`` alone on a line/block,
 * possibly multi-line, with optional surrounding whitespace) absorb a
 * bracketing ``\n``. Otherwise the invisible comment would still leave a
 * blank visible line under ``white-space: pre-wrap`` because the ``\n``s
 * outside the comment markers survive HTML parsing.
 *
 * With ``collapseBlankLines`` a lone blank line (exactly ``\n\n``) collapses
 * to a single line break on display. Under ``white-space: pre-wrap`` every
 * ``\n`` is a visible break, so the model's habitual ``\n\n`` paragraph
 * spacing would show an empty line between every paragraph. Runs of three or
 * more newlines are left intact — that much vertical space reads as
 * deliberate, and the collapse only touches inline (non-fence) segments, so
 * blank lines inside code fences survive. The flag is opt-in: callers enable
 * it for AI/contact bubbles and leave it off for user-typed text, which
 * renders verbatim. */

const KANJI_KANA_RANGES = [
  [0x4E00, 0x9FA0],
  [0x3041, 0x3094],
  [0x30A1, 0x30F4],
];
const KANJI_KANA_SINGLES = new Set([0x30FC, 0x3005, 0x3006, 0x3024, 0x30F6]);

function isKanjiKana(ch) {
  if (!ch) return false;
  const cp = ch.codePointAt(0);
  if (KANJI_KANA_SINGLES.has(cp)) return true;
  for (const [lo, hi] of KANJI_KANA_RANGES) {
    if (cp >= lo && cp <= hi) return true;
  }
  return false;
}

const SPACE_LIKE_PUNCT = new Set([
  '[', '(', '{', '<',
  '「', '『', '（', '【', '［', '〈', '《', '〔', '｛', '〖', '〚',
  '"', "'",
  '“', '‘', '‟', '‚', '„', '‛',
  '‹', '›', '〝', '〞',
  '»', '«',
  '—', '–', '―', '‒',
]);

function isSpaceLike(ch) {
  if (!ch) return false;
  if (/\s/.test(ch)) return true;
  if (isKanjiKana(ch)) return true;
  return SPACE_LIKE_PUNCT.has(ch);
}

function isSpecial(ch) {
  if (!ch) return true;
  if (isSpaceLike(ch)) return true;
  if (/[A-Za-z0-9]/.test(ch)) return false;
  if (isKanjiKana(ch)) return false;
  return true;
}

const MARKERS = ['**', '*', '_'];
const STYLE_FLAGS = {
  '**': ['bold'],
  '*':  ['italic', 'subdued'],
  '_':  ['italic'],
};

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function renderAerLine(line) {
  if (line === '') return '';

  const enabled = { '**': false, '*': false, '_': false };
  const stack = [];
  const bookmarks = { '**': [], '*': [], '_': [] };
  const runs = [{ text: '', on: new Set(), off: new Set() }];

  function newRun() {
    runs.push({ text: '', on: new Set(), off: new Set() });
    return runs.length - 1;
  }

  let prevSpaceLike = true;
  let prevMarker = false;
  let prevKanjiKana = false;

  function tryMatch(pos, m) {
    if (line.substr(pos, m.length) !== m) return false;
    if (!enabled[m] && !(prevSpaceLike || prevMarker)) return false;
    const f = pos + m.length < line.length ? line[pos + m.length] : '';
    if (!enabled[m] && !prevMarker && isSpecial(f)
        && f !== '*' && f !== '_' && !prevKanjiKana) {
      return false;
    }
    if (f === m) return false;
    return true;
  }

  let pos = 0;
  while (pos < line.length) {
    let consumed = null;
    for (const m of MARKERS) {
      if (tryMatch(pos, m)) { consumed = m; break; }
    }
    if (consumed) {
      const m = consumed;
      if (!enabled[m]) {
        stack.push(m);
        bookmarks[m].push(newRun());
        enabled[m] = true;
      } else {
        const innerToRestore = [];
        while (stack.length > 0 && stack[stack.length - 1] !== m) {
          const inner = stack.pop();
          bookmarks[inner].push(newRun());
          innerToRestore.push(inner);
        }
        stack.pop();
        bookmarks[m].push(newRun());
        for (let i = innerToRestore.length - 1; i >= 0; i--) {
          const inner = innerToRestore[i];
          bookmarks[inner].push(newRun());
          stack.push(inner);
        }
        let on = true;
        for (const idx of bookmarks[m]) {
          for (const flag of STYLE_FLAGS[m]) {
            (on ? runs[idx].on : runs[idx].off).add(flag);
          }
          on = !on;
        }
        bookmarks[m] = [];
        enabled[m] = false;
      }
      prevMarker = true;
      pos += m.length;
    } else {
      const ch = line[pos];
      runs[runs.length - 1].text += ch;
      prevSpaceLike = isSpaceLike(ch);
      prevKanjiKana = isKanjiKana(ch);
      prevMarker = false;
      pos += 1;
    }
  }

  for (const m of MARKERS) {
    if (enabled[m]) {
      const idx = bookmarks[m][0];
      runs[idx].text = m + runs[idx].text;
    }
  }

  const state = { bold: false, italic: false, subdued: false };
  const out = [];
  for (const run of runs) {
    for (const flag of run.off) state[flag] = false;
    for (const flag of run.on) state[flag] = true;
    if (run.text === '') continue;
    const classes = [];
    if (state.bold) classes.push('aer-bold');
    if (state.italic) classes.push('aer-italic');
    if (state.subdued) classes.push('aer-subdued');
    if (classes.length === 0) {
      out.push(escapeHtml(run.text));
    } else {
      out.push(`<span class="${classes.join(' ')}">${escapeHtml(run.text)}</span>`);
    }
  }
  return out.join('');
}

const FENCE_RE = /\n?```\n?([\s\S]*?)\n?```\n?/g;

export function renderBubble(text, { collapseBlankLines = false } = {}) {
  if (!text) return '';
  const s = String(text);
  let out = '';
  let lastIndex = 0;
  let m;
  FENCE_RE.lastIndex = 0;
  while ((m = FENCE_RE.exec(s)) !== null) {
    out += renderInlineSegment(s.slice(lastIndex, m.index), collapseBlankLines);
    out += `<pre><code>${escapeHtml(m[1])}</code></pre>`;
    lastIndex = m.index + m[0].length;
  }
  out += renderInlineSegment(s.slice(lastIndex), collapseBlankLines);
  return out;
}

const COMMENT_BLOCK_LEAD_RE = /(?<=^|\n)[ \t]*((?:<!--[\s\S]*?-->[ \t]*)+)\n/g;
const COMMENT_BLOCK_TAIL_RE = /\n[ \t]*((?:<!--[\s\S]*?-->[ \t]*)+)$/;

// Runs of EXACTLY two newlines — the lookbehind/lookahead exclude any run of
// three or more, which is preserved as deliberate spacing. See the file
// header for why a lone blank line collapses to a single break on display.
const DOUBLE_NEWLINE_RE = /(?<!\n)\n\n(?!\n)/g;

function renderInlineSegment(seg, collapseBlankLines) {
  if (!seg) return '';
  // Collapse before the comment-block passes: a comment sitting under a
  // blank line then absorbs a single ``\n`` cleanly instead of leaving one
  // behind. Opt-in per call — off for user-typed text (rendered verbatim).
  if (collapseBlankLines) seg = seg.replace(DOUBLE_NEWLINE_RE, '\n');
  seg = seg
    .replace(COMMENT_BLOCK_LEAD_RE, (_, body) => body.trim())
    .replace(COMMENT_BLOCK_TAIL_RE, (_, body) => body.trim());
  return seg.split('\n').map(renderAerLine).join('\n')
    .replace(/&lt;!--/g, '<!--')
    .replace(/--&gt;/g, '-->');
}
