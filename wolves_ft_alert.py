#!/usr/bin/env python3
"""
Alerts you (via ntfy.sh push notification) when the Timberwolves' opponent
misses two consecutive free throws in the same trip to the line during the
4th quarter or overtime.

Designed to run on GitHub Actions, but works anywhere:
  - Exits right away if the Wolves have no HOME game today.
  - If tip-off is soon, waits for it; if it's far off, exits (a later
    scheduled run will pick it up).
  - Watches the game, then exits when it ends.

Set the NTFY_TOPIC environment variable to your private ntfy topic name.
Uses the NBA's unofficial public live-data feeds, which are undocumented and
could change.
"""

import os
import re
import time
from datetime import datetime, timezone

import requests

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "change-me-to-something-unique")
TEAM = "MIN"
POLL_SECONDS = 15
MAX_WAIT_FOR_TIPOFF = 3 * 3600      # skip if tip-off is further away than this
MAX_RUNTIME = 5 * 3600 + 45 * 60    # stay under GitHub's 6-hour job limit
START = time.time()

SCOREBOARD = "https://cdn.nba.com/static/json/liveData/scoreboard/todaysScoreboard_00.json"
PBP = "https://cdn.nba.com/static/json/liveData/playbyplay/playbyplay_{}.json"
HEADERS = {"User-Agent": "Mozilla/5.0"}


def get_json(url):
    r = requests.get(url, headers=HEADERS, timeout=10)
    r.raise_for_status()
    return r.json()


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


def trip_attempt(action):
    """Return N from a 'N of M' free throw label, or None if not found."""
    text = f"{action.get('subType', '')} {action.get('description', '')}"
    m = re.search(r"(\d)\s+of\s+(\d)", text)
    return int(m.group(1)) if m else None


def find_game():
    """Return today's Wolves game as a dict, or None."""
    for g in get_json(SCOREBOARD)["scoreboard"]["games"]:
        home, away = g["homeTeam"]["teamTricode"], g["awayTeam"]["teamTricode"]
        if home == TEAM:  # home games only
            return {
                "id": g["gameId"],
                "opp": away,
                "status": g["gameStatus"],  # 1 = not started, 2 = live, 3 = final
                "tipoff": g.get("gameTimeUTC"),
            }
    return None


def wait_for_tipoff(game):
    """Return the live game dict, or None if we should exit."""
    if game["status"] == 1 and game["tipoff"]:
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
        if not game or game["status"] == 3:
            return None
        if game["status"] == 2:
            return game
        time.sleep(30)
    return None


def watch(game_id, opp):
    print(f"Watching game {game_id} vs {opp}", flush=True)
    seen = set()
    streak = 0  # consecutive missed opponent FTs within one trip
    first_pass = True  # replay history silently if we join mid-game

    while time.time() - START < MAX_RUNTIME:
        try:
            actions = get_json(PBP.format(game_id))["game"]["actions"]

            for a in actions:
                num = a["actionNumber"]
                if num in seen:
                    continue
                seen.add(num)

                if a.get("actionType") != "freethrow" or a.get("teamTricode") != opp:
                    continue

                # A new trip to the line starts at "1 of N"
                if trip_attempt(a) == 1:
                    streak = 0

                if a["shotResult"] == "Missed":
                    streak += 1
                    if streak >= 2 and a["period"] >= 4 and not first_pass:
                        label = "Q4" if a["period"] == 4 else f"OT{a['period'] - 4}"
                        notify(
                            f"{opp} missed 2 straight FTs",
                            f"{label} {a.get('clock', '')}: "
                            f"{a.get('personName', '')} just missed. "
                            f"{a.get('description', '')}",
                        )
                    if streak >= 2:
                        streak = 0  # don't re-alert on a 3rd miss in a row
                else:
                    streak = 0

            first_pass = False

            game = find_game()
            if not game or game["status"] == 3:
                print("Game over.", flush=True)
                return
        except requests.RequestException as e:
            print("Network error:", e, flush=True)

        time.sleep(POLL_SECONDS)

    print("Hit max runtime; exiting.", flush=True)


def main():
    game = find_game()
    if not game:
        print("No Wolves home game today.")
        return
    if game["status"] == 3:
        print("Game already over.")
        return
    live = wait_for_tipoff(game)
    if live:
        watch(live["id"], live["opp"])


if __name__ == "__main__":
    main()
