#!/usr/bin/env python3
"""
Amazfit Workout MCP Server

Exposes Supabase workout data + Zepp Health cloud (including proprietary metrics
not synced to Health Connect) as MCP tools for AI assistants.

Transports:
  stdio (default) — for Claude Code CLI
  streamable-http — for Claude.ai web/mobile (set MCP_TRANSPORT=http)

Quick start:
  pip install -r requirements.txt
  export SUPABASE_SERVICE_KEY=<service-role-key>
  export ZEPP_APP_TOKEN=<token>      # or call zepp_authenticate at runtime
  export ZEPP_USER_ID=<user_id>
  python -m amazfit_mcp.server

For Claude.ai deployment:
  export MCP_TRANSPORT=http
  export PORT=8000
  export CONNECTOR_SECRET=<random-secret>   # Bearer token Claude.ai sends
  python -m amazfit_mcp.server
"""

import json
import os
import urllib.parse
import uuid
from datetime import datetime, timezone
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
CONNECTOR_SECRET = os.environ.get("CONNECTOR_SECRET", "")

# Runtime Zepp credentials (set via env or zepp_authenticate tool)
_zepp_token: str = os.environ.get("ZEPP_APP_TOKEN", "")
_zepp_user_id: str = os.environ.get("ZEPP_USER_ID", "")


# ── Internal helpers ───────────────────────────────────────────────────────────

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


def _to_ms(date_str: str) -> int:
    """Convert YYYY-MM-DD to milliseconds since UTC epoch."""
    dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


async def _zepp_events(
    event_type: str,
    sub_type: Optional[str],
    start_date: str,
    end_date: str,
    limit: int = 200,
) -> dict:
    """Generic fetch from the Zepp /v2/users/me/events endpoint."""
    params: dict = {
        "eventType": event_type,
        "from": _to_ms(start_date),
        "to": _to_ms(end_date) + 86_400_000,  # include end of last day
        "limit": limit,
        "reverse": 1,
    }
    if sub_type:
        params["subType"] = sub_type

    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(
            f"https://{ZEPP_HOST}/v2/users/me/events",
            params=params,
            headers=_zepp_headers(),
        )
    r.raise_for_status()
    return r.json()


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
          "totalTime": 60,
          "energy": 7,
          "painPost": 2,
          "bw": 185,
          "sessionNote": "...",
          "exData": {
            "<ex_id>": {
              "sets": [{ "weight": 135, "reps": 8, "done": true }],
              "note": "..."
            }
          }
        }

    Requires SUPABASE_SERVICE_KEY env var.
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
    - ex_id: unique snake_case id (e.g. "bench_press")
    - name: display name (e.g. "Bench Press")
    - sets: number of planned sets
    - type: weighted | timed | amrap | bilateral | combo
    - order_num: display order within the day
    - enabled: whether active in the plan

    Requires SUPABASE_SERVICE_KEY env var.
    """
    if not SUPABASE_SERVICE_KEY:
        return "Write failed: SUPABASE_SERVICE_KEY env var is not set."

    payload = {
        "day_id": day_id, "ex_id": ex_id, "name": name,
        "sets": sets, "type": type, "order_num": order_num, "enabled": enabled,
    }
    async with httpx.AsyncClient() as c:
        r = await c.post(
            f"{SUPABASE_URL}/rest/v1/workout_plan",
            json=payload,
            headers={**_supa_headers(write=True), "Prefer": "resolution=merge-duplicates"},
        )
    r.raise_for_status()
    return f"Exercise '{name}' ({ex_id}) upserted for {day_id}."


@mcp.tool()
async def toggle_plan_exercise(day_id: str, ex_id: str, enabled: bool) -> str:
    """Enable or disable an exercise in the workout plan. Requires SUPABASE_SERVICE_KEY."""
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
    return f"{ex_id} on {day_id} is now {'enabled' if enabled else 'disabled'}."


# ── Zepp: authentication ───────────────────────────────────────────────────────

@mcp.tool()
async def zepp_authenticate(email: str, password: str, country_code: str = "US") -> str:
    """
    Authenticate with Zepp/Huami servers using your Zepp app credentials.
    Stores the app_token in memory for all subsequent Zepp tool calls.
    Token lasts ~30 days — save it as ZEPP_APP_TOKEN + ZEPP_USER_ID env vars to persist.
    """
    global _zepp_token, _zepp_user_id

    ua = "MiFit/4.6.0 (iPhone; iOS 14.0.1; Scale/2.00)"

    async with httpx.AsyncClient(follow_redirects=False) as c:
        r1 = await c.post(
            f"https://api-user.huami.com/registrations/{urllib.parse.quote(email)}/tokens",
            data={
                "state": "REDIRECTION",
                "client_id": "HuaMi",
                "redirect_uri": "https://s3-us-west-2.amazonaws.com/hm-registration/successsEnRegister.html",
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
            f"Location: {location or '(none)'}. Check email/password."
        )

    async with httpx.AsyncClient() as c:
        r2 = await c.post(
            "https://account.huami.com/v2/client/login",
            data={
                "app_name": "com.xiaomi.hm.health",
                "dn": "account.huami.com,api-user.huami.com,api-mifit.huami.com,app-analytics.huami.com",
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
        f"Authenticated.\n"
        f"  user_id   = {_zepp_user_id}\n"
        f"  app_token = {_zepp_token[:12]}...\n\n"
        f"Persist with env vars:\n"
        f"  ZEPP_APP_TOKEN={_zepp_token}\n"
        f"  ZEPP_USER_ID={_zepp_user_id}"
    )


# ── Zepp: standard workout data ────────────────────────────────────────────────

@mcp.tool()
async def zepp_get_workout_history(limit: int = 20) -> str:
    """
    Fetch recent workout history from Zepp cloud (runs, strength sessions, etc.).
    Returns name, distance, duration, calories, trackid.
    Requires ZEPP_APP_TOKEN. If 403, set ZEPP_HOST=api-mifit-us3.zepp.com for US.
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
            "403 Forbidden. Try a different ZEPP_HOST:\n"
            "  api-mifit-us3.zepp.com  (US)\n"
            "  api-mifit-de2.huami.com (EU, default)"
        )
    r.raise_for_status()
    data = r.json()
    items = data if isinstance(data, list) else data.get("data", [data])
    return json.dumps(items[:limit], indent=2)


