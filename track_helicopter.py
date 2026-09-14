#!/usr/bin/env python3
"""
VT-JJL trip tracker -> Bluesky poster.

Runs once per invocation (meant to be triggered on a schedule, e.g. every
5 minutes by GitHub Actions). Each run:

  1. Loads state.json (what we last knew about the aircraft).
  2. Polls a free ADS-B mirror for the aircraft's current position.
  3. Decides whether a takeoff or landing happened since the last run.
  4. Posts to Bluesky if so.
  5. Saves the updated state.json.

Data source: tries multiple free ADS-B mirrors (no key needed) that
mirror the ADS-B Exchange v2 JSON format: GET /v2/reg/<registration>.
Coverage depends on volunteer ADS-B receivers, so low-altitude helicopter
legs in receiver-sparse areas may not always be caught -- see README for
notes and for swapping in a paid source (ADS-B Exchange via RapidAPI) if
you need better reliability.
"""

import json
import os
import sys
import time
from datetime import datetime, timezone
from urllib.parse import quote

import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

TAIL_NUMBER = os.environ.get("TAIL_NUMBER", "VT-JJL")
STATE_FILE = os.environ.get("STATE_FILE", "state.json")

# Two independent free ADS-B mirrors, same JSON shape. Some of these
# services block traffic from cloud/datacenter IP ranges (which is what
# GitHub Actions runs on) even with a proper User-Agent set, so we try
# more than one and use whichever responds.
ADSB_URLS = [
    f"https://api.adsb.one/v2/reg/{quote(TAIL_NUMBER)}",
    f"https://api.adsb.lol/v2/reg/{quote(TAIL_NUMBER)}",
]
NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
# Nominatim's usage policy requires a descriptive User-Agent with contact info.
# Edit the email below to your own before running this for real.
HTTP_HEADERS = {"User-Agent": "vtjjl-tracker/1.0 (contact: limiter.tanker2n@icloud.com)"}

# Treated as airborne if reported altitude is above this (feet), or if
# ground speed is above the speed threshold (covers low hover taxi etc).
ALT_AIRBORNE_THRESHOLD_FT = 75
SPEED_AIRBORNE_THRESHOLD_KT = 15

# If the aircraft was airborne and then vanishes from the feed (common for
# low-flying helicopters between receivers) for this many consecutive
# polls, assume it landed at the last known position.
MISSED_POLLS_BEFORE_ASSUMED_LANDING = 3

BSKY_HANDLE = os.environ.get("BSKY_HANDLE")
BSKY_APP_PASSWORD = os.environ.get("BSKY_APP_PASSWORD")
DRY_RUN = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")
TEST_POST = os.environ.get("TEST_POST", "").lower() in ("1", "true", "yes")


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {
        "status": "unknown",       # "ground" | "airborne" | "unknown"
        "last_lat": None,
        "last_lon": None,
        "missed_polls": 0,
        "takeoff_time": None,
        "takeoff_place": None,
        "updated_at": None,
    }


def save_state(state):
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ---------------------------------------------------------------------------
# ADS-B lookup
# ---------------------------------------------------------------------------

def fetch_aircraft():
    """Return the aircraft dict from the feed, or None if not currently seen.

    Tries each configured mirror in turn and uses the first one that
    responds successfully. Raises the last error if every mirror fails.
    """
    last_error = None
    for url in ADSB_URLS:
        try:
            resp = requests.get(url, headers=HTTP_HEADERS, timeout=20)
            resp.raise_for_status()
            data = resp.json()
            ac_list = data.get("ac") or []
            print(f"OK: {url} -> {len(ac_list)} aircraft")
            return ac_list[0] if ac_list else None
        except requests.RequestException as e:
            print(f"Mirror failed ({url}): {e}", file=sys.stderr)
            last_error = e
    raise last_error


def is_airborne(ac):
    alt = ac.get("alt_baro")
    gs = ac.get("gs") or 0
    if isinstance(alt, str):  # ADS-B Exchange reports "ground" as a string
        alt_numeric = 0
    else:
        alt_numeric = alt or 0
    return alt_numeric > ALT_AIRBORNE_THRESHOLD_FT or gs > SPEED_AIRBORNE_THRESHOLD_KT


