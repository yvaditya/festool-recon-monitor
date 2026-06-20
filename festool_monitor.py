#!/usr/bin/env python3
"""
Festool Recon sale monitor.

Polls festoolrecon.com (a Shopify store), figures out which items are currently
for sale, diffs that against the last saved snapshot, and pushes a notification
when:
  - the CURRENT featured deal changes     (the item the site shows up front)
  - any NEW item comes on sale            (normal alert)
  - an item matching your watchlist       (urgent alert)
  - an item sells out / disappears        (optional, off by default)

Every notification also lists everything currently on sale (⭐ current deal and
🔥 watched items first).

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
import re
import smtplib
import ssl
import sys
import time
import urllib.error
import urllib.request
from email.message import EmailMessage
from email.utils import formatdate

BASE = "https://www.festoolrecon.com"
USER_AGENT = "Mozilla/5.0 (compatible; festool-recon-monitor/3.0; +github-actions)"
STATE_FILE = "state.json"
CONFIG_FILE = "config.json"
CATALOG_FILE = "docs/catalog.json"
COLLECTION = "oneanddone"
BROWSE_URL = f"{BASE}/collections/{COLLECTION}"
MAX_LIST = 25  # cap the "on sale now" list so a big sale doesn't make a giant push


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


def fetch_text(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", "ignore")


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


def fetch_collection_order():
    """Ordered list of on-sale handles from the curated OneAndDone queue."""
    try:
        data = fetch_json(f"{BASE}/collections/{COLLECTION}/products.json?limit=250")
    except RuntimeError as err:
        log(f"  collection fetch failed: {err}")
        return []
    order = []
    for p in data.get("products", []):
        if is_placeholder(p):
            continue
        _, _, available = variant_summary(p)
        if available and p.get("handle"):
            order.append(p["handle"])
    return order


def fetch_homepage_buyable():
    """Handles the homepage actually lists for sale.

    On festoolrecon.com you can ONLY buy what the homepage shows; the rest of the
    OneAndDone collection is just the upcoming queue. Returns an ordered, unique
    list of handles, or None if the homepage couldn't be fetched.
    """
    try:
        html = fetch_text(BASE)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as err:
        log(f"  homepage fetch failed: {err}")
        return None
    seen, out = set(), []
    for h in re.findall(r"/products/([a-z0-9][a-z0-9-]*)", html):
        if h not in seen:
            seen.add(h)
            out.append(h)
    return out


def buyable_items(handles, prod_by_handle):
    """Build the on-sale dict (handle -> item) from buyable handles, in order."""
    items = {}
    for h in handles:
        p = prod_by_handle.get(h)
        if not p or is_placeholder(p):
            continue
        price, compare_at, _ = variant_summary(p)
        if price is not None and price < 1:  # skip $0.01 placeholder-style entries
            continue
        items[h] = {
            "handle": h,
            "title": (p.get("title") or "").strip(),
            "price": price,
            "compare_at": compare_at,
            "type": (p.get("product_type") or "").strip(),
            "url": f"{BASE}/products/{h}",
        }
    return items


def build_catalog(products, buyable=frozenset(), queued=frozenset()):
    """Full catalog for the picker UI, flagged buyable (homepage) / queued (collection)."""
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
            "available": bool(available),                          # has inventory in Shopify
            "buyable": handle in buyable,                          # purchasable now (on homepage)
            "queued": handle in queued and handle not in buyable,  # upcoming in the queue
            "price": price,
            "compare_at": compare_at,
            "url": f"{BASE}/products/{handle}",
        })
    items.sort(key=lambda x: (x["type"].lower(), x["title"].lower()))
    return items


def write_catalog(products, buyable=frozenset(), queued=frozenset()):
    catalog = build_catalog(products, buyable, queued)
    os.makedirs(os.path.dirname(CATALOG_FILE), exist_ok=True)
    payload = {"generated": formatdate(localtime=True), "count": len(catalog), "products": catalog}
    with open(CATALOG_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    log(f"catalog written: {len(catalog)} products -> {CATALOG_FILE}")


# --------------------------------------------------------------------------- #
# Watchlist + formatting
# --------------------------------------------------------------------------- #
def _squash(s):
    """Lowercase and strip non-alphanumerics so 'T 18' matches 'T18+3-E'."""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def watchlist_hits(title, watchlist):
    t = _squash(title)
    return [w for w in watchlist if _squash(w) and _squash(w) in t]


def is_watched(item, watchlist):
    return bool(watchlist_hits(item["title"], watchlist))


def money(x):
    if x is None:
        return ""
    return f"${x:,.0f}" if float(x).is_integer() else f"${x:,.2f}"


def fmt_price(price, compare_at):
    """Compact price like '$689 (-30%)'."""
    if price is None:
        return ""
    base = money(price)
    if compare_at and compare_at > price:
        pct = round((1 - price / compare_at) * 100)
        return f"{base} (-{pct}%)"
    return base


def sorted_current(current, watchlist, featured_handle=None):
    """Featured first, then watched, then alphabetical."""
    return sorted(
        current.values(),
        key=lambda it: (
            0 if it["handle"] == featured_handle else 1,
            0 if is_watched(it, watchlist) else 1,
            it["title"].lower(),
        ),
    )


def sale_list(current, watchlist, *, emoji, with_url, featured_handle=None):
    """The 'on sale now' block, formatted for a channel."""
    items = sorted_current(current, watchlist, featured_handle)
    head = ("\U0001f6d2 " if emoji else "") + f"On sale now ({len(current)}):"
    lines = [head]
    for it in items[:MAX_LIST]:
        featured = it["handle"] == featured_handle
        watched = is_watched(it, watchlist)
        if emoji:
            mark = ("⭐" if featured else "") + ("\U0001f525" if watched else "")
            mark = mark or "•"
        else:
            tags = ([" CURRENT"] if featured else []) + (["WATCH"] if watched else [])
            mark = ("[" + "/".join(t.strip() for t in tags) + "]") if tags else "-"
        price = fmt_price(it.get("price"), it.get("compare_at"))
        line = f"{mark} {it['title']}" + (f" — {price}" if price else "")
        if with_url:
            line += f"\n   {it['url']}"
        lines.append(line)
    if len(items) > MAX_LIST:
        lines.append(f"…and {len(items) - MAX_LIST} more")
    return "\n".join(lines)


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


def send_ntfy(env, title, body, urgent=False, click=None, actions=None, tags="bell"):
    server = env["NTFY_SERVER"].rstrip("/")
    url = f"{server}/{env['NTFY_TOPIC']}"
    headers = {
        # header values must be latin-1; keep the Title ASCII (emoji come from Tags)
        "Title": title.encode("ascii", "ignore").decode().replace("\n", " ").strip()[:250],
        "Priority": "5" if urgent else "default",
        "Tags": tags,
    }
    if click:
        headers["Click"] = click
    if actions:
        parts = []
        for label, aurl in actions[:3]:
            lbl = label.replace(",", " ").replace(";", " ").replace('"', "")
            parts.append(f'view, "{lbl}", {aurl}, clear=true')
        headers["Actions"] = "; ".join(parts)
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


def notify(args, env, msg):
    """Send a composed message dict to every configured channel."""
    if args.dry_run:
        log("\n=== DRY RUN — ntfy push ===")
        log("Title:", msg["title"], "  [urgent]" if msg["urgent"] else "", " tags:", msg["tags"])
        log(msg["ntfy_body"])
        log("--- email version ---")
        log("Subject:", msg["email_subject"])
        log(msg["email_body"])
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
                send_ntfy(env, msg["title"], msg["ntfy_body"], urgent=msg["urgent"],
                          click=msg["click"], actions=msg["actions"], tags=msg["tags"])
            elif ch == "email":
                send_email(env, msg["email_subject"], msg["email_body"])
            log(f"sent via {ch}")
            ok = True
        except Exception as err:  # noqa: BLE001 - report and try other channels
            log(f"ERROR sending via {ch}: {err}")
    if not ok:
        raise RuntimeError("all notification channels failed")
    return ok


# --------------------------------------------------------------------------- #
# Message composition
# --------------------------------------------------------------------------- #
def compose(current, watchlist, *, featured=None, featured_changed=False,
            urgent=None, normal=None, gone=None, alert_on_sold_out=False,
            baseline=False, queued_count=0, queued_watch=None):
    urgent = urgent or []
    normal = normal or []
    gone = gone or []
    queued_watch = queued_watch or []
    added = urgent + normal
    featured_handle = featured["handle"] if featured else None
    featured_watched = bool(featured) and is_watched(featured, watchlist)
    urgent_flag = bool(urgent) or (featured_changed and featured_watched)

    def priced(item):
        p = fmt_price(item.get("price"), item.get("compare_at"))
        return f"{item['title']}" + (f" — {p}" if p else "")

    # ---- headline (title) + one-line summary (lede) ----
    if baseline:
        title = f"Monitoring {len(current)} Festool Recon deals"
        lede = f"Current deal: {priced(featured)}" if featured else ""
        tags = "white_check_mark"
    elif featured_changed and featured:
        title = f"Now on sale: {featured['title']}"
        lede = f"Current deal changed → {priced(featured)}"
        if added:
            lede += f"; {len(added)} new on sale"
        tags = "fire" if urgent_flag else "star"
    elif urgent and len(urgent) == 1 and not normal:
        title = f"{urgent[0]['title']} on sale!"
        lede = f"{priced(urgent[0])} just came on sale"
        tags = "fire"
    elif added:
        kind = ("watched + new" if urgent and normal
                else "watched on sale" if urgent else "new deals")
        title = f"{len(added)} {kind} on Festool Recon"
        names = ", ".join(i["title"] for i in added[:2])
        extra = f" +{len(added) - 2} more" if len(added) > 2 else ""
        lede = f"{len(added)} new on sale: {names}{extra}"
        tags = "fire" if urgent else "shopping_cart"
    elif queued_watch:
        title = f"Coming up: {queued_watch[0]['title']}"
        names = ", ".join(i["title"] for i in queued_watch[:2])
        lede = f"On your watchlist, queued (not buyable yet): {names}"
        tags = "eyes"
    elif gone:
        title = f"{len(gone)} sold out on Festool Recon"
        lede = ", ".join(g.get("title", "?") for g in gone[:3])
        tags = "checkered_flag"
    else:
        title, lede, tags = "Festool Recon update", "", "bell"
    if alert_on_sold_out and gone and not baseline and added:
        lede += f"; {len(gone)} sold out"

    # ---- ntfy body (emoji, no raw URLs; links via action buttons) ----
    ntfy_parts = []
    if lede:
        flag = "\U0001f525 " if urgent_flag else "⭐ " if featured_changed else "\U0001f195 " if added else ""
        ntfy_parts.append(flag + lede)
    ntfy_parts.append(sale_list(current, watchlist, emoji=True, with_url=False,
                                featured_handle=featured_handle))
    if queued_watch:
        ntfy_parts.append("👀 Coming up on your watchlist:\n" + "\n".join(
            "• " + i["title"] + (f" — {fmt_price(i.get('price'), i.get('compare_at'))}"
                                 if i.get("price") is not None else "")
            for i in queued_watch))
    if queued_count:
        ntfy_parts.append(f"➕ {queued_count} more queued (not buyable yet)")
    ntfy_body = "\n\n".join(ntfy_parts)

    # ---- email body (plain text, with URLs) ----
    email_parts = []
    if lede:
        email_parts.append(("[WATCH] " if urgent_flag else "") + lede)
    email_parts.append(sale_list(current, watchlist, emoji=False, with_url=True,
                                 featured_handle=featured_handle))
    if queued_watch:
        email_parts.append("Coming up on your watchlist:\n" + "\n".join(
            "- " + i["title"] + (f" — {fmt_price(i.get('price'), i.get('compare_at'))}"
                                 if i.get("price") is not None else "") + f"\n   {i['url']}"
            for i in queued_watch))
    if queued_count:
        email_parts.append(f"+{queued_count} more queued (not buyable yet)")
    email_parts.append(f"Browse: {BROWSE_URL}")
    email_body = "\n\n".join(email_parts)
    email_subject = ("[WATCH] " if urgent_flag else "") + title

    # ---- tap targets ----
    spotlight = featured if (featured_changed and featured) else (added[0] if added else featured)
    click = spotlight["url"] if (spotlight and spotlight.get("url")) else BROWSE_URL
    actions = []
    if spotlight and spotlight.get("url"):
        actions.append(("Buy " + _short(spotlight["title"]), spotlight["url"]))
    actions.append(("All deals", BROWSE_URL))

    return {
        "title": title, "urgent": urgent_flag, "click": click, "actions": actions, "tags": tags,
        "ntfy_body": ntfy_body, "email_subject": email_subject, "email_body": email_body,
    }


def _short(title, n=18):
    return title if len(title) <= n else title[:n - 1].rstrip() + "…"


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


def write_state(args, current, featured_handle, queued_handles):
    if args.dry_run:
        log("(dry-run) not writing state")
        return
    payload = {
        "on_sale": current,
        "featured": featured_handle,
        "queued": list(queued_handles),
        "last_run": formatdate(localtime=True),
        "initialized": True,
    }
    with open(args.state, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False, sort_keys=True)
    log(f"state written: {len(current)} buyable, featured={featured_handle}, queued={len(queued_handles)}")


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
        notify(args, env, {
            "title": "Festool Recon monitor test", "urgent": False, "tags": "white_check_mark",
            "click": BROWSE_URL, "actions": [("All deals", BROWSE_URL)],
            "ntfy_body": "✅ Notifications work. You'll get a clean list like this when tools come on sale.",
            "email_subject": "Festool Recon monitor test",
            "email_body": "Notifications work. You'll get a clean list like this when tools come on sale.",
        })
        log("test notification sent")
        return 0

    config = load_json(args.config, {"watchlist": [], "alert_on_new": True, "alert_on_sold_out": False})
    watchlist = config.get("watchlist") or []
    alert_on_new = config.get("alert_on_new", True)
    alert_on_sold_out = config.get("alert_on_sold_out", False)
    alert_on_queued_watch = config.get("alert_on_queued_watch", False)

    log("fetching products...")
    products = fetch_all_products()
    prod_by_handle = {p["handle"]: p for p in products if p.get("handle")}

    # You can only BUY what the homepage lists; the rest of the queue isn't purchasable yet.
    homepage = fetch_homepage_buyable()
    collection_order = fetch_collection_order()
    current = buyable_items(list(homepage) if homepage else [], prod_by_handle)
    if not current and collection_order:        # fallback: front of the curated queue
        for h in collection_order:
            if h in prod_by_handle:
                current = buyable_items([h], prod_by_handle)
                break
    featured_handle = next(iter(current), None)
    queued = [h for h in collection_order if h not in current]
    log(f"{len(products)} scanned; buyable now: {list(current) or '(none)'}; {len(queued)} queued")

    if not args.dry_run:
        write_catalog(products, buyable=set(current), queued=set(collection_order))
    if args.catalog_only:
        return 0

    featured = current.get(featured_handle) if featured_handle else None

    state = load_json(args.state, {"on_sale": {}, "featured": None, "last_run": None, "initialized": False})
    previous = state.get("on_sale") or {}
    prev_featured = state.get("featured")
    initialized = bool(state.get("initialized") and state.get("last_run"))

    # Guard: an empty result usually means the site blocked us / had a hiccup.
    if not current and previous:
        log("ERROR: 0 items on sale but previous state had items — likely a fetch problem. "
            "Leaving state untouched and exiting non-zero.")
        return 1

    # First ever run: send a one-time summary, set the baseline, then stop.
    if not initialized:
        notify(args, env, compose(current, watchlist, featured=featured,
                                  baseline=True, queued_count=len(queued)))
        write_state(args, current, featured_handle)
        return 0

    new_handles = [h for h in current if h not in previous]
    gone_handles = [h for h in previous if h not in current]

    urgent, normal = [], []
    for h in new_handles:
        item = current[h]
        (urgent if is_watched(item, watchlist) else normal).append(item)
    gone = [previous[h] for h in gone_handles]
    featured_changed = bool(featured_handle and prev_featured and featured_handle != prev_featured)

    # watched tools that JUST entered the queue (coming up, not buyable yet)
    prev_queued = set(state.get("queued") or [])
    new_queued = [h for h in queued if h not in prev_queued and h not in previous]
    queued_watch = [it for it in buyable_items(new_queued, prod_by_handle).values()
                    if is_watched(it, watchlist)]

    notify_now = (featured_changed or bool(urgent)
                  or (alert_on_new and normal) or (alert_on_sold_out and gone)
                  or (alert_on_queued_watch and queued_watch))
    if not notify_now:
        log(f"no notable changes (new={len(new_handles)}, gone={len(gone_handles)}, "
            f"featured_changed={featured_changed}, queued_watch={len(queued_watch)})")
        write_state(args, current, featured_handle, queued)  # advance baseline silently
        return 0

    msg = compose(current, watchlist,
                  featured=featured, featured_changed=featured_changed,
                  urgent=urgent,
                  normal=normal if alert_on_new else [],
                  gone=gone, alert_on_sold_out=alert_on_sold_out,
                  queued_watch=queued_watch if alert_on_queued_watch else [],
                  queued_count=len(queued))
    notify(args, env, msg)  # raises if all channels fail -> state not advanced
    write_state(args, current, featured_handle, queued)
    return 0


if __name__ == "__main__":
    sys.exit(main())
