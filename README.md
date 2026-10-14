# gmail_cleaner

An interactive CLI for cleaning up a big Gmail account. It groups your mail by who sent it
(or, with `--sent`, who you sent it to) and goes through the groups largest first, asking
what to do with each one: trash, archive, keep (never ask again) or skip.

It knows who your real contacts are from your Sent mail. It flags senders you've
emailed and can hide their mail entirely, so you don't bin a friend along with
the newsletters.

**Safe by design:** it only requests the `gmail.modify` scope and never permanently deletes
anything. Trashed mail stays in Gmail's Trash for 30 days, and archived mail stays in All Mail.

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

python gmail_cleaner.py                      # review your inbox
python gmail_cleaner.py --protect-contacts   # ...hiding mail from people you've emailed
python gmail_cleaner.py --sent               # review your Sent mail by recipient
python gmail_cleaner.py --min 10             # only show groups of 10+ emails
```

On the first run a browser window opens so you can sign in. Google will warn that it
"hasn't verified this app". That's expected for your own test app, so click
**Continue**. The token is saved to `token.json` for later runs. While the app is in
Testing mode, Google expires the token after 7 days. When that happens, the script
opens the browser login again on its own.

## Options

| Flag | What it does |
| --- | --- |
| `--sent` | Review your Sent mail instead of the inbox, grouped by recipient. |
| `--protect-contacts` | Inbox mode only: hide all mail from the exact addresses you've emailed. |
| `--min N` | Only show groups with at least N emails (default 2, the `MIN_GROUP` setting). |

## Inbox mode (default)

1. Lists every message matching `QUERY` (default `in:inbox`) and fetches just the
   From / Subject / List-Unsubscribe headers.
2. Reads your Sent mail too, to work out your contacts (see below). The first run fetches
   both folders, so expect it to take a while on a large account. Later runs only fetch
   new mail.
3. Groups mail by sender domain (e.g. `amazon.com`). For personal providers (gmail.com,
   yahoo.com, outlook.com, icloud.com, …) it groups by full address instead, so every
   Gmail user doesn't end up in one big group.
4. For each group it shows the sender, email count, a few display names, the 3 newest
   subjects, and whether there's an unsubscribe link. Then you choose:

| Key | Action |
| --- | --- |
| `t` | Trash the whole group |
| `a` | Archive (remove from inbox; it stays in All Mail) |
| `k` | Keep. Saved to `decisions.json`, so you're never asked about this sender again |
| `s` | Skip for now (you'll be asked again next run) |
| `u` | Print the unsubscribe link(s), then choose again |
| `q` | Quit and print totals |

### Contacts

A **contact** is any exact address you've sent mail to (To or Cc). Contacts are matched by
full address, not domain, so emailing `support@acme.com` once doesn't make every `acme.com`
newsletter a contact.

In a group, highlighted in green:

- `✉ You've emailed them 4 times (last: 2026-03-02)`: a sender in this group is a contact.
  For a company domain the line names the address, e.g. `✉ You've emailed bob@acme.com …`.
- `✉ You've emailed someone here (support@acme.com +1 more)`: no sender in this group
  is a contact, but you've emailed someone else at this domain. It's just a hint and
  doesn't protect anything.

With `--protect-contacts`, mail from contact addresses is removed before grouping. The
rest of that domain's mail is still shown for review.

## Sent mode (`--sent`)

Reviews `in:sent` (the `SENT_QUERY` setting), grouped by recipient using the same
domain / personal-address rule as inbox mode.

- An email to several people counts under each recipient's group. Trashing a group
  trashes those emails entirely, so each group says how many of its emails also went to
  other people. Emails trashed earlier in the session drop out of later groups.
- Each group shows the recipient, count, names, the 3 newest subjects, and the date you
  last emailed them.
- Actions are `t` trash, `k` keep, `s` skip and `q` quit. There's no archive, because
  sent mail isn't in the inbox. "Keep" decisions go to `sent_decisions.json`.

## Large mailboxes and flaky connections

Built for mailboxes with tens of thousands of emails:

- **Resumable.** Headers are cached as they're fetched and saved every few hundred
  messages. If a run stops for any reason (crash, Ctrl+C, lost connection), rerun it
  and it carries on where it stopped.
- **Rate limited.** Requests go out in batches of 25, capped at about 40 per second.
  That keeps well under Gmail's per-user quota.
- **Retries.** Rate-limit errors (429/403), server errors and network failures
  (timeouts, dropped connections, SSL errors) are retried with growing waits
  (2s, 4s, 8s…). Any connection that stalls for over 60s is abandoned and retried.
- **Clean exit.** If a request still fails after 6 attempts, the script saves its
  progress and exits with "Progress saved, rerun to continue". It doesn't crash with
  a traceback.
- The progress line shows a count, percentage and ETA.

## Settings

Edit the constants at the top of `gmail_cleaner.py`:

| Setting | Default | Meaning |
| --- | --- | --- |
| `QUERY` | `in:inbox` | Which mail inbox mode reviews, e.g. `in:inbox older_than:1y` or `category:promotions` |
| `SENT_QUERY` | `in:sent` | Which mail sent mode reviews and contacts are built from |
| `MIN_GROUP` | `2` | Smallest group shown (overridden by `--min`) |
| `BATCH_SIZE` | `25` | Requests per batch call |
| `MAX_REQUESTS_PER_SEC` | `40` | Throughput cap |
| `MAX_RETRIES` | `6` | Attempts before giving up |
| `HTTP_TIMEOUT` | `60` | Seconds before a stalled request is retried |

## Files

All of these are created next to the script and are in `.gitignore`. Never commit
`credentials.json` or `token.json`.

| File | Contents |
| --- | --- |
| `credentials.json` | Your OAuth client (you download this) |
| `token.json` | Your saved login |
| `emails_cache.json` | Cached inbox headers. Delete it to force a full re-fetch |
| `sent_cache.json` | Cached sent-mail headers |
| `decisions.json` | Inbox senders you chose to keep. Remove an entry to be asked again |
| `sent_decisions.json` | Recipients you chose to keep in sent mode |

# gmail-cleaner
