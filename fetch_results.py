#!/usr/bin/env python3
"""Fetch Texas results from civicAPI and write them into data.json for the tracker.

Runs on a schedule in GitHub Actions. Uses only the Python standard library.
Which races to fetch is set by scripts/mode.txt ("live", "rehearsal", or "historic").
Each county is stored as [Democrat votes, Republican votes].

This version will merge partial county results from civicAPI into an existing
baseline in data.json so the output always contains entries for all counties
(where possible) instead of rejecting partial fetches outright.
"""
import datetime
import json
import pathlib
import sys
import time
import urllib.request
import urllib.parse
from zoneinfo import ZoneInfo

API = "https://civicapi.org/api/v2/race/"
SEARCH = "https://civicapi.org/api/v2/race/search?query="

# For each race: either a numeric civicAPI race id, or ("search", query_string),
# followed by how to find the Democrat and how to find the Republican.
# ("party", x) matches the candidate's party; ("name", x) matches the candidate's name.
CONFIG = {
    "live": {
        "senate": (87438, ("party", "Democratic"), ("party", "Republican")),
        "governor": (55857, ("party", "Democratic"), ("party", "Republican")),
    },
    # Rehearsal: the March 3 Democratic Senate primary. Talarico stands in for the
    # "Democrat" slot and Crockett for the "Republican" slot, just to exercise the pipeline.
    "rehearsal": {
        "senate": (44286, ("name", "James Talarico"), ("name", "Jasmine Crockett")),
    },
    # Historic mode: fetch older races by searching civicAPI. The query strings are
    # intentionally descriptive to find the appropriate archived race.
    "historic": {
        "senate": (("search", "Texas U.S. Senate 2018"), ("party", "Democratic"), ("party", "Republican")),
        "governor": (("search", "Texas Governor 2022"), ("party", "Democratic"), ("party", "Republican")),
    },
}


def fetch(race_id, tries=3):
    """Fetch a race by numeric id and return its parsed JSON, or None on failure."""
    req = urllib.request.Request(API + str(race_id), headers={"User-Agent": "tx-tracker/1.0"})
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except Exception as e:  # network trouble, bad JSON, etc.
            print("fetch error for race", race_id, "->", e)
            time.sleep(3 * (attempt + 1))
    return None


def search_and_fetch(query, tries=3):
    """Search civicAPI for a race matching query and return the full race JSON for the
    first search result, or None if nothing matched."""
    url = SEARCH + urllib.parse.quote(query)
    req = urllib.request.Request(url, headers={"User-Agent": "tx-tracker/1.0"})
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                results = json.load(r)
                if isinstance(results, list) and results:
                    rid = results[0].get("id")
                    if rid:
                        return fetch(rid)
                return None
        except Exception as e:
            print("search error for", query, "->", e)
            time.sleep(3 * (attempt + 1))
    return None


def pick(cands, spec):
    kind, value = spec
    found = [c for c in cands if c.get(kind) == value]
    return max(found, key=lambda c: c.get("votes") or 0) if found else None


def extract_counties(race, dspec, rspec):
    """Extract county vote pairs from a civicAPI race JSON without enforcing the
    "complete" threshold. Returns (counties_dict, top_level_statewide_or_None).
    counties_dict maps county name -> [D_votes, R_votes]."""
    regions = race.get("region_results") or {}
    counties = {}
    for region in regions.values():
        if region.get("type") != "County":
            continue
        d = pick(region.get("candidates", []), dspec)
        r = pick(region.get("candidates", []), rspec)
        counties[region.get("name")] = [int((d or {}).get("votes") or 0), int((r or {}).get("votes") or 0)]
    # Try to get a top-level statewide total if present
    d = pick(race.get("candidates", []) or [], dspec)
    r = pick(race.get("candidates", []) or [], rspec)
    top = None
    if d is not None and r is not None:
        top = [int(d.get("votes") or 0), int(r.get("votes") or 0)]
    return counties, top


