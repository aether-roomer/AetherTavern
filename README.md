# AetherTavern

AetherTavern is your local frontend for chatting with AetherRoom contacts on NovelAI's Xialong model. Think SillyTavern, but for Aether.

It aims to provide a polished experience on both Desktop and Mobile with keyboard shortcuts, intuitive touch controls, a clean layout and overall snappy UI.

## Features

- **AetherRoom format** — chat with emotive contacts showing up to 24 different emotion sprites, depending on the situation.
- **Generic OpenAI API** — use any OpenAI compatible provider API with a chat completion endpoint
- **Multimodal** - when using a multimodal model via an OpenAI compatible provider, you can send images
- **Branching chats** — reroll any reply, swap between sibling branches, bookmark moments you want to come back to.
- **Multi-bubble messages** - with per-bubble emotion sprites.
- **Contacts, user personas, and reusable scenarios** - each with attachable brains/lore blocks.
- **Per-chat tuning** — intimacy, style, response length, and tags.
- **Streaming generation** with cancel and a live typing indicator for your contact.
- **Import / export** — imports AetherRoom formats, SillyTavern character cards, SillyTavern world-info JSON, NovelAI lorebook JSON and PNG, and bulk-import zips of any combination. Exports JSON compatible with SomethingRoom, plus PNG cards for easy sharing.
- **Mobile-friendly** — edge-swipe back, swipe between branches, tap-to-reveal controls.
- **Keyboard shortcuts** - for desktop power users: arrow-key navigation, type-to-focus, branch cycling, and more.
- **Nine color themes** - including both light and dark, with configurable UI and content font sizes.
- **Auto-save everywhere** - with conflict resolution if you have the same chat open in two tabs.
- **Fully local** — listens on `127.0.0.1`, no telemetry, your data stays in `data/` on disk.
- **Macros** — you can use many macros familiar from SillyTavern
- **Unsafe HTML rendering** - for when you want to get pwned by your model or just really need some charts rendered
- **More than just roleplay** - you can also use AetherTavern for general interactions with LLMs, not only for roleplay

## Getting started

### 1. Install uv

uv ships its own Python toolchain, so installing it is all you need — it
will fetch a matching Python interpreter on first `uv sync`.

**macOS / Linux:**

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

**Windows (PowerShell):**

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Other install methods (Homebrew, winget, pipx, pip) are documented at
<https://docs.astral.sh/uv/getting-started/installation/>. Restart your
shell after install so `uv` lands on `PATH`.

### 2. Install dependencies

```bash
uv sync --locked
```

This works the same on Linux, macOS, and Windows — uv reads `uv.lock` and
materialises the virtualenv under `.venv/`, including the Python interpreter.

### 3. Start the server

The first time you run the server it will download the GLM-4.6 tokenizer
from Hugging Face (~5 MB) and cache it.

**Linux / macOS:**

```bash
./start.sh
```

**Windows (PowerShell):**

```powershell
.\start.ps1
```

If PowerShell refuses with an execution-policy error, either run

```powershell
powershell -ExecutionPolicy Bypass -File .\start.ps1
```

or relax the policy for your user once with `Set-ExecutionPolicy
-Scope CurrentUser RemoteSigned`.

Both scripts write a pidfile under `data/`, stop any previous instance
they launched, and start `uv run uvicorn` in its place — so re-running
restarts cleanly.

### 4. Open the app and add your API token