def reverse_geocode(lat, lon):
    """Best-effort place name for a lat/lon. Falls back to coordinates."""
    try:
        resp = requests.get(
            NOMINATIM_URL,
            params={"format": "jsonv2", "lat": lat, "lon": lon, "zoom": 14},
            headers=HTTP_HEADERS,
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        addr = data.get("address", {})
        place = (
            addr.get("aerodrome")
            or addr.get("suburb")
            or addr.get("town")
            or addr.get("city")
            or addr.get("county")
            or data.get("name")
        )
        state = addr.get("state")
        if place and state and place != state:
            return f"{place}, {state}"
        return place or f"{lat:.3f}, {lon:.3f}"
    except Exception:
        return f"{lat:.3f}, {lon:.3f}"


# ---------------------------------------------------------------------------
# Bluesky posting
# ---------------------------------------------------------------------------

def post_to_bluesky(text):
    if DRY_RUN:
        print(f"[DRY RUN] Would post: {text}")
        return
    if not BSKY_HANDLE or not BSKY_APP_PASSWORD:
        print("BSKY_HANDLE / BSKY_APP_PASSWORD not set -- skipping post.")
        print(f"Would have posted: {text}")
        return
    from atproto import Client

    client = Client()
    client.login(BSKY_HANDLE, BSKY_APP_PASSWORD)
    client.send_post(text=text)
    print(f"Posted: {text}")


def fmt_duration(seconds):
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, _ = divmod(rem, 60)
    if h:
        return f"{h}h {m}m"
    return f"{m}m"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if TEST_POST:
        # Manual verification path: confirm Bluesky login/posting works
        # without needing a real takeoff or landing to happen first.
        post_to_bluesky(
            f"🚁 Test post from the {TAIL_NUMBER} tracker — if you can see "
            f"this, posting is working correctly."
        )
        return

    state = load_state()

    try:
        ac = fetch_aircraft()
    except requests.RequestException as e:
        print(f"ADS-B fetch failed: {e}", file=sys.stderr)
        return

    now = datetime.now(timezone.utc)

    if ac is not None:
        state["missed_polls"] = 0
        lat, lon = ac.get("lat"), ac.get("lon")
        airborne = is_airborne(ac)

        if airborne and state["status"] in ("ground", "unknown"):
            place = reverse_geocode(lat, lon) if lat and lon else "an unknown location"
            state["status"] = "airborne"
            state["takeoff_time"] = now.isoformat()
            state["takeoff_place"] = place
            state["last_lat"], state["last_lon"] = lat, lon
            post_to_bluesky(f"🚁 {TAIL_NUMBER} just took off from {place}.")

        elif not airborne and state["status"] == "airborne":
            place = reverse_geocode(lat, lon) if lat and lon else "an unknown location"
            duration_txt = ""
            if state.get("takeoff_time"):
                took_off_at = datetime.fromisoformat(state["takeoff_time"])
                duration_txt = f" Flight time: {fmt_duration((now - took_off_at).total_seconds())}."
            from_place = state.get("takeoff_place")
            leg = f" from {from_place}" if from_place else ""
            post_to_bluesky(f"🚁 {TAIL_NUMBER} just landed at {place}{leg}.{duration_txt}")
            state["status"] = "ground"
            state["takeoff_time"] = None
            state["takeoff_place"] = None
            state["last_lat"], state["last_lon"] = lat, lon

        else:
            # No status change -- just keep last known position fresh.
            if lat and lon:
                state["last_lat"], state["last_lon"] = lat, lon
            if state["status"] == "unknown":
                state["status"] = "airborne" if airborne else "ground"

    else:
        # Aircraft not currently visible in the feed.
        if state["status"] == "airborne":
            state["missed_polls"] = state.get("missed_polls", 0) + 1
            if state["missed_polls"] >= MISSED_POLLS_BEFORE_ASSUMED_LANDING:
                lat, lon = state.get("last_lat"), state.get("last_lon")
                place = (
                    reverse_geocode(lat, lon)
                    if lat and lon
                    else "an unknown location"
                )
                duration_txt = ""
                if state.get("takeoff_time"):
                    took_off_at = datetime.fromisoformat(state["takeoff_time"])
                    duration_txt = f" Flight time: {fmt_duration((now - took_off_at).total_seconds())}."
                from_place = state.get("takeoff_place")
                leg = f" from {from_place}" if from_place else ""
                post_to_bluesky(
                    f"🚁 {TAIL_NUMBER} appears to have landed near {place}{leg} "
                    f"(lost signal).{duration_txt}"
                )
                state["status"] = "unknown"
                state["takeoff_time"] = None
                state["takeoff_place"] = None
                state["missed_polls"] = 0
        elif state["status"] == "unknown":
            # No prior sighting to compare against -- not seen at all is
            # far more likely to mean "parked" than "airborne but hidden",
            # so start the state machine from "ground" rather than staying
            # stuck at "unknown" forever.
            state["status"] = "ground"
        # If it was already "ground" and still isn't seen, nothing to do.

    save_state(state)


if __name__ == "__main__":
    main()
