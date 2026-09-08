"""
Real GPS-based route optimization for URTO's ShowingDay feature, via
OpenRouteService (openrouteservice.org) -- chosen over Google Maps
Platform/Mapbox specifically because it's free forever with no credit card
ever required (Mario's explicit pick), while still giving genuine
driving-time-based route optimization (VROOM under the hood) with native
support for a fixed break window -- exactly what's needed to pin a lunch
break into the middle of a day of showings.

Needs Mario's own free API key (openrouteservice.org/dev -> Sign up ->
create a token) in the ORS_API_KEY env var. Deliberately plain `requests`
calls to ORS's REST endpoints, same "no heavy SDK for a small surface"
reasoning as google_drive_api.py/microsoft_graph_api.py.

Two endpoints used:
  - /geocode/search: turns a typed address into [lng, lat] + a matched
    label, so Mario can see what address was actually understood before
    trusting the route built from it (never silently route to the wrong
    place on a bad match).
  - /optimization: VROOM's job-scheduling solver. Times are expressed as
    plain seconds-since-midnight of the route's day (a common convention
    in ORS's own docs/examples) -- not real Unix timestamps -- since
    everything (jobs, the vehicle's shift window, the break window) only
    needs to agree with each other on one shared, arbitrary time axis for
    one single day, not actually be real epoch time.
"""

import os

import requests

API_KEY = os.environ.get("ORS_API_KEY", "")

GEOCODE_URL = "https://api.openrouteservice.org/geocode/search"
OPTIMIZATION_URL = "https://api.openrouteservice.org/optimization"

# The solver is only ever allowed to schedule things within this daily
# window (6am-9pm) -- keeps VROOM's search space sane and matches a
# realistic real-estate showing day; a job/break outside this can never be
# reached, which is intentional (nobody's doing a 2am showing).
_DAY_START_SECONDS = 6 * 3600
_DAY_END_SECONDS = 21 * 3600


def is_configured() -> bool:
    return bool(API_KEY)


def _headers():
    return {"Authorization": API_KEY, "Content-Type": "application/json"}


def geocode(address: str) -> dict:
    """Returns {"lat": ..., "lng": ..., "label": "<matched address>"} for
    the best match, or raises RuntimeError with a clear reason (including
    "no match found" -- a bad/incomplete address should never silently
    become some other real place)."""
    if not is_configured():
        raise RuntimeError("ShowingDay isn't set up yet — an ORS_API_KEY is needed on the server.")
    res = requests.get(
        GEOCODE_URL,
        params={"api_key": API_KEY, "text": address, "size": 1},
        timeout=15,
    )
    res.raise_for_status()
    data = res.json()
    features = data.get("features") or []
    if not features:
        raise RuntimeError(f"Couldn't find a real address match for \"{address}\".")
    feat = features[0]
    lng, lat = feat["geometry"]["coordinates"]
    label = (feat.get("properties") or {}).get("label", address)
    return {"lat": lat, "lng": lng, "label": label}


def _hm_to_seconds(hm: str) -> int:
    hour, minute = hm.split(":")
    return int(hour) * 3600 + int(minute) * 60


def _seconds_to_hm(seconds: int) -> str:
    seconds = int(seconds) % 86400
    return f"{seconds // 3600:02d}:{(seconds % 3600) // 60:02d}"


def optimize_route(start_lat: float, start_lng: float, stops: list,
                    lunch_start_time: str = None, lunch_minutes: int = 0) -> dict:
    """`stops` is a list of {"id": <showing_stop id>, "lat", "lng", "visit_minutes"}.
    Returns {"order": [stop_id, ...], "etas": {stop_id: "HH:MM"}, "lunch_eta": "HH:MM" or None,
    "total_drive_minutes": int} on success, or raises RuntimeError with a clear
    reason (ORS/VROOM couldn't find a feasible schedule, a network failure, etc.)."""
    if not is_configured():
        raise RuntimeError("ShowingDay isn't set up yet — an ORS_API_KEY is needed on the server.")
    if not stops:
        raise RuntimeError("Add at least one stop before optimizing a route.")

    jobs = [
        {
            "id": s["id"],
            "location": [s["lng"], s["lat"]],
            "service": max(1, int(s.get("visit_minutes") or 30)) * 60,
        }
        for s in stops
    ]

    vehicle = {
        "id": 1,
        "profile": "driving-car",
        "start": [start_lng, start_lat],
        "time_window": [_DAY_START_SECONDS, _DAY_END_SECONDS],
    }
    if lunch_start_time and lunch_minutes and lunch_minutes > 0:
        lunch_center = _hm_to_seconds(lunch_start_time)
        vehicle["breaks"] = [{
            "id": 1,
            # A real fixed lunch time isn't a hard scheduling requirement
            # for VROOM (it just picks SOME point in this window) -- a
            # window a bit before/after Mario's preferred time lets the
            # solver slot it in without geography fighting the exact
            # minute, while still landing close to what he asked for.
            "time_windows": [[max(_DAY_START_SECONDS, lunch_center - 1800),
                               min(_DAY_END_SECONDS, lunch_center + 5400)]],
            "service": int(lunch_minutes) * 60,
        }]

    body = {"jobs": jobs, "vehicles": [vehicle]}
    try:
        res = requests.post(OPTIMIZATION_URL, headers=_headers(), json=body, timeout=30)
        res.raise_for_status()
    except requests.exceptions.HTTPError as e:
        detail = ""
        if e.response is not None:
            detail = (e.response.text or "").strip()[:400]
        raise RuntimeError(f"OpenRouteService couldn't build a route: {detail or e}")
    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"Couldn't reach OpenRouteService: {e}")

    data = res.json()
    routes = data.get("routes") or []
    if not routes:
        unassigned = data.get("unassigned") or []
        if unassigned:
            raise RuntimeError(
                f"{len(unassigned)} stop(s) couldn't be fit into the day's driving window (6am-9pm) — "
                "try removing a stop or shortening the lunch break."
            )
        raise RuntimeError("OpenRouteService didn't return a usable route.")

    route = routes[0]
    order = []
    etas = {}
    lunch_eta = None
    for step in route.get("steps", []):
        if step.get("type") == "job":
            order.append(step["id"])
            etas[step["id"]] = _seconds_to_hm(step["arrival"])
        elif step.get("type") == "break":
            lunch_eta = _seconds_to_hm(step["arrival"])

    total_drive_seconds = route.get("duration", 0) - sum(j["service"] for j in jobs)
    if lunch_minutes:
        total_drive_seconds -= int(lunch_minutes) * 60

    return {
        "order": order,
        "etas": etas,
        "lunch_eta": lunch_eta,
        "total_drive_minutes": max(0, round(total_drive_seconds / 60)),
    }
