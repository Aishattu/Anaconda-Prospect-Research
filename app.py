import os
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout

from dotenv import load_dotenv

load_dotenv()

from flask import Flask, jsonify, render_template, request
from markupsafe import escape

import clay_client
import claude_client
import company_website
import hubspot_client
import tech_stack

# Vercel stops a request at 300s (vercel.json maxDuration). Every step of a
# search shares this budget so we answer before that cutoff, never during it.
RESEARCH_BUDGET_SECONDS = 280
PERSON_LOOKUP_SECONDS = 120
EMAIL_LOOKUP_SECONDS = 120
TECH_STACK_SECONDS = 120
# Time always held back for Claude: writing the About plus, when the website
# doesn't settle it, a few web searches for the AI check.
CLAUDE_MIN_SECONDS = 90

app = Flask(__name__)
# Without this, Flask's debug mode re-raises exceptions for the interactive
# debugger instead of running our JSON error handler below.
app.config["PROPAGATE_EXCEPTIONS"] = False


@app.errorhandler(Exception)
def handle_unexpected_error(e):
    app.logger.exception("Unhandled error")
    return jsonify({"error": f"Something went wrong: {e}"}), 500


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/research", methods=["POST"])
def research():
    body = request.get_json(silent=True) or {}
    linkedin_url = (body.get("linkedin_url") or "").strip()
    if not linkedin_url:
        return jsonify({"error": "linkedin_url is required"}), 400

    deadline = time.time() + RESEARCH_BUDGET_SECONDS

    def remaining():
        return deadline - time.time()

    person_pool = ThreadPoolExecutor(max_workers=1)
    person_job = person_pool.submit(
        clay_client.run_and_wait,
        clay_client.PERSON_ROUTINE_ID,
        {"Professional Profile URL": linkedin_url},
        timeout_seconds=PERSON_LOOKUP_SECONDS,
    )
    person_pool.shutdown(wait=False)
    try:
        person = person_job.result(timeout=PERSON_LOOKUP_SECONDS + 5)
    except FutureTimeout:
        return jsonify({"error": "Clay is taking longer than usual to look up this profile. Please try again in a minute."}), 422
    except clay_client.ClayError as e:
        return jsonify({"error": f"Could not research this LinkedIn profile: {e}"}), 422

    profile = person.get("Enrich person") or {}
    latest_experience = profile.get("latest_experience") or {}
    company_domain = latest_experience.get("company_domain") or ""

    # Email, tech stack and website all only need the profile, so they run at
    # the same time and share one time window.
    window = remaining() - CLAUDE_MIN_SECONDS - 10
    pool = ThreadPoolExecutor(max_workers=3)
    email_job = pool.submit(_find_email, profile, latest_experience, company_domain, linkedin_url, window)
    stack_job = pool.submit(_check_tech_stack, company_domain, window)
    website_job = pool.submit(_read_website, company_domain, window)
    # Hard stop: one hung connection once ran 6+ minutes past its own timeout,
    # so stop waiting at the window's end and use a fallback for that step.
    window_end = time.time() + max(window, 0) + 5
    email = _result_by(email_job, window_end, "", "Work email lookup")
    stack = _result_by(stack_job, window_end, {
        "checked": False, "error": "The tech stack check took too long", "found": {},
        "not_detected": [], "jobs_checked": 0,
    }, "Tech stack check")
    website = _result_by(website_job, window_end, {}, "Website read")
    pool.shutdown(wait=False)
    website_pages = website.get("pages", {})

    # Clay's company enrichment is only a fallback for sites we can't read.
    company = {}
    fallback_seconds = min(60, remaining() - CLAUDE_MIN_SECONDS)
    if company_domain and not website_pages and fallback_seconds >= 20:
        try:
            company_result = clay_client.run_and_wait(
                clay_client.COMPANY_ROUTINE_ID,
                {"Company Identifier": company_domain},
                timeout_seconds=fallback_seconds,
            )
            company = company_result.get("Enrich Company") or {}
        except clay_client.ClayError:
            company = {}

    try:
        ai = claude_client.summarize(
            profile, website_pages, company,
            ai_pages=website.get("ai_pages", []),
            timeout_seconds=max(remaining() - 5, 10),
        )
    except Exception:
        app.logger.exception("Claude summarize failed")
        # "" means the AI check couldn't run (unlike "no_evidence").
        ai = {"about": "", "outreach_angle": "", "uses_ai": ""}

    uses_ai = ai.get("uses_ai", "")
    status, reasons = _qualify(uses_ai, stack)
    result = {
        "name": profile.get("name", ""),
        "title": profile.get("title") or latest_experience.get("title", ""),
        "company": latest_experience.get("company") or company.get("name", ""),
        "email": email,
        "location": profile.get("location_name", ""),
        "about": ai.get("about", ""),
        "outreach_angle": ai.get("outreach_angle", ""),
        "uses_ai": uses_ai,
        "ai_evidence": ai.get("ai_evidence", ""),
        "ai_source_url": ai.get("ai_source_url", ""),
        "tech_stack": stack,
        "qualification": status,
        "qualification_reasons": reasons,
    }
    return jsonify(result)


