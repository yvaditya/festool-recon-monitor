#!/usr/bin/env python3
"""
Festool Recon sale monitor.

Polls festoolrecon.com (a Shopify store), figures out which items are currently
for sale, diffs that against the last saved snapshot, and pushes a notification
when:
  - any NEW item comes on sale            (normal alert)
  - an item matching your watchlist       (urgent alert)
  - an item sells out / disappears        (optional, off by default)

Notification channels (use either or both; at least one required):
  - ntfy.sh  -> open-source push to your phone, NO account/password/token.
                Just set NTFY_TOPIC. (recommended)
  - email/SMTP -> set SMTP_USER / SMTP_PASS / MAIL_TO.

Also writes docs/catalog.json every run so the GitHub Pages picker UI can show
the full catalog without hitting CORS-restricted festoolrecon endpoints.

Pure standard library -- no `pip install` needed.

Local testing:
    python3 festool_monitor.py --dry-run       # scrape + print, no send, no writes
    python3 festool_monitor.py --catalog-only  # just (re)build docs/catalog.json
    python3 festool_monitor.py --test          # send a test via configured channels
"""

import argparse
import json
import os
import smtplib
import ssl
import sys
import time
import urllib.error
import urllib.request
from email.message import EmailMessage
from email.utils import formatdate

BASE = "https://www.festoolrecon.com"
USER_AGENT = "Mozilla/5.0 (compatible; festool-recon-monitor/2.0; +github-actions)"
STATE_FILE = "state.json"
CONFIG_FILE = "config.json"
CATALOG_FILE = "docs/catalog.json"
BROWSE_URL = f"{BASE}/collections/oneanddone"


def log(*args):
    print(*args, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- #
# Fetching
# --------------------------------------------------------------------------- #
def fetch_json(url, retries=3, backoff=2.0):
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError) as err:
            last_err = err
            log(f"  fetch attempt {attempt}/{retries} failed for {url}: {err}")
            if attempt < retries:
                time.sleep(backoff * attempt)
    raise RuntimeError(f"could not fetch {url}: {last_err}")


def fetch_all_products():
    """Page through /products.json (250 per page) and return every product."""
    products = []
    page = 1
    while page <= 12:  # safety cap; the store has ~500 products
        data = fetch_json(f"{BASE}/products.json?limit=250&page={page}")
        batch = data.get("products", [])
        if not batch:
            break
        products.extend(batch)
        if len(batch) < 250:
            break
        page += 1
    return products


# --------------------------------------------------------------------------- #
# "On sale" model
# --------------------------------------------------------------------------- #
def is_placeholder(product):
    """The store shows a 'Wow, that went fast!!!' placeholder when sold out."""
    if (product.get("product_type") or "").strip().lower() == "placeholder":
        return True
    title = (product.get("title") or "").lower()
    return "went fast" in title or "check back" in title


def variant_summary(product):
    """Return (min_price, best_compare_at, is_available) across all variants."""
    variants = product.get("variants") or []
    available = any(v.get("available") for v in variants)
    prices, comps = [], []
    for v in variants:
        try:
            if v.get("price") not in (None, ""):
                prices.append(float(v["price"]))
        except (TypeError, ValueError):
            pass
        try:
            if v.get("compare_at_price"):
                comps.append(float(v["compare_at_price"]))
        except (TypeError, ValueError):
            pass
    price = min(prices) if prices else None
    compare_at = max(comps) if comps else None
    return price, compare_at, available


def on_sale_items(products):
    """Map handle -> item dict for every product that is buyable right now."""
    items = {}
    for p in products:
        if is_placeholder(p):
            continue
        price, compare_at, available = variant_summary(p)
        if not available:
            continue
        if price is not None and price < 1:  # skip $0.01 placeholder-style entries
            continue
        handle = p.get("handle")
        if not handle:
            continue
        items[handle] = {
            "handle": handle,
            "title": (p.get("title") or "").strip(),
            "price": price,
            "compare_at": compare_at,
            "type": (p.get("product_type") or "").strip(),
            "url": f"{BASE}/products/{handle}",
        }
    return items


def build_catalog(products):
    """Full catalog (sold + on-sale) for the picker UI."""
    items = []
    for p in products:
        if is_placeholder(p):
            continue
        handle = p.get("handle")
        if not handle:
            continue
        price, compare_at, available = variant_summary(p)
        items.append({
            "title": (p.get("title") or "").strip(),
            "type": (p.get("product_type") or "").strip() or "Other",
            "handle": handle,
            "available": bool(available),
            "price": price,
            "compare_at": compare_at,
            "url": f"{BASE}/products/{handle}",
        })
    items.sort(key=lambda x: (x["type"].lower(), x["title"].lower()))
    return items


