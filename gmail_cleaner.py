#!/usr/bin/env python3
"""
gmail_cleaner.py - interactively clean up Gmail, one sender (or recipient) group at a time.

Inbox mode (default): fetches lightweight metadata (From / Subject / List-Unsubscribe)
for every message matching QUERY, groups messages by sender, then walks you through
the groups largest-first, asking whether to trash, archive, keep or skip each one.
Senders you've emailed before are flagged, using your Sent mail.

Sent mode (--sent): same idea for your Sent mail, grouped by recipient.

Safety: the only OAuth scope requested is gmail.modify, and the script never calls
any delete endpoint. "Trash" moves mail to Gmail's Trash (recoverable for 30 days),
and "archive" just removes the INBOX label.

Usage:
    python gmail_cleaner.py                       # review inbox
    python gmail_cleaner.py --protect-contacts    # ...hiding senders you've emailed
    python gmail_cleaner.py --sent                # review sent mail by recipient
    python gmail_cleaner.py --min 5               # only show groups of 5+ emails
"""

import argparse
import http.client
import json
import os
import socket
import ssl
import sys
import time
from collections import defaultdict
from datetime import datetime
from email.utils import getaddresses, parseaddr, parsedate_to_datetime

import httplib2
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_httplib2 import AuthorizedHttp
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# ---------------------------------------------------------------------------
# Settings - tweak these
# ---------------------------------------------------------------------------
QUERY = "in:inbox"      # Gmail search query that selects which mail to review
SENT_QUERY = "in:sent"  # query used for --sent mode and for detecting contacts
MIN_GROUP = 2           # groups with fewer emails than this are not shown (--min overrides)
BATCH_SIZE = 25      # requests per batch HTTP call (Gmail allows 100; small batches avoid rate limits)
# Gmail allows ~250 quota units/sec per user, and messages.get / messages.trash
# cost 5 units each, so 50 req/s is the hard ceiling. Stay safely under it.
MAX_REQUESTS_PER_SEC = 40
BATCH_SLEEP = 0.25   # minimum pause between batch calls, even if we're under the cap
SAVE_EVERY = 10      # write the metadata cache to disk every N batches
MAX_RETRIES = 6      # attempts per request before giving up (backoff: 2s, 4s, 8s, ...)
HTTP_TIMEOUT = 60    # seconds before a stalled connection is abandoned and retried
MODIFY_CHUNK = 1000  # max IDs per messages.batchModify call (API limit)

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]

# All state files live next to this script, regardless of where it's run from.
HERE = os.path.dirname(os.path.abspath(__file__))
CREDENTIALS_FILE = os.path.join(HERE, "credentials.json")
TOKEN_FILE = os.path.join(HERE, "token.json")
CACHE_FILE = os.path.join(HERE, "emails_cache.json")
DECISIONS_FILE = os.path.join(HERE, "decisions.json")
SENT_CACHE_FILE = os.path.join(HERE, "sent_cache.json")
SENT_DECISIONS_FILE = os.path.join(HERE, "sent_decisions.json")

# Headers fetched per message in each mode.
INBOX_HEADERS = ["From", "Subject", "List-Unsubscribe"]
SENT_HEADERS = ["To", "Cc", "Subject", "Date"]

# For these providers the domain says nothing about who the sender is, so we
# group by the full address instead of lumping every Gmail user together.
PERSONAL_DOMAINS = {
    "gmail.com", "googlemail.com",
    "yahoo.com", "yahoo.co.uk", "yahoo.ca", "ymail.com", "rocketmail.com",
    "hotmail.com", "hotmail.co.uk", "outlook.com", "live.com", "msn.com",
    "icloud.com", "me.com", "mac.com",
    "aol.com", "protonmail.com", "proton.me", "pm.me",
    "gmx.com", "gmx.net", "mail.com", "zoho.com", "yandex.com",
}

# HTTP statuses worth retrying. Gmail reports some rate limits as 403
# (userRateLimitExceeded / rateLimitExceeded); 5xx are transient server errors.
RETRYABLE = {429, 403, 500, 502, 503}