def _result_by(job, deadline, fallback, label):
    """The job's result, or `fallback` if it isn't done by `deadline`."""
    try:
        return job.result(timeout=max(deadline - time.time(), 0))
    except FutureTimeout:
        app.logger.error("%s timed out; continuing without it", label)
        return fallback


def _find_email(profile, latest_experience, company_domain, linkedin_url, window):
    """Work Email needs name + company + domain, so it runs after the profile.
    A missing email isn't fatal: the card still shows everything else.
    """
    seconds = min(EMAIL_LOOKUP_SECONDS, window)
    if not (profile.get("name") and latest_experience.get("company") and company_domain) or seconds < 20:
        return ""
    try:
        result = clay_client.run_and_wait(
            clay_client.WORK_EMAIL_ROUTINE_ID,
            {
                "Full Name": profile["name"],
                "Company Name": latest_experience["company"],
                "Company Domain": company_domain,
                "Social Profile URL": linkedin_url,
            },
            timeout_seconds=seconds,
        )
        return result.get("Work Email") or ""
    except clay_client.ClayError:
        app.logger.exception("Work email lookup failed for %s", linkedin_url)
        return ""


def _check_tech_stack(company_domain, window):
    """Qualification layer 2. Returns tech_stack.check()'s dict plus "checked"
    and "error", so the card can tell "not detected" from "couldn't check".
    """
    empty = {"checked": False, "error": "", "found": {}, "not_detected": [], "jobs_checked": 0}
    seconds = min(TECH_STACK_SECONDS, window)
    if not company_domain:
        return {**empty, "error": "No company website found for this person"}
    if seconds < 20:
        return {**empty, "error": "Not enough time left in this search"}
    try:
        return {"checked": True, "error": "", **tech_stack.check(company_domain, timeout_seconds=seconds)}
    except clay_client.ClayError as e:
        app.logger.exception("Tech stack lookup failed for %s", company_domain)
        return {**empty, "error": str(e)}


def _read_website(company_domain, window):
    """The company's own website: background for the About and the AI check."""
    if not company_domain or window < 5:
        return {}
    try:
        return company_website.scrape(company_domain, budget_seconds=min(30, window))
    except Exception:
        app.logger.exception("Website scrape failed for %s", company_domain)
        return {}


def _qualify(uses_ai, stack):
    """A prospect qualifies when their company uses AI AND at least one of
    tech_stack.TARGET_TOOLS was detected. Returns (status, reasons) where status
    is "qualified", "not_qualified" or "incomplete" (a check couldn't run).
    """
    reasons = []
    if uses_ai == "yes":
        reasons.append("Uses AI")
    elif uses_ai == "no_evidence":
        reasons.append("No evidence they use AI")
    else:
        reasons.append("AI check couldn't run")

    found = sorted(stack.get("found", {}))
    if found:
        reasons.append("Uses " + ", ".join(found))
    elif stack.get("checked"):
        reasons.append("No partner platform detected")
    else:
        reasons.append("Tech stack check couldn't run")

    if uses_ai == "yes" and found:
        return "qualified", reasons
    if uses_ai == "" or (not found and not stack.get("checked")):
        return "incomplete", reasons
    return "not_qualified", reasons


def _research_note_html(data: dict) -> str:
    """Formats the research card as an HTML note for the HubSpot timeline."""
    def section(label, value):
        return f"<p><strong>{escape(label)}:</strong> {escape(value)}</p>" if value else ""

    stack = data.get("tech_stack") or {}
    platforms = ", ".join(t for t in tech_stack.TARGET_TOOLS if t in (stack.get("found") or {}))
    return "".join([
        "<p><strong>Prospect research</strong></p>",
        section("LinkedIn", data.get("linkedin_url")),
        section("Location", data.get("location")),
        section("Qualification", (data.get("qualification") or "").replace("_", " ").capitalize()),
        section("AI adoption", data.get("ai_evidence") or ("No evidence found" if data.get("uses_ai") == "no_evidence" else "")),
        section("AI source", data.get("ai_source_url")),
        section("Partner platforms", platforms or ("None found" if stack.get("checked") else "")),
        section("About", data.get("about")),
        section("Outreach angle", data.get("outreach_angle")),
    ])


@app.route("/add-to-hubspot", methods=["POST"])
def add_to_hubspot():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip()
    if not email:
        return jsonify({"error": "No email found for this prospect, so they can't be added to HubSpot"}), 400

    first, _, last = (data.get("name") or "").strip().partition(" ")
    properties = {
        "email": email,
        "firstname": first,
        "lastname": last,
        "jobtitle": data.get("title") or "",
        "company": data.get("company") or "",
    }

    try:
        contact_id = hubspot_client.upsert_contact(email, properties)
    except hubspot_client.HubSpotError as e:
        return jsonify({"error": f"Could not add to HubSpot: {e}"}), 502

    # The contact is already saved; a failed note shouldn't undo that.
    note_added = True
    try:
        hubspot_client.add_note(contact_id, _research_note_html(data))
    except hubspot_client.HubSpotError:
        app.logger.exception("HubSpot note failed for contact %s", contact_id)
        note_added = False

    return jsonify({"contact_id": contact_id, "note_added": note_added})


if __name__ == "__main__":
    app.run(debug=True, port=5000)
