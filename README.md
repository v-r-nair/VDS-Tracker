# VT-JJL tracker → Bluesky

Polls a free ADS-B feed for the helicopter tail number `VT-JJL` and posts a
Bluesky update whenever it appears to take off or land. Runs on a schedule
via GitHub Actions — no server of your own required.

## How it works

- **Data source:** [api.adsb.one](https://api.adsb.one) — a free, no-key
  mirror of the ADS-B Exchange v2 format. `track_helicopter.py` queries
  `GET /v2/reg/VT-JJL` on every run.
- **State:** `state.json` in this repo remembers whether the aircraft was
  last seen airborne or on the ground, so the script can detect the
  *transition* (takeoff/landing) rather than just the current status.
  GitHub Actions commits the updated file back after each run.
- **Posting:** uses the [`atproto`](https://github.com/MarshalX/atproto)
  Python SDK with a Bluesky **app password** (never your main password).
- **Place names:** looked up via OpenStreetMap's Nominatim reverse
  geocoder from the aircraft's last reported lat/lon.

## Setup

### 1. Create a Bluesky app password

In the Bluesky app: **Settings → App Passwords → Add App Password**. Save
the generated password somewhere safe — you won't see it again.

### 2. Create a GitHub repo and push this project

```bash
cd vtjjl-tracker
git init
git add .
git commit -m "Initial commit"
git branch -M main
git remote add origin https://github.com/<you>/vtjjl-tracker.git
git push -u origin main
```

### 3. Add repository secrets

In your GitHub repo: **Settings → Secrets and variables → Actions → New
repository secret**. Add:

| Name | Value |
|---|---|
| `BSKY_HANDLE` | your Bluesky handle, e.g. `you.bsky.social` |
| `BSKY_APP_PASSWORD` | the app password from step 1 |

### 4. Enable the workflow

The workflow in `.github/workflows/track.yml` is scheduled for every 5
minutes and also has a manual trigger. Go to the **Actions** tab, select
"Track VT-JJL", and click **Run workflow** once to test it before waiting
for the schedule. GitHub Actions cron can lag under load, so treat "every
5 minutes" as approximate.

### 5. Edit the contact email

`track_helicopter.py` sends a `User-Agent` header to Nominatim with a
placeholder email (`you@example.com`) — Nominatim's usage policy requires
a real contact. Edit `HTTP_HEADERS` near the top of the file before
running for real.

## Testing locally without posting

```bash
pip install -r requirements.txt
DRY_RUN=1 python track_helicopter.py
```

`DRY_RUN=1` prints what it *would* post instead of calling Bluesky, so you
can watch `state.json` update over a few runs before wiring up real
credentials.

## Known limitations

- **Coverage gaps:** ADS-B Exchange (and its free mirror, adsb.one) relies
  on volunteer ground receivers. Helicopters fly low, so legs in
  receiver-sparse areas (or over water, or in hilly terrain) may not be
  seen at all, or may drop out mid-flight. The script assumes a landing
  after 3 consecutive missed polls (~15 min) if the aircraft was last
  airborne — this is a heuristic, not a certainty, and is flagged as such
  in the post text ("lost signal").
- **One aircraft, one flight at a time:** the state machine tracks a
  single status, so it won't handle two disconnected sightings in the
  same short window correctly.
- **Rate limits:** Nominatim's public endpoint asks for ~1 request/second
  and no bulk use. This script only calls it on takeoff/landing events
  (not every poll), so it stays well within that.
- **Better reliability:** if adsb.one's free mirror proves flaky for your
  use case, `ADS-B Exchange` itself is available via RapidAPI (paid,
  usage-based) with the same JSON shape — swapping `ADSB_URL` in
  `track_helicopter.py` to
  `https://gateway.adsbexchange.com/api/aircraft/v2/registration/VT-JJL`
  with an `api-auth` header is a drop-in change.
