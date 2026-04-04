"""
Lightweight Firestore REST client using the public API key (no service account needed).
Reads bgvoz_stations, bgvoz_lines, bgvoz_schedules from the guzwa-fa08a project.
"""

import os
import requests
from math import radians, sin, cos, sqrt, atan2

BASE = "https://firestore.googleapis.com/v1"
PROJECT = os.getenv("FIREBASE_PROJECT_ID", "guzwa-fa08a")
DB_ROOT = f"{BASE}/projects/{PROJECT}/databases/(default)/documents"


# ---------------------------------------------------------------------------
# Firestore value helpers
# ---------------------------------------------------------------------------

def _val(field):
    """Extract a Python value from a Firestore field dict."""
    if "stringValue" in field:
        return field["stringValue"]
    if "doubleValue" in field:
        return field["doubleValue"]
    if "integerValue" in field:
        return int(field["integerValue"])
    if "booleanValue" in field:
        return field["booleanValue"]
    if "arrayValue" in field:
        return [_val(v) for v in field["arrayValue"].get("values", [])]
    if "mapValue" in field:
        return {k: _val(v) for k, v in field["mapValue"]["fields"].items()}
    if "nullValue" in field:
        return None
    return None


def _parse_doc(doc):
    """Convert a Firestore REST document to a plain dict with an 'id' key."""
    fields = doc.get("fields", {})
    result = {k: _val(v) for k, v in fields.items()}
    result["id"] = doc["name"].split("/")[-1]
    return result


# ---------------------------------------------------------------------------
# Collection fetchers (paginated)
# ---------------------------------------------------------------------------

def _fetch_collection(collection_name):
    """Fetch all documents from a Firestore collection via REST."""
    docs = []
    url = f"{DB_ROOT}/{collection_name}"
    params = {"pageSize": 100}
    while True:
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        for d in data.get("documents", []):
            docs.append(_parse_doc(d))
        token = data.get("nextPageToken")
        if not token:
            break
        params["pageToken"] = token
    return docs


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

_stations_cache = None
_lines_cache = None
_schedules_cache = None


def get_stations():
    global _stations_cache
    if _stations_cache is None:
        _stations_cache = _fetch_collection("bgvoz_stations")
    return _stations_cache


def get_lines():
    global _lines_cache
    if _lines_cache is None:
        _lines_cache = _fetch_collection("bgvoz_lines")
    return _lines_cache


def get_schedules():
    global _schedules_cache
    if _schedules_cache is None:
        _schedules_cache = _fetch_collection("bgvoz_schedules")
    return _schedules_cache


def invalidate_cache():
    global _stations_cache, _lines_cache, _schedules_cache
    _stations_cache = _lines_cache = _schedules_cache = None


# ---------------------------------------------------------------------------
# Geo helpers
# ---------------------------------------------------------------------------

def haversine_km(lat1, lng1, lat2, lng2):
    """Return distance in km between two lat/lng points."""
    R = 6371.0
    rlat1, rlng1, rlat2, rlng2 = map(radians, [lat1, lng1, lat2, lng2])
    dlat = rlat2 - rlat1
    dlng = rlng2 - rlng1
    a = sin(dlat / 2) ** 2 + cos(rlat1) * cos(rlat2) * sin(dlng / 2) ** 2
    return R * 2 * atan2(sqrt(a), sqrt(1 - a))


def find_nearest_stations(lat, lng, n=3):
    """Return the n closest stations to a given lat/lng, sorted by distance."""
    stations = get_stations()
    scored = []
    for s in stations:
        if not s.get("lines"):
            continue  # skip stations not on any active line
        dist = haversine_km(lat, lng, s["lat"], s["lng"])
        scored.append({**s, "distance_km": round(dist, 3)})
    scored.sort(key=lambda x: x["distance_km"])
    return scored[:n]