Open <http://127.0.0.1:8000> in your browser, head to **Settings**, and
paste your NovelAI API token. You can find it in your
[NovelAI account settings](https://docs.novelai.net/en/text/usersettings/account).

## Notes

- AetherTavern listens on `127.0.0.1` only — it is single-user and not designed for remote access.
- Your data lives in `data/` (gitignored). Back it up yourself.
- Override `AETHER_HOST`, `AETHER_PORT`, or `AETHER_DATA_DIR` in the environment if you want to listen elsewhere or keep state in a different directory.
- If the chosen port is already bound (another local service, a leftover process), the launcher counts up to find a free one and prints e.g. `Port 8000 is in use; falling back to port 8001.` Pass `--no-evade-used-port` to the launcher (`uv run python -m server --no-evade-used-port`) to disable that and fail loudly instead. To pass arbitrary uvicorn flags, invoke uvicorn directly: `uv run uvicorn server.main:app --host 127.0.0.1 --port 8000 --log-level debug`.

## Formatting cheat sheet

### Messages

A few lightweight conventions let the model tell narration, action, and
thought apart from spoken dialogue. Mix them as you would in prose:

- Plain text is spoken dialogue — no surrounding quotation marks; the bubble itself frames the speech.
- `*sets the cup down*` — physical actions, gestures, and stage directions.
- `(this is going nowhere)` — silent thoughts only the speaker hears.
- `((OOC: quick note about pacing))` — out-of-character asides, for nudging the scene without breaking the fiction. The `OOC:` prefix is part of the marker.
- `<don't say that aloud>` — telepathic or otherwise unspoken mind-to-mind dialogue.
- `**bold**` for strong emphasis, `_italics_` for softer emphasis. Single asterisks are reserved for actions, so reach for underscores when you want italics.
- `"a phrase like this"` — quotations, citations, or singling out a word inside a sentence.

### Japanese (日本語)

For chats with the Japanese toggle on, use the full-width conventions below
instead. The list is in Japanese.

- セリフは記号で囲まず、そのまま書く。
- `（…）` — 動作・しぐさは全角の丸括弧で。
- `【…】` — 内心・心の声は全角の隅付き括弧で。
- `「…」` — 引用句や強調したい単語、`って`・`と` で引用される語句。
- `『…』` — 特殊な用語、必殺技名、作品名など。

### Macros

Macros expand inside the contact, user, and scenario definition fields
(including greetings) and in global brain content. They are **not** expanded
inside chat message bodies — the one exception is `{{roll}}`, which is baked
into user-typed text at send-time so dice results persist in chat history.
Names are case-insensitive. Unknown macros pass through literally so a token
the system doesn't recognise is visible rather than silently vanishing.

The macro set tracks SillyTavern's where the semantics map cleanly, so most
ST character cards work without edits.

**Identity & characters**

- `{{char}}` / `$contact` / `$c` — the contact's name
- `{{user}}` / `$user` / `$u` — the active user persona's name
- `{{persona}}` — the user persona's description
- `{{description}}` / `{{charDescription}}` — the contact's persona/personality block (AER stores ST's `description` and `personality` fields together in `persona`)
- `{{personality}}` / `{{charPersonality}}` — the contact's appearance (AER doesn't split persona/personality, so this slot maps to appearance for cards that reference both)
- `{{scenario}}` / `{{charScenario}}` — the active scenario's scene
- `{{charFirstMessage}}` — the contact's greeting (overridden by the contact-scenario greeting when one is set)
- `{{group}}` / `{{groupNotMuted}}` — the contact's name (AetherTavern chats are always solo)
- `{{notChar}}` — empty (no other participants)

The `$`-prefixed shorthands only match when followed by whitespace,
punctuation, or end-of-string, so `$user.` expands but `$username` is left
alone.

**AER namespace** — direct access to individual fields, useful when ST's
collapsed names aren't precise enough:

- `{{contact.name}}` / `.species` / `.gender` / `.pronouns` / `.persona` / `.appearance` / `.description` / `.greeting` / `.tags`
- `{{user.name}}` / `.species` / `.gender` / `.pronouns` / `.persona` / `.appearance` / `.description` / `.tags`
- `{{scenario.name}}` / `.environment` / `.scene` / `.description` / `.tags` / `.greeting`
- `{{contact.pronouns.subject}}` / `.object` and `{{user.pronouns.subject}}` / `.object` — individual halves of the AER two-part `she/her` pronoun string. A one-part value (e.g. just `it`) is used for both slots.

**Date and time**