def convert(race, dspec, rspec):
    """Original strict convert: used to enforce completeness. Kept for logging.
    Returns counties,statewide or None if strictly rejected (used only by older flow).
    """
    counties, top = extract_counties(race, dspec, rspec)
    if len(counties) < 200:
        print("only", len(counties), "counties found; ignoring this fetch")
        return None
    if top is None:
        print("could not find both candidates at the top level; ignoring this fetch")
        return None
    return counties, top


def merge_with_baseline(new_counties, baseline_counties):
    """Merge new_counties into baseline_counties: baseline provides default for
    missing counties; new_counties override baseline values where present."""
    merged = dict(baseline_counties) if baseline_counties else {}
    merged.update(new_counties)
    return merged


def compute_statewide_from_counties(counties):
    d_sum = sum(v[0] for v in counties.values())
    r_sum = sum(v[1] for v in counties.values())
    return [d_sum, r_sum]


def main(root=None):
    root = pathlib.Path(root) if root else pathlib.Path(__file__).resolve().parent
    data_file = root / "data.json"
    mode_file = root / "scripts" / "mode.txt"
    mode = mode_file.read_text().strip() if mode_file.exists() else "live"
    if mode not in CONFIG:
        print("unknown mode", mode, "- using live")
        mode = "live"

    data = json.loads(data_file.read_text()) if data_file.exists() else {}
    data.setdefault("turnout", {"senate": 1.10, "governor": 1.05})
    before = json.dumps({k: data.get(k) for k in ("senate", "governor")}, sort_keys=True)

    for key, (race_spec, dspec, rspec) in CONFIG[mode].items():
        race = None
        if isinstance(race_spec, tuple) and race_spec and race_spec[0] == "search":
            query = race_spec[1]
            race = search_and_fetch(query)
            if not race:
                print("search failed for", query)
                continue
        else:
            race = fetch(race_spec)
        if not race:
            continue

        # Extract whatever counties the race provides (even if partial)
        new_counties, top_statewide = extract_counties(race, dspec, rspec)

        # If the fetch produced few counties, merge with the existing baseline in data.json
        if len(new_counties) < 200:
            print("partial fetch: only", len(new_counties), "counties; merging with baseline")
            baseline = data.get(key) or {}
            baseline_counties = {k: v for k, v in baseline.items() if k != "Statewide"}
            merged = merge_with_baseline(new_counties, baseline_counties)
            counties = merged
            # Prefer the race top-level statewide if present, otherwise compute from merged counties
            statewide = top_statewide if top_statewide is not None else compute_statewide_from_counties(merged)
        else:
            # Full fetch — use the race counties and the top-level statewide totals
            counties = new_counties
            statewide = top_statewide if top_statewide is not None else compute_statewide_from_counties(counties)

        # Extra sanity: ensure we have at least some counties before accepting
        if not counties:
            print("no counties found; ignoring this fetch")
            continue

        old = (data.get(key) or {}).get("Statewide")
        # In live mode, never let the total move backwards by more than 2%.
        if mode == "live" and old and sum(statewide) < 0.98 * sum(old):
            print(key, "new statewide total is smaller than before; keeping the old numbers")
            continue

        new = dict(counties)
        new["Statewide"] = statewide
        data[key] = new

    after = json.dumps({k: data.get(k) for k in ("senate", "governor")}, sort_keys=True)
    if after == before:
        print("no change")
        return False
    now = datetime.datetime.now(ZoneInfo("America/Chicago")).strftime("%b %-d, %-I:%M %p CT")
    data["updated"] = ("REHEARSAL, March primary data. " if mode == "rehearsal" else "") + now
    data_file.write_text(json.dumps(data, indent=1) + "\n")
    print("wrote data.json")
    return True


if __name__ == "__main__":
    main()
    sys.exit(0)
