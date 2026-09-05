# matcha-watch

Watches Marukyu-Koyamaen product pages and sends a Telegram message when something
comes back in stock. Silent otherwise. No dependencies, no LLM at runtime — one
Python file using only the standard library.

Currently watching: **Isuzu (Principal matcha)** — `1191040c1`

---

## How it decides

The obvious approach — "the *out of stock* sentence is missing, so it's in stock" —
is wrong in the expensive direction. A 403, a redirect to a login wall, a WAF page,
a truncated response or a theme tweak all make that string disappear, and all would
wake you at 3 a.m. for nothing.

So each check requires **two independent signals to agree**:

| Signal | In stock | Out of stock |
|---|---|---|
| A — `<p class="stock single-stock-status out-of-stock">` | absent | present |
| B — product container class (WooCommerce) | `instock` | `outofstock` |

- Both say in stock → **IN_STOCK** → alert
- Both say out of stock → **OUT_OF_STOCK** → silent
- Anything else (disagreement, missing container, fetch failure) → **UNKNOWN** → never alerts as in stock

Both signals were verified against the live site on 2026-09-05 using a sold-out
product (Isuzu) and a genuinely in-stock one (`1186000cc`) for comparison.

### Silence is monitored too

You asked for silence when there's no stock. That makes silence ambiguous: no stock,
or the script died three weeks ago. So after 6 consecutive unreadable checks (~40 min)
you get a **"watcher is blind"** message, and a **"recovered"** note when it comes back.
Plus a daily check-in at 09:00 MYT confirming it's alive.

---

## ⚠️ What this cannot do: per-size stock

The shop requires login to buy. **Logged out, all four size rows render identically
whether the product is in stock or sold out** — no per-row stock element, no per-row
add-to-cart button. Verified against both a sold-out and an in-stock product.

So per-size availability is **not observable anonymously**, and this script does not
pretend otherwise. It tells you *at least one size is buyable* and lists all four
sizes with prices; you open the page to see which one. The alert says so explicitly.

Making it truly per-size would require storing shop credentials and holding a logged-in
session — considerably more moving parts, and credentials sitting in CI. Say the word
if you want that tradeoff; it is a different design, not a small patch.

Per-size **prices** *are* exposed and are tracked, so price-change alerts work per size.

---

## Setup (GitHub Actions)

**The repo must be public.** GitHub Actions minutes are free on standard runners for
public repositories ([billing docs](https://docs.github.com/en/actions/concepts/billing-and-usage)).
A private repo would burn roughly 7,200 job-minutes a month against a much smaller
free allowance. Your bot token lives in Actions Secrets, which stay private either way.

### 1. Telegram bot

1. Message [@BotFather](https://t.me/BotFather) → `/newbot` → copy the token.
2. Send your new bot any message (it can't message you first).
3. Open `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` and copy
   `result[0].message.chat.id`.

### 2. Push and configure

```bash
git init && git add . && git commit -m "matcha watcher"
gh repo create matcha-watch --public --source=. --push
```

Then **Settings → Secrets and variables → Actions → New repository secret**:

| Name | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | the BotFather token |
| `TELEGRAM_CHAT_ID` | the chat id from `getUpdates` |

Add these yourself in the GitHub UI — don't paste them into a file or a chat.

### 3. Verify before trusting it

Actions → **matcha-watch** → *Run workflow*:

1. `selftest` — proves the runner can actually read the site and parse it.
   **Do this first.** If it prints `FETCH FAILED`, GitHub's Azure IPs are blocked
   and you need the VPS path below instead.
2. `test-telegram` — proves notifications land on your phone.
3. `dry-run` — a real check that prints instead of sending.

Then leave it alone. The schedule takes over.

---

## Timing

`cron: '*/5 * * * *'` plus a random 0–120 s sleep inside the job → an effective
**5–7 minute** randomized cadence, and requests never land exactly on :00/:05/:10.
Each run also rotates among five real browser User-Agent strings.

Two GitHub behaviours worth knowing, which **I could not confirm in the docs** (the
pages I fetched were truncated before the relevant section) — treat as needing
verification:

- Scheduled workflows are **best-effort** and can run late under load. Expect
  occasional gaps longer than 7 minutes. If you need hard timing, use the VPS mode.
- Scheduled workflows are reportedly auto-disabled after ~60 days of repository
  inactivity. The state file is committed whenever status, prices or the heartbeat
  date change (~1–2 commits/day), which should keep the repo active — but check back
  after two months.

State is only committed when it *meaningfully* changes; an unchanged healthy run
serializes byte-identically and produces no commit, so history stays readable.

---

## Adding more products

Edit `products.json`:

```json
{
  "products": [
    {"name": "Isuzu (Principal matcha)", "url": "https://www.marukyu-koyamaen.co.jp/english/shop/products/1191040c1"},
    {"name": "Wako", "url": "https://www.marukyu-koyamaen.co.jp/english/shop/products/1141020c1"}
  ]
}
```

The detector is generic across their shop theme — variable and simple products both
work. Products are checked in one run with a random 3–9 s gap between them.

---

## Running on a VPS instead

Same file, no changes:

```bash
TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=... python3 matcha_watch.py --loop
```

Exact 5–7 min randomized intervals, one stable IP, no scheduler drift.
`MIN_INTERVAL` / `MAX_INTERVAL` (seconds) tune it.

---

## Tuning

| Env var | Default | Meaning |
|---|---|---|
| `BLIND_AFTER` | `6` | consecutive unreadable checks before the blind alert |
| `HEARTBEAT` | `1` | `0` disables the daily check-in |
| `HEARTBEAT_HOUR` | `9` | hour (0–23, Malaysia time) for the check-in |
| `PRICE_ALERTS` | `1` | `0` disables price-change alerts |
| `TIMEOUT` | `25` | per-request timeout, seconds |

## Tests

```bash
python3 test_detect.py   # parser vs. fixtures copied from the real markup
python3 test_flow.py     # state machine: transitions, re-arm, blind streak, heartbeat
```

`test_detect.py` covers the failure modes that matter: blocked page, login wall,
truncated response, and signals disagreeing — none of which may ever read as in stock.

## Etiquette

One request per product per ~6 minutes is roughly 240 page loads a day. That's light,
but it is someone's shop: don't lower the interval, and don't point this at dozens of
products at once.
