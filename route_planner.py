"""
Multi-modal route planner: Walking/Driving → Train → Walking/Driving.

Given (lat1, lng1) and (lat2, lng2):
1. Find nearest train stations to origin and destination.
2. Find the best train (next departure) connecting them.
3. Use Google Maps Directions API for first-mile and last-mile legs.
4. Return a combined route with timings.
"""

import os
from datetime import datetime, timedelta, timezone

import googlemaps

from firestore_client import (
    get_stations,
    get_schedules,
    get_lines,
    find_nearest_stations,
    haversine_km,
)

gmaps = googlemaps.Client(key=os.getenv("GOOGLE_MAPS_API_KEY"))


# ---------------------------------------------------------------------------
# Train schedule helpers
# ---------------------------------------------------------------------------

def _parse_time(time_str):
    """Parse '6:00' or '16:30' into (hour, minute)."""
    parts = time_str.split(":")
    return int(parts[0]), int(parts[1])


def _time_to_minutes(h, m):
    return h * 60 + m


MAX_WAIT_MINUTES = 90  # Don't suggest a train if you'd wait more than 90 min


def find_train_connections(origin_station, dest_station, depart_after_hhmm="0:00"):
    """
    Find all trains that stop at origin_station BEFORE dest_station,
    departing after depart_after_hhmm.  Returns list sorted by departure time.
    """
    schedules = get_schedules()
    depart_h, depart_m = _parse_time(depart_after_hhmm)
    depart_min = _time_to_minutes(depart_h, depart_m)

    # No trains run between ~23:15 and ~5:30 — if the requested time falls
    # in this dead zone, return empty immediately
    if depart_min >= 23 * 60 + 15 or depart_min < 5 * 60 + 30:
        return []

    results = []
    for sched in schedules:
        stops = sched.get("stops", [])
        station_names = [s["station"] for s in stops]

        # Both stations must appear, origin before destination
        if origin_station not in station_names or dest_station not in station_names:
            continue
        origin_idx = station_names.index(origin_station)
        dest_idx = station_names.index(dest_station)
        if origin_idx >= dest_idx:
            continue

        origin_time = stops[origin_idx]["time"]
        dest_time = stops[dest_idx]["time"]
        oh, om = _parse_time(origin_time)
        train_depart_min = _time_to_minutes(oh, om)

        if train_depart_min < depart_min:
            continue

        # Don't suggest trains that require waiting too long
        wait_min = train_depart_min - depart_min
        if wait_min > MAX_WAIT_MINUTES:
            continue

        # Compute travel duration
        dh, dm = _parse_time(dest_time)
        duration_min = _time_to_minutes(dh, dm) - _time_to_minutes(oh, om)

        # Get line info
        line_id = sched.get("lineId", "")
        lines = get_lines()
        line_info = next((l for l in lines if l["id"] == line_id), {})

        results.append({
            "trainNumber": sched.get("trainNumber"),
            "lineId": line_id,
            "lineName": line_info.get("name", line_id),
            "lineColor": line_info.get("color", "#333"),
            "boardStation": origin_station,
            "boardTime": origin_time,
            "alightStation": dest_station,
            "alightTime": dest_time,
            "durationMinutes": duration_min,
            "intermediateStops": [
                {"station": stops[i]["station"], "time": stops[i]["time"]}
                for i in range(origin_idx + 1, dest_idx)
            ],
        })

    results.sort(key=lambda x: _time_to_minutes(*_parse_time(x["boardTime"])))
    return results


def _get_rail_polyline(stations_with_coords):
    """
    Get a realistic polyline for a train route by querying Google Directions
    (driving mode) between consecutive stations. This snaps the path to roads/bridges
    instead of drawing straight lines over water.

    Args:
        stations_with_coords: list of {"name": str, "lat": float, "lng": float}

    Returns:
        Encoded polyline string covering the full rail path.
    """
    if len(stations_with_coords) < 2:
        return None

    # Use waypoints to get a single path through all stations
    origin = stations_with_coords[0]
    destination = stations_with_coords[-1]
    waypoints = stations_with_coords[1:-1]

    wp_strs = [f"{s['lat']},{s['lng']}" for s in waypoints]

    try:
        result = gmaps.directions(
            origin=f"{origin['lat']},{origin['lng']}",
            destination=f"{destination['lat']},{destination['lng']}",
            mode="driving",
            waypoints=wp_strs if wp_strs else None,
        )
        if result:
            return result[0]["overview_polyline"]["points"]
    except Exception:
        pass
    return None


