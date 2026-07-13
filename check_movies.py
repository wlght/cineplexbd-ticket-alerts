"""
Cineplex BD ticket-sale watcher.

How it works:
1. Opens https://www.cineplexbd.com/movie-list in a headless browser.
2. Listens to every network response the page makes, and captures any
   JSON response whose URL looks movie/show/ticket related. This finds
   the site's real data API automatically, no manual DevTools needed.
3. Walks that JSON looking for movie-like entries (something with a
   title/name field) and guesses an "on sale" status from any
   status-like field.
4. Compares against state.json (from the last run). If a movie flips
   from "not on sale" to "on sale", posts a message to a Discord webhook.

Run with --diagnostic to just print what it finds, without notifying
or touching state.json. Use this first to sanity-check detection.
"""

import asyncio
import json
import os
import sys
from datetime import datetime, timezone

import requests
from playwright.async_api import async_playwright

TARGET_URL = "https://www.cineplexbd.com/movie-list"
STATE_FILE = "state.json"
DEBUG_FILE = "debug_capture.json"
WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")

KEYWORDS = ["movie", "show", "film", "ticket"]

captured = []


async def on_response(response):
    try:
        ct = response.headers.get("content-type", "")
        if "json" not in ct:
            return
        url_l = response.url.lower()
        if not any(k in url_l for k in KEYWORDS):
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


def notify_discord(title, extra=""):
    if not WEBHOOK_URL:
        print(f"[no webhook configured] would have notified: {title}")
        return
    content = f"\U0001F3AC **{title}** just went on sale on Cineplex BD!\n{TARGET_URL}"
    if extra:
        content += f"\n{extra}"
    try:
        requests.post(WEBHOOK_URL, json={"content": content}, timeout=15)
    except Exception as e:
        print(f"Discord post failed: {e}")


def guess_movies(payload):
    """Heuristic extractor: finds dict entries that look like movies."""
    results = []

    def walk(node):
        if isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, dict):
            keys = {k.lower(): k for k in node.keys()}
            name_key = next(
                (keys[k] for k in keys if k in ("title", "name", "moviename", "movie_name")),
                None,
            )
            if name_key:
                title = node[name_key]
                status_key = next(
                    (
                        keys[k]
                        for k in keys
                        if k
                        in (
                            "status",
                            "bookingstatus",
                            "isbookingopen",
                            "ticketstatus",
                            "showstatus",
                            "booking_open",
                        )
                    ),
                    None,
                )
                raw_status = node.get(status_key) if status_key else None
                on_sale = None
                if isinstance(raw_status, bool):
                    on_sale = raw_status
                elif isinstance(raw_status, str):
                    on_sale = any(
                        w in raw_status.lower()
                        for w in ("open", "now showing", "book", "sale", "available")
                    )
                results.append({"title": title, "raw_status": raw_status, "on_sale": on_sale})
            for v in node.values():
                walk(v)

    walk(payload)
    return results


async def main():
    await fetch_page_data()

    with open(DEBUG_FILE, "w") as f:
        json.dump(captured, f, indent=2, ensure_ascii=False)

    print(f"Captured {len(captured)} JSON response(s) matching keywords:")
    for c in captured:
        print(" -", c["url"])

    all_movies = []
    for c in captured:
        all_movies.extend(guess_movies(c["body"]))

    print(f"\nHeuristic detected {len(all_movies)} movie-like entries:")
    for m in all_movies[:30]:
        print("  ", m)

    if "--diagnostic" in sys.argv:
        print("\nDiagnostic mode: not sending notifications or updating state.")
        print(f"Full capture written to {DEBUG_FILE} (check the Action logs/artifact).")
        return

    state = load_state()
    for m in all_movies:
        title = str(m["title"]).strip()
        if not title:
            continue
        prev_on_sale = state.get(title, {}).get("on_sale")
        curr_on_sale = m["on_sale"]
        if curr_on_sale and not prev_on_sale:
            notify_discord(title, extra=f"(status: {m['raw_status']})")
        state[title] = {
            "on_sale": curr_on_sale,
            "raw_status": m["raw_status"],
            "last_checked": datetime.now(timezone.utc).isoformat(),
        }

    save_state(state)


if __name__ == "__main__":
    asyncio.run(main())
