# Push this to GitHub (5 minutes)

The repo is already committed locally — one commit, 22 files, no secrets, `.env` ignored.
You only need to point it at your GitHub repo and push.

---

## Step 1 — create an empty repo on GitHub

1. Go to **github.com/new**
2. Repository name: `telegram-job-scraper-automation` (or anything)
3. **Public** (so Render can read it, and so you can share the link)
4. **Do NOT** tick "Add a README", ".gitignore" or "license" — the folder already has them
5. Click **Create repository**

---

## Step 2 — push from your machine

Open a terminal **inside the project folder** (the one containing `app/`, `static/`, `render.yaml`):

```bash
git remote add origin https://github.com/<YOUR-USERNAME>/telegram-job-scraper-automation.git
git branch -M main
git push -u origin main
```

If it asks for a password, GitHub requires a **Personal Access Token**, not your account password:

1. GitHub → your avatar → **Settings** → **Developer settings** → **Personal access tokens** → **Tokens (classic)**
2. **Generate new token (classic)** → tick the **`repo`** scope → set an expiry → generate
3. Copy the token and paste it **as the password** when git asks

---

## Step 3 — confirm nothing secret went up

Open your repo on GitHub and check:

- [ ] `.env` is **not** listed (only `.env.example`)
- [ ] `session_string.txt` is **not** listed
- [ ] no `*.session` files
- [ ] `static/index.html` opens and shows the three provider cards
- [ ] the README renders

If you ever accidentally push a key: **revoke it immediately** at the provider, then
remove it from history with `git filter-repo` — deleting the file in a new commit is not enough.

---

## Step 4 — deploy on Render

**Blueprint (easiest):**
1. Render dashboard → **New** → **Blueprint**
2. Connect GitHub → pick the repo → **Apply**
3. Render reads `render.yaml` and creates the service

**Or manually:**
| Field | Value |
|---|---|
| Type | Web Service |
| Runtime | Python 3 |
| Build command | `pip install -r requirements.txt` |
| Start command | `uvicorn app.main:app --host 0.0.0.0 --port $PORT` |
| Health check path | `/health` |
| Plan | Free (sleeps) or Starter $7/mo |

Then add ONE env var:

```
PYTHON_VERSION = 3.12.7
```

Leave `TELEGRAM_*` and `LLM_*` **empty** — that keeps it bring-your-own-key with no
login required. Users paste their own Gemini or Groq key.

---

## Step 5 — test the deployed link

1. Open the URL in **incognito** (so no old localStorage)
2. First load may take ~60s if the free instance was asleep ("Waking the server…")
3. Pick **Google AI Studio** → follow the 4 steps → paste a key → Save
4. Channel: `@ethio_job_vacancy1` · criteria: `marketing officer, 0 years experience` · from: 14 days ago
5. Press **Scan channel** → you should see progress, then match cards

---

## Updating later

```bash
git add -A
git commit -m "describe what changed"
git push
```

Render redeploys automatically (`autoDeploy: true` in `render.yaml`).

---

## What's in the repo

```
app/config.py        all env vars in one place; boots even if they're missing
app/window.py        parse 'since', clamp lookback, contiguous scan windows
app/pipeline.py      dedupe, window assignment, batching, result merging
app/sources.py       t.me/s preview scraper + optional Telethon fallback
app/llm_adapter.py   OpenAI-compatible client, retries, JSON repair
app/jobs.py          in-memory jobs, scan runner, progress, cancel
app/security.py      URL/SSRF validation, rate limiter, concurrency gate
app/headers.py       CSP (hashed inline script) + security headers
app/main.py          routes + credential enforcement
static/index.html    single-page UI, three provider choices, no build step
scripts/make_session.py  one-time StringSession generator (only if needed)
tests/               20 unit tests + a mock LLM provider
render.yaml, Dockerfile, runtime.txt, .env.example, .gitignore
```