- `{{date}}` / `{{isodate}}` — today's local date as `YYYY-MM-DD`
- `{{time}}` — local time as 12-hour `HH:MM AM/PM`
- `{{time24}}` / `{{isotime}}` — local time as 24-hour `HH:MM`
- `{{weekday}}` — the current weekday name
- `{{datetimeformat::FMT}}` — custom format using the common moment.js tokens (`YYYY`, `MM`, `DD`, `HH`, `mm`, `ss`, `MMM`/`MMMM`, `ddd`/`dddd`, `Do`, `A`/`a`, …). Bracket literals (`[at]`) pass through.

**Randomness**

- `{{random::a::b::c}}` — picks one option fresh every evaluation
- `{{pick::a::b::c}}` — picks one option *stably per chat*; editing the macro (or its options) re-rolls naturally. Use the dice button in the chat-settings bar to force a re-roll of every `{{pick}}` in the chat.
- `{{roll::1d20+3}}` — rolls dice (`NdM+K` / `NdM-K` / `dM`). Capped at 100 dice and 1000 sides. Also expands in user-typed message bodies (baked at send-time).

**Chat history**

- `{{lastMessage}}`, `{{lastUserMessage}}`, `{{lastCharMessage}}` — the most recent message (full text), or the most recent of that sender
- `{{lastMessageId}}` — 0-based index of the most recent message
- `{{allChatRange}}` — `0-N` over the active path, or empty when the chat has none
- `{{firstIncludedMessageId}}` — index of the first message still in the context window after rollover
- `{{idleDuration}}` — human-readable time since the last user message (`"5 minutes"` etc.)
- `{{lastSwipeId}}` — number of sibling branches at the active tip
- `{{currentSwipeId}}` — 1-based position of the active tip among its siblings

**Runtime state**

- `{{model}}` — the model name from settings
- `{{maxContext}}` / `{{maxContextTokens}}` — base context size of the active preset
- `{{maxPrompt}}` / `{{maxPromptTokens}}` — context size minus the reserved response budget
- `{{maxResponse}}` / `{{maxResponseTokens}}` — response token budget
- `{{isMobile}}` — `"true"` if the request came from a touch device, `"false"` otherwise
- `{{lastGenerationType}}` — `"normal"` for a fresh reply, `"swipe"` when re-rolling

**Format helpers**

- `{{space::N}}`, `{{newline::N}}` — N spaces / newlines (default 1)
- `{{noop}}` — empty
- `{{//anything}}` — comment, expands to empty
- `{{reverse::value}}` — reversed string

**Control flow** — used heavily by Generic-mode context presets.

- `{{if CONDITION}}…{{/if}}` — scoped form. Body renders when CONDITION
  is truthy; falsy renders nothing. With an `{{else}}` split, the unchosen
  branch is dropped.
- `{{if CONDITION}}…{{else}}…{{/if}}` — scoped form with an else branch.
- `{{#if CONDITION}}…{{/if}}` — preserve-whitespace flag. The default
  scoped form trims + dedents the chosen branch; `#` keeps leading and
  trailing whitespace verbatim (needed when an internal `\n` is
  load-bearing for inter-block spacing).
- `{{if CONDITION::then}}` / `{{if CONDITION::then::else}}` — inline
  ternary form. Convenient for short conditionals inside a single line.
  Branches are passed verbatim (no whitespace stripping), so
  `{{if X::\n\n::\n}}` produces literal `"\n\n"` or `"\n"`.
- `{{if !CONDITION}}…{{/if}}` — `!` prefix negates the condition.

CONDITION may be:

- A bare macro name (no braces) — auto-resolved against the registry, so
  `{{if contact.persona}}` works equivalent to `{{if {{contact.persona}}}}`.
- A `{{…}}`-wrapped macro expression — resolves through the regular pass
  before evaluation. `{{if {{contact.persona}}{{contact.appearance}}}}` is
  the OR idiom (truthy iff either field is non-empty after concatenation).
- A literal string — used as-is.

A condition is **falsy** iff (after macro expansion + strip + lowercase)
it equals the empty string, `"false"`, `"0"`, or `"off"`. Everything
else is truthy.

