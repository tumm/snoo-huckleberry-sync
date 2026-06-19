#!/usr/bin/env python3
"""
SNOO read-only diagnostic.

Purpose: figure out which data source gives us individual sleep sessions with
start/end times and (ideally) an asleep-vs-soothing breakdown, so we can pick
the right architecture for the SNOO -> Huckleberry sync.

THIS SCRIPT IS READ-ONLY.
  - It authenticates to the SNOO/Happiest Baby API with your credentials.
  - It performs only HTTP GETs.
  - It NEVER writes to SNOO and NEVER touches Huckleberry at all.

Usage:
    pip install pysnoo2 --break-system-packages    # if not already installed
    export SNOO_USERNAME='you@example.com'
    export SNOO_PASSWORD='your-snoo-password'
    python3 snoo_diagnostic.py

Output is verbose and safe to paste back (it redacts your token and IDs by
default; pass --no-redact if you're comfortable sharing raw).
"""

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone

try:
    import aiohttp
    import pysnoo2
    from pysnoo2 import Snoo, SnooAuthSession, SnooPubNub
    from pysnoo2.models import SessionLevel
    from pysnoo2.const import SNOO_API_URI
except ImportError as e:
    print(f"Missing dependency: {e}\nRun: pip install -r requirements.txt")
    sys.exit(1)


REDACT = True


def redact(value: str) -> str:
    if not REDACT or not value:
        return value
    s = str(value)
    if len(s) <= 6:
        return "***"
    return s[:3] + "…" + s[-3:]


def jdump(obj) -> str:
    """Pretty-print JSON, redacting obvious token/id fields."""
    def _scrub(o):
        if isinstance(o, dict):
            out = {}
            for k, v in o.items():
                if REDACT and k.lower() in {
                    "token", "access_token", "refresh_token", "id_token",
                    "email", "userid", "user_id", "babyid", "baby_id",
                }:
                    out[k] = redact(str(v))
                else:
                    out[k] = _scrub(v)
            return out
        if isinstance(o, list):
            return [_scrub(x) for x in o]
        return o
    return json.dumps(_scrub(obj), indent=2, default=str)


def banner(title: str):
    print("\n" + "=" * 70)
    print(f"  {title}")
    print("=" * 70)


async def probe_get(session: SnooAuthSession, label: str, url: str, params: dict | None = None):
    """Perform a read-only GET and report status + body shape."""
    print(f"\n--- PROBE: {label}")
    print(f"    GET {url}")
    if params:
        print(f"    params: {params}")
    try:
        async with await session.get(url, params=params or {}) as resp:
            status = resp.status
            print(f"    status: {status}")
            if status != 200:
                body = await resp.text()
                print(f"    body (first 300 chars): {body[:300]}")
                return None
            try:
                data = await resp.json()
            except Exception:
                text = await resp.text()
                print(f"    non-JSON body (first 500 chars): {text[:500]}")
                return None
            # Summarise shape
            if isinstance(data, dict):
                print(f"    JSON object, top-level keys: {list(data.keys())}")
            elif isinstance(data, list):
                print(f"    JSON array, length: {len(data)}")
                if data:
                    print(f"    first element keys: "
                          f"{list(data[0].keys()) if isinstance(data[0], dict) else type(data[0])}")
            print("    BODY:")
            print("\n".join("    " + line for line in jdump(data).splitlines()[:80]))
            return data
    except Exception as e:
        print(f"    ERROR: {type(e).__name__}: {e}")
        return None


