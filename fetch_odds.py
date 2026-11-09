#!/usr/bin/env python3
"""
Daily Premier League odds -> odds.json with DERIVED numbers only (Python standard library only).

  ODDS_API_KEY=... python3 fetch_odds.py            # normally run by .github/workflows/odds.yml

One paid request per run at most:
  GET https://api.the-odds-api.com/v4/sports/soccer_epl/odds/?regions=uk&markets=h2h,totals&oddsFormat=decimal
  = 2 credits (1 region × 2 markets). Daily -> ~62 credits a month of the free plan's 500.

Guards, checked in this order – any of them skips the run WITHOUT the paid request (exit 0):
  (c) no Premier League fixture in the next 8 days (free FPL endpoint `fixtures/`)
  (b) own ledger state.json {month, credits_used}: this month's usage + 2 would exceed 200
  (a) the free `/v4/sports/` endpoint reports x-requests-remaining < 150 (or the header is missing)
  (d) no retries anywhere: every request is made once; a failure ends the run
The key is read from the environment only and never written anywhere. odds.json contains vig-free
probabilities, expected goals, Poisson rates and clean-sheet chances per FPL fixture id – no bookmaker
prices (see derive.py).
"""
import json
import os
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from derive import derive  # noqa: E402

ODDS_API = "https://api.the-odds-api.com/v4"
FPL_API = "https://fantasy.premierleague.com/api/"
SPORT = "soccer_epl"
ODDS_PARAMS = {"regions": "uk", "markets": "h2h,totals", "oddsFormat": "decimal"}
COST = 2                 # credits per odds request: regions (1) × markets (2)
MONTHLY_CAP = 200        # own budget per calendar month (free plan: 500)
MIN_REMAINING = 150      # never touch the last 150 credits of the account
LOOKAHEAD_DAYS = 8
STATE_PATH = os.path.join(HERE, "state.json")
OUT_PATH = os.path.join(HERE, "odds.json")
UA = {"User-Agent": "fplpro-odds/1.0", "Accept": "application/json"}

# FPL short name -> names used by bookmakers / The Odds API / football-data (normalised, see norm()).
TEAM_ALIASES = {
    "ARS": ["arsenal"],
    "AVL": ["aston villa", "villa"],
    "BOU": ["bournemouth", "afc bournemouth"],
    "BRE": ["brentford"],
    "BHA": ["brighton", "brighton and hove albion", "brighton hove albion", "brighton & hove albion"],
    "BUR": ["burnley"],
    "CAR": ["cardiff", "cardiff city"],
    "CHE": ["chelsea"],
    "COV": ["coventry", "coventry city"],
    "CRY": ["crystal palace", "palace"],
    "EVE": ["everton"],
    "FUL": ["fulham"],
    "HUD": ["huddersfield", "huddersfield town"],
    "HUL": ["hull", "hull city"],
    "IPS": ["ipswich", "ipswich town"],
    "LEE": ["leeds", "leeds united"],
    "LEI": ["leicester", "leicester city"],
    "LIV": ["liverpool"],
    "LUT": ["luton", "luton town"],
    "MCI": ["man city", "manchester city"],
    "MID": ["middlesbrough"],
    "MUN": ["man utd", "man united", "manchester united", "manchester utd"],
    "NEW": ["newcastle", "newcastle united", "newcastle utd"],
    "NFO": ["nottm forest", "nott m forest", "nottingham forest", "nottingham", "forest"],
    "NOR": ["norwich", "norwich city"],
    "QPR": ["qpr", "queens park rangers"],
    "SHU": ["sheffield utd", "sheffield united", "sheffield united fc"],
    "SOU": ["southampton"],
    "STK": ["stoke", "stoke city"],
    "SUN": ["sunderland"],
    "SWA": ["swansea", "swansea city"],
    "TOT": ["spurs", "tottenham", "tottenham hotspur"],
    "WAT": ["watford"],
    "WBA": ["west brom", "west bromwich albion", "west bromwich"],
    "WHU": ["west ham", "west ham united"],
    "WOL": ["wolves", "wolverhampton", "wolverhampton wanderers"],
}


def log(msg):
    print(f"[odds] {msg}", flush=True)


def norm(name):
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    s = s.replace("&", " and ").replace("'", " ").replace(".", " ").replace("-", " ")
    words = [w for w in s.split() if w not in ("fc", "afc")] or s.split()
    return " ".join(words)


ALIAS_TO_SHORT = {norm(a): short for short, names in TEAM_ALIASES.items() for a in names}


def http_get(url, params=None, timeout=20):
    """One GET, no retries. Returns (status, headers dict lower-case, parsed JSON or None)."""
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode()
            return r.status, {k.lower(): v for k, v in r.headers.items()}, json.loads(body) if body else None
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in (e.headers or {}).items()}, None


def safe(get, *args):
    """A free request that must never raise (network errors just skip the run)."""
    try:
        return get(*args)
    except Exception:  # noqa: BLE001
        return 0, {}, None