The scoped form is **lazy** — the unchosen branch's macros are never
resolved, so `{{roll}}` in the dropped branch doesn't burn entropy.
The inline form is **eager** — both branches resolve before the if
handler picks. Prefer scoped for branches that contain side-effectful
or expensive macros.

**Comparison & boolean helpers** (AER additions, not in SillyTavern):

- `{{eq::A::B}}` / `{{neq::A::B}}` — string equality, case-sensitive.
  Returns `"true"` or `""`.
- `{{lt::A::B}}` / `{{gt::A::B}}` / `{{lte::A::B}}` / `{{gte::A::B}}` —
  numeric comparisons. Args are parsed as floats; non-numeric → `""`.
- `{{and::A::B::…}}` — `"true"` iff every arg is truthy.
- `{{or::A::B::…}}` — `"true"` iff any arg is truthy.

All comparators return `"true"` / `""` so they compose with `{{if}}`:

```
{{if {{eq::{{datetimeformat::HH}}::12}}}}It's noon.{{/if}}
{{if {{neq::{{contact.species}}::human}}}}You are not human.{{/if}}
{{if {{or::{{eq::{{chat.intimacy}}::close}}::{{eq::{{chat.intimacy}}::romantic}}}}}}…warm-tone block…{{/if}}
```

**Generic-mode helpers**

- `{{global_brains}}` — every unconditional brain attached to the contact,
  user, scenario, or attached brain libraries, rendered as
  `----\\nName\\nContent\\n` blocks (the same format AER's system-prompt
  path uses). Conditional brains feed through the activation engine
  separately.
- `{{chat.title}}`, `{{chat.tags}}`, `{{chat.intimacy}}`, `{{chat.style}}`,
  `{{chat.response_length}}` — chat-level settings, as the value of each
  enum field; empty when no chat is in scope.
- `{{chat.cjk}}` — `"true"` / `"false"` (matches `{{ismobile}}`); empty
  with no chat in scope. Use `{{if chat.cjk}}` and `{{if !chat.cjk}}` to
  branch on Japanese mode.

### Authoring context-preset prompts (Generic mode)

The Default seeded preset demonstrates several useful idioms:

**Optional line, leading-newline preserved.** Use `#` so the body's
leading `\n` isn't auto-trimmed:

```
{{#if contact.species}}
Species: {{contact.species}}{{/if}}
```

Without `#`, the body would be trimmed to `Species: VALUE`, losing the
line break — adjacent text would concatenate.

**Optional section.** Wrap the whole section; use `{{or}}` to mean "any
of":

```
{{#if {{or::{{scenario.environment}}::{{scenario.scene}}::{{chat.tags}}}}}}
<SCENARIO>
…
</SCENARIO>{{/if}}
```

**Leading blank line vs flush continuation.** When a line should have a
blank line above it if a multi-line field preceded, and be flush
otherwise, use the inline if-else (whose branches are passed verbatim):

```
{{#if contact.tags}}{{if {{or::{{contact.persona}}::{{contact.appearance}}}}::

::
}}Tags: {{contact.tags}}{{/if}}
```

The inline branches are literally `\n\n` (then, the blank line) and
`\n` (else, flush). Wrapping the condition in `{{or}}` makes the cond
value `"true"` / `""` — values that don't collide with any registered
macro name, so the bare-name auto-resolve doesn't accidentally fire on
substituted persona / appearance text.

### HTML and comments

**Hidden comments.** Anything wrapped in an HTML comment — `<!-- like this -->`
— renders invisibly in the bubble (in both AER and Generic modes) while staying
verbatim in the saved message. That makes it a poor-man's "chain-of-thought at
home": prompt a model with no native reasoning mode to think inside a comment
before it answers, and it reasons on the page without cluttering the view. A
comment inside a code block stays visible as literal text.

**Raw HTML and the sanitizer (Generic mode).** Generic-mode replies render as
Markdown. **Sanitize HTML** (Settings → Generic mode) is on by default: raw
HTML the model writes is escaped and shown as literal text, so only the tags
Markdown itself generates reach the page.