@mcp.tool()
async def zepp_get_workout_details(trackid: str) -> str:
    """
    Get detailed GPS/HR data for a specific workout by trackid.
    trackid comes from zepp_get_workout_history. Requires ZEPP_APP_TOKEN.
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
    Get daily steps, distance, calories, and sleep from Zepp for a date (YYYY-MM-DD).
    These DO sync to Health Connect, but this tool gives the raw Zepp values.
    Requires ZEPP_APP_TOKEN + ZEPP_USER_ID.
    """
    if err := _zepp_check():
        return err
    if not _zepp_user_id:
        return "ZEPP_USER_ID not set. Authenticate first or set the env var."

    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(
            f"https://{ZEPP_HOST}/v1/data/band_data.json",
            params={"query_type": "summary", "device_type": "0",
                    "userid": _zepp_user_id, "from_date": date, "to_date": date},
            headers=_zepp_headers(),
        )
    r.raise_for_status()
    return json.dumps(r.json(), indent=2)


# ── Zepp: proprietary metrics (NOT in Health Connect) ─────────────────────────

@mcp.tool()
async def zepp_get_training_load(start_date: str, end_date: str) -> str:
    """
    Get 7-day accumulated training load (wtlSum) and per-day load from Zepp.
    This measures cumulative stress on your body over the past week.
    NOT synced to Health Connect. Requires ZEPP_APP_TOKEN + ZEPP_USER_ID.
    Dates: YYYY-MM-DD.
    """
    if err := _zepp_check():
        return err
    if not _zepp_user_id:
        return "ZEPP_USER_ID not set."

    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(
            f"https://{ZEPP_HOST}/v2/watch/users/{_zepp_user_id}/WatchSportStatistics/SPORT_LOAD",
            params={"startDay": start_date, "endDay": end_date, "limit": 900, "isReverse": "true"},
            headers=_zepp_headers(),
        )
    r.raise_for_status()
    return json.dumps(r.json(), indent=2)


