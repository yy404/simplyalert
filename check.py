import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

HOST = os.environ["TARGET_HOST"]
SITE_ID = os.environ["TARGET_SITE"]
ACTIVITY_IDS = os.environ["TARGET_ACTIVITIES"]
NTFY_TOPIC = os.environ["NTFY_TOPIC"]
WEEKDAYS = {int(d) for d in os.getenv("WEEKDAYS", "5,6").split(",")}
EARLIEST_HOUR = int(os.getenv("EARLIEST_HOUR", "0"))
LATEST_HOUR = int(os.getenv("LATEST_HOUR", "24"))
TZ = ZoneInfo(os.getenv("LOCAL_TZ", "Europe/London"))
STATE_FILE = "state.json"

BASE = f"https://{HOST}"
PAGE_URL = f"{BASE}/book"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36")


def launch(p):
    try:
        return p.chromium.launch(channel="chrome")
    except Exception:
        return p.chromium.launch()


def fetch():
    from playwright.sync_api import sync_playwright

    now = datetime.now(timezone.utc)
    params = {
        "webBookableOnly": "true",
        "siteIds": SITE_ID,
        "activityIds": ACTIVITY_IDS,
        "dateFrom": now.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "dateTo": (now + timedelta(days=8)).strftime("%Y-%m-%dT23:59:59.999Z"),
    }
    with sync_playwright() as p:
        browser = launch(p)
        ctx = browser.new_context(user_agent=UA, locale="en-GB")
        page = ctx.new_page()
        page.goto(PAGE_URL, wait_until="networkidle", timeout=60000)
        for _ in range(30):
            if any(c["name"] == "Jwt" for c in ctx.cookies()):
                break
            page.wait_for_timeout(500)
        r = ctx.request.get(
            f"{BASE}/api/availability/V2/sessions", params=params,
            headers={"referer": PAGE_URL, "x-use-sso": "1",
                     "accept": "application/json, text/plain, */*"},
            timeout=30000)
        if not r.ok:
            raise RuntimeError(f"HTTP {r.status}")
        data = r.json()
        browser.close()
    return data


def free_slots(data):
    out = {}
    for act in data:
        mins = "60" if "60" in act.get("name", "") else "40"
        for loc in act.get("locations", []):
            court = loc["locationNameToDisplay"].split()[-1]
            for s in loc.get("slots", []):
                if s.get("status") != "Available" or s.get("isBeforeTimetableSlotBookablePeriod"):
                    continue
                start = datetime.fromisoformat(s["startTime"].replace("Z", "+00:00")).astimezone(TZ)
                if start.weekday() not in WEEKDAYS or not (EARLIEST_HOUR <= start.hour < LATEST_HOUR):
                    continue
                raw = f"{s['startTime']}|{loc['locationNameToDisplay']}|{mins}"
                key = hashlib.sha256(raw.encode()).hexdigest()[:16]
                out[key] = f"{start:%a %d/%m %H:%M} - {mins}min - Court {court}"
    return out


def notify(lines):
    requests.post(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data="\n".join(sorted(lines)).encode(),
        headers={"Title": "Slots available", "Click": PAGE_URL, "Priority": "high"},
        timeout=20,
    )


def main():
    try:
        with open(STATE_FILE) as f:
            seen = set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        seen = set()

    free = free_slots(fetch())
    new = {k: v for k, v in free.items() if k not in seen}
    print(f"free={len(free)} new={len(new)}")
    if new:
        notify(new.values())

    with open(STATE_FILE, "w") as f:
        json.dump(sorted(free), f)


if __name__ == "__main__":
    main()
