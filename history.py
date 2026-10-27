#!/usr/bin/env python3
"""Keep a running history of county votes for the election-night chart.

Runs inside GitHub Actions right after fetch_results.py. It reads the freshly updated
data.json, adds a snapshot to the history that is currently published on the site (about
one every 15 minutes, and only when the numbers changed), and writes history.json.
The workflow publishes history.json next to data.json. Nothing is saved to the repository.

Safety rules:
- If the published history cannot be read (anything other than "does not exist yet"),
  this script stops with an error, so that run does not publish and the site keeps its
  last good version. The next run (5 minutes later) tries again.
- If anything goes wrong while adding the snapshot, the earlier history is published unchanged.
- If the mode changes (rehearsal to live), the history starts over.
"""
import datetime
import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.request
from zoneinfo import ZoneInfo

SITE_HISTORY = os.environ.get("HISTORY_URL", "https://jthorsson1.github.io/tx-tracker/history.json")
EVERY = 15 * 60   # seconds between snapshots
MAX_SNAPS = 200   # about 50 hours at one every 15 minutes


def fetch_previous(url=SITE_HISTORY, tries=4):
    """Return the published history, or None if there is none yet. Raise if it cannot be read."""
    err = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url + "?t=" + str(int(time.time())),
                                         headers={"User-Agent": "tx-tracker/1.0", "Cache-Control": "no-cache"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            err = e
            if e.code == 404 and attempt >= 2:
                return None          # 404 on three tries in a row: there is no history yet
        except Exception as e:
            err = e
        print("history fetch problem:", err)
        time.sleep(float(os.environ.get("HISTORY_SLEEP", "1")) if attempt < 2 else 3 * (attempt + 1))
    raise RuntimeError("could not read the published history: %s" % err)


def compact(race):
    """{county: [D, R]} keeping only entries that have votes (Statewide included)."""
    out = {}
    for name, v in (race or {}).items():
        if isinstance(v, list) and len(v) == 2 and (v[0] or v[1]):
            out[name] = [int(v[0]), int(v[1])]
    return out


def update(prev, data, now):
    mode = data.get("mode", "live")
    ok = isinstance(prev, dict) and prev.get("mode") == mode and isinstance(prev.get("snaps"), list)
    hist = prev if ok else {"mode": mode, "snaps": []}
    sen, gov = compact(data.get("senate")), compact(data.get("governor"))
    if not sen and not gov:
        return hist                      # no votes yet
    snaps = hist["snaps"]
    if snaps:
        last = snaps[-1]
        if last.get("s") == sen and last.get("g") == gov:
            return hist                  # nothing has changed
        if (now - datetime.datetime.fromisoformat(last["t"])).total_seconds() < EVERY:
            return hist                  # too soon after the last snapshot
    snaps.append({"t": now.isoformat(timespec="seconds"), "s": sen, "g": gov})
    hist["snaps"] = snaps[-MAX_SNAPS:]
    return hist


def main(root=None):
    root = pathlib.Path(root) if root else pathlib.Path(__file__).resolve().parent
    data = json.loads((root / "data.json").read_text())
    try:
        prev = fetch_previous()
    except Exception as e:
        print("STOPPING THIS RUN:", e)
        sys.exit(1)
    try:
        hist = update(prev, data, datetime.datetime.now(ZoneInfo("America/Chicago")))
    except Exception as e:
        print("could not add a snapshot, keeping the earlier history:", e)
        hist = prev if isinstance(prev, dict) else {"mode": data.get("mode", "live"), "snaps": []}
    (root / "history.json").write_text(json.dumps(hist, separators=(",", ":")) + "\n")
    print("history: mode", hist.get("mode"), "|", len(hist.get("snaps", [])), "snapshots")


if __name__ == "__main__":
    main()