def parse_time(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def iso(t):
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_json(path, default):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(data, fh, indent=1, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)


def ledger(now, state):
    month = now.strftime("%Y-%m")
    if state.get("month") != month:
        state = {"month": month, "credits_used": 0}
    return state


def team_ids(bootstrap):
    """FPL team id per normalised alias, from bootstrap-static `teams` (short_name + name)."""
    out = {}
    for t in bootstrap.get("teams", []):
        out[norm(t["name"])] = t["id"]
        out[norm(t["short_name"])] = t["id"]
        for alias, short in ALIAS_TO_SHORT.items():
            if short == t["short_name"]:
                out[alias] = t["id"]
    return out


def match_fixture(event, ids, fixtures):
    h, a = ids.get(norm(event.get("home_team"))), ids.get(norm(event.get("away_team")))
    if h is None or a is None:
        return None
    for f in fixtures:
        if f["team_h"] == h and f["team_a"] == a and not f.get("finished"):
            return f
    return None


def event_prices(event):
    h2h, totals = [], []
    home, away = event.get("home_team"), event.get("away_team")
    for book in event.get("bookmakers", []):
        for market in book.get("markets", []):
            outcomes = market.get("outcomes", [])
            if market.get("key") == "h2h":
                price = {o.get("name"): o.get("price") for o in outcomes}
                if home in price and away in price and "Draw" in price:
                    h2h.append((price[home], price["Draw"], price[away]))
            elif market.get("key") == "totals":
                lines = {}
                for o in outcomes:
                    lines.setdefault(o.get("point"), {})[o.get("name")] = o.get("price")
                for point, p in lines.items():
                    if "Over" in p and "Under" in p:
                        totals.append((point, p["Over"], p["Under"]))
    return h2h, totals


def build_odds(events, bootstrap, fixtures, now, previous=None):
    """The Odds API events -> odds.json payload (derived numbers per FPL fixture id)."""
    ids = team_ids(bootstrap)
    short = {t["id"]: t["short_name"] for t in bootstrap.get("teams", [])}
    out = {}
    # keep earlier entries of fixtures that have not kicked off (a bookmaker may pull a market for a day)
    for fid, e in ((previous or {}).get("fixtures") or {}).items():
        k = parse_time(e.get("kickoff"))
        if k and k > now:
            out[fid] = e
    for ev in events or []:
        f = match_fixture(ev, ids, fixtures)
        if f is None:
            log(f"no FPL fixture for {ev.get('home_team')} v {ev.get('away_team')}")
            continue
        h2h, totals = event_prices(ev)
        d = derive(h2h, totals)
        if d is None:
            continue
        entry = {k: (round(v, 4) if isinstance(v, float) else v) for k, v in d.items()}
        entry.update(home=short.get(f["team_h"]), away=short.get(f["team_a"]), gw=f.get("event"),
                     kickoff=f.get("kickoff_time") or ev.get("commence_time"), updated=iso(now))
        out[str(f["id"])] = entry
    return {"format": 1, "updated": iso(now), "source": "The Odds API (uk region), derived numbers only",
            "fixtures": dict(sorted(out.items(), key=lambda kv: int(kv[0])))}


def run(now=None, get=http_get, env=None):
    """Returns a short status string; writes state.json / odds.json next to this file."""
    now = now or datetime.now(timezone.utc)
    env = os.environ if env is None else env
    key = (env.get("ODDS_API_KEY") or "").strip()
    if not key:
        return "error: ODDS_API_KEY is not set"

    # (c) a Premier League fixture in the next LOOKAHEAD_DAYS? (free FPL endpoint)
    status, _, fixtures = safe(get, FPL_API + "fixtures/", {"future": 1})
    if status != 200 or not isinstance(fixtures, list):
        return f"skip: FPL fixtures unavailable ({status})"
    horizon = now + timedelta(days=LOOKAHEAD_DAYS)
    soon = [f for f in fixtures if f.get("kickoff_time") and now <= parse_time(f["kickoff_time"]) <= horizon]
    if not soon:
        return "skip: no Premier League fixture in the next 8 days"

    # (b) own monthly ledger
    state = ledger(now, load_json(STATE_PATH, {}))
    if state["credits_used"] + COST > MONTHLY_CAP:
        return f"skip: monthly budget used ({state['credits_used']}/{MONTHLY_CAP})"

    # (a) account quota from the free sports list (does not cost credits)
    status, headers, _ = safe(get, f"{ODDS_API}/sports/", {"apiKey": key})
    remaining = headers.get("x-requests-remaining")
    try:
        remaining = float(remaining)
    except (TypeError, ValueError):
        return f"skip: quota unknown (status {status})"
    if status != 200 or remaining < MIN_REMAINING:
        return f"skip: only {remaining:.0f} credits left (status {status})"

    status, _, boot = safe(get, FPL_API + "bootstrap-static/")
    if status != 200 or not isinstance(boot, dict):
        return f"skip: FPL bootstrap unavailable ({status})"

    # the one paid request – booked in the ledger before anything else can fail
    try:
        status, headers, events = get(f"{ODDS_API}/sports/{SPORT}/odds/", dict(ODDS_PARAMS, apiKey=key))
        cost = headers.get("x-requests-last")
        cost = int(float(cost)) if cost is not None else (COST if status == 200 else 0)
    except Exception:  # noqa: BLE001 – network error: assume it was charged
        status, events, cost = 0, None, COST
    state["credits_used"] += cost
    state["updated"] = iso(now)
    write_json(STATE_PATH, state)
    if status != 200 or not isinstance(events, list):
        return f"error: odds request failed ({status}), {cost} credits booked"

    payload = build_odds(events, boot, fixtures, now, load_json(OUT_PATH, None))
    write_json(OUT_PATH, payload)
    return f"ok: {len(payload['fixtures'])} fixtures, {cost} credits (month {state['credits_used']}/{MONTHLY_CAP})"


def main():
    result = run()
    log(result)
    return 1 if result.startswith("error") else 0


if __name__ == "__main__":
    sys.exit(main())
