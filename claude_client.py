import json
import os
import time

import anthropic

MODEL = "claude-sonnet-5"

# Claude may search the web for the AI-adoption check when the company's own
# website doesn't settle it. Each search is billed on top of tokens, so cap it.
WEB_SEARCH_TOOL = {"type": "web_search_20260209", "name": "web_search", "max_uses": 3}
# A long search can come back as stop_reason "pause_turn"; we resend to let
# Claude finish, at most this many times.
MAX_CONTINUATIONS = 2

_client = None


def _get_client():
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    return _client


def summarize(person: dict, website_pages: dict, company: dict, ai_pages=(), timeout_seconds: float = 60) -> dict:
    """Writes the prospect's About text and outreach angle, and judges whether
    their company uses AI (qualification layer 1).

    Draws on the Clay person profile, text scraped from the company's website
    ({url: page_text}; `ai_pages` lists which of those URLs look AI-related),
    and Clay company data (the fallback when the website couldn't be read).
    Returns {"about", "outreach_angle", "uses_ai" ("yes" | "no_evidence"),
    "ai_evidence", "ai_source_url"}.
    """
    if website_pages:
        site_text = "\n\n".join(
            f"=== {url}{' (AI-related page)' if url in ai_pages else ''} ===\n{text}"
            for url, text in website_pages.items()
        )
        company_section = f"Text from the company's own website:\n{site_text}"
    else:
        company_section = (
            "We couldn't read the company's website. Company data:\n"
            f"{json.dumps(company, indent=2)}"
        )

    prompt = f"""You are helping a sales rep understand a prospect before outreach, and checking
whether the prospect's company adopts AI.

Person data (includes their LinkedIn profile):
{json.dumps(person, indent=2)}

{company_section}

Write:
1. about: 2-3 plain sentences describing who this person is and what they do, and what
   their current company does and who it serves. Combine the person data and the company
   information into one description. Use only facts from the data given (do not invent
   facts). Don't repeat their location or email, and leave out side projects and
   volunteer roles unless they are central to what the person does.
2. outreach_angle: One sentence suggesting a specific outreach angle based on this data.
3. uses_ai: "yes" only if there is concrete evidence that this company builds AI into its
   own product or systems: for example an AI assistant, copilot, AI agents, AI-powered
   features, or a public statement that they use AI in their operations. Writing about AI
   as a trend, or a vague "innovative" claim, does not count. Otherwise "no_evidence".
   If the website text above doesn't settle it, use web_search to look for the company's
   own product pages, docs, or announcements (search about the company, not the person).
4. ai_evidence: One short sentence naming the evidence (use the real product or feature
   name), or "" if uses_ai is "no_evidence".
5. ai_source_url: The URL where that evidence appears, or "".

Respond with ONLY valid JSON, no markdown fences, in exactly this shape:
{{"about": "...", "outreach_angle": "...", "uses_ai": "yes", "ai_evidence": "...", "ai_source_url": "..."}}"""

    deadline = time.time() + timeout_seconds
    messages = [{"role": "user", "content": prompt}]
    for _ in range(MAX_CONTINUATIONS + 1):
        # The model may think before answering, which counts against max_tokens.
        response = _get_client().with_options(
            timeout=max(deadline - time.time(), 5), max_retries=0
        ).messages.create(
            model=MODEL,
            max_tokens=8192,
            tools=[WEB_SEARCH_TOOL],
            messages=messages,
        )
        if response.stop_reason != "pause_turn" or time.time() > deadline - 10:
            break
        messages.append({"role": "assistant", "content": response.content})

    # Only the text written after the last search result is the answer; text
    # before it is Claude narrating its searches.
    text_blocks = []
    for block in response.content:
        if block.type == "web_search_tool_result":
            text_blocks = []
        elif block.type == "text":
            text_blocks.append(block.text)
    text = "".join(text_blocks)
    # Tolerate markdown fences or stray prose around the JSON object.
    result = json.loads(text[text.find("{"): text.rfind("}") + 1])
    if result.get("uses_ai") != "yes":
        result.update(uses_ai="no_evidence", ai_evidence="", ai_source_url="")
    return result
