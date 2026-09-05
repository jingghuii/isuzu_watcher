#!/usr/bin/env python3
"""
Fixtures below are trimmed copies of the REAL markup observed on
marukyu-koyamaen.co.jp on 2026-09-05:

  - OUT: /english/shop/products/1191040c1  (Isuzu, sold out)
  - IN : /english/shop/products/1186000cc  (Sweetened Matcha - Excellent, in stock)

They exercise the parser, not the network. Live reachability from a given
runner is proven separately with `matcha_watch.py --selftest`.
"""
import sys

from matcha_watch import IN_STOCK, OUT_OF_STOCK, UNKNOWN, detect

ROW = """
<div class="product-form-row product-form-row-{vid}" data-variation_id="{vid}">
  <div class="woocommerce-variation single_variation product-form-info-block">
    <div class="product-attributes">
      <dl class="pa pa-sku"><dt>SKU</dt><dd>{sku}</dd></dl>
      <dl class="pa pa-size"><dt>Size</dt><dd>{size}</dd></dl>
    </div>
    <div class="price">
      <span class="woocs_price_code woocs_price_JPY" data-product-id="{vid}">
        <span class="woocommerce-Price-amount amount"><bdi>
          <span class="woocommerce-Price-currencySymbol">&yen;</span>{price}</bdi></span>
      </span>
      <span class="woocs_price_code woocs_price_USD">
        <span class="woocommerce-Price-amount amount"><bdi>$16.17</bdi></span>
      </span>
    </div>
  </div>
</div>
"""

ISUZU_ROWS = (
    ROW.format(vid="16943", sku="1191040C1", size="40g can", price="2,520")
    + ROW.format(vid="16944", sku="1191100C1", size="100g can", price="5,650")
    + ROW.format(vid="16945", sku="1F43100C6", size="100g bag", price="5,340")
    + ROW.format(vid="16946", sku="1191200C1", size="200g can", price="10,700")
)


def page(container_cls, stock_p, rows=ISUZU_ROWS, title="Isuzu"):
    return f"""<!DOCTYPE html><html><head><title>{title}</title></head>
<body class="wp-singular product-template-default single single-product woocommerce-page">
<div class="{container_cls}">
  <div class="summary entry-summary">
    <header class="product_title_header"><h1 class="product_title entry-title">{title}</h1></header>
    {stock_p}
  </div>
  <div class="product-forms">{rows}</div>
</div>
<div class="filler">{'padding ' * 400}</div>
</body></html>"""


OUT_CLS = ("post-403 product type-product status-publish has-post-thumbnail "
           "product_cat-principal product_cat-matcha first outofstock taxable "
           "shipping-taxable purchasable product-type-variable")
IN_CLS = ("post-2030 product type-product status-publish has-post-thumbnail "
          "product_cat-greentea product_cat-matcha first instock featured taxable "
          "shipping-taxable purchasable product-type-variable")
OUT_P = ('<p class="stock single-stock-status out-of-stock">'
         'This product is currently out of stock and unavailable.</p>')

CASES = []


def case(name, fn):
    CASES.append((name, fn))


def case_out_of_stock():
    r = detect(page(OUT_CLS, OUT_P))
    assert r["status"] == OUT_OF_STOCK, r
    assert r["title"] == "Isuzu", r
    assert len(r["sizes"]) == 4, r
    assert r["sizes"][0] == {"variation_id": "16943", "sku": "1191040C1",
                             "size": "40g can", "price_jpy": 2520}, r["sizes"][0]
    assert r["sizes"][3]["price_jpy"] == 10700, r["sizes"][3]


def case_in_stock():
    r = detect(page(IN_CLS, ""))
    assert r["status"] == IN_STOCK, r
    assert len(r["sizes"]) == 4, r


def case_blocked_page():
    """A WAF/403 body has no product container -> must NOT read as in stock."""
    html = "<html><body><h1>Access denied</h1>" + "x" * 5000 + "</body></html>"
    r = detect(html)
    assert r["status"] == UNKNOWN, r
    assert "container not found" in r["reason"], r


def case_login_wall():
    """A redirect to a login page also lacks the container."""
    html = "<html><body><form id='loginform'>Please log in</form>" + "y" * 5000 + "</body></html>"
    assert detect(html)["status"] == UNKNOWN


def case_signals_disagree():
    """Container says in stock but the out-of-stock notice is still there.
    That means the theme changed; refuse to guess."""
    r = detect(page(IN_CLS, OUT_P))
    assert r["status"] == UNKNOWN, r
    assert "disagree" in r["reason"], r


def case_stale_class_only():
    """Notice gone but container class also gone -> not enough evidence."""
    cls = OUT_CLS.replace(" outofstock", "")
    r = detect(page(cls, ""))
    assert r["status"] == UNKNOWN, r


def case_truncated_html():
    """A cut-off response must not parse as in stock."""
    html = page(OUT_CLS, OUT_P)[: len(page(OUT_CLS, OUT_P)) // 3]
    r = detect(html)
    assert r["status"] in (UNKNOWN, OUT_OF_STOCK), r
    assert r["status"] != IN_STOCK, r


def case_simple_product_no_rows():
    """Extensibility: a non-variable product still resolves stock, just no sizes."""
    r = detect(page(IN_CLS, "", rows="", title="Some Simple Product"))
    assert r["status"] == IN_STOCK, r
    assert r["sizes"] == [], r


def case_price_change_detected():
    a = detect(page(OUT_CLS, OUT_P))
    bumped = ISUZU_ROWS.replace("2,520", "2,780")
    b = detect(page(OUT_CLS, OUT_P, rows=bumped))
    pa = {s["sku"]: s["price_jpy"] for s in a["sizes"]}
    pb = {s["sku"]: s["price_jpy"] for s in b["sizes"]}
    assert pa["1191040C1"] == 2520 and pb["1191040C1"] == 2780, (pa, pb)


for _n, _f in [
    ("out of stock (real Isuzu markup)", case_out_of_stock),
    ("in stock (real 1186000cc markup)", case_in_stock),
    ("blocked / 403 body", case_blocked_page),
    ("login wall", case_login_wall),
    ("signals disagree", case_signals_disagree),
    ("missing container class", case_stale_class_only),
    ("truncated response", case_truncated_html),
    ("simple product, no variations", case_simple_product_no_rows),
    ("per-size price parsing", case_price_change_detected),
]:
    case(_n, _f)


if __name__ == "__main__":
    failed = 0
    for name, fn in CASES:
        try:
            fn()
            print(f"  PASS  {name}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL  {name}\n        {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  ERROR {name}\n        {type(e).__name__}: {e}")
    print(f"\n{len(CASES) - failed}/{len(CASES)} passed")
    sys.exit(1 if failed else 0)
