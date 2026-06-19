# Festool Recon Monitor

Watches [festoolrecon.com](https://www.festoolrecon.com) and **emails / texts your phone the moment something new comes on sale** — with a 🔥 priority alert when a tool on your personal watchlist shows up.

Runs entirely on **GitHub Actions** (free, in the cloud) on a schedule. No always-on computer required.

---

## How it works

Festool Recon is a Shopify store. The "One and Done" recon queue is exposed as JSON:

- `https://www.festoolrecon.com/products.json` — every product, with an `available` flag.
- An item that is **`available: true`** (and isn't the *"Wow, that went fast!!!"* placeholder) is **on sale right now**. When it sells, it flips to `available: false` — that's the "greyed out / done" state you see on the site.

Every run, `festool_monitor.py`:

1. Fetches all products and builds the current **on-sale set**.
2. Loads the previous on-sale set from `state.json` (committed in this repo).
3. Diffs them:
   - **New item on sale** → normal email alert.
   - **New item matching your watchlist** → 🔥 urgent email alert (louder subject line).
   - **Item sold out** → optional alert (off by default).
4. Emails you a single summary of the changes.
5. Saves the new `state.json` back to the repo so the next run remembers.

State is only advanced **after** a successful send, so a transient email failure just retries next run instead of silently dropping an alert.

---

## One-time setup

### 1. Pick how email gets sent (the "From" account)

The script speaks plain SMTP. Easiest options:

**Gmail (recommended)**
1. Turn on 2-Step Verification: <https://myaccount.google.com/security>
2. Create an App Password: <https://myaccount.google.com/apppasswords> (pick "Mail"). You get a 16-character password.
3. Use:
   - `SMTP_HOST` = `smtp.gmail.com` (this is the default, so you can skip it)
   - `SMTP_PORT` = `587` (default, skippable)
   - `SMTP_USER` = your full Gmail address
   - `SMTP_PASS` = the 16-char App Password (**not** your normal password)

**iCloud (you already have an Apple ID)**
1. Create an app-specific password: <https://appleid.apple.com> → Sign-In and Security → App-Specific Passwords.
2. Use `SMTP_HOST` = `smtp.mail.me.com`, `SMTP_PORT` = `587`, `SMTP_USER` = your iCloud email, `SMTP_PASS` = the app-specific password.

### 2. Pick where the alert lands (the "To" — your phone)

Set `MAIL_TO` to **either**:

- **Your email** (e.g. `vyerra@icloud.com`) — your phone's Mail app shows a push notification. Simple and reliable.
- **A carrier SMS gateway** — turns the email into a real **text message** on your phone. Format: `<10-digit-number>@gateway`:

  | Carrier      | Gateway address              |
  |--------------|------------------------------|
  | AT&T         | `5551234567@txt.att.net`     |
  | Verizon      | `5551234567@vtext.com`       |
  | T-Mobile     | `5551234567@tmomail.net`     |
  | Google Fi    | `5551234567@msg.fi.google.com` |
  | US Cellular  | `5551234567@email.uscc.net`  |
  | Cricket      | `5551234567@sms.cricketwireless.net` |

  > Carrier gateways are free but some carriers are phasing them out and may truncate long messages. If texts don't arrive, use your email address instead. You can put **both** in `MAIL_TO`, comma-separated.

### 3. Add the secrets to GitHub

In your repo: **Settings → Secrets and variables → Actions → New repository secret**. Add:

| Secret      | Required | Example                          |
|-------------|----------|----------------------------------|
| `SMTP_USER` | ✅       | `youraddress@gmail.com`          |
| `SMTP_PASS` | ✅       | `abcd efgh ijkl mnop` (app pwd)  |
| `MAIL_TO`   | ✅       | `vyerra@icloud.com`              |
| `SMTP_HOST` | optional | defaults to `smtp.gmail.com`     |
| `SMTP_PORT` | optional | defaults to `587`                |
| `MAIL_FROM` | optional | defaults to `SMTP_USER`          |

### 4. Set your watchlist

Edit [`config.json`](./config.json) and commit it:

```json
{
  "watchlist": ["domino", "kapex", "ts 60", "ct 36"],
  "alert_on_new": true,
  "alert_on_sold_out": false
}
```

- `watchlist` — case-insensitive keywords matched against product titles. A match → 🔥 urgent. Replace the examples with the tools you actually want. Leave `[]` to just get plain "new item" alerts.
- `alert_on_new` — email for every new item on sale (not just watchlist). `true` per your choice.
- `alert_on_sold_out` — also alert when items sell out. `false` = less noise.

### 5. Turn it on

The workflow is in [`.github/workflows/monitor.yml`](./.github/workflows/monitor.yml). After pushing:

1. Go to the **Actions** tab → enable workflows if prompted.
2. Click **Festool Recon Monitor → Run workflow** to fire it immediately. The first run sends a one-time "monitor is live" summary of everything currently on sale, then only alerts on changes afterward.
3. After that it runs automatically every ~5 minutes (GitHub's minimum; often throttled to 5–15 min in practice).

---

## Testing it locally (optional)

No secrets needed for a dry run:

```bash
python3 festool_monitor.py --dry-run     # scrape + print what it WOULD send; no email, no state change
```

To verify your email actually works end-to-end:

```bash
export SMTP_USER="youraddress@gmail.com"
export SMTP_PASS="your-app-password"
export MAIL_TO="vyerra@icloud.com"
python3 festool_monitor.py --test-email   # sends one test message and exits
```

---

## Tuning

- **Check more / less often** — edit the `cron` line in `.github/workflows/monitor.yml`. Currently `*/5 * * * *` = every 5 min, which is **GitHub's hard minimum** — you can't poll faster on Actions. Schedules are best-effort, so real spacing is often 5–15 min under load. To check less often, use e.g. `*/15 * * * *`.
- **Less noise** — set `alert_on_new` to `false` to only get watchlist 🔥 alerts.
- **More signal** — set `alert_on_sold_out` to `true`.

---

## Cost & caveats

- **Public repo** → GitHub Actions minutes are **free and unlimited**. No secrets live in the code (they're encrypted in GitHub Secrets), so a public repo is safe here.
- **Private repo** → only ~2000 free Action-minutes/month. At a 10-min cadence you'd exceed that; use a `*/30` cron (every 30 min) to stay free, or accept usage charges.
- GitHub **disables scheduled workflows after 60 days of repo inactivity** — the bot's own state commits keep it active, but if it ever pauses, just re-enable it in the Actions tab.
- Scheduled runs are **best-effort**; expect occasional 5–20 min delays at the top of the hour.
- True **iMessage** can only be sent from a logged-in Mac, so it isn't possible from a cloud runner. The carrier-SMS-gateway option above is the closest thing to a real text.

---

## Files

| File | Purpose |
|------|---------|
| `festool_monitor.py` | The monitor (pure Python stdlib, no dependencies). |
| `config.json` | Your watchlist + alert toggles. |
| `state.json` | Last-seen on-sale set (auto-updated by the bot). |
| `.github/workflows/monitor.yml` | The scheduled GitHub Action. |
