# Two questions, answered with evidence

---

# 1. What "preview-disabled channel" means

## The mechanism

Telegram gives every **public** channel a read-only web page that needs no account:

```
https://t.me/s/<username>        ← "s" = the public preview
```

It is what powers the t.me embed widgets. It shows the last ~20 posts, and you page
backwards with `?before=<message_id>`. **No login, no API id/hash, no session.**

The channel owner can switch this off (Channel settings → discussion/preview, and
Telegram also disables it on some channels automatically). When it is off, Telegram
answers with a redirect instead of the feed:

| Channel | `GET https://t.me/s/<name>` | Result |
|---|---|---|
| `ethio_jobs` | `200`, 138 KB, **20 posts parsed** | ✅ preview ON |
| `vacancy_ethiopia` | `200`, 17 posts | ✅ preview ON |
| `hailejobs` | `200`, 2 posts | ✅ preview ON |
| `ethiojobs` | `302 → https://t.me/ethiojobs` | ❌ preview OFF |
| `JobsInEthiopia` | `302 → https://t.me/JobsInEthiopia` | ❌ preview OFF |

That 302 is the whole story: the server is redirected to the normal (login-walled)
channel page, so there is no HTML to parse.

## What the user sees

It is **not** a crash and **not** a silent empty result. The scan ends with:

```
stage : error
error : This channel has its public web preview turned off, so it can only be read
        through a Telegram login.
warning: 20 Oct → 29 Oct: This channel has its public web preview turned off, so it
        can only be read through a Telegram login. (no Telegram session configured)
```

Every window is reported separately, so if one window is readable and another isn't,
you still get the readable half plus a warning naming the bad window.

## Your three options per channel

| Want | Do this | Cost |
|---|---|---|
| Only readable channels | nothing — that's the default | free, zero ban risk |
| Also the preview-disabled ones | run `python scripts/make_session.py` once, paste the string into `TELEGRAM_SESSION_STRING` | one Telegram account gets used for everyone's scans |
| Tell users up front | the UI hint already says "public channels only" | — |

## Important correction to something you might assume

**The Telegram Bot API (the token from @BotFather) does not help here.** A bot can only
read messages in a channel it was *added* to as a member/admin. It cannot read arbitrary
public job channels. Reading any public channel needs either the web preview (above) or
a **user account** via MTProto/'Telethon' — which is the session string. That's why
"users type their own Telegram API ID/hash into the page" isn't a workable design: that
pair belongs to a phone login with an SMS code, so it would need per-user OTP flows and
an encrypted session store, i.e. a database.

---

# 2. Is it secure for every client once the service is awake, each using their own key?

Short answer: **yes — every visitor's key and results are isolated from every other
visitor's.** It is proven below, not just asserted. It is not a bank; the honest limits
are listed at the end.

## The isolation model

```
Visitor A's browser                    Render (one process)                  Provider
  localStorage: keyA   ──POST /search──►  job_A {keyA in memory}
                                          │  classify_batch(keyA) ─────────►  A pays
                                          ▼                                       for A
                                        results_A
  ◄──GET /search/job_A──── only job_A's payload, never keyA

Visitor B's browser
  localStorage: keyB   ──POST /search──►  job_B {keyB in memory} ─────────►  B pays for B
  ◄──GET /search/job_B──── only job_B's payload, never keyB
```

* Keys are **never written to a store**. They exist as a function argument for the
  duration of one scan.
* Keys are **never logged** (the log line records channel + IP only).
* Keys are **never returned** — not in `/search`, not in `/config`, not in errors.
* Results are addressed by a **random 12-char job id** (`uuid4().hex[:12]`), not by IP or
  by a guessable counter.
* Jobs expire 10 minutes after their last poll and are deleted; a restart wipes all of
  them.

## The proof (run against the running server)

Two visitors, two different keys, scanning at the same time from different IPs:

```
A: {"job_id":"0f2e94ad5d2d", ...}
B: {"job_id":"80eaebdedbca", ...}

A's job payload scanned for B's key  →  False
A's key echoed anywhere in payload   →  False
A's payload keys: batches_done, batches_failed, batches_total, channel,
                  clamped_from, current_window, cursor, done, ...   (no credential field)

server log, counting occurrences of either key: 0
15:39:03 [INFO] Scan 0f2e94ad5d2d started by 203.0.113.10 (channel=ethio_jobs, server_key=False)
15:39:03 [INFO] Scan 80eaebdedbca started by 198.51.100.20 (channel=ethio_jobs, server_key=False)
```

