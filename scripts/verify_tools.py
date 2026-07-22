"""
verify_tools.py

Weekly verification script for the real estate tool database (tools.json).

What it does:
1. Loads tools.json
2. For each tool, fetches its source_url with plain requests (free, no API cost)
3. Sends the extracted page text to Claude Haiku (cheap, no web-search tool) and asks
   it to verify current price/status against the recorded entry
4. Classifies the result as "minor" (safe to auto-update) or "flag" (needs human review)
5. Writes:
   - tools.json      -> updated in place with any "minor" changes applied
   - flags.json      -> structured list of anything that needs manual review
   - flags.md        -> human-readable version of flags.json, used as the PR body

Run from the repo root:
    python scripts/verify_tools.py

Requires:
    pip install requests beautifulsoup4 anthropic
    ANTHROPIC_API_KEY environment variable set
"""

import json
import os
import sys
import time
from datetime import date

import requests
from bs4 import BeautifulSoup
from anthropic import Anthropic

TOOLS_PATH = "tools.json"
FLAGS_JSON_PATH = "flags.json"
FLAGS_MD_PATH = "flags.md"

MODEL = "claude-haiku-4-5-20251001"
MAX_PAGE_CHARS = 4000
REQUEST_TIMEOUT = 15
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; ToolDatabaseVerifier/1.0)"
}


def load_tools():
    with open(TOOLS_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_tools(data):
    with open(TOOLS_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def fetch_page_text(url):
    try:
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        if resp.status_code != 200:
            return None, f"HTTP {resp.status_code}"
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script", "style", "nav", "footer"]):
            tag.decompose()
        text = soup.get_text(separator=" ", strip=True)
        return text[:MAX_PAGE_CHARS], None
    except Exception as e:
        return None, str(e)


def ask_claude(client, tool, page_text):
    prompt = f"""You are verifying pricing data for a real estate software tool.

TOOL NAME: {tool['name']}
RECORDED COST: {tool['cost']}
RECORDED STATUS: active

PAGE TEXT (scraped just now, may be truncated):
{page_text}

Respond with ONLY a JSON object, no markdown fences, no preamble, in this exact shape:
{{
  "current_price_text": "<short price description as found on the page, or 'not found'>",
  "status": "active" | "discontinued" | "renamed_or_unclear" | "not_found",
  "matches_recorded_cost": true or false,
  "confidence": "high" | "medium" | "low",
  "notes": "<one sentence on any discrepancy or context>"
}}"""

    resp = client.messages.create(
        model=MODEL,
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = resp.content[0].text.strip()
    raw = raw.replace("```json", "").replace("```", "").strip()
    return json.loads(raw)


def classify(verdict):
    """Return 'minor', 'no_change', or 'flag'."""
    if verdict["status"] != "active":
        return "flag"
    if verdict["confidence"] == "low":
        return "flag"
    if verdict["matches_recorded_cost"]:
        return "no_change"
    return "minor"


def main():
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ANTHROPIC_API_KEY not set", file=sys.stderr)
        sys.exit(1)

    client = Anthropic(api_key=api_key)
    data = load_tools()
    today = date.today().isoformat()

    flags = []
    updated_count = 0

    for tool in data["tools"]:
        page_text, fetch_error = fetch_page_text(tool["source_url"])

        if fetch_error:
            flags.append({
                "tool": tool["name"],
                "reason": f"Could not fetch source_url: {fetch_error}",
                "source_url": tool["source_url"],
                "recorded_cost": tool["cost"],
            })
            continue

        try:
            verdict = ask_claude(client, tool, page_text)
        except Exception as e:
            flags.append({
                "tool": tool["name"],
                "reason": f"Claude verification call failed: {e}",
                "source_url": tool["source_url"],
                "recorded_cost": tool["cost"],
            })
            continue

        action = classify(verdict)

        if action == "no_change":
            tool["last_verified"] = today

        elif action == "minor":
            flags_note = f"Auto-updated from '{tool['cost']}' to reflect: {verdict['current_price_text']}"
            tool["cost"] = verdict["current_price_text"]
            tool["last_verified"] = today
            updated_count += 1
            print(f"[auto-updated] {tool['name']}: {flags_note}")

        else:  # flag
            flags.append({
                "tool": tool["name"],
                "reason": verdict.get("notes", "Needs manual review"),
                "status_found": verdict["status"],
                "confidence": verdict["confidence"],
                "current_price_text": verdict.get("current_price_text"),
                "recorded_cost": tool["cost"],
                "source_url": tool["source_url"],
            })

        time.sleep(1)  # be polite to source sites and avoid rate limits

    save_tools(data)

    with open(FLAGS_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(flags, f, indent=2)

    with open(FLAGS_MD_PATH, "w", encoding="utf-8") as f:
        if not flags:
            f.write("No items need manual review this week.\n")
        else:
            f.write("## Tool database items needing manual review\n\n")
            for item in flags:
                f.write(f"### {item['tool']}\n")
                f.write(f"- Reason: {item['reason']}\n")
                f.write(f"- Recorded cost on file: {item['recorded_cost']}\n")
                if "current_price_text" in item and item["current_price_text"]:
                    f.write(f"- Possible new value found: {item['current_price_text']}\n")
                f.write(f"- Source: {item['source_url']}\n\n")

    print(f"Done. {updated_count} auto-updated, {len(flags)} flagged for review.")


if __name__ == "__main__":
    main()
