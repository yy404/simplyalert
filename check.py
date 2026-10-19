import hashlib
import json
import os
import subprocess
import time as clock
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

HOST = os.environ["TARGET_HOST"]
SITE_ID = os.environ["TARGET_SITE"]
ACTIVITY_IDS = os.environ["TARGET_ACTIVITIES"]
NTFY_TOPIC = os.environ["NTFY_TOPIC"]
RULES = {}
for part in os.getenv("RULES", "5@00:00,6@00:00").split(","):
    day, t = part.strip().split("@")
    h, m = t.split(":")
    RULES[int(day)] = int(h) * 60 + int(m)
DAYS = int(os.getenv("DAYS", "14"))
TZ = ZoneInfo(os.getenv("LOCAL_TZ", "Europe/London"))
STATE_FILE = "state.json"
LOOP_MINUTES = int(os.getenv("LOOP_MINUTES", "0"))
INTERVAL = int(os.getenv("INTERVAL", "300"))
QUIET_START, QUIET_END = (int(x) for x in os.getenv("QUIET_HOURS", "1-7").split("-"))

BASE = f"https://{HOST}"
PAGE_URL = f"{BASE}/book"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36")


def launch(p):
    try:
        return p.chromium.launch(channel="chrome")
    except Exception:
        return p.chromium.launch()


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def fetch():
    from playwright.sync_api import sync_playwright

    now = datetime.now(timezone.utc)
    end = datetime.combine((now + timedelta(days=DAYS)).date(), time(23, 59, 59), tzinfo=timezone.utc)
    data = []
    with sync_playwright() as p:
        browser = launch(p)
        ctx = browser.new_context(user_agent=UA, locale="en-GB")
        page = ctx.new_page()
        page.goto(PAGE_URL, wait_until="networkidle", timeout=60000)
        for _ in range(30):
            if any(c["name"] == "Jwt" for c in ctx.cookies()):
                break
            page.wait_for_timeout(500)
        start = now
        while start < end:
            stop = min(start + timedelta(days=7), end)
            params = {
                "webBookableOnly": "true",
                "siteIds": SITE_ID,
                "activityIds": ACTIVITY_IDS,
                "dateFrom": iso(start),
                "dateTo": iso(stop),
            }
            r = ctx.request.get(
                f"{BASE}/api/availability/V2/sessions", params=params,
                headers={"referer": PAGE_URL, "x-use-sso": "1",
                         "accept": "application/json, text/plain, */*"},
                timeout=30000)
            if not r.ok:
                raise RuntimeError(f"HTTP {r.status}")
            data += r.json()
            start = stop
        browser.close()
    return data


def is_free(s):
    return s.get("status") == "Available"


def free_slots(data):
    out = {}
    for act in data:
        mins = "60" if "60" in act.get("name", "") else "40"
        for loc in act.get("locations", []):
            court = loc["locationNameToDisplay"].split()[-1]
            for s in loc.get("slots", []):
                if not is_free(s):
                    continue
                start = datetime.fromisoformat(s["startTime"].replace("Z", "+00:00")).astimezone(TZ)
                earliest = RULES.get(start.weekday())
                if earliest is None or start.hour * 60 + start.minute < earliest:
                    continue
                raw = f"{s['startTime']}|{loc['locationNameToDisplay']}|{mins}"
                key = hashlib.sha256(raw.encode()).hexdigest()[:16]
                out[key] = f"{start:%a %d/%m %H:%M} - {mins}min - Court {court}"
    return out


def pre_stats(data):
    total = free = 0
    for act in data:
        for loc in act.get("locations", []):
            for s in loc.get("slots", []):
                if s.get("isBeforeTimetableSlotBookablePeriod"):
                    total += 1
                    free += is_free(s)
    return total, free


def notify(lines):
    requests.post(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data="\n".join(sorted(lines)).encode(),
        headers={"Title": "Slots available", "Click": PAGE_URL, "Priority": "high"},
        timeout=20,
    )


def load_state():
    try:
        with open(STATE_FILE) as f:
            return set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def commit():
    subprocess.run(["git", "add", STATE_FILE], check=True)
    if subprocess.run(["git", "diff", "--cached", "--quiet"]).returncode == 0:
        return
    subprocess.run(["git", "commit", "-qm", "update"], check=True)
    subprocess.run(["git", "pull", "--rebase", "-q"], check=True)
    subprocess.run(["git", "push", "-q"], check=True)


def check_once():
    seen = load_state()
    data = fetch()
    free = free_slots(data)
    new = {k: v for k, v in free.items() if k not in seen}
    pre_total, pre_free = pre_stats(data)
    print(f"{datetime.now(TZ):%H:%M} free={len(free)} new={len(new)} "
          f"pre_total={pre_total} pre_free={pre_free}", flush=True)
    if new:
        notify(new.values())
    if set(free) != seen:
        with open(STATE_FILE, "w") as f:
            json.dump(sorted(free), f)
        commit()


def main():
    deadline = clock.monotonic() + LOOP_MINUTES * 60
    while True:
        if not QUIET_START <= datetime.now(TZ).hour < QUIET_END:
            try:
                check_once()
            except Exception as e:
                print(f"{datetime.now(TZ):%H:%M} error {type(e).__name__}", flush=True)
        if clock.monotonic() + INTERVAL > deadline:
            break
        clock.sleep(INTERVAL)


if __name__ == "__main__":
    main()