def _google_directions_leg(origin, destination, mode="walking", departure_time=None):
    """
    Call Google Maps Directions API for a single leg.
    origin/destination: (lat, lng) tuple or string.
    mode: "walking", "driving", or "transit".
    Returns simplified leg dict with full transit sub-step details when mode=transit.
    """
    params = {
        "origin": origin,
        "destination": destination,
        "mode": mode,
    }
    if departure_time:
        params["departure_time"] = departure_time

    result = gmaps.directions(**params)
    if not result:
        return None

    leg = result[0]["legs"][0]

    # Build detailed steps — for transit mode, include transit details
    detailed_steps = []
    for step in leg["steps"]:
        s = {
            "instruction": step.get("html_instructions", ""),
            "distance": step["distance"]["text"],
            "duration": step["duration"]["text"],
            "durationSeconds": step["duration"]["value"],
            "travelMode": step["travel_mode"],
            "polyline": step.get("polyline", {}).get("points", ""),
        }
        if step["travel_mode"] == "TRANSIT" and "transit_details" in step:
            td = step["transit_details"]
            s["transit"] = {
                "lineName": td["line"].get("name", ""),
                "lineShortName": td["line"].get("short_name", ""),
                "vehicleType": td["line"].get("vehicle", {}).get("type", ""),
                "lineColor": td["line"].get("color", ""),
                "departureStop": td["departure_stop"]["name"],
                "arrivalStop": td["arrival_stop"]["name"],
                "departureTime": td["departure_time"]["text"],
                "arrivalTime": td["arrival_time"]["text"],
                "numStops": td.get("num_stops", 0),
            }
        detailed_steps.append(s)

    return {
        "mode": mode,
        "distance": leg["distance"]["text"],
        "duration": leg["duration"]["text"],
        "durationSeconds": leg["duration"]["value"],
        "startAddress": leg["start_address"],
        "endAddress": leg["end_address"],
        "startLocation": leg["start_location"],
        "endLocation": leg["end_location"],
        "polyline": result[0]["overview_polyline"]["points"],
        "steps": detailed_steps,
    }


# ---------------------------------------------------------------------------
# Main route planner
# ---------------------------------------------------------------------------

