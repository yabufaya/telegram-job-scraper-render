# What to push to GitHub — exact scope

Your GitHub currently sits at commit **`a094388`**.
Your local copy is **4 commits ahead** and the working tree is clean.

---

## The direct answer

**One code file changed: `static/index.html`.**
Everything else is documentation. No Python file changed at all.

| File | Type | What changed |
|---|---|---|
| `static/index.html` | **CODE** | The apply-link fix (CSS + 3 functions + the card renderer) |
| `LICENSE` | docs | New file. MIT. Without it the repo is legally *all rights reserved* |
| `RUN_ON_RENDER.md` | docs | New file. Render deploy steps |
| `ENV_VARS.md` | docs | New file. All 21 env vars |
| `render.yaml` | docs | One comment line about free-tier sleeping |

`app/main.py`, `app/sources.py`, `app/llm_adapter.py`, `app/pipeline.py` — **untouched.**
Nothing on the server side needs updating.

---

## The 4 places inside `static/index.html`

### 1. CSS — around line 207–236

Three additions. Find `.result-text { ... }` and `.btn-solid:hover`:

```css
/* ADD after .result-text block: */
.result-text::after {
  content: ''; position: absolute; left: 0; right: 0; bottom: 0; height: 46px;
  background: linear-gradient(to bottom, transparent, var(--panel));
  pointer-events: none;
}
.result-text.expanded { max-height: none; }
.result-text.expanded::after { display: none; }
.result-text a {
  color: var(--accent); text-decoration: underline; text-underline-offset: 2px;
  overflow-wrap: anywhere; word-break: break-word;
}
.result-text a:hover { color: var(--accent-strong, var(--accent)); }

/* ADD after .btn-solid:hover: */
.btn-ghost {
  font-size: .84rem; padding: 7px 13px; border-radius: 8px; cursor: pointer; text-decoration: none;
  background: transparent; color: var(--text-soft); font-weight: 600;
  border: 1px solid var(--border-strong); transition: color .15s ease, border-color .15s ease;
}
.btn-ghost:hover { color: var(--text); border-color: var(--text-dim); }
```

### 2. New JS block — around line 1140

Inserted directly above `function renderResults(results) {`:

```js
var URL_RE = /https?:\/\/[^\s<>"']+/g;
var TELEGRAM_RE = /^https?:\/\/(t\.me|telegram\.me|telegram\.dog)\//i;

function cleanUrl(raw) {
  return raw.replace(/[)\]}.,;:!?\u201d\u2019]+$/, '');
}

function extractApplyUrl(text) {
  var found = (text || '').match(URL_RE) || [];
  for (var i = 0; i < found.length; i++) {
    var url = cleanUrl(found[i]);
    if (!TELEGRAM_RE.test(url)) return url;
  }
  return '';
}

function fillRichText(el, text) {
  el.textContent = '';
  URL_RE.lastIndex = 0;
  var last = 0, m;
  while ((m = URL_RE.exec(text)) !== null) {
    var url = cleanUrl(m[0]);
    if (m.index > last) el.appendChild(document.createTextNode(text.slice(last, m.index)));
    var a = document.createElement('a');
    a.href = url;
    a.target = '_blank';
    a.rel = 'noopener noreferrer';
    a.textContent = url;
    el.appendChild(a);
    last = m.index + url.length;
  }
  if (last < text.length) el.appendChild(document.createTextNode(text.slice(last)));
}
```

### 3. `renderResults()` — around line 1240

**DELETE these two lines:**

```js
const shortText = isLong ? text.slice(0, 300) + '…' : text;
textEl.textContent = shortText;
```

**REPLACE with one line:**

```js
fillRichText(textEl, text);
```

This is the actual bug fix. The old code cut the post at 300 characters, and
your link started at character 436, so it never reached the page.

### 4. Inside the expand toggle — around line 1273

**DELETE this line** from the click handler:

```js
textEl.textContent = expanded ? text : shortText;
```

The handler now only toggles the class:

```js
const open = textEl.classList.toggle('expanded');
toggle.textContent = open ? 'Show less' : 'Show full text';
```

And the toggle is created **after** the card is inserted into the page:

```js
if (textEl.scrollHeight > textEl.clientHeight + 8) { ... }
```

Measuring before insertion returns `scrollHeight === 0`, so the button would
never appear.

---

## The command to push

```bash
cd telegram-job-scraper-render
git push
```

That pushes all 4 commits. **One command. Nothing to stage** — the working tree
is already clean.

---

## Please do NOT hand-edit these into the file

You do not need to apply the snippets above by hand. They are already committed
locally as `bf41ef1`. The snippets are here so you can *see* the change, or
re-apply it if your local copy and this one ever drift apart.

If you edit `static/index.html` by hand, two things will bite you:

1. **The CSP hash.** `app/headers.py` computes a SHA-256 of the inline script at
   **import time**. Edit the JS without restarting the server and the browser
   silently blocks the whole script — the page loads but nothing works.
2. **Never use naive string replace.** Ask me, or rewrite the whole file. A
   `s.replace(old, new)` once destroyed this file (it grew to 82 MB).

---

## After pushing

Render redeploys automatically (about 1–2 minutes on free tier).

Then verify the fix on your live URL:

1. Scan `@ethio_job_vacancy1` for the last 14 days
2. Find the Huawei / Account Manager Intern card
3. It should show a green **Apply↗** button and the link in the text should be
   underlined and clickable

If the link is still plain text, the deploy has not finished — or the browser
cached the old page. Hard-refresh with **Ctrl+Shift+R**.
