# Environment variables on Render — the complete reference

`app/config.py` reads exactly **21** variables. **Zero are required.**
Everything below has a working default baked into the code, so an empty
Environment tab produces a fully functional site.

---

## Quick answer

### What to actually put in Render

Go to **Dashboard → your service → Environment → Add Environment Variable**:

| Key | Value | Required? |
|---|---|---|
| `PYTHON_VERSION` | `3.12.7` | Recommended — pins the interpreter |

**That's the whole list.** Add it and stop there.

### What Render sets for you (never set these yourself)

| Key | Why not |
|---|---|
| `PORT` | Render injects it; the start command consumes it via `$PORT`. Hardcoding it breaks routing. |

> Do **not** add `TELEGRAM_*` or `LLM_*` unless you specifically want the
> behaviours described below. The safe default is an empty environment.

---

## All 21 variables

### App — 2 variables

| Variable | Default | What it does | Change it when |
|---|---|---|---|
| `PORT` | `8000` | Port uvicorn binds. Render overrides this. | Never — Render sets it. |
| `LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR`. | Set `DEBUG` while diagnosing a failing scan. **Note:** DEBUG can echo request bodies into logs — turn it back after. |

### Batching and scan limits — 6 variables

These control cost and duration. Every post is an LLM token you (or the visitor) pay for.

| Variable | Default | What it does | Change it when |
|---|---|---|---|
| `BATCH_SIZE` | `15` | Posts sent to the LLM per request. Smaller = more calls, more latency, but each request is cheaper and less likely to hit a context limit. | Provider rejects prompts as too long → lower to `8`. |
| `MAX_CONCURRENT_LLM_CALLS` | `3` | Parallel LLM requests per scan. Higher = faster, but more likely to hit the provider's per-minute rate limit. | Faster scans on a paid key → `5`. Free key returns 429 → `2`. |
| `MAX_POSTS_PER_SCAN` | `600` | Hard ceiling on posts pulled per scan. Protects you and Telegram from abuse. | Almost never. Raising it makes scans slow — 600 posts already takes ~3.5 min. |
| `MAX_LOOKBACK_DAYS` | `120` | How far back one scan may reach. | Never, unless you want long history. Scans beyond ~30 days get slow (9 windows re-walk the channel). |
| `MAX_BATCH_CHARS` | `40000` | A batch is also split if combined text exceeds this. | Provider has a small context window → `15000`. |
| `MAX_CHARS_PER_POST` | `6000` | Each post is truncated to this length before sending. | Posts are huge and get cut off mid-deadline → raise slightly. |
| `DEDUPE_BY_TEXT` | `true` | Collapses identical reposts so the LLM is not paid twice. `Ethiojobs2000` reposts heavily (54 raw → 20 unique). | Leave on. Turning it off roughly triples cost on repost-heavy channels. |

### Abuse protection — 4 variables

These are what make a public link safe to post on LinkedIn.

| Variable | Default | What it does | Change it when |
|---|---|---|---|
| `RATE_LIMIT_SCANS_PER_HOUR` | `6` | Scans per IP per hour. Verified: the 4th call with limit `3` returns `429 "Hourly limit reached (3 scans/hour). Try again in ~61 min."` | Testing → raise to `50`. Sharing widely → keep `6`. |
| `RATE_LIMIT_IP_CONCURRENCY` | `1` | Simultaneous scans per IP. `1` stops one person opening 10 tabs. Verified message: *"You already have a scan running."* | Leave at `1`. |
| `GLOBAL_CONCURRENCY` | `4` | Simultaneous scans **across all visitors**. The whole server. | Got 5+ regular users → `8`. Each scan uses ~3 LLM calls, so watch the provider's rate limit. |
| `ALLOW_INSECURE_LLM_URLS` | `false` | When `false`, custom base URLs must be `https` and cannot point at private/loopback/link-local IPs (blocks SSRF to cloud metadata). | **Only** for local testing against `http://127.0.0.1`. Never on a public site. |

### Telegram — 4 variables (all optional)

> ⚠️ These are **your** credentials on **your** Telegram account, not the visitor's.
> Skip them entirely unless you need channels whose public preview is off.

| Variable | Default | What it does |
|---|---|---|
| `TELEGRAM_API_ID` | *(empty)* | From https://my.telegram.org → API Development Tools. |
| `TELEGRAM_API_HASH` | *(empty)* | Same page. |
| `TELEGRAM_SESSION_STRING` | *(empty)* | Generated once with `python scripts/make_session.py`. **This string *is* the login.** |
| `TELEGRAM_SOURCE_ORDER` | `preview,telethon` | Which reader to try first. `preview` needs no login and only sends an anonymous cookie. `telethon` needs the session. |

**Rules:**
- Set **all three** of `API_ID` / `API_HASH` / `SESSION_STRING`, or **none**. Partial sets are ignored and fall back to preview-only.
- With all three set, `/health` shows `"telegram_mode": "telethon+preview"`.
- A session string is a bearer credential for a real account. Use a **burner** account, and if it leaks, revoke it in Telegram → Devices.
- Render's filesystem is ephemeral — a `.session` file cannot survive. Use `SESSION_STRING` only.

