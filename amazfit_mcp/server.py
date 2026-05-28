#!/usr/bin/env python3
"""
Amazfit Workout MCP Server

Exposes your Supabase workout data and Zepp Health cloud data as MCP tools
so AI assistants (Claude, etc.) can read and update your training directly.

NOTE on Zepp plan writes: The Zepp/Huami unofficial API has no documented
endpoint for pushing training plans to the watch. Use upsert_plan_exercise /
toggle_plan_exercise to manage plans in Supabase instead — the workout-log
viewer at index.html reads from there.

Setup:
  pip install -r requirements.txt
  export SUPABASE_SERVICE_KEY=<your-service-role-key>   # needed for writes
  export ZEPP_APP_TOKEN=<token>                          # optional, or call zepp_authenticate
  export ZEPP_USER_ID=<user_id>                          # optional, paired with above
  export ZEPP_HOST=api-mifit-de2.huami.com               # change for US: api-mifit-us3.zepp.com
  python -m amazfit_mcp.server
"""

import json
import os
import urllib.parse
import uuid
from typing import Optional

import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("amazfit-workout")

# ── Config ─────────────────────────────────────────────────────────────────────

SUPABASE_URL = os.environ.get(
    "SUPABASE_URL", "https://amchallknjysticvfohh.supabase.co"
)
SUPABASE_ANON_KEY = os.environ.get(
    "SUPABASE_ANON_KEY",
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
    ".eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImFtY2hhbGxrbmp5c3RpY3Zmb2hoIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzkxMTU3OTcsImV4cCI6MjA5NDY5MTc5N30"
    ".rKKRtiovYTtEjzGtiRPmgoSYP0kkKJItKYRPVHzgNRI",
)
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
ZEPP_HOST = os.environ.get("ZEPP_HOST", "api-mifit-de2.huami.com")

# Runtime Zepp credentials (populated by zepp_authenticate or env vars)
_zepp_token: str = os.environ.get("ZEPP_APP_TOKEN", "")
_zepp_user_id: str = os.environ.get("ZEPP_USER_ID", "")


# ── Helpers ────────────────────────────────────────────────────────────────────

def _supa_headers(write: bool = False) -> dict:
    key = SUPABASE_SERVICE_KEY if (write and SUPABASE_SERVICE_KEY) else SUPABASE_ANON_KEY
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }


def _zepp_headers() -> dict:
    return {
        "apptoken": _zepp_token,
        "appPlatform": "web",
        "appname": "com.xiaomi.hm.health",
        "Accept": "application/json",
    }


def _zepp_check() -> Optional[str]:
    if not _zepp_token:
        return (
            "Not authenticated with Zepp. "
            "Call zepp_authenticate(email, password) or set ZEPP_APP_TOKEN env var."
        )
    return None


# ── Supabase: read ─────────────────────────────────────────────────────────────

@mcp.tool()
async def get_workout_sessions(start_date: str, end_date: str) -> str:
    """
    List workout sessions logged in Supabase between start_date and end_date.
    Dates in YYYY-MM-DD format. Returns date, day_id, and top-level session stats.
    """
    params = [
        ("date", f"gte.{start_date}"),
        ("date", f"lte.{end_date}"),
        ("select", "date,day_id,session_data"),
        ("order", "date.asc"),
    ]
    async with httpx.AsyncClient() as c:
        r = await c.get(
            f"{SUPABASE_URL}/rest/v1/workout_sessions",
            params=params,
            headers=_supa_headers(),
        )
    r.raise_for_status()
    rows = r.json()

    # Surface compact summaries so the model doesn't drown in set-level detail
    summaries = []
    for row in rows:
        s = row.get("session_data", {})
        summaries.append({
            "date": row["date"],
            "day_id": row["day_id"],
            "totalTime": s.get("totalTime"),
            "energy": s.get("energy"),
            "bw": s.get("bw"),
            "painPost": s.get("painPost"),
            "sessionNote": s.get("sessionNote"),
            "exerciseCount": len(s.get("exData", {})),
        })
    return json.dumps(summaries, indent=2)


@mcp.tool()
async def get_session_details(date: str) -> str:
    """
    Get the full session record for a specific date (YYYY-MM-DD), including
    per-exercise set data (weight, reps, done flag).
    """
    params = [("date", f"eq.{date}"), ("select", "*")]
    async with httpx.AsyncClient() as c:
        r = await c.get(
            f"{SUPABASE_URL}/rest/v1/workout_sessions",
            params=params,
            headers=_supa_headers(),
        )
    r.raise_for_status()
    rows = r.json()
    return json.dumps(rows[0] if rows else None, indent=2)


