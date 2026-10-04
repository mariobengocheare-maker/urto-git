"""
Address -> map coordinates for URTO's Map tab (build order #173).

Two free geocoders, tried in this order:
  1. OpenRouteService -- only when ORS_API_KEY is already set (Mario may
     have added it for ShowingDay); higher daily allowance, no throttling.
  2. OpenStreetMap Nominatim -- no key, no account, works out of the box.
     Its usage policy allows light use (max ~1 request/second, with an
     identifying User-Agent), which a solo user placing his own CRM clients
     comfortably fits; geocode_address() enforces the 1/sec pacing itself.

Every lookup is clamped to Mario's market (Miami-Dade + Broward + the
Florida Keys) and a result outside that box is treated as "not found" -- a
bare street like "900 NW 10TH ST" exists in dozens of US cities, and the
Zillow button already landed in Texas once for exactly that reason (build
order #168). Same fix here: an address that doesn't name Florida gets
", Miami-Dade County, FL" appended before it's ever sent anywhere.

Never raises for a plain "no match" -- returns None, so a batch pass can
mark that one client as needing hand-placement and keep going.
"""

import os
import re
import threading
import time

import requests

# lat_min, lat_max, lng_min, lng_max -- Key West (~-81.8) up through
# northern Broward (~26.35), Atlantic coast (~-80.0).
MARKET_BOUNDS = (24.40, 26.45, -82.10, -79.90)

ORS_GEOCODE_URL = "https://api.openrouteservice.org/geocode/search"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "URTO-CRM/1.0 (solo-realtor CRM; low-volume address placement)"

_FL_RE = re.compile(r"\bfl(orida)?\b", re.IGNORECASE)
_nominatim_lock = threading.Lock()
_last_nominatim_call = [0.0]


def _ors_key() -> str:
    # Read live (not at import) so setting the key on Render takes effect
    # on the next deploy/restart exactly like every other env var here.
    return os.environ.get("ORS_API_KEY", "")


def provider_name() -> str:
    return "OpenRouteService" if _ors_key() else "OpenStreetMap"


def anchor_to_market(address: str) -> str:
    address = (address or "").strip()
    if address and not _FL_RE.search(address):
        address = f"{address}, Miami-Dade County, FL"
    return address


def in_market(lat: float, lng: float) -> bool:
    lat_min, lat_max, lng_min, lng_max = MARKET_BOUNDS
    return lat_min <= lat <= lat_max and lng_min <= lng <= lng_max


def _geocode_ors(text: str):
    lat_min, lat_max, lng_min, lng_max = MARKET_BOUNDS
    res = requests.get(
        ORS_GEOCODE_URL,
        params={
            "api_key": _ors_key(),
            "text": text,
            "size": 1,
            "boundary.country": "US",
            "boundary.rect.min_lat": lat_min,
            "boundary.rect.max_lat": lat_max,
            "boundary.rect.min_lon": lng_min,
            "boundary.rect.max_lon": lng_max,
        },
        timeout=15,
    )
    res.raise_for_status()
    features = res.json().get("features") or []
    if not features:
        return None
    lng, lat = features[0]["geometry"]["coordinates"]
    return float(lat), float(lng)


def _geocode_nominatim(text: str):
    lat_min, lat_max, lng_min, lng_max = MARKET_BOUNDS
    with _nominatim_lock:
        wait = 1.1 - (time.time() - _last_nominatim_call[0])
        if wait > 0:
            time.sleep(wait)
        try:
            res = requests.get(
                NOMINATIM_URL,
                params={
                    "q": text,
                    "format": "json",
                    "limit": 1,
                    "countrycodes": "us",
                    "viewbox": f"{lng_min},{lat_max},{lng_max},{lat_min}",
                    "bounded": 1,
                },
                headers={"User-Agent": USER_AGENT},
                timeout=15,
            )
        finally:
            _last_nominatim_call[0] = time.time()
    res.raise_for_status()
    results = res.json() or []
    if not results:
        return None
    return float(results[0]["lat"]), float(results[0]["lon"])


def geocode_address(address: str):
    """(lat, lng) inside Mario's market, or None if nothing credible was
    found. Network/HTTP errors propagate (RuntimeError-wrapped) so a caller
    can tell "the geocoder is down" apart from "this address doesn't exist"
    and avoid permanently marking a perfectly good address as failed."""
    text = anchor_to_market(address)
    if not text:
        return None
    try:
        hit = _geocode_ors(text) if _ors_key() else _geocode_nominatim(text)
    except requests.RequestException as e:
        raise RuntimeError(f"{provider_name()} geocoder unreachable: {e}")
    if hit and in_market(*hit):
        return hit
    return None
