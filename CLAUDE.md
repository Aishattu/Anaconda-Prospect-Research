# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A small Flask app: paste a LinkedIn URL, get back a prospect profile (name, title, work email, company info), an AI-written summary and outreach angle, and a qualification verdict for Anaconda: does the company use AI (layer 1), and does it use AWS, Databricks or Snowflake (layer 2)? Enrichment comes from Clay; synthesis and the AI check come from Claude. This is a copy of `~/prospect-research-app` with the qualification layers added; the original is untouched and deploys separately.

## Commands

```bash
source venv/bin/activate          # Python 3.9 venv (already created)
pip install -r requirements.txt
python app.py                     # dev server on http://localhost:5000 (debug=True)
python company_website.py gong.io # print the website text (and AI pages) read for one domain
python tech_stack.py doordash.com  # run the tech stack check for one domain (spends ~2 Clay credits)
```

Requires `CLAY_API_KEY`, `ANTHROPIC_API_KEY` and `HUBSPOT_ACCESS_TOKEN` in `.env` (see `.env.example`). There are no tests, linter, or build step.

Keep code Python 3.9 compatible (the venv runs 3.9.6), e.g. no `X | Y` union types or `match`.

## Architecture

One synchronous request pipeline in `app.py` → `POST /research`:

1. **Person enrichment**: `clay_client.run_and_wait(PERSON_ROUTINE_ID, {"Professional Profile URL": ...})` (Clay-managed "Enrich Person", 1 credit). Failure here returns 422. The next three steps only need the profile, so they run **in parallel** (`ThreadPoolExecutor`) inside one shared time window. `WORK_EMAIL_ROUTINE_ID` (managed "Work Email", 3 credits) runs with name + company + domain from the profile; a failed email lookup just leaves `email` empty. Don't switch back to the bundled "Enrich Person and Find Contact Details" routine: it adds an unused mobile-phone waterfall (18.2 credits, observed ~6 min per search).
2. **Partner platforms (qualification layer 2)**: `tech_stack.check(domain)` runs `TECH_STACK_ROUTINE_ID`, the custom Clay workflow **"Anaconda Tech Stack Check"** (`workflow:wf_0tmciv4u3oNBAKaPMnD`, ~2 credits, ~10s). It looks for Anaconda's partner platforms (anaconda.com/partners): AWS, Microsoft Azure, Google Cloud, Oracle Cloud, Databricks, Snowflake. Steps: manual trigger (`company_domain`) → BuiltWith "Find technology stack" (detects tech from the site's code, hosting and DNS) filtered by keyword to those platforms → TheirStack "Find jobs" with `technologies_used` slugs (amazon-web-services, microsoft-azure, google-cloud-platform, google-bigquery, oracle-cloud-infrastructure, snowflake, databricks, azure-databricks), last 365 days, max 20 jobs → a code node returning flat fields `{aws,azure,gcp,oracle,databricks,snowflake}_{website,jobs,job_title,job_url}` + `jobs_checked`. The code node excludes look-alikes (Azure Active Directory, Amazon Pay, Oracle Eloqua...). Any one platform found counts toward qualification; the card lists only the platforms found. Changing the workflow in Clay needs a **publish** before the app sees it, and renaming the output fields breaks `tech_stack.py`.
3. **Company website (background + AI evidence)**: takes `result["Enrich person"]["latest_experience"]["company_domain"]` from step 1 and calls `company_website.scrape(domain)`, which returns visible text from the homepage plus up to 4 high-signal subpages (about, customers, solutions, product...) and up to 2 AI-related pages (`ai_pages`: /ai, /copilot, /ai-assistant...) found from homepage links or `sitemap.xml`. JS-only sites usually yield just the title and meta description, which is still used. If nothing readable comes back, it falls back to the Clay `COMPANY_ROUTINE_ID`; that routine only runs in the fallback case.
4. **AI synthesis + AI check (qualification layer 1)**: `claude_client.summarize(profile, website_pages, clay_company, ai_pages)` makes one Claude call with the server-side **web search tool** (`web_search_20260209`, `max_uses` 3), returning JSON `{"about", "outreach_angle", "uses_ai" ("yes"|"no_evidence"), "ai_evidence", "ai_source_url"}`. Claude only searches when the website text doesn't settle the AI question; `pause_turn` is continued up to `MAX_CONTINUATIONS` times. Web search must stay enabled for the org in the Anthropic Console. The `about` text combines person and company into 2-3 sentences; the card shows no raw LinkedIn About or raw company copy (a deliberate product choice by the owner). Failure is logged and swallowed (empty strings).
5. `_qualify()` sets `qualification`: `qualified` (uses AI AND ≥1 partner platform), `not_qualified`, or `incomplete` (a check couldn't run). Everything is flattened into a fixed JSON shape that `templates/index.html` renders.

**Time budget:** deployed on Vercel, where `vercel.json` sets `maxDuration: 300` and the platform kills the request at 300s. `/research` shares `RESEARCH_BUDGET_SECONDS` (280) across steps: person lookup up to `PERSON_LOOKUP_SECONDS` (120; typically ~10s), then email (`EMAIL_LOOKUP_SECONDS`, typically ~25s), tech stack (`TECH_STACK_SECONDS`) and website (≤30s) run in parallel within `remaining() - CLAUDE_MIN_SECONDS - 10`, Clay company fallback only if ≥20s is left, and Claude gets whatever remains (`CLAUDE_MIN_SECONDS` = 90 held back, because web searches observed up to ~80s). A full search took ~40s in testing. New slow steps must take a timeout derived from `remaining()`.

Key details that span files:

- **Clay routines** (`clay_client.py`): calls the Clay Public API (`/routines/{id}/run`, then polls `/routines/run/{run_id}/results` every 2s, 120s timeout). Routine IDs are hardcoded constants. The result keys used in `app.py` (`"Enrich person"`, `"Work Email"`, `"Enrich Company"`) are the column/step names configured inside those Clay routines. If a routine is changed in Clay, those keys must change too.
- `_request_with_retry` retries connection errors and 429s (honoring `Retry-After`, capped at 20s) and converts every other HTTP error into `ClayError`, so Clay failures take the 422 path with a readable message.
- `app.config["PROPAGATE_EXCEPTIONS"] = False` is deliberate so the global `@app.errorhandler(Exception)` returns JSON even in debug mode. The frontend expects every response to be JSON with an `error` field on failure.
- **Frontend contract**: `templates/index.html` is a single vanilla HTML/CSS/JS file. It reads these response fields: `name, title, company, email, location, about, outreach_angle, uses_ai, ai_evidence, ai_source_url, tech_stack {checked, error, found: {tool: {website, jobs, job_title, job_url}}, jobs_checked}, qualification`. Renaming a field in `app.py` means updating `renderCard()` and `_research_note_html()` too.
- **Claude model** is set by `MODEL` in `claude_client.py`. The model can think before answering, which uses up output tokens, so `summarize` uses `max_tokens=8192` (1024 truncated the JSON). It parses only the text after the last web search result, and strips fences/stray prose before `json.loads`. The client is lazily created so the app boots without the API key.
- **HubSpot** is the CRM (replaced the old Salesforce stub). `POST /add-to-hubspot` receives the full research JSON back from the frontend (plus `linkedin_url`); there is no server-side state between `/research` and this call. `hubspot_client.py` upserts the contact by email (a contact with no email is rejected with 400), then attaches the research as an HTML note (association type 202). A failed note doesn't fail the request; it returns `note_added: false`. Uses a HubSpot private-app token over the REST API. The HubSpot MCP is only for Claude Code sessions, not the running app.
