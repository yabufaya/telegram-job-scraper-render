# Job Radar — Telegram Job Filter (deploy-ready for Render)

Reads a **public** Telegram job channel, asks an LLM which posts match your background,
skips postings whose deadline has passed, and links straight back to the original post.

Built to be shared publicly: **every visitor brings their own LLM API key**, and the
server stores **nothing** — no database, no accounts, no saved searches.

```
Channel ──► fetch ──► dedupe ──► windows ──► batches ──► LLM ──► matches
(t.me/s)   (HTML)    (no dupes)  (no gaps)  (no overlap) (user key)
```

---

## Live demo checklist (what a visitor experiences)

1. Opens the URL → server wakes (free tier) → "Your LLM API key" panel.
2. Picks a provider (Google AI Studio / Groq / Custom), pastes **their** key,
   then presses **Load models** — the app asks the provider which models that
   key can use *right now* and shows a picker. This matters because model names
   are retired constantly: `gemini-2.5-flash` was shut down on 20 Oct 2026 and
   Groq's Llama models moved to enterprise-only. Nothing is hardcoded.
3. Types `@ethio_jobs`, their profile, a start date → the page shows the scan plan.
4. Watches real progress: `Reading 12 Oct → 26 Oct…` then `Classifying… 7/21 batches`.
5. Gets cards with **Strong / Possible** badges, the model's reason, full text, and
   an "Open in Telegram" deep link. Nothing is written to the server's disk.

---

## Deploy to Render (10 minutes)

### 1. Push this folder to GitHub

```bash
git init && git add . && git commit -m "Job Radar: Render-ready"
git remote add origin https://github.com/<you>/telegram-job-scraper-automation.git
git push -u origin main
```

`.gitignore` already excludes `.env`, `*.session` and `session_string.txt`.

### 2. Create the service

**Option A — Blueprint (recommended):** Dashboard → **New → Blueprint** → pick the repo.
`render.yaml` is read automatically.

**Option B — manual:** New → Web Service → Python 3

| Field | Value |
|---|---|
| Build command | `pip install -r requirements.txt` |
| Start command | `uvicorn app.main:app --host 0.0.0.0 --port $PORT` |
| Health check path | `/health` |
| Plan | Free (sleeps) or Starter $7/mo (always on) |

### 3. Environment variables

Only these matter at first — everything else has a sane default:

| Key | Value | Notes |
|---|---|---|
| `PYTHON_VERSION` | `3.12.7` | Render's default may be older |
| `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` | *blank* | **leave blank** for no-login mode |
| `TELEGRAM_SESSION_STRING` | *blank* | only needed for preview-disabled channels |
| `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL_NAME` | *blank* | blank = strict bring-your-own-key |
| `ALLOW_SERVER_LLM_FALLBACK` | `false` | keep false on a public site |
| `RATE_LIMIT_SCANS_PER_HOUR` | `6` | per IP |
| `GLOBAL_CONCURRENCY` | `4` | simultaneous scans across all visitors |

### 4. Keep it warm (free plan only)

Free instances sleep after 15 minutes idle and take ~1 minute to wake. The page
shows a "waking the server" overlay and retries `/health`, so it *works* — it's just
slow. Two options:

* **Free:** create a monitor at [cron-job.org](https://cron-job.org) hitting
  `https://<your-app>.onrender.com/health` every 10 minutes (uses ~730 of your 750
  monthly instance-hours — don't run a second free service).
* **Paid:** `plan: starter` in `render.yaml` → always on, no cold starts.

---

## Where does the Telegram data come from?

Two sources, tried in the order set by `TELEGRAM_SOURCE_ORDER`:

**What "preview disabled" means:** Telegram gives every public channel a read-only web
page at `https://t.me/s/<username>`, readable with no account. Owners can switch it off;
Telegram then answers `302 → https://t.me/<username>` and there is no HTML to read.
The app reports this per window in plain language instead of returning an empty result.
A **bot token from @BotFather does not help** — bots can only read channels they were
added to. The only alternative is a user session (the StringSession below).

| Source | Needs an account? | Coverage | Ban risk | Example |
|---|---|---|---|---|
| `preview` — `https://t.me/s/<channel>` HTML | **No** | most public job channels | none | `ethio_jobs`, `vacancy_ethiopia` ✅ / `ethiojobs` ❌ (302) |
| `telethon` — MTProto with a StringSession | yes (server-side) | 100% of public channels | on your account | — |

Default is `preview,telethon`. If `TELEGRAM_SESSION_STRING` is empty, channels with
preview disabled return a clear message instead of failing weirdly.

> Verified in testing: `ethio_jobs`, `vacancy_ethiopia`, `hailejobs` have preview on;
> `ethiojobs`, `JobsInEthiopia` have it off (they 302 to `t.me/<name>`).

### Enabling the Telethon fallback (optional)

```bash
pip install telethon
python scripts/make_session.py        # run locally, in a terminal
```

Paste the printed string into `TELEGRAM_SESSION_STRING` on Render. Use a **dedicated
account**, not your personal one — strangers' scans run on it.

---

## How a scan is cut up (the batching question)

### 1. Windows — so nothing is missed and nothing overlaps

The range `[since, now)` is cut into contiguous **14-day windows**, half-open:

```
0..9 weeks ago        4..0 weeks
[── window 1 ──)[── window 2 ──)[── window 3 ──)
        t1             t2            now
```

* A post at exactly `t1` belongs to window 2 **only** (`start <= t < end`).
* Windows never share a timestamp, so a post can never be fetched twice by design.
* If one window fails, the others still succeed and the failure is reported per window.
* Progress becomes meaningful: `window 2/3`, not a spinner.

### 2. Batches — so the LLM never re-reads a post

```
unique posts ──► batch 1 (≤15 posts, ≤40k chars) ──► batch 2 ──► …
```

* Batches are cut from **one** ordered, deduplicated list, in order — batch *n* only
  contains posts that no earlier batch has.
* Cut by **count** *and* **characters**: 15 short posts fit; 3 essay-length posts don't.
* A post longer than `MAX_BATCH_CHARS` still gets its own (truncated) batch — dropping
  it would create a silent hole.
* `MAX_CONCURRENT_LLM_CALLS` (default 3) bounds how many are in flight, so a wide date
  range takes longer rather than hammering a rate-limited free tier.

### 3. The "previous batch reaches into the next" problem

This is the classic pagination bug: `?before=<id>` is **inclusive**, so the boundary
post appears on two pages. Three defences, all tested:

1. **Stable identity** — every post is keyed `channel/message_id`; a duplicate key is
   dropped before batching.
2. **Text fingerprint** — the same job reposted later with a new id and a different URL
   is collapsed (`DEDUPE_BY_TEXT=true`), normalising whitespace, emoji and links but
   **keeping Ethiopic script**, so two similar-looking Amharic posts stay distinct.
3. **Explicit interval maths** — a post is assigned to the single window whose half-open
   interval contains its timestamp, even if it arrived "by accident".

Measured on a live channel: 500 days of history → 114 posts, **exactly 1** boundary
duplicate removed, zero id collisions, cap respected.

### 4. Continuing later (no database)

At the end of a scan the server returns a **cursor** = the oldest post it covered. The
browser keeps it in `localStorage`; "Continue from my last scan" then starts at
`oldest_date − 1 day`. Consecutive runs tile the timeline with no gap and no rework.
Delete the browser's storage and the knowledge is gone — which is the point.

### 5. Cost control

| Guard | Default | Why |
|---|---|---|
| `MAX_POSTS_PER_SCAN` | 600 | stops one scan from reading a channel's history |
| `MAX_LOOKBACK_DAYS` | 120 | "since 2021" becomes "since 120 days ago" + a visible note |
| `MAX_CHARS_PER_POST` | 6000 | one giant post can't eat the context window |
| `RATE_LIMIT_SCANS_PER_HOUR` | 6/IP | stops one visitor (or a script) using you as a proxy |
| `GLOBAL_CONCURRENCY` | 4 | protects a small Render instance from CPU/memory spikes |

Typical scan (Gemini Flash): 200 posts ≈ 14 batches ≈ **$0.05–0.10** on the user's key.

---

## API

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/` | the UI |
| `GET` | `/health` | liveness + config summary (**no secrets**) — use as health check |
| `GET` | `/config` | non-secret defaults; returns `has_server_key` boolean, never the key |
| `POST` | `/models` | live model list for a given key + base URL (also validates the key) |
| `POST` | `/plan` | dry run: windows, span and batch estimate for a date → free |
| `POST` | `/search` | validate → returns `{job_id}` immediately (no long request) |
| `GET` | `/search/{job_id}` | `{stage, windows_done, batches_done, results[], cursor, …}` |
| `POST` | `/search/{job_id}/cancel` | stop a running scan, stop spending tokens |

```bash
curl -s https://<app>.onrender.com/plan -H 'Content-Type: application/json' \
  -d '{"since":"2026-09-01","tz_offset_minutes":-180}'

curl -s https://<app>.onrender.com/search -H 'Content-Type: application/json' -d '{
  "channel":"ethio_jobs",
  "criteria":"Electrical engineering graduate, IT roles, 0 years experience",
  "since":"2026-10-01",
  "tz_offset_minutes":-180,
  "llm_base_url":"https://generativelanguage.googleapis.com/v1beta/openai",
  "llm_api_key":"<the user's own key>",
  "llm_model":"gemini-2.5-flash"}'
```

---

## Security notes

Full write-up with test evidence: [`SECURITY_AND_CHANNELS.md`](SECURITY_AND_CHANNELS.md).

* **Per-visitor isolation (verified).** Two visitors scanning simultaneously with two
  different keys: neither key appears in the other's payload, neither is echoed back, and
  neither appears anywhere in the server log. Results are addressed by a random 12-char
  job id and expire 10 minutes after the last poll.
* **Strict CSP with a hashed inline script**, plus `X-Frame-Options: DENY`, `no-referrer`,
  `COOP/CORP: same-origin` and `Cache-Control: no-store` on result endpoints — so an
  injected script cannot execute and the page cannot be framed to harvest keys.
* **No key exposure.** `/config` used to return the owner's `LLM_API_KEY`; it no longer
  does. Visitor keys live in `localStorage`/`sessionStorage` and are only sent with that
  visitor's scans — never logged, never written to disk, never included in errors.
* **SSRF guard.** The LLM base URL is user-supplied, so it must be `https://`, must not
  embed credentials, and must not resolve to loopback/private/link-local/metadata IPs.
* **Abuse limits.** Per-IP hourly quota + per-IP concurrency + global concurrency. The
  client IP is taken from the *right-most* `X-Forwarded-For` entry (the one Render writes),
  so the quota cannot be dodged by sending a fake header.
* **Stateless by design.** Jobs live in memory, are garbage-collected 10 minutes after
  their last poll, and vanish entirely on restart. No database is used anywhere.
* **Errors are friendly.** Users see "the model name doesn't exist for this provider",
  not a traceback.

---

## Tests

```bash
python -m unittest discover -s tests -v     # 20 tests, no network, no keys
```

Covers: window contiguity, half-open boundary ownership, client-timezone `since`,
pagination-overlap removal, repost collapse, batch partitioning (no loss, no repeats),
char-limit splitting, string-vs-int id matching, expired handling, cursor selection.

For a full offline run, `tests/mock_llm.py` is a fake OpenAI-compatible provider:

```bash
python tests/mock_llm.py 8777 &          # fake provider
ALLOW_INSECURE_LLM_URLS=true uvicorn app.main:app --port 8000
# then point the UI's base URL at http://127.0.0.1:8777/v1
```

---

## Project layout

```
app/config.py        all env vars in one place, boots even when they're missing
app/window.py        parse 'since', clamp lookback, cut contiguous windows
app/pipeline.py      dedupe, assign→windows, batch, merge LLM verdicts, cursor
app/sources.py       t.me/s preview scraper + Telethon StringSession fallback
app/llm_adapter.py   OpenAI-compatible client, retries, JSON repair, error mapping
app/jobs.py          in-memory jobs, the scan runner, progress, cancellation
app/security.py      URL/SSRF validation, rate limiter, concurrency gate
app/main.py          routes + credential enforcement
static/index.html    single-page UI, no build step, light/dark/system themes
scripts/make_session.py  one-time StringSession generator (local only)
tests/               20 unit tests + mock provider
render.yaml, Dockerfile, runtime.txt
```

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| First load takes ~60s | free instance was asleep | expected; add a keep-alive ping or use Starter |
| "This channel has its public web preview turned off" | preview disabled | configure `TELEGRAM_SESSION_STRING`, or pick another channel |
| `404` from the LLM provider | base URL ≠ model's provider | pick a preset; don't mix Gemini URL with a GLM model |
| `429` from the LLM provider | free-tier rate limit | lower `MAX_CONCURRENT_LLM_CALLS` to 1 |
| Batches slow/serial (GLM) | provider queues one request at a time | set `MAX_CONCURRENT_LLM_CALLS=1`, raise timeout |
| `Telegram is rate-limiting this server` | too many preview requests | raise window size (fewer windows) or wait |

## Licence

MIT — do what you like, but don't put a shared Telegram session on a public site
without thinking about who ends up owning the abuse.
