# Run it on Render — exact steps

Verified locally with the identical start command and **zero** environment
variables: the app boots, binds `0.0.0.0:$PORT`, and `/health` returns `200`.

---

## Before you start

You need:
- the code pushed to a GitHub repo (see `PUSH_TO_GITHUB.md`)
- a Render account (free, sign in with GitHub)

Nothing else. **No Telegram credentials, no LLM key.** The app is designed to run
with zero config — each visitor brings their own key.

---

## Option A — Blueprint (recommended, 2 minutes)

The repo already contains `render.yaml`, so Render configures everything for you.

1. Go to **dashboard.render.com**
2. Click **New** → **Blueprint**
3. Connect GitHub if asked, then pick your repository
4. Render shows the service from `render.yaml` (`job-radar`, Python, health check `/health`)
5. Click **Apply** / **Create**
6. Wait for the build. First deploy takes 2–4 minutes.

That's it. Skip to **Step 3**.

---

## Option B — Manual web service (if the Blueprint fails)

1. **dashboard.render.com** → **New** → **Web Service**
2. Pick your repository
3. Fill in exactly:

| Field | Value |
|---|---|
| **Name** | `job-radar` (or anything) |
| **Region** | `Frankfurt` (closest to Ethiopia) |
| **Branch** | `main` |
| **Runtime** | `Python 3` |
| **Build Command** | `pip install -r requirements.txt` |
| **Start Command** | `uvicorn app.main:app --host 0.0.0.0 --port $PORT` |
| **Instance Type** | `Free` (or `Starter` — see below) |
| **Health Check Path** | `/health` |

4. Leave **Environment Variables** empty for now (see Step 2)
5. Click **Create Web Service**

> ⚠️ The start command must bind `0.0.0.0` and use `$PORT`. If it binds
> `127.0.0.1` or a fixed port, Render reports *"no open ports detected"*.

---

## Step 2 — Environment variables

### Minimum (recommended)

**None.** Truly. With an empty environment the app runs in
*preview-only + bring-your-own-key* mode, which is the safest default.

### The only one worth adding

| Key | Value | Why |
|---|---|---|
| `PYTHON_VERSION` | `3.12.7` | Pins the interpreter. `runtime.txt` also sets this; the env var wins if both exist. |

Add it under **Environment** → **Add Environment Variable**.

### Optional — only if you want these behaviours

| Key | Value | Effect |
|---|---|---|
| `TELEGRAM_API_ID`<br>`TELEGRAM_API_HASH`<br>`TELEGRAM_SESSION_STRING` | from `python scripts/make_session.py` | Reads channels whose public preview is off (`ethiojobs`, `JobsInEthiopia`). Runs on **your** Telegram account — use a burner. |
| `LLM_BASE_URL`<br>`LLM_API_KEY`<br>`LLM_MODEL_NAME`<br>`ALLOW_SERVER_LLM_FALLBACK=true` | e.g. Gemini + your key | Site becomes **free for visitors** — you pay (~$0.01/scan). Without `ALLOW_SERVER_LLM_FALLBACK=true` the key is ignored. |
| `RATE_LIMIT_SCANS_PER_HOUR` | `6` | Default. Lower it if you enable your own key. |
| `GLOBAL_CONCURRENCY` | `4` | Max simultaneous scans across all visitors. |
| `MAX_POSTS_PER_SCAN` | `600` | Ceiling per scan. |
| `MAX_LOOKBACK_DAYS` | `120` | How far back one scan may reach. |

After adding variables, Render redeploys automatically.

### Never put these in the repo

- `LLM_API_KEY`
- `TELEGRAM_API_HASH`
- `TELEGRAM_SESSION_STRING`

They belong only in Render's Environment tab. `.gitignore` already blocks `.env`
and `*.session`.

---

## Step 3 — First load (this is normal, not a bug)

1. Open the service URL, e.g. `https://job-radar-xxxx.onrender.com`
2. You may see **"Waking the server…"** for up to ~60 seconds — free instances
   sleep after 15 minutes idle. The page retries `/health` automatically.
3. Then the "Connect your AI account" panel appears.

### Keep it awake (free plan only)

