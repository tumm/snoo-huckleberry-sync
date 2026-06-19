#!/usr/bin/env python3
"""
SNOO read-only diagnostic.

Purpose: confirm which data source gives us individual sleep sessions with
start/end times and an asleep-vs-soothing breakdown so we can lock the
architecture for the SNOO -> Huckleberry sync.

THIS SCRIPT IS READ-ONLY.
  - Authenticates to SNOO via AWS Cognito (the current auth mechanism).
  - Performs only HTTP GETs (REST probes + PubNub v2/history).
  - NEVER writes to SNOO. NEVER touches Huckleberry.

Note: pysnoo2 is broken — Happiest Baby migrated from OAuth (/us/v3/login)
to AWS Cognito. This script uses 'python-snoo' (Lash-L) for auth.

Usage:
    pip install -r requirements.txt
    export SNOO_USERNAME='you@example.com'
    export SNOO_PASSWORD='your-snoo-password'
    .venv/bin/python snoo_diagnostics.py

Output is redacted by default (safe to paste). Pass --no-redact for raw.
"""

import argparse
import asyncio
import json
import os
import secrets
import sys
import uuid
from datetime import datetime, timedelta, timezone

try:
    import aiohttp
    import python_snoo
    from python_snoo.snoo import Snoo
    from python_snoo.containers import SnooData, SnooStates
except ImportError as e:
    print(f"Missing dependency: {e}\nRun: pip install -r requirements.txt")
    sys.exit(1)

SNOO_API_URI = "https://api-us-east-1-prod.happiestbaby.com"
PUBNUB_SUB_KEY = "sub-c-97bade2a-483d-11e6-8b3b-02ee2ddab7fe"
PUBNUB_ORIGIN = "happiestbaby.pubnubapi.com"

REDACT = True


def redact(value) -> str:
    if not REDACT or not value:
        return str(value) if value else ""
    s = str(value)
    if len(s) <= 6:
        return "***"
    return s[:3] + "…" + s[-3:]