@mcp.tool()
async def get_workout_plan(day_id: Optional[str] = None, enabled_only: bool = True) -> str:
    """
    Return exercises in the Supabase workout plan.
    - day_id: filter to one day (mon/tue/wed/thu/fri/sat/sun), or omit for full week
    - enabled_only: if True (default), skip disabled exercises
    """
    params = [
        ("select", "day_id,ex_id,name,sets,type,order_num,enabled"),
        ("order", "day_id.asc,order_num.asc"),
    ]
    if day_id:
        params.append(("day_id", f"eq.{day_id}"))
    if enabled_only:
        params.append(("enabled", "eq.true"))
    async with httpx.AsyncClient() as c:
        r = await c.get(
            f"{SUPABASE_URL}/rest/v1/workout_plan",
            params=params,
            headers=_supa_headers(),
        )
    r.raise_for_status()
    return json.dumps(r.json(), indent=2)


# ── Supabase: write ────────────────────────────────────────────────────────────

@mcp.tool()
async def log_workout_session(date: str, day_id: str, session_data: str) -> str:
    """
    Log (or overwrite) a workout session in Supabase.

    Parameters:
    - date: YYYY-MM-DD
    - day_id: mon | tue | wed | thu | fri | sat | sun
    - session_data: JSON string with structure:
        {
          "totalTime": 60,       // minutes
          "energy": 7,           // /10
          "painPost": 2,         // /10
          "bw": 185,             // body weight lbs (optional)
          "sessionNote": "...",  // optional
          "exData": {
            "<ex_id>": {
              "sets": [
                { "weight": 135, "reps": 8, "done": true },
                ...
              ],
              "note": "..."  // optional
            }
          }
        }

    Requires SUPABASE_SERVICE_KEY env var for write access.
    """
    if not SUPABASE_SERVICE_KEY:
        return "Write failed: SUPABASE_SERVICE_KEY env var is not set."
    try:
        parsed = json.loads(session_data)
    except json.JSONDecodeError as e:
        return f"Invalid session_data JSON: {e}"

    payload = {"date": date, "day_id": day_id, "session_data": parsed}
    async with httpx.AsyncClient() as c:
        r = await c.post(
            f"{SUPABASE_URL}/rest/v1/workout_sessions",
            json=payload,
            headers={**_supa_headers(write=True), "Prefer": "resolution=merge-duplicates"},
        )
    r.raise_for_status()
    return f"Session for {date} ({day_id}) saved successfully."


@mcp.tool()
async def upsert_plan_exercise(
    day_id: str,
    ex_id: str,
    name: str,
    sets: int,
    type: str,
    order_num: int = 0,
    enabled: bool = True,
) -> str:
    """
    Add or update an exercise in the Supabase workout plan.

    Parameters:
    - day_id: mon | tue | wed | thu | fri | sat | sun
    - ex_id: unique snake_case identifier (e.g. "bench_press", "lat_pulldown")
    - name: display name (e.g. "Bench Press")
    - sets: number of planned sets
    - type: weighted | timed | amrap | bilateral | combo
    - order_num: display order within the day (lower = shown first)
    - enabled: whether this exercise is active in the plan

    Requires SUPABASE_SERVICE_KEY env var for write access.
    """
    if not SUPABASE_SERVICE_KEY:
        return "Write failed: SUPABASE_SERVICE_KEY env var is not set."

    payload = {
        "day_id": day_id,
        "ex_id": ex_id,
        "name": name,
        "sets": sets,
        "type": type,
        "order_num": order_num,
        "enabled": enabled,
    }
    async with httpx.AsyncClient() as c:
        r = await c.post(
            f"{SUPABASE_URL}/rest/v1/workout_plan",
            json=payload,
            headers={**_supa_headers(write=True), "Prefer": "resolution=merge-duplicates"},
        )
    r.raise_for_status()
    action = "enabled" if enabled else "disabled"
    return f"Exercise '{name}' ({ex_id}) upserted for {day_id}, {action}."


@mcp.tool()
async def toggle_plan_exercise(day_id: str, ex_id: str, enabled: bool) -> str:
    """
    Enable or disable an exercise in the workout plan without changing other fields.
    Requires SUPABASE_SERVICE_KEY env var for write access.
    """
    if not SUPABASE_SERVICE_KEY:
        return "Write failed: SUPABASE_SERVICE_KEY env var is not set."

    async with httpx.AsyncClient() as c:
        r = await c.patch(
            f"{SUPABASE_URL}/rest/v1/workout_plan",
            params=[("day_id", f"eq.{day_id}"), ("ex_id", f"eq.{ex_id}")],
            json={"enabled": enabled},
            headers=_supa_headers(write=True),
        )
    r.raise_for_status()
    state = "enabled" if enabled else "disabled"
    return f"{ex_id} on {day_id} is now {state}."


# ── Zepp: authentication ───────────────────────────────────────────────────────

