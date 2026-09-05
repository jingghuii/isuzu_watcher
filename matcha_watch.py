#!/usr/bin/env python3
"""
matcha_watch.py - stock watcher for Marukyu-Koyamaen product pages.

Zero third-party dependencies (Python 3.9+ stdlib only), so the same file runs
identically on GitHub Actions, a VPS, or a laptop with no install step.

WHY THE DETECTION LOOKS OVER-BUILT
----------------------------------
The naive check is "if the out-of-stock sentence is missing, it's in stock".
That is wrong in the expensive direction. A 403, a redirect to a login wall, a
Cloudflare interstitial, a truncated response or a theme change all produce a
missing string, and all would fire a false restock alert.

So every check is three-state and needs two INDEPENDENT signals to agree:

  signal A : <p class="stock single-stock-status out-of-stock"> present/absent
  signal B : the product container's WooCommerce class, `instock` / `outofstock`

  A absent  + B instock     -> IN_STOCK   (alert)
  A present + B outofstock  -> OUT_OF_STOCK (silent)
  anything else             -> UNKNOWN    (never alerts as in-stock)

Both signals were verified against this site on 2026-09-05: an out-of-stock
product (Isuzu) carries `outofstock` + the <p>; a genuinely in-stock product
(1186000cc) carries `instock` and omits the <p> entirely.

KNOWN LIMITATION - PER-SIZE STOCK
---------------------------------
This shop requires login to purchase. Logged out, the four size rows render
identically whether the product is in stock or sold out: no per-row stock
element, no per-row add-to-cart button. Per-size availability is therefore NOT
observable anonymously, and this script does not pretend otherwise. It detects
"at least one size is buyable" at the product level, and tracks per-size PRICES
(which are exposed).
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Any

# Malaysia has no DST, so a fixed offset avoids depending on system tzdata.
MYT = timezone(timedelta(hours=8), "MYT")

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "products.json")
STATE_PATH = os.path.join(HERE, "state.json")

IN_STOCK = "IN_STOCK"
OUT_OF_STOCK = "OUT_OF_STOCK"
UNKNOWN = "UNKNOWN"

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:131.0) Gecko/20100101 Firefox/131.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
]


# --------------------------------------------------------------------------
# Minimal DOM (stdlib HTMLParser -> queryable tree)
# --------------------------------------------------------------------------

VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
}


class Node:
    __slots__ = ("tag", "attrs", "children", "parent", "_text")

    def __init__(self, tag: str, attrs: dict):
        self.tag = tag
        self.attrs = attrs
        self.children: list = []
        self.parent = None
        self._text: list = []

    def add(self, child: "Node") -> None:
        child.parent = self
        self.children.append(child)

    @property
    def classes(self) -> set:
        return set((self.attrs.get("class") or "").split())

    def text(self) -> str:
        parts = list(self._text)
        for c in self.children:
            parts.append(c.text())
        return " ".join(p for p in parts if p).strip()

    def walk(self):
        for c in self.children:
            yield c
            yield from c.walk()

    def find_all(self, pred):
        return [n for n in self.walk() if pred(n)]

    def find(self, pred):
        for n in self.walk():
            if pred(n):
                return n
        return None


class _TreeBuilder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("#root", {})
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = Node(tag, {k: (v or "") for k, v in attrs})
        self.stack[-1].add(node)
        if tag not in VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.stack[-1].add(Node(tag, {k: (v or "") for k, v in attrs}))

    def handle_endtag(self, tag):
        # Tolerate unclosed tags: unwind to the nearest matching open element.
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return

    def handle_data(self, data):
        s = data.strip()
        if s:
            self.stack[-1]._text.append(s)


def parse_html(html: str) -> Node:
    b = _TreeBuilder()
    try:
        b.feed(html)
    except Exception:
        pass  # partial tree is still usable; sentinel checks will judge it
    return b.root


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------

class FetchError(Exception):
    def __init__(self, reason: str, status: int | None = None):
        super().__init__(reason)
        self.reason = reason
        self.status = status


def _decode(raw: bytes, encoding_header: str) -> str:
    enc = (encoding_header or "").lower()
    if "gzip" in enc:
        try:
            raw = gzip.decompress(raw)
        except Exception:
            pass
    elif "deflate" in enc:
        try:
            raw = zlib.decompress(raw, -zlib.MAX_WBITS)
        except Exception:
            try:
                raw = zlib.decompress(raw)
            except Exception:
                pass
    for codec in ("utf-8", "cp932", "latin-1"):
        try:
            return raw.decode(codec)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def fetch(url: str, timeout: int = 25, attempts: int = 2) -> str:
    last: Exception | None = None
    for attempt in range(attempts):
        req = urllib.request.Request(url, method="GET")
        req.add_header("User-Agent", random.choice(USER_AGENTS))
        req.add_header("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8")
        req.add_header("Accept-Language", "en-US,en;q=0.9")
        req.add_header("Accept-Encoding", "gzip, deflate")
        req.add_header("Connection", "close")
        req.add_header("Upgrade-Insecure-Requests", "1")
        req.add_header("Sec-Fetch-Dest", "document")
        req.add_header("Sec-Fetch-Mode", "navigate")
        req.add_header("Sec-Fetch-Site", "none")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = _decode(resp.read(), resp.headers.get("Content-Encoding", ""))
                if resp.status != 200:
                    raise FetchError(f"HTTP {resp.status}", resp.status)
                if len(body) < 2000:
                    raise FetchError(f"suspiciously short response ({len(body)} bytes)")
                return body
        except urllib.error.HTTPError as e:
            last = FetchError(f"HTTP {e.code}", e.code)
        except urllib.error.URLError as e:
            last = FetchError(f"network error: {e.reason}")
        except FetchError as e:
            last = e
        except Exception as e:  # noqa: BLE001
            last = FetchError(f"{type(e).__name__}: {e}")
        if attempt < attempts - 1:
            time.sleep(random.uniform(2.0, 6.0))
    raise last if last else FetchError("unknown fetch failure")


# --------------------------------------------------------------------------
# Detection
# --------------------------------------------------------------------------

PRICE_RE = re.compile(r"[\d,]+")


def _price_to_int(text: str):
    m = PRICE_RE.search(text or "")
    if not m:
        return None
    try:
        return int(m.group(0).replace(",", ""))
    except ValueError:
        return None


def detect(html: str) -> dict:
    """Return {status, reason, title, sizes:[{sku,size,price_jpy}]}."""
    dom = parse_html(html)

    container = dom.find(
        lambda n: n.tag == "div"
        and "product" in n.classes
        and "type-product" in n.classes
        and any(c.startswith("post-") for c in n.classes)
    )
    if container is None:
        return {"status": UNKNOWN, "reason": "product container not found (blocked page, redirect, or theme change?)",
                "title": None, "sizes": []}

    title_el = dom.find(lambda n: n.tag == "h1" and "product_title" in n.classes)
    title = title_el.text() if title_el else None

    cls = container.classes
    b_in = "instock" in cls
    b_out = "outofstock" in cls

    stock_p = dom.find(lambda n: n.tag == "p" and "single-stock-status" in n.classes)
    a_out = stock_p is not None and "out-of-stock" in stock_p.classes

    sizes = []
    for row in dom.find_all(lambda n: n.tag == "div" and "product-form-row" in n.classes):
        def dd_of(cls_name):
            dl = row.find(lambda n: n.tag == "dl" and cls_name in n.classes)
            if not dl:
                return None
            dd = dl.find(lambda n: n.tag == "dd")
            return dd.text() if dd else None

        jpy_el = row.find(lambda n: "woocs_price_JPY" in n.classes)
        sizes.append({
            "variation_id": row.attrs.get("data-variation_id"),
            "sku": dd_of("pa-sku"),
            "size": dd_of("pa-size"),
            "price_jpy": _price_to_int(jpy_el.text()) if jpy_el else None,
        })

    if b_out and a_out:
        status, reason = OUT_OF_STOCK, "container=outofstock + out-of-stock notice present"
    elif b_in and not a_out:
        status, reason = IN_STOCK, "container=instock + no out-of-stock notice"
    else:
        status = UNKNOWN
        reason = (f"signals disagree (container instock={b_in} outofstock={b_out}, "
                  f"out-of-stock notice={a_out}) - the theme may have changed")

    return {"status": status, "reason": reason, "title": title, "sizes": sizes}


# --------------------------------------------------------------------------
# Telegram
# --------------------------------------------------------------------------

def esc(s) -> str:
    return (str(s if s is not None else "")
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def send_telegram(text: str, quiet: bool = False) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        print("[telegram] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set - message not sent:",
              file=sys.stderr)
        print(text, file=sys.stderr)
        return False
    payload = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "false",
        "disable_notification": "true" if quiet else "false",
    }).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload, method="POST",
    )
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                body = json.loads(resp.read().decode("utf-8", "replace"))
                if body.get("ok"):
                    return True
                print(f"[telegram] API said: {body}", file=sys.stderr)
        except Exception as e:  # noqa: BLE001
            print(f"[telegram] attempt {attempt + 1} failed: {type(e).__name__}: {e}", file=sys.stderr)
        time.sleep(2 * (attempt + 1))
    return False


def fmt_sizes(sizes) -> str:
    lines = []
    for s in sizes:
        if not s.get("size"):
            continue
        price = f"¥{s['price_jpy']:,}" if s.get("price_jpy") else "price n/a"
        lines.append(f"• {esc(s['size'])} — {esc(price)}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------

def load_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except json.JSONDecodeError as e:
        print(f"[state] {path} is corrupt ({e}); starting fresh", file=sys.stderr)
        return default


def save_state(state: dict) -> None:
    # Deliberately no volatile "last checked" timestamp: an unchanged healthy run
    # must produce a byte-identical file so CI does not commit on every run.
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True, ensure_ascii=False)
        f.write("\n")


# --------------------------------------------------------------------------
# Core check
# --------------------------------------------------------------------------

def check_product(product: dict, state: dict, opts) -> list:
    """Check one product, mutate its state entry, return messages to send."""
    url = product["url"]
    name = product.get("name") or url
    entry = state.setdefault(url, {})
    msgs = []

    now_myt = datetime.now(MYT)

    try:
        html = fetch(url, timeout=opts.timeout)
        result = detect(html)
    except FetchError as e:
        result = {"status": UNKNOWN, "reason": f"fetch failed: {e.reason}", "title": None, "sizes": []}

    status = result["status"]
    prev = entry.get("status")
    print(f"[{now_myt:%Y-%m-%d %H:%M:%S %Z}] {name}: {status} ({result['reason']})")

    # ---- blind-streak tracking -------------------------------------------
    if status == UNKNOWN:
        entry["fail_streak"] = int(entry.get("fail_streak", 0)) + 1
        if entry["fail_streak"] >= opts.blind_after and not entry.get("blind_notified"):
            entry["blind_notified"] = True
            msgs.append(
                f"⚠️ <b>Watcher is blind</b> — {esc(name)}\n\n"
                f"{entry['fail_streak']} consecutive checks could not be read.\n"
                f"Last reason: {esc(result['reason'])}\n\n"
                f"Silence from this watcher no longer means \"no stock\". "
                f"Check whether the site changed or is blocking the runner.\n\n{esc(url)}"
            )
        # Do not overwrite last known good status with UNKNOWN.
        return msgs

    if entry.get("blind_notified"):
        entry["blind_notified"] = False
        msgs.append(f"✅ <b>Watcher recovered</b> — {esc(name)}\nReading the page normally again.")
    entry["fail_streak"] = 0

    # ---- price tracking ---------------------------------------------------
    new_prices = {s["sku"]: s["price_jpy"] for s in result["sizes"] if s.get("sku") and s.get("price_jpy")}
    old_prices = entry.get("prices") or {}
    if opts.price_alerts and old_prices and new_prices:
        changed = []
        for sku, new_p in new_prices.items():
            old_p = old_prices.get(sku)
            if old_p and old_p != new_p:
                label = next((s["size"] for s in result["sizes"] if s.get("sku") == sku), sku)
                arrow = "▲" if new_p > old_p else "▼"
                changed.append(f"• {esc(label)}: ¥{old_p:,} → ¥{new_p:,} {arrow}")
        if changed:
            msgs.append(
                f"💴 <b>Price change</b> — {esc(name)}\n\n" + "\n".join(changed) + f"\n\n{esc(url)}"
            )
    if new_prices:
        entry["prices"] = new_prices

    # ---- stock transition -------------------------------------------------
    if status == IN_STOCK:
        if not entry.get("notified_in_stock"):
            entry["notified_in_stock"] = True
            entry["in_stock_since"] = now_myt.isoformat(timespec="seconds")
            body = fmt_sizes(result["sizes"])
            msgs.append(
                f"🍵 <b>IN STOCK</b> — {esc(result['title'] or name)}\n\n"
                + (body + "\n\n" if body else "")
                + "⚠️ Per-size availability is not shown to logged-out visitors, "
                  "so open the page to see which size is actually buyable.\n\n"
                + esc(url)
            )
    else:  # OUT_OF_STOCK -> re-arm
        if entry.get("notified_in_stock"):
            entry["notified_in_stock"] = False
            entry.pop("in_stock_since", None)

    entry["status"] = status
    if prev != status:
        entry["since"] = now_myt.isoformat(timespec="seconds")
    return msgs


def maybe_heartbeat(products, state, opts) -> list:
    if not opts.heartbeat:
        return []
    now = datetime.now(MYT)
    today = now.strftime("%Y-%m-%d")
    if now.hour < opts.heartbeat_hour:
        return []
    if state.get("_meta", {}).get("last_heartbeat") == today:
        return []
    state.setdefault("_meta", {})["last_heartbeat"] = today

    lines = []
    for p in products:
        e = state.get(p["url"], {})
        st = e.get("status", "never checked")
        icon = {IN_STOCK: "🟢", OUT_OF_STOCK: "⚪", }.get(st, "🟡")
        extra = f" (blind ×{e['fail_streak']})" if e.get("fail_streak") else ""
        lines.append(f"{icon} {esc(p.get('name') or p['url'])}: {esc(st)}{extra}")
    return [f"🫖 <b>Daily check-in</b> — {now:%d %b %Y}\n\n" + "\n".join(lines)]


def run_once(opts) -> int:
    cfg = load_json(CONFIG_PATH, {"products": []})
    products = cfg.get("products") or []
    if not products:
        print(f"No products configured in {CONFIG_PATH}", file=sys.stderr)
        return 2

    state = load_json(STATE_PATH, {})
    messages = []
    for i, product in enumerate(products):
        if i:
            time.sleep(random.uniform(3.0, 9.0))  # don't hammer in lockstep
        try:
            messages.extend(check_product(product, state, opts))
        except Exception as e:  # noqa: BLE001
            print(f"[error] {product.get('url')}: {type(e).__name__}: {e}", file=sys.stderr)

    messages.extend(maybe_heartbeat(products, state, opts))
    save_state(state)

    ok = True
    for m in messages:
        if opts.dry_run:
            print("--- would send ---\n" + m + "\n")
        else:
            ok = send_telegram(m) and ok
    if messages and not ok:
        return 1
    return 0


def run_loop(opts) -> int:
    print(f"Loop mode: every {opts.min_interval}-{opts.max_interval}s. Ctrl-C to stop.")
    while True:
        try:
            run_once(opts)
        except KeyboardInterrupt:
            return 0
        except Exception as e:  # noqa: BLE001
            print(f"[loop] unhandled: {type(e).__name__}: {e}", file=sys.stderr)
        time.sleep(random.uniform(opts.min_interval, opts.max_interval))


def selftest(opts) -> int:
    """Prove, from THIS machine, that the site is readable and parsing works."""
    cfg = load_json(CONFIG_PATH, {"products": []})
    products = cfg.get("products") or []
    print("=== self-test ===")
    failures = 0
    for p in products:
        print(f"\n{p.get('name')}\n  {p['url']}")
        try:
            html = fetch(p["url"], timeout=opts.timeout)
        except FetchError as e:
            print(f"  FETCH FAILED: {e.reason}")
            print("  -> this runner cannot read the site (likely IP/WAF block).")
            failures += 1
            continue
        print(f"  fetched {len(html):,} bytes")
        r = detect(html)
        print(f"  title  : {r['title']}")
        print(f"  status : {r['status']}")
        print(f"  reason : {r['reason']}")
        for s in r["sizes"]:
            price = f"¥{s['price_jpy']:,}" if s["price_jpy"] else "n/a"
            print(f"    - {s['size']:<40} {s['sku'] or '':<12} {price}")
        if r["status"] == UNKNOWN:
            failures += 1
        if not r["sizes"]:
            print("  NOTE: no variation rows parsed (fine for a simple product, "
                  "suspicious for a variable one).")
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat = os.environ.get("TELEGRAM_CHAT_ID", "")
    print(f"\nTelegram configured: token={'yes' if token else 'NO'} chat_id={'yes' if chat else 'NO'}")
    print(f"\n{'FAILURES: ' + str(failures) if failures else 'All products read cleanly.'}")
    return 1 if failures else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Marukyu-Koyamaen stock watcher")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="single check (default; used by CI)")
    mode.add_argument("--loop", action="store_true", help="run forever with randomized interval (VPS)")
    mode.add_argument("--selftest", action="store_true", help="fetch + parse + report, never notifies")
    mode.add_argument("--test-telegram", action="store_true", help="send one test message and exit")
    ap.add_argument("--dry-run", action="store_true", help="print messages instead of sending")
    ap.add_argument("--timeout", type=int, default=int(os.environ.get("TIMEOUT", 25)))
    ap.add_argument("--blind-after", type=int, default=int(os.environ.get("BLIND_AFTER", 6)),
                    help="consecutive unreadable checks before alerting (default 6 ~ 40 min)")
    ap.add_argument("--no-price-alerts", dest="price_alerts", action="store_false",
                    default=os.environ.get("PRICE_ALERTS", "1") != "0")
    ap.add_argument("--no-heartbeat", dest="heartbeat", action="store_false",
                    default=os.environ.get("HEARTBEAT", "1") != "0")
    ap.add_argument("--heartbeat-hour", type=int, default=int(os.environ.get("HEARTBEAT_HOUR", 9)),
                    help="hour (0-23, Malaysia time) for the daily check-in")
    ap.add_argument("--min-interval", type=int, default=int(os.environ.get("MIN_INTERVAL", 300)))
    ap.add_argument("--max-interval", type=int, default=int(os.environ.get("MAX_INTERVAL", 420)))
    opts = ap.parse_args()

    if opts.selftest:
        return selftest(opts)
    if opts.test_telegram:
        ok = send_telegram("🍵 <b>Test</b> — matcha watcher is wired up correctly.")
        print("sent" if ok else "FAILED - check token/chat_id")
        return 0 if ok else 1
    if opts.loop:
        return run_loop(opts)
    return run_once(opts)


if __name__ == "__main__":
    sys.exit(main())