def write_catalog(products):
    catalog = build_catalog(products)
    os.makedirs(os.path.dirname(CATALOG_FILE), exist_ok=True)
    payload = {"generated": formatdate(localtime=True), "count": len(catalog), "products": catalog}
    with open(CATALOG_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    log(f"catalog written: {len(catalog)} products -> {CATALOG_FILE}")


# --------------------------------------------------------------------------- #
# Watchlist + formatting
# --------------------------------------------------------------------------- #
def watchlist_hits(title, watchlist):
    t = title.lower()
    return [w for w in watchlist if w.strip() and w.strip().lower() in t]


def fmt_price(price, compare_at):
    if price is None:
        return ""

    def money(x):
        return f"${x:,.0f}" if float(x).is_integer() else f"${x:,.2f}"

    base = money(price)
    if compare_at and compare_at > price:
        pct = round((1 - price / compare_at) * 100)
        return f"{base} (was {money(compare_at)}, -{pct}%)"
    return base


def fmt_item(item, bullet="-"):
    price = fmt_price(item.get("price"), item.get("compare_at"))
    line = f"{bullet} {item['title']}"
    if price:
        line += f" - {price}"
    line += f"\n  {item['url']}"
    return line


# --------------------------------------------------------------------------- #
# Notification channels
# --------------------------------------------------------------------------- #
def channel_config():
    env = {k: os.environ.get(k, "").strip() for k in (
        "NTFY_TOPIC", "NTFY_SERVER", "NTFY_TOKEN",
        "SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASS", "MAIL_FROM", "MAIL_TO",
    )}
    env["NTFY_SERVER"] = env["NTFY_SERVER"] or "https://ntfy.sh"
    env["SMTP_HOST"] = env["SMTP_HOST"] or "smtp.gmail.com"
    env["SMTP_PORT"] = env["SMTP_PORT"] or "587"
    return env


def enabled_channels(env):
    chans = []
    if env.get("NTFY_TOPIC"):
        chans.append("ntfy")
    if env.get("SMTP_USER") and env.get("SMTP_PASS") and env.get("MAIL_TO"):
        chans.append("email")
    return chans


def send_ntfy(env, title, body, urgent=False, click=None):
    server = env["NTFY_SERVER"].rstrip("/")
    url = f"{server}/{env['NTFY_TOPIC']}"
    headers = {
        "Title": title.encode("ascii", "ignore").decode().replace("\n", " ")[:250],
        "Priority": "5" if urgent else "4",
        "Tags": "rotating_light,fire" if urgent else "package",
    }
    if click:
        headers["Click"] = click
    if env.get("NTFY_TOKEN"):
        headers["Authorization"] = f"Bearer {env['NTFY_TOKEN']}"
    req = urllib.request.Request(url, data=body.encode("utf-8"), headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        resp.read()


def send_email(env, subject, body):
    host, port = env["SMTP_HOST"], int(env["SMTP_PORT"])
    user, password = env["SMTP_USER"], env["SMTP_PASS"]
    mail_from = env.get("MAIL_FROM") or user
    recipients = [x.strip() for x in env["MAIL_TO"].split(",") if x.strip()]

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = mail_from
    msg["To"] = ", ".join(recipients)
    msg["Date"] = formatdate(localtime=True)
    msg.set_content(body)

    context = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=context, timeout=30) as s:
            s.login(user, password)
            s.send_message(msg, from_addr=mail_from, to_addrs=recipients)
    else:
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.ehlo()
            s.starttls(context=context)
            s.ehlo()
            s.login(user, password)
            s.send_message(msg, from_addr=mail_from, to_addrs=recipients)


def notify(args, env, title, body, urgent=False, click=None):
    """Send to every configured channel. Returns True if at least one succeeded."""
    if args.dry_run:
        log("\n=== DRY RUN — would notify ===")
        log(("[URGENT] " if urgent else "") + title)
        log(body)
        log("=== end dry run ===\n")
        return True

    chans = enabled_channels(env)
    if not chans:
        raise SystemExit(
            "No notification channel configured.\n"
            "Set NTFY_TOPIC (recommended: open-source, no credentials) "
            "or SMTP_USER/SMTP_PASS/MAIL_TO as GitHub Actions secrets."
        )

    ok = False
    for ch in chans:
        try:
            if ch == "ntfy":
                send_ntfy(env, title, body, urgent=urgent, click=click)
            elif ch == "email":
                subject = ("[WATCH] " if urgent else "") + title + " — Festool Recon"
                send_email(env, subject, body)
            log(f"sent via {ch}")
            ok = True
        except Exception as err:  # noqa: BLE001 - report and try other channels
            log(f"ERROR sending via {ch}: {err}")
    if not ok:
        raise RuntimeError("all notification channels failed")
    return ok


# --------------------------------------------------------------------------- #
# State I/O
# --------------------------------------------------------------------------- #
def load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as err:
        log(f"warning: could not read {path} ({err}); using default")
        return default


def write_state(args, current):
    if args.dry_run:
        log("(dry-run) not writing state")
        return
    payload = {"on_sale": current, "last_run": formatdate(localtime=True), "initialized": True}
    with open(args.state, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False, sort_keys=True)
    log(f"state written: {len(current)} items on sale")


# --------------------------------------------------------------------------- #
# Message builders
# --------------------------------------------------------------------------- #
def build_baseline_body(current, watchlist):
    lines = [f"Festool Recon monitor is live. {len(current)} item(s) currently on sale:\n"]
    for item in sorted(current.values(), key=lambda x: x["title"].lower()):
        hits = watchlist_hits(item["title"], watchlist)
        lines.append(fmt_item(item, bullet="[WATCH]" if hits else "-"))
    lines.append("\nFrom now on you'll get an alert when something NEW comes on sale")
    lines.append("([WATCH] = matches your watchlist).")
    lines.append(f"Watchlist: {', '.join(watchlist) if watchlist else '(empty — pick items in the web UI)'}")
    lines.append(f"\nBrowse: {BROWSE_URL}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    parser = argparse.ArgumentParser(description="Festool Recon sale monitor")
    parser.add_argument("--dry-run", action="store_true",
                        help="scrape and print; do not send or write anything")
    parser.add_argument("--catalog-only", action="store_true",
                        help="only (re)build docs/catalog.json, then exit")
    parser.add_argument("--test", action="store_true",
                        help="send a test notification via configured channels and exit")
    parser.add_argument("--state", default=STATE_FILE)
    parser.add_argument("--config", default=CONFIG_FILE)
    args = parser.parse_args()

    env = channel_config()

    if args.test:
        notify(args, env, "Festool Recon monitor test",
               "If you're reading this on your phone, notifications work. 🎉",
               urgent=False, click=BROWSE_URL)
        log("test notification sent")
        return 0

    config = load_json(args.config, {"watchlist": [], "alert_on_new": True, "alert_on_sold_out": False})
    watchlist = config.get("watchlist") or []
    alert_on_new = config.get("alert_on_new", True)
    alert_on_sold_out = config.get("alert_on_sold_out", False)

    log("fetching products...")
    products = fetch_all_products()
    current = on_sale_items(products)
    log(f"{len(products)} products scanned, {len(current)} currently on sale")

    if not args.dry_run:
        write_catalog(products)
    if args.catalog_only:
        return 0

    state = load_json(args.state, {"on_sale": {}, "last_run": None, "initialized": False})
    previous = state.get("on_sale") or {}
    initialized = bool(state.get("initialized") and state.get("last_run"))

    # Guard: an empty result usually means the site blocked us / had a hiccup.
    if not current and previous:
        log("ERROR: 0 items on sale but previous state had items — likely a fetch problem. "
            "Leaving state untouched and exiting non-zero.")
        return 1

    # First ever run: send a one-time summary, set the baseline, then stop.
    if not initialized:
        notify(args, env, f"Monitor live — {len(current)} on sale now",
               build_baseline_body(current, watchlist), urgent=False, click=BROWSE_URL)
        write_state(args, current)
        return 0

    new_handles = [h for h in current if h not in previous]
    gone_handles = [h for h in previous if h not in current]

    urgent, normal = [], []
    for h in new_handles:
        item = current[h]
        (urgent if watchlist_hits(item["title"], watchlist) else normal).append(item)

    sections, title_bits, notify_now, click = [], [], False, BROWSE_URL

    if urgent:
        notify_now = True
        title_bits.append(f"{len(urgent)} watched")
        click = urgent[0]["url"]
        sections.append("ON YOUR WATCHLIST — ON SALE NOW:\n" +
                        "\n".join(fmt_item(i, bullet="[WATCH]") for i in urgent))

    if alert_on_new and normal:
        notify_now = True
        title_bits.append(f"{len(normal)} new")
        if not urgent:
            click = normal[0]["url"]
        sections.append("New on sale:\n" + "\n".join(fmt_item(i) for i in normal))

    if alert_on_sold_out and gone_handles:
        notify_now = True
        gone = [previous[h] for h in gone_handles]
        title_bits.append(f"{len(gone)} sold out")
        sections.append("Sold out / removed:\n" +
                        "\n".join(f"- {g.get('title', g)}" for g in gone))

    if not notify_now:
        log(f"no notable changes (new={len(new_handles)}, gone={len(gone_handles)})")
        write_state(args, current)  # advance baseline silently
        return 0

    title = " + ".join(title_bits) + " on Festool Recon"
    body = "\n\n".join(sections) + f"\n\n{len(current)} item(s) on sale total.\nBrowse: {BROWSE_URL}"

    notify(args, env, title, body, urgent=bool(urgent), click=click)  # raises -> state not advanced
    write_state(args, current)
    return 0


if __name__ == "__main__":
    sys.exit(main())
