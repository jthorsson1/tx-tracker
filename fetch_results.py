#!/usr/bin/env python3
"""Fetch Texas results from civicAPI and write them into data.json for the tracker.

Runs on a schedule in GitHub Actions. Uses only the Python standard library.
Which races to fetch is set by scripts/mode.txt ("live", "rehearsal", or "historic").
Each county is stored as [Democrat votes, Republican votes].

This version will merge partial county results from civicAPI into an existing
baseline in data.json so the output always contains entries for all counties
(where possible) instead of rejecting partial fetches outright.

Changes: more tolerant candidate matching (case-insensitive, substring,
normalized) plus a party-based fallback when name matching fails. Logs
are now compact: an aggregated summary is always printed and per-county
messages are only printed for true no-match counties.
"""
import datetime
import json
import pathlib
import sys
import time
import urllib.request
import urllib.parse
import re
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

# Aggregated counters for matching methods for the current run
MATCH_COUNTS = {
    "exact": 0,
    "ci": 0,
    "substr": 0,
    "norm": 0,
    "party_prefix": 0,
    "party_fallback": 0,
    "none": 0,
}

# List of problem counties: (county_name, reason, candidates_list)
PROBLEMS = []

def _reset_counters():
    for k in MATCH_COUNTS:
        MATCH_COUNTS[k] = 0
    PROBLEMS.clear()

def _print_summary():
    print(
        "Match summary: exact={exact}, ci={ci}, substr={substr}, norm={norm}, party_prefix={party_prefix}, party_fallback={party_fallback}, none={none}, problems={problems_count}".format(
            exact=MATCH_COUNTS["exact"],
            ci=MATCH_COUNTS["ci"],
            substr=MATCH_COUNTS["substr"],
            norm=MATCH_COUNTS["norm"],
            party_prefix=MATCH_COUNTS["party_prefix"],
            party_fallback=MATCH_COUNTS["party_fallback"],
            none=MATCH_COUNTS["none"],
            problems_count=len(PROBLEMS),
        )
    )
    if PROBLEMS:
        print("Problem counties (both or one candidate missing):")
        for name, reason, cand_names in PROBLEMS:
            print(f" - {name}: {reason}; candidates: {cand_names}")

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

def _norm_name(s):
    if not s:
        return ""
    s = str(s).strip()
    # remove punctuation and whitespace for a normalized compare
    return re.sub(r"[^a-z0-9]", "", s.lower())

def pick(cands, spec):
    """Tolerant matching for a candidate in the per-region candidate list.

    Tries, in order:
      - exact equality on the requested field
      - case-insensitive equality
      - substring match (case-insensitive)
      - normalized token match (remove punctuation/spacing)

    If the spec is a party match and a name-based lookup fails, we will use
    party matching where sensible (see extract_counties fallback behavior).

    Returns a tuple (candidate_dict_or_None, method_str).
    method_str is one of: 'exact','ci','substr','norm','party_prefix','none'
    """
    if not cands:
        MATCH_COUNTS["none"] += 1
        return None, "none"
    kind, value = spec
    val = (value or "")

    # Exact match first
    found = [c for c in cands if c.get(kind) == value]
    if found:
        MATCH_COUNTS["exact"] += 1
        return max(found, key=lambda c: c.get("votes") or 0), "exact"

    # Case-insensitive equality
    found = [c for c in cands if str(c.get(kind, "")).strip().lower() == val.strip().lower()]
    if found:
        MATCH_COUNTS["ci"] += 1
        return max(found, key=lambda c: c.get("votes") or 0), "ci"

    # Substring match (value contained in candidate field)
    lval = val.strip().lower()
    if lval:
        found = [c for c in cands if lval in str(c.get(kind, "")).lower()]
        if found:
            MATCH_COUNTS["substr"] += 1
            return max(found, key=lambda c: c.get("votes") or 0), "substr"

    # Normalized match (remove punctuation/spacing)
    nval = _norm_name(val)
    if nval:
        found = [c for c in cands if nval and nval in _norm_name(c.get(kind, ""))]
        if found:
            MATCH_COUNTS["norm"] += 1
            return max(found, key=lambda c: c.get("votes") or 0), "norm"

    # If kind is 'party', try prefix matching (e.g., 'Dem' matches 'Democratic')
    if kind == "party":
        lwant = val.strip().lower()
        found = [c for c in cands if c.get("party") and c.get("party").strip().lower().startswith(lwant)]
        if found:
            MATCH_COUNTS["party_prefix"] += 1
            return max(found, key=lambda c: c.get("votes") or 0), "party_prefix"

    MATCH_COUNTS["none"] += 1
    return None, "none"

def _find_by_party(cands, party_name):
    if not cands:
        return None
    want = (party_name or "").strip().lower()
    if not want:
        return None
    # prefer prefix match
    for c in cands:
        p = (c.get("party") or "").strip().lower()
        if p.startswith(want):
            return c
    # fallback: any candidate whose party contains the token
    for c in cands:
        if want in (c.get("party") or "").strip().lower():
            return c
    return None

def extract_counties(race, dspec, rspec):
    """Extract county vote pairs from a civicAPI race JSON without enforcing the
    "complete" threshold. Returns (counties_dict, top_level_statewide_or_None).
    counties_dict maps county name -> [D_votes, R_votes].
    """
    regions = race.get("region_results") or {}
    counties = {}
    for region in regions.values():
        if region.get("type") != "County":
            continue
        cands = region.get("candidates", []) or []
        # Candidate names for diagnostics
        cand_names = [f"{c.get('name') or '<no-name>'} ({c.get('party') or ''})" for c in cands]
        d_cand, d_method = pick(cands, dspec)
        r_cand, r_method = pick(cands, rspec)

        # If name-based picks failed, fall back to party matching where possible
        if d_cand is None and dspec[0] == "name":
            fb = _find_by_party(cands, "Democratic")
            if fb:
                MATCH_COUNTS["party_fallback"] += 1
                d_cand = fb
        if r_cand is None and rspec[0] == "name":
            fb = _find_by_party(cands, "Republican")
            if fb:
                MATCH_COUNTS["party_fallback"] += 1
                r_cand = fb

        # Record problems only; do not print per-county success messages
        if d_cand is None or r_cand is None:
            if d_cand is None and r_cand is None:
                PROBLEMS.append((region.get("name"), "both", cand_names))
            elif d_cand is None:
                PROBLEMS.append((region.get("name"), "dem", cand_names))
            else:
                PROBLEMS.append((region.get("name"), "rep", cand_names))

        counties[region.get("name")] = [int((d_cand or {}).get("votes") or 0), int((r_cand or {}).get("votes") or 0)]
    # Try to get a top-level statewide total if present
    d_top, _ = pick(race.get("candidates", []) or [], dspec)
    r_top, _ = pick(race.get("candidates", []) or [], rspec)
    top = None
    if d_top is not None and r_top is not None:
        top = [int(d_top.get("votes") or 0), int(r_top.get("votes") or 0)]
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

    _reset_counters()

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

    # Print compact summary of matching behavior for this run
    _print_summary()

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
