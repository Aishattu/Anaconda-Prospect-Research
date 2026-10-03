import os
import time

import requests

HUBSPOT_API_BASE = "https://api.hubapi.com"

NOTE_TO_CONTACT_ASSOCIATION = 202  # HubSpot-defined association type id


class HubSpotError(Exception):
    pass


def _headers():
    token = os.environ.get("HUBSPOT_ACCESS_TOKEN")
    if not token:
        raise HubSpotError("HUBSPOT_ACCESS_TOKEN is not set")
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _post(path: str, body: dict) -> dict:
    try:
        resp = requests.post(f"{HUBSPOT_API_BASE}{path}", headers=_headers(), json=body, timeout=30)
    except requests.exceptions.RequestException as e:
        raise HubSpotError(f"Could not reach HubSpot: {e}")
    if resp.status_code >= 400:
        try:
            message = resp.json().get("message", resp.text)
        except ValueError:
            message = resp.text
        raise HubSpotError(f"HubSpot returned {resp.status_code}: {message}")
    return resp.json()


def upsert_contact(email: str, properties: dict) -> str:
    """Creates the contact, or updates it if one with this email exists.
    Returns the HubSpot contact id.
    """
    data = _post(
        "/crm/v3/objects/contacts/batch/upsert",
        {"inputs": [{"idProperty": "email", "id": email, "properties": properties}]},
    )
    return data["results"][0]["id"]


def add_note(contact_id: str, body_html: str) -> str:
    """Attaches a note to the contact's timeline. Returns the note id."""
    data = _post(
        "/crm/v3/objects/notes",
        {
            "properties": {"hs_timestamp": int(time.time() * 1000), "hs_note_body": body_html},
            "associations": [
                {
                    "to": {"id": contact_id},
                    "types": [
                        {
                            "associationCategory": "HUBSPOT_DEFINED",
                            "associationTypeId": NOTE_TO_CONTACT_ASSOCIATION,
                        }
                    ],
                }
            ],
        },
    )
    return data["id"]