@mcp.tool()
async def zepp_authenticate(email: str, password: str, country_code: str = "US") -> str:
    """
    Authenticate against Zepp/Huami servers with your Zepp app login.
    Stores the app_token in memory for subsequent Zepp tool calls.
    Tokens last ~30 days; persist them via ZEPP_APP_TOKEN + ZEPP_USER_ID env vars.

    NOTE: Uses the reverse-engineered Huami auth API — may break if Zepp changes it.
    """
    global _zepp_token, _zepp_user_id

    ua = "MiFit/4.6.0 (iPhone; iOS 14.0.1; Scale/2.00)"

    # Step 1: obtain short-lived access token via redirect
    async with httpx.AsyncClient(follow_redirects=False) as c:
        r1 = await c.post(
            f"https://api-user.huami.com/registrations/{urllib.parse.quote(email)}/tokens",
            data={
                "state": "REDIRECTION",
                "client_id": "HuaMi",
                "redirect_uri": (
                    "https://s3-us-west-2.amazonaws.com/hm-registration/successsEnRegister.html"
                ),
                "token": "access",
                "country_code": country_code,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": ua},
        )

    location = r1.headers.get("Location", "")
    qs = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(location).query))
    access_token = qs.get("access") or qs.get("token")
    if not access_token:
        return (
            f"Step 1 failed (status {r1.status_code}). "
            f"Location header: {location or '(none)'}. "
            "Check email/password."
        )

    # Step 2: exchange access token for long-lived app_token
    async with httpx.AsyncClient() as c:
        r2 = await c.post(
            "https://account.huami.com/v2/client/login",
            data={
                "app_name": "com.xiaomi.hm.health",
                "dn": (
                    "account.huami.com,api-user.huami.com,"
                    "api-mifit.huami.com,app-analytics.huami.com"
                ),
                "device_id": str(uuid.uuid4()),
                "device_model": "phone",
                "grant_type": "access_token",
                "third_name": "huami",
                "login_token": access_token,
                "country_code": country_code,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": ua},
        )

    body = r2.json()
    token_info = body.get("token_info", body)
    _zepp_token = token_info.get("app_token", "")
    _zepp_user_id = str(token_info.get("user_id", ""))

    if not _zepp_token:
        return f"Step 2 failed: {json.dumps(body, indent=2)}"

    return (
        f"Authenticated successfully.\n"
        f"  user_id   = {_zepp_user_id}\n"
        f"  app_token = {_zepp_token[:12]}...\n\n"
        f"To persist across restarts, set env vars:\n"
        f"  ZEPP_APP_TOKEN={_zepp_token}\n"
        f"  ZEPP_USER_ID={_zepp_user_id}"
    )


# ── Zepp: read workout data ────────────────────────────────────────────────────

@mcp.tool()
async def zepp_get_workout_history(limit: int = 20) -> str:
    """
    Fetch recent workout history from your Zepp/Amazfit cloud account.
    Returns run/activity summaries (name, distance, duration, calories, trackid).

    Requires zepp_authenticate or ZEPP_APP_TOKEN env var.
    If you get 403, set ZEPP_HOST=api-mifit-us3.zepp.com for US accounts.
    """
    if err := _zepp_check():
        return err

    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(
            f"https://{ZEPP_HOST}/v1/sport/run/history.json",
            params={"source": "run.mifit.huami.com"},
            headers=_zepp_headers(),
        )

    if r.status_code == 403:
        return (
            "403 Forbidden. Your account may use a different regional host.\n"
            "Try setting ZEPP_HOST to one of:\n"
            "  api-mifit-us3.zepp.com  (US)\n"
            "  api-mifit-de2.huami.com (EU, default)\n"
            "  api-mifit.huami.com     (generic)"
        )
    r.raise_for_status()

    data = r.json()
    items = data if isinstance(data, list) else data.get("data", [data])
    return json.dumps(items[:limit], indent=2)


@mcp.tool()
async def zepp_get_workout_details(trackid: str) -> str:
    """
    Get detailed GPS/HR data for a specific Zepp workout by its trackid.
    trackid comes from zepp_get_workout_history results.

    Requires zepp_authenticate or ZEPP_APP_TOKEN env var.
    Note: location data is Base64 + delta-encoded in the raw response.
    """
    if err := _zepp_check():
        return err

    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(
            f"https://{ZEPP_HOST}/v1/sport/run/detail.json",
            params={"trackid": trackid, "source": "run.mifit.huami.com"},
            headers=_zepp_headers(),
        )
    r.raise_for_status()
    return json.dumps(r.json(), indent=2)


@mcp.tool()
async def zepp_get_daily_stats(date: str) -> str:
    """
    Get daily health stats from Zepp for a given date (YYYY-MM-DD):
    steps, distance, calories, sleep summary, and activity stages.

    Requires zepp_authenticate or ZEPP_APP_TOKEN + ZEPP_USER_ID env vars.
    """
    if err := _zepp_check():
        return err
    if not _zepp_user_id:
        return "ZEPP_USER_ID not set. Authenticate first or set the env var."

    params = {
        "query_type": "summary",
        "device_type": "0",
        "userid": _zepp_user_id,
        "from_date": date,
        "to_date": date,
    }
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(
            f"https://{ZEPP_HOST}/v1/data/band_data.json",
            params=params,
            headers=_zepp_headers(),
        )
    r.raise_for_status()
    return json.dumps(r.json(), indent=2)


# ── Entry ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run()
