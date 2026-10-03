"""Reads the company's own website as background for Claude's About text and
the "does this company use AI?" check.

Fetches the homepage plus a few high-signal pages (about, customers, product,
solutions...) and up to MAX_AI_PAGES AI-related pages (/ai, /copilot,
/ai-assistant...), and extracts their visible text. AI pages are found from the
homepage links first, then from sitemap.xml, which JS-only sites usually still
publish as plain XML.

Run on its own to see what gets read for a domain:
    python company_website.py stripe.com
"""
import json
import re
import sys
import time
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
}

# Paths most likely to describe what the company does and who it sells to,
# in priority order.
PAGE_KEYWORDS = [
    "about", "company", "customers", "case-stud", "solutions", "product",
    "platform", "industries", "who-we-serve", "use-case", "services", "pricing",
]
MAX_EXTRA_PAGES = 4
MAX_AI_PAGES = 2
# A path counts as AI-related if one of its words (split on / - _ .) is in
# AI_WORDS, or it contains one of AI_PHRASES.
AI_WORDS = {"ai", "genai", "copilot", "assistant", "agentic", "llm", "gpt"}
AI_PHRASES = ("artificial-intelligence", "machine-learning", "ai-agent")
# Blog/news posts about AI are weaker evidence than a product page, so they
# are only picked when nothing better exists.
ARTICLE_PATHS = ("/blog", "/news", "/press", "/resources", "/insights", "/events")
MAX_CHARS_PER_PAGE = 6000
# JS-only sites often yield just a title and meta description (~150 chars),
# which is still worth passing on; less than this is usually a block page.
MIN_TOTAL_CHARS = 100


class WebsiteError(Exception):
    pass