def plan_route(lat1, lng1, lat2, lng2, departure_time=None, mode="transit"):
    """
    Plan a multi-modal route: origin → train station → train → station → destination.

    Args:
        lat1, lng1: Origin coordinates
        lat2, lng2: Destination coordinates
        departure_time: Optional datetime, defaults to now
        mode: 'transit', 'walking', or 'driving' for first/last mile

    Returns a dict with the full route breakdown.
    """
    if departure_time is None:
        departure_time = datetime.now()

    # Convert to Belgrade local time (CET=UTC+1, CEST=UTC+2)
    # Serbia uses CEST (UTC+2) from last Sunday of March to last Sunday of October
    if departure_time.tzinfo is not None:
        # Convert UTC-aware datetime to Belgrade local time
        utc_ts = departure_time.timestamp()
        # Determine if DST is active in Belgrade (rough check: April-October = CEST)
        utc_dt = datetime.fromtimestamp(utc_ts, tz=timezone.utc)
        month = utc_dt.month
        if 4 <= month <= 10:
            belgrade_offset = timedelta(hours=2)  # CEST
        else:
            belgrade_offset = timedelta(hours=1)  # CET
        departure_time = utc_dt + belgrade_offset
        departure_time = departure_time.replace(tzinfo=None)  # make naive for comparison

    # Check timetable validity
    schedules = get_schedules()
    if schedules:
        valid_from = schedules[0].get("validFrom", "")
        valid_to = schedules[0].get("validTo", "")
        today_str = departure_time.strftime("%Y-%m-%d")
        if valid_from and today_str < valid_from:
            return {"error": f"Timetable not yet valid. Valid from {valid_from}."}
        if valid_to and today_str > valid_to:
            return {"error": f"Timetable expired. Valid until {valid_to}."}

    # 1. Find nearest stations to origin and destination
    origin_candidates = find_nearest_stations(lat1, lng1, n=5)
    dest_candidates = find_nearest_stations(lat2, lng2, n=5)

    # 2. For each origin station candidate, get REAL first-mile travel time,
    #    then find trains departing after actual arrival at station.
    best_route = None
    best_total = float("inf")

    for o_station in origin_candidates:
        # Get real first-mile directions from Google
        first_mile = _google_directions_leg(
            origin=f"{lat1},{lng1}",
            destination=f"{o_station['lat']},{o_station['lng']}",
            mode=mode,
            departure_time=departure_time,
        )
        if not first_mile:
            continue

        real_first_mile_secs = first_mile.get("durationSeconds", 0)
        real_first_mile_min = real_first_mile_secs / 60.0

        # Earliest boarding = departure_time + real first mile + 2 min buffer
        earliest_board = departure_time + timedelta(seconds=real_first_mile_secs + 120)
        earliest_board_str = f"{earliest_board.hour}:{earliest_board.minute:02d}"

        for d_station in dest_candidates:
            if o_station["id"] == d_station["id"]:
                continue

            trains = find_train_connections(
                o_station["name"], d_station["name"], earliest_board_str
            )
            if not trains:
                continue

            # Take the first (soonest) catchable train
            train = trains[0]

            # Rough last-mile estimate for comparison (real directions fetched later)
            speed = 5.0 if mode == "walking" else 20.0 if mode == "transit" else 30.0
            last_mile_min = (d_station["distance_km"] / speed) * 60

            total = real_first_mile_min + train["durationMinutes"] + last_mile_min

            if total < best_total:
                best_total = total
                best_route = {
                    "originStation": o_station,
                    "destStation": d_station,
                    "train": train,
                    "firstMile": first_mile,
                    "estimatedTotalMin": round(total, 1),
                }

    if not best_route:
        return {"error": "No train route found between these locations."}

    # 3. Get real last-mile directions
    o_station = best_route["originStation"]
    d_station = best_route["destStation"]
    first_mile = best_route["firstMile"]

    last_mile = _google_directions_leg(
        origin=f"{d_station['lat']},{d_station['lng']}",
        destination=f"{lat2},{lng2}",
        mode=mode,
    )

    # 4. Get realistic rail polyline snapped to roads/bridges
    train = best_route["train"]
    all_stations = get_stations()
    station_map = {s["name"]: s for s in all_stations}

    rail_stations = [train["boardStation"]]
    rail_stations += [s["station"] for s in train.get("intermediateStops", [])]
    rail_stations += [train["alightStation"]]

    rail_coords = []
    for name in rail_stations:
        st = station_map.get(name)
        if st:
            rail_coords.append({"name": name, "lat": st["lat"], "lng": st["lng"]})

    rail_polyline = _get_rail_polyline(rail_coords) if len(rail_coords) >= 2 else None

    return {
        "summary": {
            "origin": {"lat": lat1, "lng": lng1},
            "destination": {"lat": lat2, "lng": lng2},
            "departureTime": departure_time.isoformat(),
            "estimatedTotalMinutes": best_route["estimatedTotalMin"],
            "mode": mode,
        },
        "legs": [
            {
                "type": "first_mile",
                "description": f"{mode.title()} to {o_station['name']} station",
                "station": {
                    "name": o_station["name"],
                    "nameCyrillic": o_station.get("nameCyrillic", ""),
                    "lat": o_station["lat"],
                    "lng": o_station["lng"],
                    "distanceKm": o_station["distance_km"],
                },
                "directions": first_mile,
            },
            {
                "type": "train",
                "description": f"Train {best_route['train']['lineName']}",
                "railPolyline": rail_polyline,
                **best_route["train"],
            },
            {
                "type": "last_mile",
                "description": f"{mode.title()} from {d_station['name']} station to destination",
                "station": {
                    "name": d_station["name"],
                    "nameCyrillic": d_station.get("nameCyrillic", ""),
                    "lat": d_station["lat"],
                    "lng": d_station["lng"],
                    "distanceKm": d_station["distance_km"],
                },
                "directions": last_mile,
            },
        ],
    }
