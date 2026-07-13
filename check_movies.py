"""
Cineplex BD ticket-sale watcher.

How it works:
1. Opens https://www.cineplexbd.com/show-time in a headless browser.
2. Captures the response from the site's own API call to
   /api/v1/movie-show-time — this is the list of movies that
   currently have active showtimes, i.e. tickets on sale right now.
3. Compares the set of on-sale movie slugs against the previous run
   (state.json). Any slug that's new gets posted to Discord.
4. On the very first run (no prior state), it just saves a baseline
   silently instead of notifying about every movie already on sale.

Run with --diagnostic to just print what it finds, without notifying
or touching state.json.
"""

import asyncio
import json
import os
import sys
from datetime import datetime, timezone

import requests
from playwright.async_api import async_playwright

TARGET_URL = "https://www.cineplexbd.com/show-time"
SHOWTIME_API_HINT = "movie-show-time"

STATE_FILE = "state.json"
DEBUG_FILE = "debug_capture.json"
WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")

captured = []


async def on_response(response):
    try:
        if SHOWTIME_API_HINT not in response.url.lower():
            return
        ct = response.headers.get("content-type", "")
        if "json" not in ct:
            return
        body = await response.json()
        captured.append({"url": response.url, "body": body})
    except Exception:
        pass


async def fetch_page_data():
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        page.on("response", lambda r: asyncio.ensure_future(on_response(r)))
        await page.goto(TARGET_URL, wait_until="networkidle", timeout=60000)
        await page.wait_for_timeout(4000)
        await browser.close()


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def notify_discord(title):
    if not WEBHOOK_URL:
        print(f"[no webhook configured] would have notified: {title}")
        return
    content = (
        f"\U0001F3AC **{title}** just went on sale on Cineplex BD!\n"
        f"https://www.cineplexbd.com/show-time"
    )
    try:
        requests.post(WEBHOOK_URL, json={"content": content}, timeout=15)
    except Exception as e:
        print(f"Discord post failed: {e}")


def extract_movies(payload):
    """Walk the payload, collecting any dict entry that looks like a
    movie-show-time record (has both a slug and a title)."""
    results = {}

    def walk(node):
        if isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, dict):
            if "slug" in node and "title" in node and node["slug"]:
                results[node["slug"]] = node["title"]
            for v in node.values():
                walk(v)

    walk(payload)
    return results


async def main():
    if "--test-notify" in sys.argv:
        print("Sending test notification to Discord...")
        notify_discord("Test Movie (this is a test, not a real listing)")
        print("Done. Check your Discord channel.")
        return

    await fetch_page_data()

    with open(DEBUG_FILE, "w") as f:
        json.dump(captured, f, indent=2, ensure_ascii=False)

    print(f"Captured {len(captured)} response(s) from the show-time API.")

    current_on_sale = {}
    for c in captured:
        current_on_sale.update(extract_movies(c["body"]))

    print(f"\nCurrently on sale ({len(current_on_sale)} movies):")
    for slug, title in current_on_sale.items():
        print(f"   {title}  (slug: {slug})")

    if "--diagnostic" in sys.argv:
        print("\nDiagnostic mode: not sending notifications or updating state.")
        return

    state = load_state()
    is_first_run = "on_sale_slugs" not in state
    prev_on_sale = state.get("on_sale_slugs", {})

    if is_first_run:
        print("\nFirst run: seeding baseline state, no notifications sent.")
    else:
        new_slugs = set(current_on_sale) - set(prev_on_sale)
        for slug in new_slugs:
            print(f"NEW on sale: {current_on_sale[slug]}")
            notify_discord(current_on_sale[slug])
        if not new_slugs:
            print("\nNo new movies on sale since last check.")

    state["on_sale_slugs"] = current_on_sale
    state["last_checked"] = datetime.now(timezone.utc).isoformat()
    save_state(state)


if __name__ == "__main__":
    asyncio.run(main())