Turning Sanitize HTML **off is genuinely risky.** Every tag the model emits
then becomes live DOM — images and links that phone home, inline event handlers
that can run JavaScript, markup that wrecks the layout. **`<script>` tags run**
(see below). Treat it like opening an unknown web page the model handed you,
and leave it on unless you trust both the model and the provider relaying it.

With sanitize off, wrap a region in `<|RAWHTML|>…<|/RAWHTML|>` to pass it through
**verbatim**, untouched by Markdown — the `<|RAWHTML|>` / `<|/RAWHTML|>` markers
themselves are dropped, and everything between them (multiple lines included)
renders exactly as written. Reach for it when the model emits HTML or JS
containing `*`, `` ` `` or `_` that Markdown would otherwise mangle into
formatting, or markup the page would otherwise interpret (e.g. a document built
in a string that contains `</html>`). The marker is a deliberately distinctive
sentinel so it can't collide with the HTML inside the region. The marker shown
inside a code block is left alone as visible code; only markers outside code
blocks delimit a passthrough region.

**Scripts execute.** With sanitize off, `<script>` tags in a reply run once the
bubble has rendered, so the model can emit self-contained interactive widgets.
The app is a long-lived SPA, so `DOMContentLoaded` and the window `load` event
fired at boot, before any message exists — an injected script must do its setup
immediately, not wait on those events (they won't call back). A script whose
source doesn't parse — an unterminated string, or a `</script>` the HTML parser
split out of a string mid-token — is skipped, and its parse error + source are
logged to the console. A script that parses but throws at runtime is wrapped so
the error is caught and logged with a `A <script> in a message bubble threw…`
prefix, rather than surfacing as an uncaught error pinned on the render loop —
so the console tells you which broke and why. (Only synchronous throws are
caught; an error inside a later callback or timer still surfaces uncaught.)
Because a bubble is re-rendered from its saved text on every (re)mount — page
reload, branch navigation, or simply scrolling it back into view — its scripts
**re-run each time**, web-page style. Scripts that keep lasting state, timers,
or listeners should therefore be written to be idempotent: stash a handle on a
`window`-scoped namespace and, on re-run, detect what's already there and reuse
it instead of starting a second `setInterval` or stacking another listener. A
naive script that blindly re-creates them leaks a little more on every revisit.
Inline event handlers (`onclick="…"`) and `<|RAWHTML|>…<|/RAWHTML|>`
passthrough work without a script.

The seeded **Default** context preset carries an always-enabled "HTML output"
block, gated by `{{#if !sanitize_html}}`, that explains all of the above to the
model — so it stays out of the prompt while sanitize is on and appears only
once you turn sanitize off. (Seeded presets are created on first boot; an
existing install keeps the preset it already has.)

## For developers

If you'd rather run uvicorn yourself:

```bash
uv run uvicorn server.main:app --host 127.0.0.1 --port 8000
```

### Tests

AetherTavern comes with a suite of tests. You can run them like this:

```bash
uv run pytest
```

Set `AETHER_SKIP_TOKENIZER=1` to skip the GLM-4.6 tokenizer download on a fresh
checkout — useful for fast iteration:

```bash
AETHER_SKIP_TOKENIZER=1 uv run pytest --ignore=tests/test_ui.py
```

`tests/test_ui.py` is a Playwright suite. By default it auto-spawns an
isolated `uvicorn` on a free port with a temporary `AETHER_DATA_DIR`, so
running it doesn't touch the data directory of your real instance.

`uv sync` installs the Playwright Python package, but the chromium
binary it drives is a separate download. Grab it once with:

```bash
uv run playwright install chromium
```

### Layout

- `server/` — FastAPI app, AER format builders, rollover, inference client
- `static/` — frontend (HTML/CSS/JS, no build step)
- `tests/` — pytest + Playwright suites
- `data/` — runtime data (gitignored): contacts, users, scenarios, chats, settings, presets