async def main():
    global REDACT
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-redact", action="store_true",
                        help="Do not redact IDs/tokens in output")
    args = parser.parse_args()
    REDACT = not args.no_redact

    username = os.environ.get("SNOO_USERNAME")
    password = os.environ.get("SNOO_PASSWORD")
    if not username or not password:
        print("Set SNOO_USERNAME and SNOO_PASSWORD environment variables first.")
        sys.exit(1)

    print("SNOO read-only diagnostic")
    print(f"pysnoo2 location: {pysnoo2.__file__}")
    print(f"API base: {SNOO_API_URI}")
    print(f"Redaction: {'ON (safe to paste)' if REDACT else 'OFF (raw)'}")

    token_holder = {}

    def token_updater(t):
        token_holder["token"] = t

    async with aiohttp.ClientSession() as websession:
        # pysnoo2's SnooAuthSession manages its own aiohttp internally via OAuth2Session;
        # we construct it with credentials and let it fetch a token.
        auth = SnooAuthSession(username=username, password=password,
                               token_updater=token_updater)
        try:
            banner("STEP 1: Authenticate (read-only token fetch)")
            token = await auth.fetch_token()
            token_updater(token)
            print("    Auth OK. Token acquired (value hidden).")
        except Exception as e:
            print(f"    AUTH FAILED: {type(e).__name__}: {e}")
            print("    Check SNOO_USERNAME / SNOO_PASSWORD. Nothing was written.")
            await auth.close()
            return

        snoo = Snoo(auth)

        # ---- Identify baby ----
        baby_id = None
        try:
            banner("STEP 2: Devices & baby")
            devices = await snoo.get_devices()
            print(f"    devices found: {len(devices)}")
            baby = await snoo.get_baby()
            baby_id = getattr(baby, "baby", None) or getattr(baby, "id", None)
            # Baby model may store id under different attr; dump what we can
            print(f"    baby object attrs: "
                  f"{[a for a in dir(baby) if not a.startswith('_')][:20]}")
            print(f"    resolved baby_id: {redact(baby_id) if baby_id else 'UNKNOWN'}")
        except Exception as e:
            print(f"    ERROR identifying baby: {type(e).__name__}: {e}")

        if not baby_id:
            print("\n    Could not resolve baby_id; remaining probes need it. "
                  "Paste output above and we'll adjust.")
            await auth.close()
            return

        # ---- Exposed method: last session (Path A baseline) ----
        try:
            banner("STEP 3: get_last_session (Path A baseline)")
            last = await snoo.get_last_session(baby_id)
            print(f"    start_time: {last.start_time}")
            print(f"    end_time:   {last.end_time}")
            print(f"    levels (count): {len(last.levels)}")
            print(f"    levels: {[l.value for l in last.levels][:30]}")
            print(f"    current_status: {last.current_status}")
        except Exception as e:
            print(f"    ERROR: {type(e).__name__}: {e}")

        # ---- Exposed method: aggregated avg (daily totals only) ----
        try:
            banner("STEP 4: get_aggregated_session_avg (daily totals)")
            from pysnoo2.models import AggregatedSessionInterval
            start = datetime.now() - timedelta(days=2)
            agg = await snoo.get_aggregated_session_avg(
                baby_id, start_time=start,
                interval=AggregatedSessionInterval.WEEK, days=True)
            print(f"    total_sleep_avg:   {agg.total_sleep_avg}")
            print(f"    day_sleep_avg:     {agg.day_sleep_avg}")
            print(f"    night_sleep_avg:   {agg.night_sleep_avg}")
            print(f"    longest_sleep_avg: {agg.longest_sleep_avg}")
            print(f"    has per-day 'days' block: {agg.days is not None}")
        except Exception as e:
            print(f"    ERROR: {type(e).__name__}: {e}")

        # ---- PROBES: hunt for per-segment session timeline (Paths B/C) ----
        banner("STEP 5: Probe candidate endpoints for per-SESSION timeline")
        print("These are read-only GETs hunting for a list of individual sessions")
        print("with start/end + asleep/soothing breakdown. 404/403 is fine — it just")
        print("means that endpoint shape isn't available on your account.")

        now = datetime.now(timezone.utc)
        start_24h = now - timedelta(hours=24)
        start_48h = now - timedelta(hours=48)

        # Common datetime formats the API might expect
        fmt_micro = "%Y-%m-%d %H:%M:%S.%f"
        fmt_iso = "%Y-%m-%dT%H:%M:%S.000Z"

        base = SNOO_API_URI
        b = baby_id

        candidates = [
            # (label, url, params)
            ("aggregated (no /avg) micro-fmt",
             f"{base}/ss/v2/babies/{b}/sessions/aggregated/",
             {"startTime": start_24h.strftime(fmt_micro)[:-3]}),
            ("aggregated (no /avg) with days",
             f"{base}/ss/v2/babies/{b}/sessions/aggregated/",
             {"startTime": start_24h.strftime(fmt_micro)[:-3], "days": "true"}),
            ("sessions list (v2) start+end iso",
             f"{base}/ss/v2/babies/{b}/sessions/",
             {"startTime": start_24h.strftime(fmt_iso), "endTime": now.strftime(fmt_iso)}),
            ("sessions list (v10) start+end iso",
             f"{base}/ss/me/v10/babies/{b}/sessions/",
             {"startTime": start_24h.strftime(fmt_iso), "endTime": now.strftime(fmt_iso)}),
            ("sessions list (v2) micro-fmt",
             f"{base}/ss/v2/babies/{b}/sessions/",
             {"startTime": start_48h.strftime(fmt_micro)[:-3]}),
            ("sessions/aggregated/avg WITHOUT avg suffix variant",
             f"{base}/ss/v2/babies/{b}/sessions/aggregated",
             {"startTime": start_24h.strftime(fmt_micro)[:-3], "interval": "week", "days": "true"}),
            ("daily sessions",
             f"{base}/ss/v2/babies/{b}/sessions/daily/",
             {"startTime": start_24h.strftime(fmt_iso), "endTime": now.strftime(fmt_iso)}),
        ]

        found_timeline = False
        for label, url, params in candidates:
            data = await probe_get(auth, label, url, params)
            # Heuristic: does the payload contain per-segment items with a 'type'
            # of asleep/soothing/awake and individual startTimes?
            if data and isinstance(data, dict):
                blob = json.dumps(data).lower()
                if '"asleep"' in blob or '"soothing"' in blob or "sessionid" in blob:
                    print("    >>> This endpoint appears to contain per-segment "
                          "session data (asleep/soothing/sessionId). PROMISING.")
                    found_timeline = True

        # ---- PATH C: PubNub ActivityState history ----
        try:
            banner("STEP 6: PubNub Path C — ActivityState history (100 events)")
            print("This is READ-ONLY. We subscribe to no live channel; history() is a REST call.")
            devices = await snoo.get_devices()
            if not devices:
                print("    No devices found — cannot build PubNub channel.")
            else:
                serial = devices[0].serial_number
                print(f"    Device serial: {redact(serial)}")
                pubnub_token = await snoo.pubnub_auth()
                pubnub = SnooPubNub(
                    pubnub_token,
                    snoo.pubnub_auth,
                    serial,
                    f"pn-pysnoo-diag-{serial}",
                )
                try:
                    events = await pubnub.history(100)
                    print(f"    Retrieved {len(events)} ActivityState events.")
                    if not events:
                        print("    No events returned — SNOO may not have been used recently.")
                    else:
                        # Sort oldest-first so the timeline reads naturally
                        events_sorted = sorted(events, key=lambda e: e.event_time)
                        now_utc = datetime.now(timezone.utc)
                        lookback = now_utc - timedelta(hours=6)

                        distinct_levels = set()
                        session_ids = set()

                        print(f"\n    Full timeline (oldest → newest):")
                        print(f"    {'event_time (UTC)':<28} {'state':<20} {'active':>6}  session_id")
                        print(f"    {'-'*28} {'-'*20} {'-'*6}  {'-'*12}")
                        for ev in events_sorted:
                            sm = ev.state_machine
                            distinct_levels.add(sm.state)
                            if sm.session_id:
                                session_ids.add(sm.session_id)
                            age_marker = " ← in 6h window" if ev.event_time >= lookback else ""
                            sid_display = redact(sm.session_id) if sm.session_id else "—"
                            print(f"    {str(ev.event_time):<28} {sm.state.value:<20} "
                                  f"{'yes' if sm.is_active_session else 'no':>6}  "
                                  f"{sid_display}{age_marker}")

                        print(f"\n    Distinct SessionLevel values seen: "
                              f"{sorted(l.value for l in distinct_levels)}")
                        print(f"    Distinct session_ids seen: {len(session_ids)}")

                        in_window = [e for e in events_sorted if e.event_time >= lookback]
                        print(f"\n    Events within 6-hour lookback window: {len(in_window)}")
                        if len(in_window) < len(events_sorted):
                            oldest_in_window = min((e.event_time for e in in_window), default=None)
                            print(f"    Oldest event in window: {oldest_in_window}")
                            print(f"    NOTE: if 100 events don't cover 6 hours, we'll need")
                            print(f"    timetoken-based pagination for the full window.")

                        # Proposed mapping for confirmation:
                        asleep_levels = {SessionLevel.BASELINE, SessionLevel.WEANING_BASELINE}
                        soothing_levels = {SessionLevel.LEVEL1, SessionLevel.LEVEL2,
                                           SessionLevel.LEVEL3, SessionLevel.LEVEL4}
                        inactive_levels = {SessionLevel.ONLINE, SessionLevel.NONE,
                                           SessionLevel.PRETIMEOUT, SessionLevel.TIMEOUT}
                        print(f"\n    Proposed level mapping (lock after reviewing above):")
                        print(f"      ASLEEP   : {sorted(l.value for l in asleep_levels & distinct_levels)}")
                        print(f"      SOOTHING : {sorted(l.value for l in soothing_levels & distinct_levels)}")
                        print(f"      AWAKE    : {sorted(l.value for l in inactive_levels & distinct_levels)}")
                        unseen_map = distinct_levels - asleep_levels - soothing_levels - inactive_levels
                        if unseen_map:
                            print(f"      UNMAPPED : {sorted(l.value for l in unseen_map)} ← needs decision")
                finally:
                    await pubnub.stop()
        except Exception as e:
            print(f"    ERROR in PubNub Path C: {type(e).__name__}: {e}")

        banner("FINAL SUMMARY")
        print("Path A (get_last_session): single most-recent session only; no segment split.")
        print(f"Path B (data API /ss/): "
              f"{'PROMISING endpoint found ✅ — see Step 5 above' if found_timeline else 'no per-segment endpoint found ❌'}")
        print("Path C (PubNub history): see Step 6 above.")
        print()
        print("Decision guide:")
        print("  If Step 5 found a PROMISING endpoint → Path B (data API) is viable.")
        print("  If Step 6 returned events with asleep/soothing/awake transitions → Path C confirmed.")
        print("  If Step 6 returned 0 events or <10 events → SNOO not used recently; try again after a session.")
        print()
        print("Paste this entire output back and we'll lock the architecture and level mapping.")
        print("Reminder: this script wrote nothing, anywhere.")

        await auth.close()


if __name__ == "__main__":
    asyncio.run(main())