## Browser-side protection (added in this hardening pass)

Because visitors' keys live in web storage, the browser is part of the threat model:

| Header | Value | What it stops |
|---|---|---|
| `Content-Security-Policy` | `script-src 'self' 'sha256-C/xnkSY5thw/…'` | Any injected script. The inline script is pinned **by hash**, so reflected/`onerror` payloads cannot run — nothing like an XSS can read another visitor's `localStorage`. |
| `X-Frame-Options` + `frame-ancestors 'none'` | DENY | Clickjacking: a fake "Save key" button drawn over ours to harvest keys. |
| `Referrer-Policy` | `no-referrer` | Leaking the page URL to `t.me` or any third party. |
| `Cross-Origin-Opener-Policy` / `-Resource-Policy` | `same-origin` | Another origin getting a handle to the page. |
| `Cache-Control: no-store` on `/search*`, `/config`, `/plan` | — | Results sitting in a shared proxy cache. |
| `connect-src 'self'` | — | The page phoning home to anything but its own origin. |

Verified: `CSP violations: none`, `JS executed: True`, `inline handlers stripped: True`
— i.e. the policy is strict *and* the UI still works.

## Two real weaknesses found and fixed during this pass

1. **Rate-limit evasion via a spoofed header.** `client_ip()` used the *left-most*
   `X-Forwarded-For` entry, which the client controls — anyone could send a new fake IP on
   every request and never hit the quota. Now it uses the **right-most** entry (the one
   Render's proxy writes).
2. **Inline `onclick=` handlers** (needed `'unsafe-inline'` in the CSP). All removed; events
   are bound in JS, so the CSP can now be an exact hash allowlist.

## Your responsibility as the operator

| Setting | Keep it like this | Why |
|---|---|---|
| `ALLOW_SERVER_LLM_FALLBACK` | `false` | `true` means visitors can spend *your* quota |
| `LLM_API_KEY` / `LLM_*` | blank | blank = strict bring-your-own-key |
| `TELEGRAM_SESSION_STRING` | blank unless needed | a logged-in session is a credential; on a public site, abuse lands on that account |
| `RATE_LIMIT_SCANS_PER_HOUR` | 6 | per visitor |
| `GLOBAL_CONCURRENCY` | 4 | protects a small instance |

## Honest limits — what this design does **not** protect against

1. **The provider still sees the traffic.** Each visitor's key is used from *your*
   server's IP. If one visitor abuses it, the provider may rate-limit or block that IP —
   which is shared with everyone else on the instance. Nothing can be done about this
   while the server (not the browser) makes the LLM call, which is required because the
   provider must not be given the post text directly from a browser (CORS + key exposure).
2. **Keys would be exposed by a server compromise.** Running code on the host means the
   key is in RAM. Standard platform risk; mitigated by not storing it and by stripping it
   from all output/logs.
3. **Not an XSS-audited product.** The CSP makes XSS very hard, but this is a hobby-scale
   app, not a bank.
4. **`localStorage` is per-origin and per-browser-profile** — it does not protect a visitor
   who shares a computer, or who uses a malicious browser extension.
5. **The job id is unguessable but unauthenticated.** If someone obtains another
   visitor's job id (they can't from the UI, only by shoulder-surfing a network log on
   plain HTTP), they could read that scan's *public job posts* for 10 minutes. There is no
   private data in it beyond the results, and no key.
6. **Free-tier cold starts are not a security issue but a UX one:** the page shows
   "Waking the server…" and retries `/health` for up to 75 s before giving up.

## Recommended settings for posting the link publicly

```yaml
ALLOW_SERVER_LLM_FALLBACK: "false"
LLM_BASE_URL: ""
LLM_API_KEY: ""
LLM_MODEL_NAME: ""
TELEGRAM_SESSION_STRING: ""        # add only if you need preview-disabled channels
RATE_LIMIT_SCANS_PER_HOUR: "6"
RATE_LIMIT_IP_CONCURRENCY: "1"
GLOBAL_CONCURRENCY: "4"
```

With those, a visitor can spend only their own tokens, can only see their own results,
and cannot do either faster than 6 scans/hour / 1 at a time.