### LLM — 4 variables (all optional)

> ⚠️ Setting these makes **you** pay for every visitor's scan.

| Variable | Default | What it does |
|---|---|---|
| `LLM_BASE_URL` | *(empty)* | e.g. `https://generativelanguage.googleapis.com/v1beta/openai` |
| `LLM_API_KEY` | *(empty)* | Your key. Never sent to the browser — `/config` only ever returns `has_server_key: true/false`. |
| `LLM_MODEL_NAME` | *(empty)* | **Do not trust a model name from a tutorial.** Check what your key can use with the **Load models** button, or `POST /models`. |
| `ALLOW_SERVER_LLM_FALLBACK` | `false` | The master switch. **Even with all three `LLM_*` set, your key is ignored unless this is `true`.** |

**In the default configuration** (`ALLOW_SERVER_LLM_FALLBACK=false`), a visitor
who does not supply a key gets:

```
400  Your own LLM API key is required. Open 'Use your own API key'
     and paste it — it stays in your browser and is only used for your scans.
```

---

## The three configurations

### 1. Default — visitors pay, you pay nothing (recommended)

```
Environment: (empty, or just PYTHON_VERSION)
```

`/health` → `server_llm_ready: false`, `allow_server_llm_fallback: false`.
Everything runs on the visitor's own free key. No key of yours exists on the
server to leak or exhaust.

### 2. You pay for everyone — visitors need no key

```
LLM_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai
LLM_API_KEY=AIza...your-key
LLM_MODEL_NAME=gemini-3.8-flash
ALLOW_SERVER_LLM_FALLBACK=true
RATE_LIMIT_SCANS_PER_HOUR=2
GLOBAL_CONCURRENCY=3
```

I measured this at roughly **$0.01 per 15-post scan** on a paid key. Lower the
rate limits first — a public link with unlimited free scans will drain your key.

> ⚠️ Since `gemini-2.5-flash` was shut down on **20 Oct 2026**, confirm
> `LLM_MODEL_NAME` against your live key before relying on it. A retired model
> name fails every scan with a `400`.

### 3. Reading preview-disabled channels (adds `ethiojobs`, `JobsInEthiopia`)

```
TELEGRAM_API_ID=1234567
TELEGRAM_API_HASH=0123456789abcdef0123456789abcdef
TELEGRAM_SESSION_STRING=1BVtsOK4Bu... (long string)
TELEGRAM_SOURCE_ORDER=preview,telethon
```

Channels that **do** work with no credentials at all: `ethio_job_vacancy1`,
`Ethiojobs2000`, `vacancy_ethiopia` (dead since 2023), `durov`.

---

## Verifying what actually took effect

Open `https://<your-app>.onrender.com/health`:

```json
{
  "telegram_mode": "preview-only",     // "telethon+preview" once all 3 Telegram vars are set
  "telethon_ready": false,
  "allow_server_llm_fallback": false,  // your master switch
  "server_llm_ready": false,           // true only if all 3 LLM vars are filled
  "batch_size": 15,
  "max_concurrent_llm_calls": 3,
  "max_posts_per_scan": 600,
  "max_lookback_days": 120,
  "rate_limit_scans_per_hour": 6,
  "status": "ok",
  "active_scans": 0,
  "global_concurrency_limit": 4
}
```

Every number here is a live echo of your environment variables. If you changed
`RATE_LIMIT_SCANS_PER_HOUR=2` and this still says `6`, the redeploy has not
finished.

`https://<your-app>.onrender.com/config` returns the provider defaults and
`has_server_key` — **never a key itself**.

---

## Security rules

1. **Never** commit a key or session string to GitHub. `.gitignore` blocks
   `.env` and `*.session`; that is not a substitute for care.
2. **Never** put secrets in the start command — Render shows it in the dashboard
   and it lands in shell history.
3. Rotate immediately if a key appears in a screenshot, log, or commit. A
   `TELEGRAM_SESSION_STRING` leak is worse than an LLM key leak: it is full
   account access.
4. `ALLOW_INSECURE_LLM_URLS=true` on a public site removes the SSRF guard.
   Leave it `false`.
5. Changing any variable triggers an automatic redeploy (~1–2 min on free tier).

---

## One caveat about the per-IP limit

`client_ip()` uses the **right-most** entry of `X-Forwarded-For` — the one Render
writes — because the left entries are client-supplied and spoofable. Verified:

| Header sent | IP used for the quota |
|---|---|
| `41.86.1.1` | `41.86.1.1` |
| `9.9.9.9, 41.86.1.1` (spoof attempt) | `41.86.1.1` — spoof ignored |
| *(none)* | the direct peer address |

This is correct for a standard Render URL. **If you later put Cloudflare in
front**, XFF becomes `<client>, <cloudflare-edge>` and the right-most entry
becomes the edge IP, so many visitors would share one quota. Set
`RATE_LIMIT_SCANS_PER_HOUR` higher, or ask me to add `CF-Connecting-IP` support
before adding a proxy.
