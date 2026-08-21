"""
Real Microsoft Graph API access for the HOSTED deployment only -- URTO's
Virtual Meeting feature (see build order #89) creating a real Teams
meeting on Mario's actual Outlook calendar, exactly the way his Outlook
already can when he does it by hand.

This is a completely separate OAuth connection from google_drive_api.py --
Microsoft and Google don't share credentials or consent, so this needs its
own one-time app registration (Azure Portal) and its own consent flow,
mirrored on the Microsoft side of everything google_drive_api.py already
does on the Google side (same reasoning: plain `requests` calls to
Microsoft's REST endpoints rather than the official SDK, since the actual
surface used here -- OAuth token exchange/refresh, create one calendar
event -- is small).

Scope used is Calendars.ReadWrite (plus offline_access for a refresh
token) -- the app can create/edit calendar events, nothing broader.
Creating an event with isOnlineMeeting=true, onlineMeetingProvider=
"teamsForBusiness" gets a real Teams join link back, and adding the
client as an attendee makes Outlook send them the invite email
automatically -- no separate email-sending code needed here, same as the
Google Meet path.
"""

import os

import requests

# The "common" tenant accepts both personal Microsoft accounts and work/
# school (Microsoft 365) accounts -- Mario's Outlook is the latter.
AUTHORITY = "https://login.microsoftonline.com/common"
TOKEN_URL = f"{AUTHORITY}/oauth2/v2.0/token"
AUTH_URL = f"{AUTHORITY}/oauth2/v2.0/authorize"
GRAPH_API = "https://graph.microsoft.com/v1.0"
SCOPE = "offline_access Calendars.ReadWrite"

CLIENT_ID = os.environ.get("MICROSOFT_OAUTH_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("MICROSOFT_OAUTH_CLIENT_SECRET", "")


def is_configured() -> bool:
    return bool(CLIENT_ID and CLIENT_SECRET)


def build_auth_url(redirect_uri: str, state: str) -> str:
    params = {
        "client_id": CLIENT_ID,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPE,
        "response_mode": "query",
        "state": state,
        # Forces a fresh consent so a refresh_token comes back reliably,
        # same reasoning as google_drive_api.py's prompt=consent.
        "prompt": "consent",
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
        "scope": SCOPE,
    }, timeout=20)
    res.raise_for_status()
    return res.json()


def refresh_access_token(refresh_token: str) -> str:
    res = requests.post(TOKEN_URL, data={
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
        "scope": SCOPE,
    }, timeout=20)
    res.raise_for_status()
    return res.json()["access_token"]


def _headers(access_token):
    return {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}


def create_teams_event(access_token: str, subject: str, start_iso: str, end_iso: str, timezone: str,
                        attendee_email: str = None, body_html: str = "") -> dict:
    """Creates a real event on Mario's Outlook calendar with a Teams
    meeting attached, and (if attendee_email is given) invites the client
    -- Outlook sends that invite email automatically once the event is
    created with an attendee, same as the Google Meet path. Returns the
    raw Graph API event dict; the join link is at
    event["onlineMeeting"]["joinUrl"]."""
    body = {
        "subject": subject,
        "body": {"contentType": "HTML", "content": body_html},
        "start": {"dateTime": start_iso, "timeZone": timezone},
        "end": {"dateTime": end_iso, "timeZone": timezone},
        "isOnlineMeeting": True,
        "onlineMeetingProvider": "teamsForBusiness",
    }
    if attendee_email:
        body["attendees"] = [{
            "emailAddress": {"address": attendee_email},
            "type": "required",
        }]
    res = requests.post(f"{GRAPH_API}/me/events", headers=_headers(access_token), json=body, timeout=20)
    res.raise_for_status()
    return res.json()
