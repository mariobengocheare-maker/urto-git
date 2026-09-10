"""
Real Google API access for the HOSTED deployment only.

Two unrelated uses share this one OAuth connection:
  1. Google Drive backup (drive.file scope) -- Render has no filesystem
     access to a locally-synced Google Drive folder the way desktop does
     (that's Google Drive for Desktop, a Windows app), so "instant Google
     Drive backup" for changes made from the phone needs the real API.
  2. Google Calendar event creation with a Meet link (calendar.events
     scope), for URTO's Virtual Meeting feature (see build order #89) --
     creating an event with conferenceData gets a real Meet link back, and
     adding the client as an attendee makes Google send them the invite
     email automatically, no separate email-sending code needed.

Deliberately implemented with plain `requests` calls to Google's REST
endpoints rather than the official google-api-python-client SDK — the
actual surface used here is small, and this keeps requirements-hosted.txt
free of a large dependency tree.

Both scopes are requested together in ONE consent flow/refresh token --
asking Mario to connect Google twice (once per scope) would be needless
friction. Each scope is still least-privilege for what it's used for:
drive.file only sees files this app created; calendar.events only touches
events, never the rest of Calendar settings/other calendars.
"""

import io
import os

import requests

TOKEN_URL = "https://oauth2.googleapis.com/token"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
DRIVE_API = "https://www.googleapis.com/drive/v3"
UPLOAD_API = "https://www.googleapis.com/upload/drive/v3"
CALENDAR_API = "https://www.googleapis.com/calendar/v3"
SCOPE = "https://www.googleapis.com/auth/drive.file https://www.googleapis.com/auth/calendar.events"

CLIENT_ID = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "")


def is_configured() -> bool:
    return bool(CLIENT_ID and CLIENT_SECRET)


def build_auth_url(redirect_uri: str, state: str) -> str:
    params = {
        "client_id": CLIENT_ID,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        "prompt": "consent",  # forces Google to hand back a refresh_token every time
        "state": state,
    }
    query = "&".join(f"{k}={requests.utils.quote(str(v))}" for k, v in params.items())
    return f"{AUTH_URL}?{query}"


def exchange_code(redirect_uri: str, code: str) -> dict:
    """Returns the token response (access_token, refresh_token, expires_in, ...)
    or raises requests.HTTPError on failure."""
    res = requests.post(TOKEN_URL, data={
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "redirect_uri": redirect_uri,
        "code": code,
        "grant_type": "authorization_code",
    }, timeout=20)
    res.raise_for_status()
    return res.json()


def refresh_access_token(refresh_token: str) -> str:
    res = requests.post(TOKEN_URL, data={
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }, timeout=20)
    res.raise_for_status()
    return res.json()["access_token"]


def _headers(access_token):
    return {"Authorization": f"Bearer {access_token}"}


def find_or_create_folder(access_token: str, name: str, parent_id: str = None) -> str:
    """Returns the folder's file id, creating it (once) if it doesn't exist yet."""
    query = f"name = '{name}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    if parent_id:
        query += f" and '{parent_id}' in parents"
    res = requests.get(
        f"{DRIVE_API}/files", headers=_headers(access_token),
        params={"q": query, "fields": "files(id,name)", "spaces": "drive"}, timeout=20,
    )
    res.raise_for_status()
    files = res.json().get("files", [])
    if files:
        return files[0]["id"]

    metadata = {"name": name, "mimeType": "application/vnd.google-apps.folder"}
    if parent_id:
        metadata["parents"] = [parent_id]
    res = requests.post(f"{DRIVE_API}/files", headers=_headers(access_token), json=metadata, timeout=20)
    res.raise_for_status()
    return res.json()["id"]


def _find_file(access_token: str, name: str, folder_id: str):
    query = f"name = '{name}' and '{folder_id}' in parents and trashed = false"
    res = requests.get(
        f"{DRIVE_API}/files", headers=_headers(access_token),
        params={"q": query, "fields": "files(id,name)", "spaces": "drive"}, timeout=20,
    )
    res.raise_for_status()
    files = res.json().get("files", [])
    return files[0]["id"] if files else None


