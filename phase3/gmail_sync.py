#!/usr/bin/env python3
"""
gmail_sync.py — incremental Gmail API pull: fetch only new/changed messages since the
last sync and write them into a fresh .mbox that ingest_mbox.py ingests unchanged.

Why: Google Takeout is the right tool for the one-time historical backfill (Phase 1/2),
but re-exporting all of Gmail nightly to catch new mail does not scale. The Gmail API's
users.history.list endpoint returns exactly what changed since a stored historyId, so a
nightly top-up costs a handful of API calls instead of a multi-GB export.

Setup (once per Google account)
  1. Google Cloud Console -> enable the Gmail API -> OAuth client ID (Desktop app) ->
     download as gmail_credentials.json.
  2. First run opens a browser for consent and caches a token in --token-file; every
     run after that is unattended (suitable for cron / phase3/incremental_sync.py).

Usage
  python gmail_sync.py --credentials gmail_credentials.json --token-file token.json \
         --account projects@bridgeit.com --out incoming
  # then feed the produced file(s) to the existing ingester, unchanged:
  python ../phase2/ingest_mbox.py --config ../phase1/config.local.yaml \
         incoming/projects@bridgeit.com_*.mbox --account projects@bridgeit.com

State: --state-file (default gmail_sync_state.json) stores each account's last
historyId. An account with no state, or one whose historyId has expired on Google's
side (typically >1 week old), falls back to a --since-days window instead of the full
mailbox — use Takeout for anything older than that.

Requires (Phase 3 section of requirements.txt):
  google-api-python-client google-auth-httplib2 google-auth-oauthlib
"""
from __future__ import annotations

import argparse
import base64
import json
import mailbox
import sys
import time
from pathlib import Path

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


def get_service(credentials_path: Path, token_path: Path):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    creds = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), SCOPES)
            creds = flow.run_local_server(port=0)
        token_path.write_text(creds.to_json(), encoding="utf-8")
    return build("gmail", "v1", credentials=creds)


def load_state(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def save_state(path: Path, state: dict) -> None:
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def list_message_ids_full(service, since_days: int) -> list[str]:
    after = time.strftime("%Y/%m/%d", time.gmtime(time.time() - since_days * 86400))
    ids, page_token = [], None
    while True:
        resp = service.users().messages().list(userId="me", q=f"after:{after}",
                                                 pageToken=page_token, maxResults=500).execute()
        ids += [m["id"] for m in resp.get("messages", [])]
        page_token = resp.get("nextPageToken")
        if not page_token:
            return ids


def list_message_ids_incremental(service, start_history_id: str) -> tuple[list[str], bool]:
    """Returns (message_ids, fell_back). fell_back=True means start_history_id expired."""
    ids, page_token = [], None
    try:
        while True:
            resp = service.users().history().list(
                userId="me", startHistoryId=start_history_id, historyTypes=["messageAdded"],
                pageToken=page_token, maxResults=500).execute()
            for h in resp.get("history", []):
                ids += [m["message"]["id"] for m in h.get("messagesAdded", [])]
            page_token = resp.get("nextPageToken")
            if not page_token:
                return sorted(set(ids)), False
    except Exception as e:  # googleapiclient.errors.HttpError 404 = historyId too old
        print(f"  historyId {start_history_id} expired ({e}); falling back to --since-days", file=sys.stderr)
        return [], True


def fetch_raw(service, msg_id: str) -> bytes:
    data = service.users().messages().get(userId="me", id=msg_id, format="raw").execute()
    return base64.urlsafe_b64decode(data["raw"] + "=" * (-len(data["raw"]) % 4))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--credentials", required=True, help="OAuth client secret JSON from Cloud Console")
    ap.add_argument("--token-file", default="gmail_token.json")
    ap.add_argument("--account", required=True, help="label used in the output filename and state key")
    ap.add_argument("--out", default="incoming", help="directory for the produced .mbox file")
    ap.add_argument("--state-file", default="gmail_sync_state.json")
    ap.add_argument("--since-days", type=int, default=30, help="backfill window when there is no usable state")
    args = ap.parse_args()

    service = get_service(Path(args.credentials), Path(args.token_file))
    state_path = Path(args.state_file)
    state = load_state(state_path)
    acct_state = state.get(args.account, {})

    start_history_id = acct_state.get("history_id")
    fell_back = start_history_id is None
    ids: list[str] = []
    if start_history_id:
        ids, fell_back = list_message_ids_incremental(service, start_history_id)
    if fell_back:
        ids = list_message_ids_full(service, args.since_days)
        print(f"{args.account}: full pull, since {args.since_days} day(s): {len(ids)} message(s)", file=sys.stderr)
    else:
        print(f"{args.account}: incremental since historyId {start_history_id}: {len(ids)} new message(s)", file=sys.stderr)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    if ids:
        out_path = out_dir / f"{args.account}_{time.strftime('%Y%m%dT%H%M%S')}.mbox"
        # mailbox.mbox needs a real path it owns; build it directly rather than via tempfile+rename
        box = mailbox.mbox(str(out_path))
        box.lock()
        try:
            for i, mid in enumerate(ids, 1):
                try:
                    raw = fetch_raw(service, mid)
                    box.add(mailbox.mboxMessage(raw))
                except Exception as e:
                    print(f"  !! {mid}: {e}", file=sys.stderr)
                if i % 200 == 0:
                    print(f"  {i}/{len(ids)} fetched", file=sys.stderr)
            box.flush()
        finally:
            box.unlock(); box.close()
        print(f"wrote {out_path} ({len(ids)} message(s))")
    else:
        print("no new messages")

    profile = service.users().getProfile(userId="me").execute()
    state[args.account] = {"history_id": profile["historyId"], "synced_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    save_state(state_path, state)


if __name__ == "__main__":
    main()