def _fetch(url: str, timeout: float = 10):
    """Returns (final_url, soup) or None if the page can't be fetched as HTML."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=timeout, allow_redirects=True)
    except requests.exceptions.RequestException:
        return None
    if resp.status_code != 200 or "html" not in resp.headers.get("Content-Type", ""):
        return None
    # Drop the default port some servers put in redirect URLs (https://x.com:443/).
    return resp.url.replace(":443/", "/", 1), BeautifulSoup(resp.text, "html.parser")


def _page_text(soup: BeautifulSoup) -> str:
    title = soup.title.get_text(strip=True) if soup.title else ""
    meta = soup.find("meta", attrs={"name": "description"}) or soup.find(
        "meta", attrs={"property": "og:description"}
    )
    meta_desc = meta.get("content", "").strip() if meta else ""

    for tag in soup(["script", "style", "noscript", "svg", "nav", "footer", "form", "iframe"]):
        tag.decompose()
    body = soup.get_text(separator=" ", strip=True)
    body = re.sub(r"\s+", " ", body)

    parts = [p for p in (f"Title: {title}" if title else "",
                         f"Meta description: {meta_desc}" if meta_desc else "",
                         body) if p]
    return "\n".join(parts)[:MAX_CHARS_PER_PAGE]


def _pick_subpages(base_url: str, soup: BeautifulSoup) -> list:
    """Same-site links whose path matches PAGE_KEYWORDS, best matches first."""
    host = urlparse(base_url).netloc.removeprefix("www.")
    ranked = {}
    for a in soup.find_all("a", href=True):
        url = urljoin(base_url, a["href"]).split("#")[0].split("?")[0].rstrip("/")
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            continue
        if parsed.netloc.removeprefix("www.") != host:
            continue
        path = parsed.path.lower()
        if path in ("", "/") or path.count("/") > 2:
            continue
        for rank, keyword in enumerate(PAGE_KEYWORDS):
            if keyword in path:
                if url not in ranked or rank < ranked[url]:
                    ranked[url] = rank
                break
    # Take the shortest URL per keyword first (e.g. /customers over
    # /customers/acme) so we cover different page types, then fill any
    # remaining slots with the rest.
    ordered = sorted(ranked, key=lambda u: (ranked[u], len(u)))
    picked, seen_ranks = [], set()
    for url in ordered:
        if ranked[url] not in seen_ranks:
            picked.append(url)
            seen_ranks.add(ranked[url])
    picked += [u for u in ordered if u not in picked]
    return picked[:MAX_EXTRA_PAGES]


def _is_ai_path(path: str) -> bool:
    path = path.lower()
    if any(phrase in path for phrase in AI_PHRASES):
        return True
    return bool(AI_WORDS.intersection(re.split(r"[/\-_.]+", path)))


def _same_site_urls(base_url: str, hrefs) -> list:
    """Normalizes hrefs to absolute same-site URLs (no query/fragment), deduped."""
    host = urlparse(base_url).netloc.removeprefix("www.")
    urls = []
    for href in hrefs:
        url = urljoin(base_url, href.strip()).split("#")[0].split("?")[0].rstrip("/")
        parsed = urlparse(url)
        if parsed.scheme in ("http", "https") and parsed.netloc.removeprefix("www.") == host:
            if url not in urls:
                urls.append(url)
    return urls


def _pick_ai_pages(urls: list, exclude: list) -> list:
    """AI-related URLs, product-style pages and short paths first."""
    candidates = [u for u in urls if u not in exclude and _is_ai_path(urlparse(u).path)]
    candidates.sort(key=lambda u: (
        urlparse(u).path.lower().startswith(ARTICLE_PATHS),
        urlparse(u).path.count("/"),
        len(u),
    ))
    return candidates[:MAX_AI_PAGES]


def _sitemap_urls(home_url: str, remaining) -> list:
    """Page URLs listed in /sitemap.xml (following one level of sitemap index).
    `remaining` returns the seconds left in the scrape's budget.
    """
    def locs(url):
        if remaining() < 4:
            return [], False
        try:
            resp = requests.get(url, headers=HEADERS, timeout=min(5, remaining() - 2))
        except requests.exceptions.RequestException:
            return [], False
        if resp.status_code != 200:
            return [], False
        return re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", resp.text), "<sitemapindex" in resp.text

    root = urljoin(home_url + "/", "/sitemap.xml")
    found, is_index = locs(root)
    if not is_index:
        return found
    pages = []
    # Child sitemaps for pages come before huge blog/product-catalog ones.
    children = sorted(found, key=lambda u: ("page" not in u.lower(), len(u)))[:3]
    for child in children:
        pages += locs(child)[0]
    return pages


def scrape(domain: str, budget_seconds: float = 30) -> dict:
    """Returns {"url": homepage_url, "pages": {url: text}, "ai_pages": [urls]}.
    Raises WebsiteError.

    Stops fetching once `budget_seconds` is used up and returns what it has, so
    a slow site can't eat the rest of the request's time.
    """
    deadline = time.time() + budget_seconds

    def remaining():
        return deadline - time.time()

    domain = domain.strip().removeprefix("https://").removeprefix("http://").strip("/")
    if not domain:
        raise WebsiteError("No company domain")

    home = None
    for url in (f"https://{domain}", f"https://www.{domain}", f"http://{domain}"):
        if remaining() < 2:
            break
        home = _fetch(url, timeout=min(10, remaining()))
        if home:
            break
    if home is None:
        raise WebsiteError(f"Could not load {domain}")
    home_url, home_soup = home

    subpages = _pick_subpages(home_url, home_soup)
    home_links = _same_site_urls(home_url, [a["href"] for a in home_soup.find_all("a", href=True)])
    ai_pages = _pick_ai_pages(home_links, subpages)
    if len(ai_pages) < MAX_AI_PAGES:
        sitemap = _same_site_urls(home_url, _sitemap_urls(home_url, remaining))
        ai_pages += _pick_ai_pages(sitemap, subpages + ai_pages)[: MAX_AI_PAGES - len(ai_pages)]

    pages = {home_url: _page_text(home_soup)}
    fetched_ai_pages = []
    for url in ai_pages + subpages:
        if remaining() < 2:
            break
        fetched = _fetch(url, timeout=min(10, remaining()))
        if fetched:
            pages[url] = _page_text(fetched[1])
            if _is_ai_path(urlparse(url).path):
                fetched_ai_pages.append(url)

    if sum(len(t) for t in pages.values()) < MIN_TOTAL_CHARS:
        raise WebsiteError(f"{domain} returned too little readable text")
    return {"url": home_url, "pages": pages, "ai_pages": fetched_ai_pages}


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: python company_website.py <domain>")
    print(json.dumps(scrape(sys.argv[1]), indent=2))