def upload_or_update_file(access_token: str, name: str, content: bytes, mimetype: str, folder_id: str):
    """Creates the file if it doesn't exist in folder_id yet, otherwise
    overwrites its content in place -- so re-running this for the same
    logical file (e.g. urto_crm_latest.db) never creates duplicates."""
    existing_id = _find_file(access_token, name, folder_id)
    if existing_id:
        res = requests.patch(
            f"{UPLOAD_API}/files/{existing_id}?uploadType=media",
            headers={**_headers(access_token), "Content-Type": mimetype},
            data=content, timeout=60,
        )
    else:
        boundary = "urtoboundary"
        metadata = f'{{"name": "{name}", "parents": ["{folder_id}"]}}'
        body = (
            f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{metadata}\r\n"
            f"--{boundary}\r\nContent-Type: {mimetype}\r\n\r\n"
        ).encode("utf-8") + content + f"\r\n--{boundary}--".encode("utf-8")
        res = requests.post(
            f"{UPLOAD_API}/files?uploadType=multipart",
            headers={**_headers(access_token), "Content-Type": f"multipart/related; boundary={boundary}"},
            data=body, timeout=60,
        )
    res.raise_for_status()
    return res.json()


def list_files_in_folder(access_token: str, folder_id: str) -> list:
    """Returns [{"id": ..., "name": ..., "is_folder": bool}, ...] for every
    non-trashed item directly inside folder_id -- used to find and remove
    files/subfolders Drive still has that are no longer expected (a
    renamed/removed document), the same deletion-propagation every other
    backup destination already gets. `is_folder` lets a caller recurse into
    subfolders (e.g. one per transaction) while pruning leaf files."""
    query = f"'{folder_id}' in parents and trashed = false"
    res = requests.get(
        f"{DRIVE_API}/files", headers=_headers(access_token),
        params={"q": query, "fields": "files(id,name,mimeType)", "spaces": "drive"}, timeout=20,
    )
    res.raise_for_status()
    return [
        {"id": f["id"], "name": f["name"], "is_folder": f.get("mimeType") == "application/vnd.google-apps.folder"}
        for f in res.json().get("files", [])
    ]


def delete_file(access_token: str, file_id: str):
    res = requests.delete(f"{DRIVE_API}/files/{file_id}", headers=_headers(access_token), timeout=20)
    if res.status_code not in (200, 204, 404):
        res.raise_for_status()


def create_meet_event(access_token: str, summary: str, start_iso: str, end_iso: str, timezone: str,
                       attendee_email: str = None, description: str = "") -> dict:
    """Creates a real event on Mario's primary Google Calendar with a Google
    Meet link attached, and (if attendee_email is given) invites the
    client -- Google sends that invite email automatically once the event
    is created with an attendee, so no separate email-sending code is
    needed here. Returns the raw Calendar API event dict; the Meet link is
    at event["hangoutLink"] (also present as one of the entryPoints in
    event["conferenceData"])."""
    body = {
        "summary": summary,
        "description": description,
        "start": {"dateTime": start_iso, "timeZone": timezone},
        "end": {"dateTime": end_iso, "timeZone": timezone},
        "conferenceData": {
            "createRequest": {
                # Must be unique per request -- Google uses it to dedupe
                # retries, not as a real identifier we need to remember.
                "requestId": f"urto-{os.urandom(8).hex()}",
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
            }
        },
    }
    if attendee_email:
        body["attendees"] = [{"email": attendee_email}]
    res = requests.post(
        f"{CALENDAR_API}/calendars/primary/events",
        headers=_headers(access_token),
        params={"conferenceDataVersion": 1, "sendUpdates": "all"},
        json=body,
        timeout=20,
    )
    res.raise_for_status()
    return res.json()
