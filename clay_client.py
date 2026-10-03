import os
import time

import requests

CLAY_API_BASE = "https://api.clay.com/public/v0"

# Profile and email are separate managed routines on purpose: the bundled
# "Enrich Person and Find Contact Details" routine also runs a mobile-phone
# waterfall the app never uses, costing 18.2 credits and up to ~6 minutes per
# search versus 1 + 3 credits and ~35s for these two.
PERSON_ROUTINE_ID = "function:t_0ti9fgrxiDHzrVdqDjX"  # Enrich Person
WORK_EMAIL_ROUTINE_ID = "function:t_0ti9fgwAMdXnbRR2JBw"  # Work Email
COMPANY_ROUTINE_ID = "function:t_0ti9fgrrbqU3ujitTkX"  # Enrich Company
# "Anaconda Tech Stack Check" workflow: BuiltWith (website) + TheirStack (job
# posts) filtered to AWS, Databricks, Snowflake. ~2 credits.
# https://app.clay.com/workspaces/595455/terracotta/tc-workflows/wf_0tmciv4u3oNBAKaPMnD
TECH_STACK_ROUTINE_ID = "workflow:wf_0tmciv4u3oNBAKaPMnD"


class ClayError(Exception):
    pass


def _headers():
    api_key = os.environ.get("CLAY_API_KEY")
    if not api_key:
        raise ClayError("CLAY_API_KEY is not set")
    return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}


def _request_with_retry(method: str, url: str, retries: int = 3, **kwargs):
    """Retries connection errors and 429 rate limits; raises ClayError otherwise."""
    last_error = None
    for attempt in range(retries):
        try:
            resp = requests.request(method, url, timeout=30, **kwargs)
        except requests.exceptions.ConnectionError as e:
            last_error = e
            if attempt < retries - 1:
                time.sleep(2 * (attempt + 1))
            continue

        if resp.status_code == 429:
            # Clay says how long to wait; cap it so one request can't stall the search.
            last_error = "rate limited by Clay"
            if attempt < retries - 1:
                try:
                    wait = float(resp.headers.get("Retry-After", 5))
                except ValueError:
                    wait = 5
                time.sleep(min(wait, 20))
            continue
        if resp.status_code >= 400:
            try:
                message = resp.json().get("message") or resp.text
            except ValueError:
                message = resp.text
            if resp.status_code in (401, 403):
                message = f"Clay rejected the API key ({message})"
            raise ClayError(f"Clay returned {resp.status_code}: {message}")
        return resp
    raise ClayError(f"Could not reach Clay API after {retries} attempts: {last_error}")


def start_run(routine_id: str, inputs: dict) -> str:
    resp = _request_with_retry(
        "post",
        f"{CLAY_API_BASE}/routines/{routine_id}/run",
        headers=_headers(),
        json={"items": [{"id": "item-1", "inputs": inputs}]},
    )
    return resp.json()["routine_run_id"]


def get_run(routine_run_id: str) -> dict:
    resp = _request_with_retry(
        "get",
        f"{CLAY_API_BASE}/routines/run/{routine_run_id}/results",
        headers=_headers(),
    )
    return resp.json()


def run_and_wait(routine_id: str, inputs: dict, timeout_seconds: int = 120, poll_interval: float = 2.0) -> dict:
    """Starts a routine run over a single item and blocks until it finishes.

    Returns the item's `result` dict. Raises ClayError if the run times out
    or the item fails/errors.
    """
    routine_run_id = start_run(routine_id, inputs)
    deadline = time.time() + timeout_seconds

    while time.time() < deadline:
        run = get_run(routine_run_id)
        if run["status"] == "complete":
            item = run["data"][0]
            if item["status"] == "failed":
                raise ClayError(item.get("error", {}).get("message", "Routine item failed"))
            return item["result"]
        time.sleep(poll_interval)

    raise ClayError(
        f"Clay is taking longer than usual (no result after {int(timeout_seconds)}s, "
        f"run {routine_run_id}). Please try again in a minute."
    )