# Errors from a flaky connection (timeouts, resets, DNS, TLS). Most are OSError
# subclasses; they're listed explicitly for clarity. These are always retried.
NETWORK_ERRORS = (
    TimeoutError, ConnectionError, socket.timeout, ssl.SSLError,
    httplib2.HttpLib2Error, http.client.HTTPException, OSError,
)


class GaveUp(Exception):
    """A request still failed after MAX_RETRIES attempts."""


# Bold green for the "you've emailed them" line, unless output isn't a terminal.
USE_COLOR = sys.stdout.isatty() and "NO_COLOR" not in os.environ


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def load_json(path, default):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path, data):
    # Write to a temp file and rename it over the original, so a crash
    # mid-write can never leave a half-written (corrupt) file behind.
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def chunks(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def status_of(error):
    return error.resp.status if isinstance(error, HttpError) else None


def fmt_duration(seconds):
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m" if hours else f"{minutes}m {secs:02d}s"


def fmt_date(ms):
    return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d") if ms else "unknown"


def highlight(text):
    return f"\033[1;32m{text}\033[0m" if USE_COLOR else text


def throttle(batch_started, n_requests):
    """Pause after a batch so we average no more than MAX_REQUESTS_PER_SEC."""
    min_duration = n_requests / MAX_REQUESTS_PER_SEC
    elapsed = time.monotonic() - batch_started
    time.sleep(max(BATCH_SLEEP, min_duration - elapsed))


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
def get_service():
    """Log in (browser on first run, token.json afterwards) and return a Gmail client."""
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except RefreshError:
            # Token was revoked or expired (tokens for apps in "Testing" mode
            # expire after 7 days) - fall through to a fresh browser login.
            creds = None

    if not creds or not creds.valid:
        if not os.path.exists(CREDENTIALS_FILE):
            sys.exit(f"Missing {CREDENTIALS_FILE} - see README.md for setup steps.")
        flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
        creds = flow.run_local_server(port=0)

    with open(TOKEN_FILE, "w", encoding="utf-8") as f:
        f.write(creds.to_json())

    # An explicit timeout makes a stalled connection fail (and get retried)
    # instead of hanging. AuthorizedHttp also refreshes the token mid-run.
    http = AuthorizedHttp(creds, http=httplib2.Http(timeout=HTTP_TIMEOUT))
    return build("gmail", "v1", http=http, cache_discovery=False)


# ---------------------------------------------------------------------------
# API calls with retry
# ---------------------------------------------------------------------------
def backoff(attempt, reason):
    """Sleep before retry number `attempt`, or raise GaveUp if that was the last try."""
    if attempt == MAX_RETRIES - 1:
        raise GaveUp(f"Gave up after {MAX_RETRIES} attempts ({reason})")
    wait = 2 ** (attempt + 1)
    print(f"\n  {reason}, retrying in {wait}s...")
    time.sleep(wait)


def execute_with_retry(request):
    """Execute a single API request, backing off on rate limits and network errors."""
    for attempt in range(MAX_RETRIES):
        try:
            return request.execute()
        except HttpError as e:
            if status_of(e) not in RETRYABLE:
                raise
            backoff(attempt, f"HTTP {status_of(e)}")
        except NETWORK_ERRORS as e:
            backoff(attempt, f"network hiccup ({type(e).__name__})")


def execute_batch(service, ids, make_request, on_success):
    """
    Send one request per message ID in a single batch HTTP call.

    make_request(id) builds the API request for a message; on_success(id, response)
    is called for each one that succeeds. Rate-limited requests and network failures
    are retried with exponential backoff; 404s (message gone since we listed it) are
    skipped. Raises GaveUp if requests are still failing after MAX_RETRIES attempts.
    """
    pending = list(ids)
    for attempt in range(MAX_RETRIES):
        done = set()
        rate_limited = 0

        def callback(request_id, response, exception):
            nonlocal rate_limited
            if exception is None:
                done.add(request_id)
                on_success(request_id, response)
            elif status_of(exception) == 404:
                done.add(request_id)  # deleted/moved since listing - nothing to do
            elif status_of(exception) in RETRYABLE:
                rate_limited += 1     # left out of `done`, so it gets retried
            else:
                done.add(request_id)
                print(f"\n  skipping {request_id}: {exception}")

        batch = service.new_batch_http_request(callback=callback)
        for mid in pending:
            batch.add(make_request(mid), request_id=mid)
        try:
            batch.execute()
            reason = f"rate limited on {rate_limited} requests"
        except HttpError as e:
            # The whole batch call was rejected.
            if status_of(e) not in RETRYABLE:
                raise
            reason = f"HTTP {status_of(e)}"
        except NETWORK_ERRORS as e:
            reason = f"network hiccup ({type(e).__name__})"

        pending = [mid for mid in pending if mid not in done]
        if not pending:
            return
        backoff(attempt, reason)


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------
def list_message_ids(service, query):
    """Return the IDs of all messages matching query (500 per page)."""
    ids = []
    page_token = None
    while True:
        resp = execute_with_retry(service.users().messages().list(
            userId="me", q=query, maxResults=500, pageToken=page_token))
        ids.extend(m["id"] for m in resp.get("messages", []))
        print(f"\rListing {query} messages... {len(ids)}", end="", flush=True)
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    print()
    return list(dict.fromkeys(ids))  # de-duplicate, keep order


def inbox_record(headers, internal_date):
    """What we cache for each inbox message."""
    return {
        "from": headers.get("from", ""),
        "subject": headers.get("subject", "") or "(no subject)",
        "unsub": headers.get("list-unsubscribe", ""),
        "date": internal_date,  # ms since epoch, for "recent"
    }


def sent_record(headers, internal_date):
    """What we cache for each sent message."""
    date = internal_date
    if not date and headers.get("date"):
        # internalDate is Gmail's own timestamp and nearly always present;
        # the Date header is only a fallback (it can be malformed).
        try:
            date = int(parsedate_to_datetime(headers["date"]).timestamp() * 1000)
        except (TypeError, ValueError):
            date = 0
    return {
        "to": headers.get("to", ""),
        "cc": headers.get("cc", ""),
        "subject": headers.get("subject", "") or "(no subject)",
        "date": date,
    }


def fetch_metadata(service, query, cache_file, header_names, to_record):
    """
    Return {id: metadata} for every message matching query, fetching only what
    isn't in cache_file yet. to_record(headers, internal_date) builds each entry.

    The cache is saved every SAVE_EVERY batches and on exit (including Ctrl+C,
    GaveUp or a crash), so a rerun picks up where the last one stopped.
    """
    ids = list_message_ids(service, query)
    cache = load_json(cache_file, {})
    todo = [mid for mid in ids if mid not in cache]
    print(f"{len(ids)} messages: {len(ids) - len(todo)} cached, {len(todo)} to fetch")

    def make_request(mid):
        return service.users().messages().get(
            userId="me", id=mid, format="metadata",
            metadataHeaders=header_names,
            fields="id,internalDate,payload/headers",
        )

    def store(mid, msg):
        headers = {h["name"].lower(): h["value"]
                   for h in msg.get("payload", {}).get("headers", [])}
        cache[mid] = to_record(headers, int(msg.get("internalDate", 0)))

    started = time.monotonic()
    try:
        for n, batch_ids in enumerate(chunks(todo, BATCH_SIZE), 1):
            batch_started = time.monotonic()
            execute_batch(service, batch_ids, make_request, store)

            fetched = min(n * BATCH_SIZE, len(todo))
            elapsed = time.monotonic() - started
            eta = (len(todo) - fetched) * elapsed / fetched
            print(f"\rFetching metadata... {fetched}/{len(todo)} "
                  f"({fetched / len(todo):.0%})  ETA {fmt_duration(eta)}    ",
                  end="", flush=True)

            if n % SAVE_EVERY == 0:
                save_json(cache_file, cache)
            throttle(batch_started, len(batch_ids))
    finally:
        save_json(cache_file, cache)
    if todo:
        print()

    return {mid: cache[mid] for mid in ids if mid in cache}


# ---------------------------------------------------------------------------
# Grouping
# ---------------------------------------------------------------------------
def address_key(addr):
    """Domain for companies (e.g. 'amazon.com'), full address for personal mail."""
    addr = addr.lower()
    if "@" not in addr:
        return addr or "(unknown)"
    domain = addr.rsplit("@", 1)[1]
    return addr if domain in PERSONAL_DOMAINS else domain


def sender_keys(meta):
    """Inbox: [(key, display name)] for the single sender."""
    name, addr = parseaddr(meta["from"])
    return [(address_key(addr), name or addr)]


def recipient_keys(meta):
    """Sent: [(key, display name)] for each distinct recipient key in To + Cc."""
    keys = {}
    for name, addr in getaddresses([meta["to"], meta["cc"]]):
        if "@" in addr:
            keys.setdefault(address_key(addr), name or addr)
    return list(keys.items())


def group_emails(metadata, keys_of, skip, min_group):
    """
    Return [(key, [(id, meta, name), ...]), ...] largest first.

    keys_of(meta) gives the group keys for a message; a sent email with several
    recipients lands in several groups. Groups in `skip` or smaller than
    min_group are left out.
    """
    groups = defaultdict(list)
    for mid, meta in metadata.items():
        for key, name in keys_of(meta):
            groups[key].append((mid, meta, name))
    result = [(key, items) for key, items in groups.items()
              if key not in skip and len(items) >= min_group]
    result.sort(key=lambda g: len(g[1]), reverse=True)
    return result


def sender_address(meta):
    return parseaddr(meta["from"])[1].lower()


def build_contacts(sent_metadata):
    """
    {exact address: {"count": emails sent, "last": ms of latest}} from sent mail.

    Keyed by full address even for company domains, so emailing support@acme.com
    doesn't make every acme.com newsletter count as a contact.
    """
    contacts = {}
    for meta in sent_metadata.values():
        addrs = {addr.lower() for _, addr in getaddresses([meta["to"], meta["cc"]])
                 if "@" in addr}
        for addr in addrs:
            c = contacts.setdefault(addr, {"count": 0, "last": 0})
            c["count"] += 1
            c["last"] = max(c["last"], meta["date"])
    return contacts


def contact_lines(key, items, contacts):
    """
    The ✉ lines for an inbox group. Exact matches first: senders in this group
    you've emailed. Failing that, for a company-domain group, anyone you've
    emailed at that domain (informational only - it doesn't protect the group).
    """
    def most_emailed_first(addrs):
        return sorted(addrs, key=lambda a: contacts[a]["count"], reverse=True)

    senders = {sender_address(meta) for _, meta, _ in items}
    matched = most_emailed_first(senders & contacts.keys())
    lines = []
    for addr in matched[:3]:
        c = contacts[addr]
        who = "them" if addr == key else addr  # personal groups are keyed by address
        lines.append(f"✉ You've emailed {who} {c['count']} time{'s' if c['count'] != 1 else ''} "
                     f"(last: {fmt_date(c['last'])})")
    if len(matched) > 3:
        lines.append(f"✉ ...and {len(matched) - 3} more senders here you've emailed")

    if not matched and "@" not in key:
        here = most_emailed_first(a for a in contacts if a.rsplit("@", 1)[1] == key)
        if here:
            more = f" +{len(here) - 1} more" if len(here) > 1 else ""
            lines.append(f"✉ You've emailed someone here ({here[0]}{more})")
    return lines


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------
def trash_messages(service, ids):
    """Move messages to Trash (recoverable). Returns how many were trashed."""
    trashed = 0
    for chunk in chunks(ids, MODIFY_CHUNK):
        try:
            execute_with_retry(service.users().messages().batchModify(
                userId="me", body={"ids": chunk, "addLabelIds": ["TRASH"]}))
            trashed += len(chunk)
        except HttpError as e:
            print(f"  batchModify couldn't add TRASH (HTTP {status_of(e)}), "
                  f"falling back to messages.trash...")
            trashed += trash_one_by_one(service, chunk)
    return trashed


def trash_one_by_one(service, ids):
    """Fallback: call messages.trash for each ID, sent in batch HTTP requests."""
    trashed = 0

    def ok(mid, response):
        nonlocal trashed
        trashed += 1

    for batch_ids in chunks(ids, BATCH_SIZE):
        batch_started = time.monotonic()
        execute_batch(service, batch_ids,
                      lambda mid: service.users().messages().trash(userId="me", id=mid),
                      ok)
        print(f"\r  trashed {trashed}/{len(ids)}", end="", flush=True)
        throttle(batch_started, len(batch_ids))
    print()
    return trashed


def archive_messages(service, ids):
    """Remove the INBOX label (the mail stays in All Mail). Returns how many were archived."""
    archived = 0
    for chunk in chunks(ids, MODIFY_CHUNK):
        try:
            execute_with_retry(service.users().messages().batchModify(
                userId="me", body={"ids": chunk, "removeLabelIds": ["INBOX"]}))
            archived += len(chunk)
        except HttpError as e:
            print(f"  archive failed for {len(chunk)} messages: {e}")
    return archived


# ---------------------------------------------------------------------------
# Interactive review
# ---------------------------------------------------------------------------
def unsubscribe_links(items_newest_first):
    """Links from the newest List-Unsubscribe header (older links may have expired)."""
    for _, meta, _ in items_newest_first:
        if meta["unsub"]:
            # Header looks like: <mailto:x@y.com?subject=unsub>, <https://y.com/u/123>
            return [part.strip().strip("<>") for part in meta["unsub"].split(",") if part.strip()]
    return []


def show_group(index, total, key, items, sent_mode, contacts):
    newest = sorted(items, key=lambda x: x[1]["date"], reverse=True)

    names = []
    for _, _, name in newest:
        if name and name not in names:
            names.append(name)
        if len(names) == 3:
            break

    print("\n" + "=" * 70)
    print(f"[{index}/{total}]  {key}  -  {len(items)} emails")
    print(f"  {'To:  ' if sent_mode else 'From:'}   {' | '.join(names)}")
    print("  Recent subjects:")
    for _, meta, _ in newest[:3]:
        print(f"    - {meta['subject'][:90]}")

    if sent_mode:
        print(f"  Last sent: {fmt_date(newest[0][1]['date'])}")
        # Trashing a sent email removes it for every recipient group, so say so.
        shared = sum(1 for _, meta, _ in items if len(recipient_keys(meta)) > 1)
        if shared:
            print(f"  ({shared} of these also went to other recipients)")
    else:
        print(f"  Unsubscribe link: {'yes' if unsubscribe_links(newest) else 'no'}")
        for line in contact_lines(key, items, contacts):
            print("  " + highlight(line))
    return newest


def review(service, groups, sent_mode, contacts, decisions, decisions_file):
    totals = {"trashed": 0, "archived": 0}
    if sent_mode:
        prompt = "[t]rash  [k]eep  [s]kip  [q]uit > "
        valid = "t, k, s or q"
    else:
        prompt = "[t]rash  [a]rchive  [k]eep  [s]kip  [u]nsubscribe link  [q]uit > "
        valid = "t, a, k, s, u or q"
    # In sent mode one email can sit in several groups; don't trash/count it twice.
    trashed_ids = set()

    for index, (key, items) in enumerate(groups, 1):
        items = [item for item in items if item[0] not in trashed_ids]
        if not items:
            continue
        newest = show_group(index, len(groups), key, items, sent_mode, contacts)
        ids = [mid for mid, _, _ in items]

        while True:
            try:
                choice = input(prompt).strip().lower()
            except (EOFError, KeyboardInterrupt):
                choice = "q"

            if choice == "t" or (choice == "a" and not sent_mode):
                try:
                    if choice == "t":
                        n = trash_messages(service, ids)
                        trashed_ids.update(ids)
                        totals["trashed"] += n
                        print(f"  Trashed {n} emails.")
                    else:
                        n = archive_messages(service, ids)
                        totals["archived"] += n
                        print(f"  Archived {n} emails.")
                except GaveUp as e:
                    # Some of this group may already be moved; a rerun re-lists
                    # the mail, so whatever is left will show up again.
                    print(f"\n  {e}. Stopping here - rerun to continue.")
                    return totals
                break
            elif choice == "k":
                decisions["keep"].append(key)
                save_json(decisions_file, decisions)
                print(f"  Keeping {key} - won't ask again "
                      f"(edit {os.path.basename(decisions_file)} to undo).")
                break
            elif choice == "s":
                break
            elif choice == "u" and not sent_mode:
                links = unsubscribe_links(newest)
                for link in links:
                    print(f"    {link}")
                if not links:
                    print("    No List-Unsubscribe header found.")
                # stay on this group so you can still choose what to do with it
            elif choice == "q":
                return totals
            else:
                print(f"  Please type {valid}.")

    print("\nNo more groups to review.")
    return totals


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def parse_args():
    parser = argparse.ArgumentParser(description="Interactively clean up Gmail by sender group.")
    parser.add_argument("--sent", action="store_true",
                        help="review Sent mail grouped by recipient, instead of the inbox")
    parser.add_argument("--protect-contacts", action="store_true",
                        help="inbox mode: hide mail from exact addresses you've emailed")
    parser.add_argument("--min", type=int, default=MIN_GROUP, metavar="N",
                        help=f"only show groups with at least N emails (default {MIN_GROUP})")
    args = parser.parse_args()
    if args.sent and args.protect_contacts:
        parser.error("--protect-contacts only applies to inbox mode")
    if args.min < 1:
        parser.error("--min must be at least 1")
    return args


def main():
    args = parse_args()
    service = get_service()

    try:
        if args.sent:
            metadata = fetch_metadata(service, SENT_QUERY, SENT_CACHE_FILE,
                                      SENT_HEADERS, sent_record)
            contacts = {}
        else:
            metadata = fetch_metadata(service, QUERY, CACHE_FILE,
                                      INBOX_HEADERS, inbox_record)
            # Sent mail tells us who your real contacts are. Cached in
            # sent_cache.json, so after the first run only new sent mail is fetched.
            print("\nChecking your Sent mail for contacts...")
            contacts = build_contacts(fetch_metadata(service, SENT_QUERY, SENT_CACHE_FILE,
                                                     SENT_HEADERS, sent_record))
    except GaveUp as e:
        sys.exit(f"\n{e}.\nProgress saved, rerun to continue.")
    except KeyboardInterrupt:
        sys.exit("\nInterrupted. Progress saved, rerun to continue.")

    decisions_file = SENT_DECISIONS_FILE if args.sent else DECISIONS_FILE
    decisions = load_json(decisions_file, {"keep": []})
    if args.protect_contacts:
        before = len(metadata)
        metadata = {mid: meta for mid, meta in metadata.items()
                    if sender_address(meta) not in contacts}
        print(f"Protecting {before - len(metadata)} emails from addresses you've emailed.")

    groups = group_emails(metadata, recipient_keys if args.sent else sender_keys,
                          set(decisions["keep"]), args.min)
    covered = len({mid for _, items in groups for mid, _, _ in items})
    print(f"\n{len(groups)} groups of {args.min}+ emails covering {covered} messages "
          f"({len(decisions['keep'])} senders marked keep).")

    totals = review(service, groups, args.sent, contacts, decisions, decisions_file)
    if args.sent:
        print(f"\nDone. Trashed: {totals['trashed']}")
    else:
        print(f"\nDone. Trashed: {totals['trashed']}   Archived: {totals['archived']}")


if __name__ == "__main__":
    main()