Free instances sleep after 15 min idle and take ~1 min to wake.

- **Free workaround:** sign up at [cron-job.org](https://cron-job.org) (or
  UptimeRobot) and ping `https://<your-app>.onrender.com/health` every 10 minutes.
  750 free instance-hours/month ÷ ~730 hours in a month ≈ one always-on service —
  so *don't* run a second free service in the same workspace.
- **Paid fix:** change `plan: free` → `plan: starter` in `render.yaml` ($7/mo).
  No sleeping, no cold starts.

> Tip: `robots.txt` gets an automatic "disallow all" reply while a free service
> is asleep — that's Render, not your app.

---

## Step 4 — Smoke test the deployment

**1. Health check** — should return JSON with `"status": "ok"`:
```
https://<your-app>.onrender.com/health
```

**2. Config check** — must NOT contain a key:
```
https://<your-app>.onrender.com/config
```
Expected: `"has_server_key": false, "require_user_key": true`

**3. End-to-end** in a browser (use incognito):
1. Choose **Google AI Studio** → follow the 4 steps → paste a key
2. Press **Load models** → a picker appears with live model names
3. Pick one, press **Save key & continue**
4. Channel: `@ethio_job_vacancy1`
5. Criteria: `marketing officer, 0 years experience`
6. Scan from: 14 days ago
7. **Scan channel** → progress bar, then match cards

If step 3 shows *"That key was rejected"*, the key is wrong — not your deployment.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Build fails: `No module named app` | start command run from the wrong directory | Render runs from the repo root; keep `app/` and `static/` at the top level |
| Deploy fails at startup with `KeyError` | you set a partial Telegram pair | Set all three `TELEGRAM_*` or none. The app tolerates missing vars, but a half-set pair with a bad `api_id` will be skipped — check logs |
| *"no open ports detected"* | start command not binding `0.0.0.0:$PORT` | Use exactly `uvicorn app.main:app --host 0.0.0.0 --port $PORT` |
| Health check failing | health check path wrong | Set **Health Check Path** to `/health` |
| 502 after idle | free instance is waking | wait ~60s, or add the cron ping |
| Page loads but scan says "Your own LLM API key is required" | expected in strict mode | the visitor must add their key, or you enable `ALLOW_SERVER_LLM_FALLBACK` |
| "This channel has its public web preview turned off" | channel disabled its preview | pick another channel, or configure `TELEGRAM_SESSION_STRING` |
| Scans work then stop at ~6/hour | per-IP rate limit | raise `RATE_LIMIT_SCANS_PER_HOUR` |
| `429` from the LLM provider | visitor's own free-quota limit | wait for their daily reset, or they switch provider |

---

## Reading the logs

Render dashboard → your service → **Logs**. The app logs each stage:

```
14:02:11 [INFO] Scan a1b2c3d4e5f6 started by 41.86.x.x (channel=ethio_job_vacancy1, server_key=False)
14:02:12 [INFO] Scan a1b2c3d4e5f6: 2026-10-15 -> now = 2 window(s) of 14 days
14:02:14 [INFO] Window 15 Oct → 29 Oct: preview returned 54 post(s).
14:02:16 [INFO] Scan a1b2c3d4e5f6: 20 unique post(s), 2 batch(es), max 3 in flight
14:02:19 [INFO] Scan a1b2c3d4e5f6 done: 9 match(es), 4 expired, 0 batch failure(s), 34 duplicate(s)
```

No API keys ever appear in these lines — by design.

---

## Deploying updates

```bash
git add -A && git commit -m "your change" && git push
```

With `autoDeploy: true`, Render rebuilds on every push to `main`. Manual
alternative: dashboard → **Manual Deploy** → **Deploy latest commit**.

---

## What it costs

| Plan | Price | Behaviour |
|---|---|---|
| **Free** | $0 | sleeps after 15 min idle, ~60s cold start, 750 instance-hours/month |
| **Starter** | ~$7/mo | always on, no cold starts, no sleeping |

LLM cost to you: **$0** in the default mode (visitors use their own keys).
You only pay if you enable `ALLOW_SERVER_LLM_FALLBACK`.
