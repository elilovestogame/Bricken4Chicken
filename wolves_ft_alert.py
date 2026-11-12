#!/usr/bin/env python3
"""
Alerts you (via ntfy.sh push notification) when the Timberwolves' opponent
misses two consecutive free throws in the same trip to the line during the
4th quarter or overtime, at Wolves HOME games.

Data comes from ESPN's unofficial public API (the NBA's own feed blocks
GitHub's servers with a 403).

Designed for GitHub Actions:
  - Exits right away if the Wolves have no home game today.
  - If tip-off is soon, waits for it; if far off, exits (a later run catches it).
  - Watches the game, then exits when it ends.

Environment variables:
  NTFY_TOPIC         your private ntfy topic name
  TEST_NOTIFICATION  "true" -> send one test notification and exit
  REPLAY_DATE        YYYYMMDD -> replay that day's Wolves home game from the
                     saved play-by-play, printing every opponent free throw
                     and sending alerts for anything that would have fired
"""

import os
import re
import sys
import time
from datetime import datetime, timezone

import requests

NTFY_TOPIC = os.environ.get("NTFY_TOPIC") or "change-me-to-something-unique"
TEAM = "MIN"
POLL_SECONDS = 15
MAX_WAIT_FOR_TIPOFF = 3 * 3600      # skip if tip-off is further away than this
MAX_RUNTIME = 5 * 3600 + 45 * 60    # stay under GitHub's 6-hour job limit
START = time.time()

SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"
SUMMARY_URLS = [
    "https://site.web.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event={}",
    "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event={}",
]
HEADERS = {"User-Agent": "Mozilla/5.0"}


def get_json(url):
    r = requests.get(url, headers=HEADERS, timeout=15)
    r.raise_for_status()
    return r.json()


def get_summary(game_id):
    last_err = None
    for url in SUMMARY_URLS:
        try:
            return get_json(url.format(game_id))
        except requests.RequestException as e:
            last_err = e
    raise last_err


def notify(title, message):
    print(f"ALERT: {title} - {message}", flush=True)
    try:
        requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=message.encode("utf-8"),
            headers={"Title": title},
            timeout=10,
        )
    except requests.RequestException as e:
        print("Notification failed:", e, flush=True)


def trip_attempt(text):
    """Return N from an 'N of M' free throw label, or None if not found."""
    m = re.search(r"(\d)\s+of\s+(\d)", text)
    return int(m.group(1)) if m else None


def find_game(date=None):
    """Return the Wolves HOME game on the scoreboard as a dict, or None."""
    url = SCOREBOARD + (f"?dates={date}" if date else "")
    for e in get_json(url).get("events", []):
        comp = e["competitions"][0]
        teams = {c["homeAway"]: c["team"] for c in comp["competitors"]}
        if teams["home"]["abbreviation"] == TEAM:
            return {
                "id": e["id"],
                "opp": teams["away"]["abbreviation"],
                "opp_id": str(teams["away"]["id"]),
                "status": e["status"]["type"]["state"],  # pre / in / post
                "tipoff": e.get("date"),
            }
    return None


def process_plays(plays, opp_id, opp, st, alert, tag=""):
    """Feed plays through the miss-streak logic. st holds seen ids + streak."""
    plays = sorted(plays, key=lambda p: int(p.get("sequenceNumber") or 0))
    for p in plays:
        pid = p.get("id")
        if pid in st["seen"]:
            continue
        st["seen"].add(pid)

        text = p.get("text", "")
        if not re.search(r"free throw", text, re.I):
            continue
        missed = bool(re.search(r"\bmisses\b", text, re.I))
        made = bool(re.search(r"\bmakes\b", text, re.I))
        if not (missed or made):
            continue
        if str(p.get("team", {}).get("id")) != opp_id:
            continue

        period = p.get("period", {}).get("number", 0)
        clock = p.get("clock", {}).get("displayValue", "")
        print(f"  {opp} FT | period {period} {clock} | {text}", flush=True)

        # A new trip to the line starts at "1 of N"
        if trip_attempt(text) == 1:
            st["streak"] = 0

        if missed:
            st["streak"] += 1
            if st["streak"] >= 2:
                if period >= 4 and alert:
                    label = "Q4" if period == 4 else f"OT{period - 4}"
                    notify(
                        f"{tag}{opp} missed 2 straight FTs",
                        f"{label} {clock}: {text}",
                    )
                st["streak"] = 0  # don't re-alert on a 3rd miss in a row
        else:
            st["streak"] = 0


def wait_for_tipoff(game):
    """Return the live game dict, or None if we should exit."""
    if game["status"] == "pre" and game["tipoff"]:
        tip = datetime.fromisoformat(game["tipoff"].replace("Z", "+00:00"))
        secs = (tip - datetime.now(timezone.utc)).total_seconds()
        if secs > MAX_WAIT_FOR_TIPOFF:
            print(f"Tip-off is {secs / 3600:.1f}h away; exiting.", flush=True)
            return None
        print(f"Tip-off in {max(secs, 0) / 60:.0f} min; waiting.", flush=True)

    while time.time() - START < MAX_RUNTIME:
        try:
            game = find_game()
        except requests.RequestException as e:
            print("Network error:", e, flush=True)
            time.sleep(30)
            continue
        if not game or game["status"] == "post":
            return None
        if game["status"] == "in":
            return game
        time.sleep(30)
    return None


def watch(game):
    print(f"Watching game {game['id']} vs {game['opp']}", flush=True)
    st = {"seen": set(), "streak": 0}
    first_pass = True  # replay history silently if we join mid-game

    while time.time() - START < MAX_RUNTIME:
        try:
            summary = get_summary(game["id"])
            process_plays(summary.get("plays", []), game["opp_id"], game["opp"],
                          st, alert=not first_pass)
            first_pass = False

            state = None
            try:
                state = summary["header"]["competitions"][0]["status"]["type"]["state"]
            except (KeyError, IndexError, TypeError):
                pass
            if state is None:
                g = find_game()
                state = g["status"] if g else "post"
            if state == "post":
                print("Game over.", flush=True)
                return
        except requests.RequestException as e:
            print("Network error:", e, flush=True)

        time.sleep(POLL_SECONDS)

    print("Hit max runtime; exiting.", flush=True)


def replay(date):
    game = find_game(date)
    if not game:
        print(f"No Wolves home game found for {date}.")
        return
    print(f"Replaying game {game['id']} vs {game['opp']} ({date})", flush=True)
    summary = get_summary(game["id"])
    plays = summary.get("plays", [])
    print(f"{len(plays)} plays in feed.", flush=True)
    st = {"seen": set(), "streak": 0}
    process_plays(plays, game["opp_id"], game["opp"], st, alert=True, tag="REPLAY: ")
    print("Replay done.", flush=True)


def main():
    if os.environ.get("TEST_NOTIFICATION") == "true":
        if NTFY_TOPIC == "change-me-to-something-unique":
            print("NTFY_TOPIC secret is not set; no test sent.")
            return
        notify("Wolves FT alert test", "If you see this, ntfy is set up correctly.")
        return

    try:
        date = os.environ.get("REPLAY_DATE", "").strip()
        if date:
            replay(date)
            return

        game = find_game()
        if not game:
            print("No Wolves home game today.")
            return
        if game["status"] == "post":
            print("Game already over.")
            return
        live = wait_for_tipoff(game)
        if live:
            watch(live)
    except requests.RequestException as e:
        print("Could not reach ESPN:", e, flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
