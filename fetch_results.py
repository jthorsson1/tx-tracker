#!/usr/bin/env python3
"""Fetch Texas results from civicAPI and update data.json before the site is published.

Runs inside GitHub Actions. The result is published with the site and is NOT saved back
to the repository, so the repository's data.json only holds your settings.
Settings in data.json:  "mode": "rehearsal" or "live"   |   "manual": true to skip civicAPI
Each county is stored as [Democrat votes, Republican votes]; third-party votes are stored apart, in "other".
"""
import datetime
import json
import pathlib
import time
import urllib.request
from zoneinfo import ZoneInfo

API = "https://civicapi.org/api/v2/race/"
SITE_DATA = "https://jthorsson1.github.io/tx-tracker/data.json"  # what is published right now

CONFIG = {
    "live": {
        "senate": (87438, ("party", "Democratic"), ("party", "Republican")),
        "governor": (55857, ("party", "Democratic"), ("party", "Republican")),
    },
    # Rehearsal: the March 3 Democratic Senate primary (Talarico stands in for the Democrat,
    # Crockett for the Republican) just to test the pipeline.
    "rehearsal": {
        "senate": (44286, ("name", "James Talarico"), ("name", "Jasmine Crockett")),
    },
}


def get_json(url, tries=3):
    req = urllib.request.Request(url, headers={"User-Agent": "tx-tracker/1.0"})
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except Exception as e:
            print("fetch error:", url, "->", e)
            time.sleep(3 * (attempt + 1))
    return None


def fetch(race_id):
    return get_json(API + str(race_id))


def fetch_previous():
    return get_json(SITE_DATA + "?t=" + str(int(time.time())), tries=1)


def pick(cands, spec):
    kind, value = spec
    found = [c for c in cands if c.get(kind) == value]
    return max(found, key=lambda c: c.get("votes") or 0) if found else None


def other_votes(cands, d, r):
    """Votes for every candidate who is not the chosen Democrat or Republican (Brown, Dixon, write-ins...)."""
    return sum(int(c.get("votes") or 0) for c in cands if c is not d and c is not r)


def describe(key, race):
    """One-time check: print which fields the feed carries (for example a call or winner). The page does not use this."""
    try:
        if not race:
            return
        top = {k: (v if not isinstance(v, (dict, list)) else type(v).__name__ + "[%d]" % len(v)) for k, v in race.items()}
        print("feed fields", key, json.dumps(top)[:1500])
        for c in (race.get("candidates") or [])[:6]:
            print("feed candidate", key, json.dumps({k: v for k, v in c.items() if not isinstance(v, (dict, list))})[:400])
    except Exception as e:
        print("describe error", e)


def convert(race, dspec, rspec):
    """One civicAPI race -> ({county: [D, R]}, [D, R], {county: other votes}), or None if it looks wrong."""
    counties = {}
    others = {}
    for region in (race.get("region_results") or {}).values():
        if region.get("type") != "County":
            continue
        d = pick(region.get("candidates", []), dspec)
        r = pick(region.get("candidates", []), rspec)
        counties[region["name"]] = [int((d or {}).get("votes") or 0), int((r or {}).get("votes") or 0)]
        others[region["name"]] = other_votes(region.get("candidates", []), d, r)
    if len(counties) < 200:
        print("only", len(counties), "counties found")
        return None
    d = pick(race.get("candidates", []), dspec)
    r = pick(race.get("candidates", []), rspec)
    if d is None or r is None:
        print("could not find both candidates")
        return None
    others["Statewide"] = other_votes(race.get("candidates", []), d, r)
    return counties, [int(d.get("votes") or 0), int(r.get("votes") or 0)], others


def keep_other(data, prev, key):
    """Keep the earlier 'other' numbers for a race whose new numbers could not be used."""
    old = (prev.get("other") or {}).get(key)
    if old:
        data.setdefault("other", {})[key] = old


def main(root=None):
    root = pathlib.Path(root) if root else pathlib.Path(__file__).resolve().parent
    data_file = root / "data.json"
    data = json.loads(data_file.read_text())
    data.setdefault("turnout", {"senate": 1.10, "governor": 1.05})
    mode = data.get("mode", "live")
    if mode not in CONFIG:
        mode = "live"
    now = datetime.datetime.now(ZoneInfo("America/Chicago")).strftime("%b %-d, %-I:%M %p CT")
    status = {"checked": now, "ok": True, "counties": 0, "mode": mode, "manual": bool(data.get("manual")), "note": ""}

    if data.get("manual"):
        if not data.get("updated"):
            data["updated"] = now
        data["status"] = status
        data_file.write_text(json.dumps(data, indent=1) + "\n")
        print("manual mode: civicAPI not used")
        return status

    prev = fetch_previous()
    pstat = (prev or {}).get("status") or {}
    prev_ok = bool(prev) and pstat.get("mode") == mode and not pstat.get("manual")
    problems = []
    for key, (race_id, dspec, rspec) in CONFIG[mode].items():
        race = fetch(race_id)
        describe(key, race)
        result = convert(race, dspec, rspec) if race else None
        if not result:
            problems.append(key + ": could not read civicAPI")
            if prev_ok and key in prev:
                data[key] = prev[key]
                keep_other(data, prev, key)
            continue
        counties, statewide, others = result
        old = (prev.get(key) or {}).get("Statewide") if prev_ok else None
        if old and sum(statewide) < 0.98 * sum(old):
            problems.append(key + ": total dropped, kept earlier numbers")
            data[key] = prev[key]
            keep_other(data, prev, key)
            continue
        new = dict(counties)
        new["Statewide"] = statewide
        data[key] = new
        # "Other" votes go in their own section so the [D, R] pairs everything else reads stay as they are.
        # If the number looks wrong (over 15% of all votes), it is left out and the page falls back to two-party.
        tot = sum(statewide) + others["Statewide"]
        if tot and others["Statewide"] / tot <= 0.15:
            data.setdefault("other", {})[key] = others
        else:
            data.setdefault("other", {}).pop(key, None)
            print(key + ": other votes looked wrong, left out")
        status["counties"] = len(counties)

    status["ok"] = not problems
    status["note"] = "; ".join(problems)
    races_now = json.dumps({k: data.get(k) for k in ("senate", "governor")}, sort_keys=True)
    races_prev = json.dumps({k: (prev or {}).get(k) for k in ("senate", "governor")}, sort_keys=True) if prev_ok else None
    data["updated"] = (prev or {}).get("updated", now) if races_now == races_prev else now
    data["status"] = status
    data_file.write_text(json.dumps(data, indent=1) + "\n")
    print("mode:", mode, "| counties:", status["counties"], "| ok:", status["ok"], "|", status["note"] or "no problems")
    return status


if __name__ == "__main__":
    main()
