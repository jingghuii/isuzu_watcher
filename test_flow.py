#!/usr/bin/env python3
"""End-to-end state-machine test with the network and Telegram mocked.

Verifies the behaviour that was actually specified:
  - silent while out of stock
  - exactly ONE alert per restock, then re-arm
  - blind-streak alert when the page becomes unreadable, and a recovery notice
  - price-change alert
  - daily heartbeat fires once per day
"""
import json
import os
import sys
import tempfile
from argparse import Namespace
from datetime import datetime, timedelta

import matcha_watch as mw
from test_detect import IN_CLS, ISUZU_ROWS, OUT_CLS, OUT_P, page

SENT: list = []
NEXT_HTML = {"body": None, "raise": None}


def fake_fetch(url, timeout=25, attempts=2):
    if NEXT_HTML["raise"]:
        raise mw.FetchError(NEXT_HTML["raise"])
    return NEXT_HTML["body"]


def fake_send(text, quiet=False):
    SENT.append(text)
    return True


mw.fetch = fake_fetch
mw.send_telegram = fake_send
mw.time.sleep = lambda *_a, **_k: None

OPTS = Namespace(timeout=5, blind_after=3, price_alerts=True, heartbeat=True,
                 heartbeat_hour=9, dry_run=False, min_interval=1, max_interval=2)

URL = "https://example.test/p/1191040c1"
PRODUCTS = [{"name": "Isuzu", "url": URL}]


def serve(body=None, fail=None):
    NEXT_HTML["body"] = body
    NEXT_HTML["raise"] = fail


def tick(state):
    SENT.clear()
    msgs = mw.check_product(PRODUCTS[0], state, OPTS)
    for m in msgs:
        mw.send_telegram(m)
    return msgs


def expect(cond, label):
    if not cond:
        raise AssertionError(label)
    print(f"  PASS  {label}")


def main():
    tmp = tempfile.mkdtemp()
    mw.STATE_PATH = os.path.join(tmp, "state.json")
    state: dict = {}

    OUT_PAGE = page(OUT_CLS, OUT_P)
    IN_PAGE = page(IN_CLS, "", rows=ISUZU_ROWS)

    # 1. out of stock, repeatedly -> total silence
    serve(OUT_PAGE)
    quiet = all(tick(state) == [] for _ in range(5))
    expect(quiet, "5 out-of-stock checks send nothing")

    # 2. restock -> exactly one alert
    serve(IN_PAGE)
    m = tick(state)
    expect(len(m) == 1 and "IN STOCK" in m[0], "restock fires exactly one alert")
    expect("40g can" in m[0] and "2,520" in m[0], "alert lists sizes and prices")
    expect("logged-out" in m[0], "alert states the per-size caveat honestly")

    # 3. still in stock -> no repeats (chosen behaviour: once, then re-arm)
    quiet = all(tick(state) == [] for _ in range(4))
    expect(quiet, "stays silent while it remains in stock")

    # 4. sells out -> silent, and re-arms
    serve(OUT_PAGE)
    expect(tick(state) == [], "selling out is silent")
    expect(state[URL]["notified_in_stock"] is False, "watcher re-armed after selling out")

    # 5. restock again -> alerts again
    serve(IN_PAGE)
    expect(len(tick(state)) == 1, "second restock alerts again")

    # 6. price change
    serve(page(IN_CLS, "", rows=ISUZU_ROWS.replace("5,650", "6,100")))
    m = tick(state)
    expect(any("Price change" in x for x in m), "price change is reported")
    expect(any("5,650" in x and "6,100" in x for x in m), "price alert shows old and new")

    # 7. site goes unreadable -> silent until the streak threshold, then one alert
    serve(fail="HTTP 403")
    expect(tick(state) == [] and tick(state) == [], "first 2 blind checks stay quiet")
    m = tick(state)
    expect(len(m) == 1 and "blind" in m[0].lower(), "blind-streak alert fires at threshold")
    expect(all(tick(state) == [] for _ in range(3)), "blind alert does not repeat")
    expect(state[URL]["status"] == mw.IN_STOCK,
           "UNKNOWN does not overwrite last known good status")

    # 8. recovery notice
    serve(OUT_PAGE)
    m = tick(state)
    expect(any("recovered" in x.lower() for x in m), "recovery is announced")

    # 9. a blocked page must never be read as a restock
    serve("<html><body>Access denied" + "x" * 5000 + "</body></html>")
    state2: dict = {}
    for _ in range(2):
        tick(state2)
    expect(all("IN STOCK" not in s for s in SENT), "blocked page never alerts as in stock")

    # 10. heartbeat: once per day, only after the configured hour
    st = {URL: {"status": mw.OUT_OF_STOCK}}
    real_dt = mw.datetime

    class At:
        def __init__(self, h, d=5):
            self.h, self.d = h, d

        def now(self, tz=None):
            return real_dt(2026, 9, self.d, self.h, 0, tzinfo=mw.MYT)

    mw.datetime = At(7)
    expect(mw.maybe_heartbeat(PRODUCTS, st, OPTS) == [], "no heartbeat before the set hour")
    mw.datetime = At(9)
    expect(len(mw.maybe_heartbeat(PRODUCTS, st, OPTS)) == 1, "heartbeat fires at the set hour")
    mw.datetime = At(14)
    expect(mw.maybe_heartbeat(PRODUCTS, st, OPTS) == [], "heartbeat does not repeat same day")
    mw.datetime = At(9, 6)
    expect(len(mw.maybe_heartbeat(PRODUCTS, st, OPTS)) == 1, "heartbeat fires again next day")
    mw.datetime = real_dt

    # 11. state file must be byte-stable so CI does not commit every run
    mw.save_state({"a": {"status": mw.OUT_OF_STOCK, "prices": {"X": 1}}})
    first = open(mw.STATE_PATH, "rb").read()
    mw.save_state({"a": {"prices": {"X": 1}, "status": mw.OUT_OF_STOCK}})
    expect(open(mw.STATE_PATH, "rb").read() == first,
           "unchanged state serializes byte-identically (no commit churn)")

    print("\nAll flow checks passed.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as e:
        print(f"  FAIL  {e}")
        sys.exit(1)
