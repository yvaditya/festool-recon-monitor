# Festool Recon Monitor

Watches [festoolrecon.com](https://www.festoolrecon.com) and **pushes a notification to your phone the moment something new comes on sale** — with a 🔥 priority alert when a tool on your personal watchlist shows up.

- Runs entirely on **GitHub Actions** (free, in the cloud) every ~5 minutes. No always-on computer required.
- Notifications via **[ntfy](https://ntfy.sh)** — open-source, **no account, no password, no token**.
- A **web page** (GitHub Pages) lets you pick what to watch with checkboxes — no editing JSON by hand.

---

## How it works

Festool Recon is a Shopify store; its catalog is exposed as JSON. A product that is `available: true` (and isn't the *"Wow, that went fast!!!"* placeholder) is **on sale right now**; when it sells it flips to `available: false` (the "greyed out / done" state on the site).

Every run, `festool_monitor.py`:
1. Fetches all products, builds the current **on-sale set**, and writes `docs/catalog.json` (so the picker UI has data).
2. Diffs the on-sale set against `state.json` (committed in this repo).
3. Sends a notification: **new item** → normal alert; **new item matching your watchlist** → 🔥 urgent; **sold out** → optional.
4. Saves the new state back so the next run remembers.

State only advances **after** a successful send, so a transient failure retries instead of dropping an alert.

---

## Setup (one time, ~3 minutes)

### 1. Get notifications on your phone with ntfy (no credentials)

1. Install the **ntfy** app: [iOS](https://apps.apple.com/app/ntfy/id1625396347) · [Android](https://play.google.com/store/apps/details?id=io.heckel.ntfy).
2. Pick a **secret topic name** — make it long and random so nobody else can guess it, e.g. `festool-recon-vy-9f3k2x7q`.
3. In the app: **+ → Subscribe to topic →** enter that exact name.
4. Add it as a repo secret (you only do this once):
   ```bash
   gh secret set NTFY_TOPIC --body "festool-recon-vy-9f3k2x7q"
   ```
   > The topic is the only thing you need. It is **not** in the public repo (secrets are encrypted), so your alerts stay private. Want a real account/auth or a self-hosted server? Set `NTFY_SERVER` / `NTFY_TOKEN` too — both optional.

**Prefer email or a real SMS text instead?** It also supports SMTP — set `SMTP_USER`, `SMTP_PASS`, and `MAIL_TO` (an email address, or a carrier SMS gateway like `5551234567@vtext.com`). You can use ntfy **and** email together. See [`config`/secrets table](#secrets) below.

### 2. Pick what to watch (the web UI)

Once GitHub Pages is enabled (see below), open:

```
https://<your-username>.github.io/festool-recon-monitor/
```

- Tick the tools you want 🔥 priority alerts for. Each keyword shows how many catalog items it matches, so you can tell if it's too broad/narrow.
- Toggle "alert for any new item" and "alert on sell-out".
- Click **Copy config.json**, then **Open config.json on GitHub**, paste, and **Commit**. Done — the next run uses it. (You're already logged into GitHub in your browser, so there's no token to manage.)

You can also just edit [`config.json`](./config.json) directly:
```json
{ "watchlist": ["domino", "kapex", "ts 60", "ct 36"], "alert_on_new": true, "alert_on_sold_out": false }
```
`watchlist` = case-insensitive substrings matched against product titles.

### 3. Turn it on

1. **Enable Pages:** repo **Settings → Pages → Source: Deploy from a branch → `main` / `/docs`** (or run the `gh api` command in the deploy notes).
2. **Actions tab →** enable workflows if prompted → **Festool Recon Monitor → Run workflow** to fire immediately. The first run sends a one-time "monitor is live" summary, then only alerts on changes.
3. After that it runs automatically every ~5 minutes (GitHub's minimum; often throttled to 5–15 min in practice).

---

## Secrets

In **Settings → Secrets and variables → Actions** (or via `gh secret set <NAME>`):

| Secret        | For    | Required | Notes |
|---------------|--------|----------|-------|
| `NTFY_TOPIC`  | ntfy   | ✅ (for ntfy) | your secret topic name |
| `NTFY_SERVER` | ntfy   | optional | default `https://ntfy.sh` |
| `NTFY_TOKEN`  | ntfy   | optional | only for protected/self-hosted topics |
| `SMTP_USER`   | email  | ✅ (for email) | sending address |
| `SMTP_PASS`   | email  | ✅ (for email) | app password |
| `MAIL_TO`     | email  | ✅ (for email) | recipient (email or carrier SMS gateway) |
| `SMTP_HOST`/`SMTP_PORT`/`MAIL_FROM` | email | optional | default Gmail `smtp.gmail.com:587` |

At least one channel (ntfy **or** email) must be configured, or the run fails on purpose.

---

## Testing locally (optional)

```bash
python3 festool_monitor.py --dry-run        # scrape + print; no send, no writes
python3 festool_monitor.py --catalog-only   # just rebuild docs/catalog.json
NTFY_TOPIC="your-topic" python3 festool_monitor.py --test   # send a test push
```

---

## Tuning & caveats

- **Frequency** — edit the `cron` in `.github/workflows/monitor.yml`. `*/5 * * * *` is GitHub's hard minimum; schedules are best-effort so real spacing is often 5–15 min.
- **Less noise** — set `alert_on_new` to `false` to get only watchlist 🔥 alerts. **More signal** — set `alert_on_sold_out` to `true`.
- **Cost** — public repo ⇒ Actions minutes are free and unlimited. No secrets live in the code.
- GitHub **disables scheduled workflows after 60 days of repo inactivity**; the bot's own commits keep it active, but re-enable in the Actions tab if it ever pauses.
- True **iMessage** can't be sent from a cloud runner (needs a logged-in Mac). ntfy/SMS-gateway are the closest phone alerts.

---

## Files

| File | Purpose |
|------|---------|
| `festool_monitor.py` | The monitor (pure Python stdlib, no dependencies). |
| `config.json` | Your watchlist + alert toggles. |
| `state.json` | Last-seen on-sale set (auto-updated by the bot). |
| `docs/index.html` | The watchlist picker UI (served via GitHub Pages). |
| `docs/catalog.json` | Product catalog cache for the UI (auto-updated by the bot). |
| `.github/workflows/monitor.yml` | The scheduled GitHub Action. |
