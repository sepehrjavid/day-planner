"""Google Calendar tool for the day planner agent.

Handles OAuth (installed-app flow, cached in token.json) and exposes
get_calendar_events as a plain function the ADK agent can call as a tool.
"""

import os

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CREDENTIALS_PATH = os.path.join(_BASE_DIR, "credentials.json")
TOKEN_PATH = os.path.join(_BASE_DIR, "token.json")


def _get_credentials() -> Credentials:
    creds = None
    if os.path.exists(TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_PATH, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_PATH, "w") as token_file:
            token_file.write(creds.to_json())

    return creds


def get_calendar_events(calendar_id: str, date_from: str, date_to: str) -> dict:
    """Retrieve events from a Google Calendar within a date range.

    Args:
        calendar_id: The calendar to query — "primary" for the authenticated
            user's default calendar, or a specific calendar's email/ID.
        date_from: Start date, inclusive, in YYYY-MM-DD format.
        date_to: End date, exclusive, in YYYY-MM-DD format.

    Returns:
        A dict with "status" ("success" or "error"). On success, "events" is
        a list of dicts with event_id, title, start_time, end_time, and
        location. On error, "error_message" explains what went wrong.
    """
    try:
        creds = _get_credentials()
        service = build("calendar", "v3", credentials=creds)

        response = (
            service.events()
            .list(
                calendarId=calendar_id,
                timeMin=f"{date_from}T00:00:00Z",
                timeMax=f"{date_to}T00:00:00Z",
                singleEvents=True,
                orderBy="startTime",
            )
            .execute()
        )

        events = []
        for item in response.get("items", []):
            start = item.get("start", {})
            end = item.get("end", {})
            events.append(
                {
                    "event_id": item.get("id"),
                    "title": item.get("summary", "(no title)"),
                    "start_time": start.get("dateTime", start.get("date")),
                    "end_time": end.get("dateTime", end.get("date")),
                    "location": item.get("location"),
                }
            )

        return {"status": "success", "events": events}

    except HttpError as e:
        return {"status": "error", "error_message": str(e)}
    except FileNotFoundError:
        return {
            "status": "error",
            "error_message": (
                f"Missing OAuth client file at {CREDENTIALS_PATH}. Download it "
                "from GCP Console (APIs & Services > Credentials) and place it "
                "there."
            ),
        }
