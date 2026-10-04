"""
Address -> map coordinates for URTO's Map tab (build orders #173/#174).

Three free geocoders, tried in order for every address:
  1. OpenRouteService -- only when ORS_API_KEY is set (Mario's ShowingDay
     key). Free plan caps geocoding per minute/day.
  2. US Census Bureau geocoder -- free, no key, no account, built for US
     street addresses.
  3. OpenStreetMap Nominatim -- free, no key; usage policy ~1 request/sec.

Why three (build order #174): with only one provider, the first time it
said "slow down" (HTTP 429) or refused a burst, the whole placement pass
stopped -- Mario saw ~30 pins and "107 not placed." Now a provider that
rate-limits or errors is put on a short cooldown and the NEXT one is tried,
and each provider is paced to its own polite request rate. Only when every
provider is cooling down does a caller hear RateLimited (wait, then resume).

Every lookup is clamped to Mario's market (Miami-Dade + Broward + the
Florida Keys); a hit outside it is "not found." An address that doesn't
name Florida gets ", Miami-Dade County, FL" appended first (the Texas
Zillow lesson, build order #168). An address that explicitly names ANOTHER
state ("..., Austin Tx") is never looked up at all -- see names_other_state().
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
CENSUS_GEOCODE_URL = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "URTO-CRM/1.0 (solo-realtor CRM; low-volume address placement)"

# Seconds between calls to the same provider (polite pacing), and how long a
# provider sits out after it rate-limits/errors.
_MIN_INTERVAL = {"ors": 0.8, "census": 0.4, "nominatim": 1.1}
_COOLDOWN_RATE_LIMITED = 90
_COOLDOWN_ERROR = 45

_FL_RE = re.compile(r"\bfl(orida)?\b", re.IGNORECASE)
_OTHER_STATES = (
    "AL AK AZ AR CA CO CT DE GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM "
    "NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC"
).split()
# State abbreviations that double as common street suffixes ("SW 84th Ct",
# "Ocean Wy", "Palm La") -- these only count as a state when a zip follows.
_SUFFIX_LOOKALIKES = {"CT", "WY", "LA", "AL"}
_ZIP_RE = re.compile(r"^\d{5}(-\d{4})?$")

_lock = threading.Lock()
_last_call = {}
_cooldown_until = {}


class RateLimited(RuntimeError):
    """Every provider is asking us to slow down -- wait retry_after seconds."""

    def __init__(self, retry_after, detail=""):
        super().__init__(f"Address lookup services asked to slow down ({detail})")
        self.retry_after = max(5, int(retry_after))


class _ProviderRateLimited(Exception):
    pass


def _ors_key() -> str:
    return os.environ.get("ORS_API_KEY", "")


def provider_name() -> str:
    names = (["OpenRouteService"] if _ors_key() else []) + ["US Census", "OpenStreetMap"]
    return " / ".join(names)


def anchor_to_market(address: str) -> str:
    address = (address or "").strip()
    if address and not _FL_RE.search(address):
        address = f"{address}, Miami-Dade County, FL"
    return address


def names_other_state(address: str) -> bool:
    """True when the address explicitly ends in a non-Florida US state
    ("2505 San Gabriel St #103, Austin Tx", "1 Main St, Dallas, TX 75201") --
    an out-of-market deal that should never be geocoded or nag Mario to
    place it by hand. Deliberately conservative: the state must come after a
    comma (i.e. in the city/state part, never the street), the city words in
    front of it can't contain digits, and a state that's also a street
    suffix (Ct/Wy/La/Al) only counts with a zip after it -- otherwise
    "14435 SW 84th Ct" would be mistaken for Connecticut."""
    a = (address or "").strip().rstrip(".")
    if not a or "," not in a or _FL_RE.search(a):
        return False
    tail = a.rsplit(",", 1)[1].split()
    has_zip = bool(tail) and bool(_ZIP_RE.match(tail[-1]))
    if has_zip:
        tail = tail[:-1]
    if not tail:
        return False
    state = tail[-1].upper().rstrip(".")
    if state not in _OTHER_STATES:
        return False
    if state in _SUFFIX_LOOKALIKES and not has_zip:
        return False
    return not any(ch.isdigit() for tok in tail[:-1] for ch in tok)


def in_market(lat: float, lng: float) -> bool:
    lat_min, lat_max, lng_min, lng_max = MARKET_BOUNDS
    return lat_min <= lat <= lat_max and lng_min <= lng <= lng_max


def _pace(name):
    with _lock:
        wait = _MIN_INTERVAL[name] - (time.time() - _last_call.get(name, 0))
        if wait > 0:
            time.sleep(wait)
        _last_call[name] = time.time()


def _get(name, url, **kw):
    _pace(name)
    res = requests.get(url, timeout=15, **kw)
    if res.status_code in (429, 403, 503):
        raise _ProviderRateLimited(f"{name} HTTP {res.status_code}")
    res.raise_for_status()
    return res.json()


def _geocode_ors(text):
    lat_min, lat_max, lng_min, lng_max = MARKET_BOUNDS
    data = _get("ors", ORS_GEOCODE_URL, params={
        "api_key": _ors_key(), "text": text, "size": 1, "boundary.country": "US",
        "boundary.rect.min_lat": lat_min, "boundary.rect.max_lat": lat_max,
        "boundary.rect.min_lon": lng_min, "boundary.rect.max_lon": lng_max,
    })
    features = data.get("features") or []
    if not features:
        return None
    lng, lat = features[0]["geometry"]["coordinates"]
    return float(lat), float(lng)


def _geocode_census(text):
    data = _get("census", CENSUS_GEOCODE_URL, params={
        "address": text, "benchmark": "Public_AR_Current", "format": "json",
    })
    matches = ((data.get("result") or {}).get("addressMatches")) or []
    for m in matches:
        c = m.get("coordinates") or {}
        if "x" in c and "y" in c and in_market(float(c["y"]), float(c["x"])):
            return float(c["y"]), float(c["x"])
    return None


def _geocode_nominatim(text):
    lat_min, lat_max, lng_min, lng_max = MARKET_BOUNDS
    results = _get("nominatim", NOMINATIM_URL, params={
        "q": text, "format": "json", "limit": 1, "countrycodes": "us",
        "viewbox": f"{lng_min},{lat_max},{lng_max},{lat_min}", "bounded": 1,
    }, headers={"User-Agent": USER_AGENT})
    if not results:
        return None
    return float(results[0]["lat"]), float(results[0]["lon"])


def _providers():
    out = []
    if _ors_key():
        out.append(("ors", _geocode_ors))
    out.append(("census", _geocode_census))
    out.append(("nominatim", _geocode_nominatim))
    return out


def geocode_address(address: str):
    """(lat, lng) inside Mario's market, or None for a genuine no-match.

    Raises RateLimited when every provider is currently cooling down (the
    caller should wait and resume, NOT mark the address failed), and
    RuntimeError when the providers that were tried all errored -- in both
    cases the address may be perfectly good, so it must not be written off."""
    text = anchor_to_market(address)
    if not text:
        return None
    now = time.time()
    errors = []
    rate_limited = 0
    answered = False
    for name, fn in _providers():
        if _cooldown_until.get(name, 0) > now:
            continue
        try:
            hit = fn(text)
        except _ProviderRateLimited as e:
            _cooldown_until[name] = time.time() + _COOLDOWN_RATE_LIMITED
            errors.append(str(e))
            rate_limited += 1
            continue
        except (requests.RequestException, ValueError, KeyError) as e:
            _cooldown_until[name] = time.time() + _COOLDOWN_ERROR
            errors.append(f"{name}: {e}")
            continue
        answered = True
        if hit and in_market(*hit):
            return hit
    if answered and not errors:
        return None
    if answered:
        # One provider said "no match" but another errored on this very
        # address -- the errored one might have found it. Not a write-off;
        # the caller skips it for now and it's retried on a later pass.
        raise RuntimeError("; ".join(errors))
    if not errors or rate_limited == len(errors):
        # Nothing was tried (all cooling down) or everything that was tried
        # said "slow down" -- wait for the soonest provider to come back.
        soonest = min((t for t in _cooldown_until.values() if t > time.time()), default=time.time() + 30)
        raise RateLimited(soonest - time.time(), "; ".join(errors) or "all providers cooling down")
    raise RuntimeError("; ".join(errors))


def reset_cooldowns():
    """For tests."""
    _cooldown_until.clear()
    _last_call.clear()