@mcp.tool()
async def zepp_get_readiness(start_date: str, end_date: str) -> str:
    """
    Get daily readiness / HybridCharge score (0-100) from Zepp.
    Zepp's composite recovery+energy score combining sleep, HR, stress, and RPE.
    NOT synced to Health Connect. Requires ZEPP_APP_TOKEN. Dates: YYYY-MM-DD.
    """
    if err := _zepp_check():
        return err
    try:
        data = await _zepp_events("readiness", "watch_score", start_date, end_date)
        return json.dumps(data, indent=2)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
async def zepp_get_body_battery(start_date: str, end_date: str) -> str:
    """
    Get body battery / energy charge levels throughout the day from Zepp.
    Shows your energy reserve curve across the day.
    NOT synced to Health Connect. Requires ZEPP_APP_TOKEN. Dates: YYYY-MM-DD.
    """
    if err := _zepp_check():
        return err
    try:
        data = await _zepp_events("Charge", "real_data", start_date, end_date)
        return json.dumps(data, indent=2)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
async def zepp_get_stress_history(start_date: str, end_date: str) -> str:
    """
    Get daily stress score history from Zepp (distinct from raw HRV).
    NOT synced to Health Connect. Requires ZEPP_APP_TOKEN. Dates: YYYY-MM-DD.
    """
    if err := _zepp_check():
        return err
    try:
        data = await _zepp_events("all_day_stress", None, start_date, end_date)
        return json.dumps(data, indent=2)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
async def zepp_get_pai_score(start_date: str, end_date: str) -> str:
    """
    Get PAI (Personal Activity Intelligence) score history from Zepp.
    PAI scores your weekly activity quality against your personal heart rate profile.
    NOT synced to Health Connect. Requires ZEPP_APP_TOKEN. Dates: YYYY-MM-DD.
    """
    if err := _zepp_check():
        return err
    try:
        data = await _zepp_events("PaiHealthInfo", None, start_date, end_date, limit=2000)
        return json.dumps(data, indent=2)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
async def zepp_get_daily_health_summary(start_date: str, end_date: str) -> str:
    """
    Get Zepp's daily health summary — a combined snapshot of activity, sleep,
    stress and recovery metrics for each day.
    NOT fully synced to Health Connect. Requires ZEPP_APP_TOKEN. Dates: YYYY-MM-DD.
    """
    if err := _zepp_check():
        return err
    try:
        data = await _zepp_events("DailyHealth", "summary", start_date, end_date)
        return json.dumps(data, indent=2)
    except Exception as e:
        return f"Error: {e}"


# ── Entry ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    transport = os.environ.get("MCP_TRANSPORT", "stdio")

    if transport == "http":
        # HTTP mode for Claude.ai web/mobile integration
        port = int(os.environ.get("PORT", 8000))

        if CONNECTOR_SECRET:
            # Wrap the ASGI app with a simple Bearer token check
            from starlette.middleware.base import BaseHTTPMiddleware
            from starlette.responses import JSONResponse

            class BearerAuthMiddleware(BaseHTTPMiddleware):
                async def dispatch(self, request, call_next):
                    auth = request.headers.get("Authorization", "")
                    if not auth.startswith("Bearer ") or auth[7:] != CONNECTOR_SECRET:
                        return JSONResponse(
                            {"error": "Unauthorized"}, status_code=401,
                            headers={"WWW-Authenticate": "Bearer"},
                        )
                    return await call_next(request)

            import uvicorn
            from starlette.applications import Starlette

            base_app = mcp.streamable_http_app()
            app = Starlette()
            app.add_middleware(BearerAuthMiddleware)
            app.mount("/", base_app)
            uvicorn.run(app, host="0.0.0.0", port=port)
        else:
            mcp.run(transport="streamable-http", host="0.0.0.0", port=port)
    else:
        mcp.run()