def jdump(obj) -> str:
    def _scrub(o):
        if isinstance(o, dict):
            out = {}
            for k, v in o.items():
                if REDACT and k.lower() in {
                    "token", "access_token", "refresh_token", "id_token",
                    "accesstoken", "idtoken", "refreshtoken",
                    "email", "userid", "user_id", "babyid", "baby_id",
                    "auth", "authorization",
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


async def probe_get(session: aiohttp.ClientSession, label: str, url: str,
                    headers: dict, params: dict | None = None):
    """Perform a read-only GET and report status + body shape."""
    print(f"\n--- PROBE: {label}")
    print(f"    GET {url}")
    if params:
        print(f"    params: {params}")
    try:
        async with session.get(url, headers=headers, params=params or {}) as resp:
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
            if isinstance(data, dict):
                print(f"    JSON object, top-level keys: {list(data.keys())}")
            elif isinstance(data, list):
                print(f"    JSON array, length: {len(data)}")
                if data:
                    first = data[0]
                    print(f"    first element keys: "
                          f"{list(first.keys()) if isinstance(first, dict) else type(first)}")
            print("    BODY:")
            print("\n".join("    " + line for line in jdump(data).splitlines()[:80]))
            return data
    except Exception as e:
        print(f"    ERROR: {type(e).__name__}: {e}")
        return None


def _event_time(snoo_data: SnooData) -> datetime:
    """Convert event_time_ms (int) to UTC datetime."""
    return datetime.utcfromtimestamp(snoo_data.event_time_ms / 1000).replace(
        tzinfo=timezone.utc)


async def main():
    global REDACT
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-redact", action="store_true")
    args = parser.parse_args()
    REDACT = not args.no_redact

    username = os.environ.get("SNOO_USERNAME")
    password = os.environ.get("SNOO_PASSWORD")
    if not username or not password:
        print("Set SNOO_USERNAME and SNOO_PASSWORD environment variables first.")
        sys.exit(1)

    print("SNOO read-only diagnostic")
    print(f"python-snoo location: {python_snoo.__file__}")
    print(f"API base: {SNOO_API_URI}")
    print(f"Redaction: {'ON (safe to paste)' if REDACT else 'OFF (raw)'}")

    async with aiohttp.ClientSession() as websession:
        snoo = Snoo(username, password, websession)

        # ---- Auth via AWS Cognito ----
        banner("STEP 1: Authenticate (AWS Cognito → Snoo token)")
        auth_info = None
        try:
            auth_info = await snoo.authorize()
            print("    Auth OK.")
            print(f"    aws_id (IdToken):    {redact(str(getattr(auth_info, 'aws_id', '')))}")
            print(f"    aws_access:          {redact(str(getattr(auth_info, 'aws_access', '')))}")
            print(f"    snoo (PubNub token): {redact(str(getattr(auth_info, 'snoo', '')))}")
        except Exception as e:
            print(f"    AUTH FAILED: {type(e).__name__}: {e}")
            print("    Check SNOO_USERNAME / SNOO_PASSWORD. Nothing was written.")
            return

        id_token = getattr(auth_info, "aws_id", None)
        snoo_token = getattr(auth_info, "snoo", None)
        if not id_token:
            print("    Could not extract IdToken from auth_info — inspect auth_info attrs:")
            print(f"    {[a for a in dir(auth_info) if not a.startswith('_')]}")
            return

        rest_headers = {
            "Authorization": f"Bearer {id_token}",
            "Content-Type": "application/json",
            "User-Agent": "okhttp/4.7.2",
        }

        # ---- Devices + baby ----
        banner("STEP 2: Devices & baby")
        device_id = None
        baby_id = None
        try:
            devices = await snoo.get_devices()
            print(f"    devices found: {len(devices)}")
            if devices:
                d = devices[0]
                print(f"    device attrs: {[a for a in dir(d) if not a.startswith('_')]}")
                # python-snoo uses serialNumber (camelCase via Mashumaro alias or snake_case)
                device_id = (getattr(d, "serial_number", None)
                             or getattr(d, "serialNumber", None)
                             or getattr(d, "device_id", None))
                print(f"    device_id/serial: {redact(device_id)}")
        except Exception as e:
            print(f"    ERROR getting devices: {type(e).__name__}: {e}")

        try:
            babies = await snoo.get_babies()
            print(f"    babies found: {len(babies)}")
            if babies:
                b = babies[0]
                print(f"    baby attrs: {[a for a in dir(b) if not a.startswith('__')]}")
                baby_id = (getattr(b, "_id", None)
                           or getattr(b, "baby_id", None)
                           or getattr(b, "uid", None)
                           or getattr(b, "id", None))
                print(f"    baby_id: {redact(baby_id)}")
        except Exception as e:
            print(f"    ERROR getting babies: {type(e).__name__}: {e}")

        # ---- Path A: last session ----
        banner("STEP 3: get_last_session (Path A baseline)")
        if baby_id:
            url = f"{SNOO_API_URI}/ss/me/v10/babies/{baby_id}/sessions/last"
            data = await probe_get(websession, "last session", url, rest_headers)
            if data:
                print(f"    startTime: {data.get('startTime')}")
                print(f"    endTime:   {data.get('endTime')}")
                levels = data.get("levels", [])
                print(f"    levels (count): {len(levels)}")
                print(f"    first few levels: {levels[:5]}")
        else:
            print("    Skipped — no baby_id resolved.")

        # ---- Path A: aggregated avg ----
        banner("STEP 4: aggregated session avg (daily totals only)")
        if baby_id:
            start_2d = (datetime.now(timezone.utc) - timedelta(days=2)).strftime(
                "%Y-%m-%d %H:%M:%S.%f")[:-3]
            url = f"{SNOO_API_URI}/ss/v2/babies/{baby_id}/sessions/aggregated/avg/"
            data = await probe_get(websession, "aggregated/avg", url, rest_headers,
                                   {"startTime": start_2d, "interval": "week", "days": "true"})
        else:
            print("    Skipped — no baby_id resolved.")

        # ---- Path B: probe for per-segment data-API timeline ----
        banner("STEP 5: Probe candidate endpoints for per-session timeline (Path B)")
        print("GETs only — 404/403 expected for missing endpoints.")
        found_timeline = False
        if baby_id:
            now = datetime.now(timezone.utc)
            s24 = now - timedelta(hours=24)
            s48 = now - timedelta(hours=48)
            fmt_micro = "%Y-%m-%d %H:%M:%S.%f"
            fmt_iso = "%Y-%m-%dT%H:%M:%S.000Z"
            b = baby_id
            base = SNOO_API_URI

            candidates = [
                ("aggregated (no /avg) micro-fmt",
                 f"{base}/ss/v2/babies/{b}/sessions/aggregated/",
                 {"startTime": s24.strftime(fmt_micro)[:-3]}),
                ("aggregated (no /avg) with days",
                 f"{base}/ss/v2/babies/{b}/sessions/aggregated/",
                 {"startTime": s24.strftime(fmt_micro)[:-3], "days": "true"}),
                ("sessions list (v2) iso",
                 f"{base}/ss/v2/babies/{b}/sessions/",
                 {"startTime": s24.strftime(fmt_iso), "endTime": now.strftime(fmt_iso)}),
                ("sessions list (v10) iso",
                 f"{base}/ss/me/v10/babies/{b}/sessions/",
                 {"startTime": s24.strftime(fmt_iso), "endTime": now.strftime(fmt_iso)}),
                ("sessions list (v2) micro-fmt",
                 f"{base}/ss/v2/babies/{b}/sessions/",
                 {"startTime": s48.strftime(fmt_micro)[:-3]}),
                ("daily sessions",
                 f"{base}/ss/v2/babies/{b}/sessions/daily/",
                 {"startTime": s24.strftime(fmt_iso), "endTime": now.strftime(fmt_iso)}),
            ]
            for label, url, params in candidates:
                data = await probe_get(websession, label, url, rest_headers, params)
                if data:
                    blob = json.dumps(data).lower()
                    if '"asleep"' in blob or '"soothing"' in blob or "sessionid" in blob:
                        print("    >>> PROMISING: endpoint appears to have per-segment data.")
                        found_timeline = True
        else:
            print("    Skipped — no baby_id resolved.")

        # ---- Path C: PubNub v2/history ----
        banner("STEP 6: PubNub Path C — ActivityState history (100 events)")
        print("READ-ONLY REST call to PubNub v2/history — no live subscription.")
        if not device_id:
            print("    Skipped — no device_id resolved.")
        elif not snoo_token:
            print("    Skipped — no snoo PubNub token in auth_info.")
        else:
            try:
                req_uuid = uuid.uuid1()
                dev_uuid = uuid.uuid1()
                app_dev_id = secrets.token_urlsafe(18)
                channel = f"ActivityState.{device_id}"
                pub_url = (
                    f"https://{PUBNUB_ORIGIN}/v2/history"
                    f"/sub-key/{PUBNUB_SUB_KEY}"
                    f"/channel/{channel}"
                    f"?pnsdk=PubNub-Kotlin%2F7.4.0"
                    f"&auth={snoo_token}"
                    f"&requestid={req_uuid}"
                    f"&include_token=true"
                    f"&count=100"
                    f"&include_meta=false"
                    f"&reverse=false"
                    f"&uuid=android_{app_dev_id}_{dev_uuid}"
                )
                print(f"    Channel: ActivityState.{redact(device_id)}")
                async with websession.get(pub_url) as resp:
                    status = resp.status
                    print(f"    PubNub history status: {status}")
                    if status != 200:
                        body = await resp.text()
                        print(f"    Error body: {body[:400]}")
                    else:
                        raw = await resp.json()
                        # PubNub v2 history with include_token=true:
                        # [[{"message": {...}, "timetoken": "..."}, ...], start_tt, end_tt]
                        # Without include_token: [[msg1, msg2, ...], start_tt, end_tt]
                        if not isinstance(raw, list) or len(raw) < 1:
                            print(f"    Unexpected response shape: {type(raw)}")
                        else:
                            messages_raw = raw[0]
                            print(f"    Retrieved {len(messages_raw)} raw messages.")

                            events = []
                            parse_errors = 0
                            for item in messages_raw:
                                try:
                                    if isinstance(item, dict) and "message" in item:
                                        msg_dict = item["message"]
                                        tt = item.get("timetoken")
                                    else:
                                        msg_dict = item
                                        tt = None
                                    if isinstance(msg_dict, dict) and "system_state" in msg_dict:
                                        sd = SnooData.from_dict(msg_dict)
                                        events.append((sd, tt))
                                    else:
                                        print(f"    Skipping non-ActivityState message "
                                              f"(keys: {list(msg_dict.keys()) if isinstance(msg_dict, dict) else type(msg_dict)})")
                                except Exception as ex:
                                    parse_errors += 1
                                    if parse_errors <= 3:
                                        print(f"    Parse error on message: {ex}")

                            if parse_errors:
                                print(f"    Total parse errors: {parse_errors}")

                            if not events:
                                print("    No SnooData events parsed — SNOO may not have been used recently.")
                                print("    (Try running again after a session.)")
                            else:
                                events_sorted = sorted(events, key=lambda t: _event_time(t[0]))
                                now_utc = datetime.now(timezone.utc)
                                lookback = now_utc - timedelta(hours=6)

                                distinct_states: set[str] = set()
                                session_ids: set = set()

                                print(f"\n    Full timeline (oldest → newest):")
                                print(f"    {'event_time (UTC)':<28} {'state':<20} "
                                      f"{'active':>6}  session_id")
                                print(f"    {'-'*28} {'-'*20} {'-'*6}  {'-'*12}")
                                for sd, _tt in events_sorted:
                                    sm = sd.state_machine
                                    state_val = sm.state.value if hasattr(sm.state, 'value') else str(sm.state)
                                    distinct_states.add(state_val)
                                    sid = getattr(sm, "session_id", None)
                                    if sid:
                                        session_ids.add(sid)
                                    t = _event_time(sd)
                                    marker = " ← in 6h window" if t >= lookback else ""
                                    sid_display = redact(sid) if sid else "—"
                                    active = getattr(sm, "is_active_session", "?")
                                    print(f"    {str(t):<28} {state_val:<20} "
                                          f"{'yes' if active else 'no':>6}  {sid_display}{marker}")

                                print(f"\n    Distinct state values seen: {sorted(distinct_states)}")
                                print(f"    Distinct session_ids seen: {len(session_ids)}")

                                in_window = [(sd, tt) for sd, tt in events_sorted
                                             if _event_time(sd) >= lookback]
                                print(f"\n    Events within 6-hour lookback window: {len(in_window)}")
                                if len(in_window) < len(events_sorted):
                                    print(f"    NOTE: 100 events cover less than 6 hours.")
                                    print(f"    Timetoken pagination will be needed for full coverage.")

                                # Proposed mapping:
                                asleep_states = {"BASELINE", "WEANING_BASELINE", "baseline", "weaning_baseline"}
                                soothing_states = {"LEVEL1", "LEVEL2", "LEVEL3", "LEVEL4",
                                                   "level1", "level2", "level3", "level4"}
                                inactive_states = {"ONLINE", "NONE", "PRETIMEOUT", "TIMEOUT",
                                                   "stop", "none", "pretimeout", "timeout",
                                                   "online", "suspended", "manual"}
                                print(f"\n    Proposed level mapping (lock after reviewing above):")
                                print(f"      ASLEEP   : {sorted(asleep_states & distinct_states)}")
                                print(f"      SOOTHING : {sorted(soothing_states & distinct_states)}")
                                print(f"      AWAKE    : {sorted(inactive_states & distinct_states)}")
                                unmapped = distinct_states - asleep_states - soothing_states - inactive_states
                                if unmapped:
                                    print(f"      UNMAPPED : {sorted(unmapped)} ← needs decision")
            except Exception as e:
                print(f"    ERROR in PubNub Path C: {type(e).__name__}: {e}")
                import traceback; traceback.print_exc()

        banner("FINAL SUMMARY")
        print("Path A (last session REST): single most-recent session only; no segment split.")
        print(f"Path B (data API /ss/): "
              f"{'PROMISING endpoint found ✅ — see Step 5' if found_timeline else 'no per-segment endpoint found ❌'}")
        print("Path C (PubNub history): see Step 6 above.")
        print()
        print("Decision guide:")
        print("  Step 5 PROMISING marker → Path B viable (data API has the timeline).")
        print("  Step 6 events with asleep/soothing/awake states → Path C confirmed.")
        print("  Step 6 = 0 events → SNOO not used recently; run again after a session.")
        print()
        print("Paste this entire output back to lock the architecture and level mapping.")
        print("Reminder: this script wrote nothing, anywhere.")


if __name__ == "__main__":
    asyncio.run(main())
