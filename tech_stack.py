"""Qualification layer 2: does the company use one of Anaconda's partner
platforms (AWS, Microsoft Azure, Google Cloud, Oracle Cloud, Databricks,
Snowflake; from anaconda.com/partners)?

Runs the "Anaconda Tech Stack Check" Clay workflow (registered as a routine),
which combines two signals for the company's domain:

- BuiltWith "Find technology stack", filtered to just these platforms: what it
  detects in the company website's code, hosting and DNS. Reliable for cloud
  hosting; internal data tools (Snowflake, Databricks) leave fewer traces.
- TheirStack "Find jobs": job posts from the last 365 days whose description
  mentions one of the platforms. This is where Snowflake and Databricks usually
  show up. TheirStack returns at most 20 jobs, so counts are capped at 20.

The workflow's last step sorts both into per-tool fields
({tool}_website, {tool}_jobs, {tool}_job_title, {tool}_job_url).

Run on its own to check one domain (spends ~2 Clay credits):
    python tech_stack.py doordash.com
"""
import json
import sys

import clay_client

# Display name -> field prefix in the Clay workflow's output.
_FIELD_PREFIX = {
    "AWS": "aws",
    "Microsoft Azure": "azure",
    "Google Cloud": "gcp",
    "Oracle Cloud": "oracle",
    "Databricks": "databricks",
    "Snowflake": "snowflake",
}
TARGET_TOOLS = list(_FIELD_PREFIX)


def check(domain: str, timeout_seconds: float = 120) -> dict:
    """Returns {"found": {tool: {"website": [names], "jobs": int, "job_title": str,
    "job_url": str}}, "not_detected": [tools], "jobs_checked": int}.
    Raises clay_client.ClayError.
    """
    result = clay_client.run_and_wait(
        clay_client.TECH_STACK_ROUTINE_ID,
        {"company_domain": domain},
        timeout_seconds=timeout_seconds,
    )

    found = {}
    for tool in TARGET_TOOLS:
        prefix = _FIELD_PREFIX[tool]
        website = [n.strip() for n in (result.get(f"{prefix}_website") or "").split(",") if n.strip()]
        jobs = int(result.get(f"{prefix}_jobs") or 0)
        if website or jobs:
            found[tool] = {
                "website": website,
                "jobs": jobs,
                "job_title": result.get(f"{prefix}_job_title") or "",
                "job_url": result.get(f"{prefix}_job_url") or "",
            }

    return {
        "found": found,
        "not_detected": [t for t in TARGET_TOOLS if t not in found],
        "jobs_checked": int(result.get("jobs_checked") or 0),
    }


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv()
    if len(sys.argv) != 2:
        sys.exit("usage: python tech_stack.py <domain>")
    print(json.dumps(check(sys.argv[1]), indent=2))
