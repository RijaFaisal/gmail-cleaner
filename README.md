# gmail_cleaner

An interactive CLI that groups your inbox by sender and asks what to do with each group:
trash, archive, keep (never ask again), or skip.

It only uses the `gmail.modify` scope and never permanently deletes anything. Trashed mail
stays in Gmail's Trash for 30 days, and archived mail stays in All Mail.

## Google Cloud setup (one time)

1. Go to <https://console.cloud.google.com/> and create a new project (or pick an existing one).
2. **Enable the Gmail API:** APIs & Services → Library → search "Gmail API" → **Enable**.
3. **Configure the OAuth consent screen** (APIs & Services → OAuth consent screen, which newer
   consoles call "Google Auth Platform"):
   - User type: **External**. Fill in an app name and your email.
   - Under **Test users** (the "Audience" page in newer consoles), add your own Gmail address.
   - Leave the app in **Testing** mode. It doesn't need to be published.
4. **Create the OAuth client:** APIs & Services → Credentials (or Google Auth Platform → Clients)
   → **Create credentials → OAuth client ID** → Application type **Desktop app** → Create.
5. **Download the JSON**, rename it to `credentials.json`, and put it in this folder.

## Install & run

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python gmail_cleaner.py
```

On the first run a browser window opens so you can sign in. Google will warn that it
"hasn't verified this app". That's expected for your own test app, so click
**Continue**. The token is saved to `token.json` for later runs. While the app is in
Testing mode, Google expires the token after 7 days. When that happens, the script
opens the browser login again on its own.

## How it works

- Lists every message matching `QUERY` (default `in:inbox`), then fetches only the
  From / Subject / List-Unsubscribe headers in batches. Results are cached in
  `emails_cache.json`, so if a run is interrupted, the next one resumes where it stopped.
  Later runs only fetch new mail.
- Groups mail by sender domain. For personal providers (gmail.com, yahoo.com, outlook.com,
  icloud.com, …) it groups by full address. Groups are reviewed largest first.
- Also reads your Sent mail (cached in `sent_cache.json`) and flags, in green, any sender
  address you've emailed ("✉ You've emailed them N times"). A company-domain group with no
  such sender, where you've still emailed someone at that domain, shows
  "✉ You've emailed someone here (address)". That line is informational only.
- For each group, choose:
  - `t` trash
  - `a` archive (remove from inbox)
  - `k` keep. The sender is saved to `decisions.json` and you won't be asked again.
  - `s` skip (you'll be asked again next run)
  - `u` print the unsubscribe link(s)
  - `q` quit

## Options

| Flag | What it does |
| --- | --- |
| `--sent` | Review your Sent mail instead, grouped by recipient (To + Cc). An email to several people counts under each of them. Actions are trash / keep / skip / quit, and "keep" goes to `sent_decisions.json`. |
| `--protect-contacts` | Inbox mode: hide all mail from the exact addresses you've emailed. Other senders at the same domain are still reviewed, so emailing `support@acme.com` protects only that address, not `acme.com` newsletters. |
| `--min N` | Only show groups with at least N emails (overrides `MIN_GROUP`). |

Edit the settings at the top of `gmail_cleaner.py` to change `QUERY` (e.g.
`"in:inbox older_than:1y"` or `"category:promotions"`), `MIN_GROUP`, or batch sizes.
To un-keep a sender, remove it from `decisions.json` (or `sent_decisions.json`).
# gmail-cleaner
