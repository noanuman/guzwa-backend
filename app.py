"""
Flask backend for GUZWA — train routing + ride sharing + road problems.

Train routing:
  GET  /api/stations          — list all BG Voz stations
  GET  /api/stations/nearest  — find nearest stations to a lat/lng
  GET  /api/lines             — list all BG Voz lines
  GET  /api/schedules         — list all train schedules
  GET  /api/trains            — find trains between two stations
  POST /api/route             — plan a multi-modal route (transit → train → transit)

Ride sharing:
  POST /api/putanja           — driver adds route with path points
  POST /api/povezi            — match passenger with best driver
  POST /api/odaberiTacku      — determine pickup time for passenger
  POST /api/korisnik          — add a user

Utility:
  POST /api/cache/invalidate  — bust station/schedule cache
  GET  /api/health            — health check
"""

import os
from datetime import datetime, timedelta

from dotenv import load_dotenv
load_dotenv()

import json
import firebase_admin
from firebase_admin import credentials, firestore as admin_firestore
import requests as http_requests

from flask import Flask, request, jsonify
from flask_cors import CORS

from firestore_client import get_stations, get_lines, get_schedules, find_nearest_stations, invalidate_cache
from route_planner import plan_route, find_train_connections
from funkcije import closest_point_on_path, add_times_simple

# ---------------------------------------------------------------------------
# App & Firebase Admin init
# ---------------------------------------------------------------------------

app = Flask(__name__)
CORS(app)

# Load Firebase service account from file or env var
# Supports FIREBASE_SERVICE_ACCOUNT as raw JSON or FIREBASE_SERVICE_ACCOUNT_B64 as base64
if os.path.exists("PraviKljuc.json"):
    cred = credentials.Certificate("PraviKljuc.json")
else:
    import base64
    sa_b64 = os.getenv("FIREBASE_SERVICE_ACCOUNT_B64", "")
    if sa_b64:
        sa_dict = json.loads(base64.b64decode(sa_b64))
    else:
        sa_json = os.getenv("FIREBASE_SERVICE_ACCOUNT", "{}")
        sa_dict = json.loads(sa_json, strict=False)
    cred = credentials.Certificate(sa_dict)

firebase_admin.initialize_app(cred)
admin_db = admin_firestore.client()

GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY", "")


# ===========================================================================
# TRAIN ROUTING
# ===========================================================================

@app.route("/api/stations", methods=["GET"])
def list_stations():
    return jsonify(get_stations())


@app.route("/api/stations/nearest", methods=["GET"])
def nearest_stations():
    lat = request.args.get("lat", type=float)
    lng = request.args.get("lng", type=float)
    n = request.args.get("n", default=3, type=int)
    if lat is None or lng is None:
        return jsonify({"error": "lat and lng query params required"}), 400
    return jsonify(find_nearest_stations(lat, lng, n))


@app.route("/api/lines", methods=["GET"])
def list_lines():
    return jsonify(get_lines())


@app.route("/api/schedules", methods=["GET"])
def list_schedules():
    line_id = request.args.get("lineId")
    schedules = get_schedules()
    if line_id:
        schedules = [s for s in schedules if s.get("lineId") == line_id]
    return jsonify(schedules)


@app.route("/api/trains", methods=["GET"])
def find_trains():
    """Find trains between two stations after a given time."""
    origin = request.args.get("from")
    dest = request.args.get("to")
    after = request.args.get("after", "0:00")
    if not origin or not dest:
        return jsonify({"error": "'from' and 'to' query params required"}), 400
    trains = find_train_connections(origin, dest, after)
    return jsonify(trains)


@app.route("/api/route", methods=["POST"])
def calculate_route():
    """Plan a multi-modal route (transit → train → transit)."""
    data = request.get_json(force=True)

    origin = data.get("origin", {})
    dest = data.get("destination", {})

    lat1 = origin.get("lat")
    lng1 = origin.get("lng")
    lat2 = dest.get("lat")
    lng2 = dest.get("lng")

    if None in (lat1, lng1, lat2, lng2):
        return jsonify({"error": "origin.lat, origin.lng, destination.lat, destination.lng required"}), 400

    dep_time = None
    dep_str = data.get("departureTime")
    if dep_str:
        try:
            dep_time = datetime.fromisoformat(dep_str)
        except ValueError:
            return jsonify({"error": "Invalid departureTime format. Use ISO 8601."}), 400

    mode = data.get("mode", "transit")
    if mode not in ("walking", "driving", "transit"):
        return jsonify({"error": "mode must be 'walking', 'driving', or 'transit'"}), 400

    result = plan_route(lat1, lng1, lat2, lng2, departure_time=dep_time, mode=mode)
    return jsonify(result)


# ===========================================================================
# RIDE SHARING
# ===========================================================================

@app.route("/api/putanja", methods=["POST"])
def dodaj_putanju():
    """
    Driver adds a route with path points.

    JSON body:
    {
        "id_vozaca": "uid",
        "vreme": "14:30",
        "tacke": ["44.123, 20.456", "44.124, 20.457", ...],
        "datumVozac": "2026-04-10"
    }
    """
    data = request.get_json(force=True)
    id_vozaca = data.get("id_vozaca")
    vreme = data.get("vreme")
    points = data.get("tacke")
    vozac_datum = data.get("datumVozac")

    if not all([id_vozaca, vreme, points, vozac_datum]):
        return jsonify({"error": "id_vozaca, vreme, tacke, datumVozac required"}), 400

    doc_ref = admin_db.collection("Putanje").document()
    doc_ref.set({
        "idPair": None,
        "idVozaca": id_vozaca,
        "vreme": vreme,
        "listaTacaka": points,
        "pairPoint": None,
        "pairTime": None,
        "datumVozac": vozac_datum,
        "datumPutnik": None,
    })
    return jsonify({"ok": True, "id": doc_ref.id})


@app.route("/api/povezi", methods=["POST"])
def povezi_putnika():
    """
    Match a passenger with the best available driver.

    JSON body:
    {
        "id_putnika": "uid",
        "datumPutnik": "2026-04-10",
        "pairTime": "14:30",
        "putnik_start_point": "44.123, 20.456",
        "putnik_end_point": "44.789, 20.321"
    }

    Returns the matched route doc ID or "Fail".
    """
    data = request.get_json(force=True)
    id_putnika = data.get("id_putnika")
    putnik_datum = data.get("datumPutnik")
    putnik_vreme = data.get("pairTime")
    putnik_end_raw = data.get("putnik_end_point")
    putnik_start_raw = data.get("putnik_start_point")

    if not all([id_putnika, putnik_datum, putnik_vreme, putnik_end_raw, putnik_start_raw]):
        return jsonify({"error": "id_putnika, datumPutnik, pairTime, putnik_start_point, putnik_end_point required"}), 400

    putnik_end_point = list(map(float, putnik_end_raw.split(", ")))
    putnik_start_point = list(map(float, putnik_start_raw.split(", ")))

    putanje = admin_db.collection("Putanje").stream()

    best_score = float("inf")
    best_route = None

    for putanja in putanje:
        d = putanja.to_dict()

        # Must be same date
        if d.get("datumVozac") != putnik_datum:
            continue
        # Driver and passenger must be within 30 minutes of each other
        driver_vreme = d.get("vreme", "")
        if driver_vreme:
            dh, dm = map(int, driver_vreme.split(":"))
            ph, pm = map(int, putnik_vreme.split(":"))
            diff = abs((dh * 60 + dm) - (ph * 60 + pm))
            if diff > 30:
                continue
        # Skip own routes — can't pair with yourself
        if d.get("idVozaca") == id_putnika:
            continue
        # Skip if this passenger is already paired to this route
        paired = d.get("idPair") or []
        if isinstance(paired, str):
            paired = [paired]
        if id_putnika in paired:
            continue

        tacke = [list(map(float, t.split(", "))) for t in d.get("listaTacaka", [])]
        if len(tacke) < 2:
            continue

        start_p, d1, start_idx = closest_point_on_path(tacke, putnik_start_point)
        end_p, d2, end_idx = closest_point_on_path(tacke, putnik_end_point)

        # Passenger pickup must be before dropoff along the route
        if start_idx > end_idx:
            continue

        score = (d1 + d2) / 2
        if score < best_score:
            best_score = score
            best_route = putanja

    if best_route is not None:
        rd = best_route.to_dict()
        return jsonify({
            "ok": True,
            "id": best_route.id,
            "score": best_score,
            "idVozaca": rd.get("idVozaca"),
            "vreme": rd.get("vreme"),
            "listaTacaka": rd.get("listaTacaka", []),
        })
    else:
        return jsonify({"ok": False, "error": "No matching route found"}), 404


@app.route("/api/potvrdiPar", methods=["POST"])
def potvrdi_par():
    """
    Passenger confirms pairing with a driver route and sets a pickup point.

    JSON body:
    {
        "putanjaId": "doc_id",
        "id_putnika": "uid",
        "datumPutnik": "2026-04-10",
        "pickupLat": 44.123,
        "pickupLng": 20.456
    }
    """
    data = request.get_json(force=True)
    putanja_id = data.get("putanjaId")
    id_putnika = data.get("id_putnika")
    putnik_datum = data.get("datumPutnik")
    pickup_lat = data.get("pickupLat")
    pickup_lng = data.get("pickupLng")

    if not all([putanja_id, id_putnika, putnik_datum, pickup_lat is not None, pickup_lng is not None]):
        return jsonify({"error": "putanjaId, id_putnika, datumPutnik, pickupLat, pickupLng required"}), 400

    doc_ref = admin_db.collection("Putanje").document(putanja_id)
    doc = doc_ref.get()
    if not doc.exists:
        return jsonify({"error": "Route not found"}), 404

    d = doc.to_dict()

    # Append to paired list instead of overwriting
    from google.cloud.firestore import ArrayUnion
    doc_ref.update({
        "idPair": ArrayUnion([id_putnika]),
        "datumPutnik": putnik_datum,
        "pairPoint": ArrayUnion([f"{pickup_lat}, {pickup_lng}"]),
        "pairTime": None,
    })

    return jsonify({"ok": True})


@app.route("/api/odaberiTacku", methods=["POST"])
def odaberi_tacku():
    """
    Determine the time when a passenger should arrive at their pickup point.

    JSON body:
    {
        "tacka_putnika": "44.123, 20.456",
        "id_putanje": "docId",
        "prva_tacka_putanje": "44.100, 20.400"
    }
    """
    data = request.get_json(force=True)
    tacka_putnika = data.get("tacka_putnika")
    id_putanje = data.get("id_putanje")
    prva_tacka_putanje = data.get("prva_tacka_putanje")

    if not all([tacka_putnika, id_putanje, prva_tacka_putanje]):
        return jsonify({"error": "tacka_putnika, id_putanje, prva_tacka_putanje required"}), 400

    doc_ref = admin_db.collection("Putanje").document(id_putanje)
    putanja = doc_ref.get()
    if not putanja.exists:
        return jsonify({"error": "Route not found"}), 404

    # Get driving duration from first point to passenger point via Google Maps
    url = "https://maps.googleapis.com/maps/api/directions/json"
    params = {
        "origin": prva_tacka_putanje,
        "destination": tacka_putnika,
        "key": GOOGLE_MAPS_API_KEY,
    }

    res = http_requests.get(url, params=params, timeout=10)
    data_mape = res.json()

    if not data_mape.get("routes"):
        return jsonify({"error": "Could not get directions"}), 400

    vreme_puta_sec = data_mape["routes"][0]["legs"][0]["duration"]["value"]
    td = timedelta(seconds=vreme_puta_sec)
    total_seconds = int(td.total_seconds())
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    vreme_puta_str = f"{hours:02d}:{minutes:02d}"

    vreme_vozaca = putanja.to_dict().get("vreme", "00:00")
    konacno_vreme = add_times_simple(vreme_puta_str, vreme_vozaca)

    doc_ref.update({
        "pairPoint": tacka_putnika,
        "pairTime": konacno_vreme,
    })

    return jsonify({
        "ok": True,
        "pickupTime": konacno_vreme,
        "travelDuration": vreme_puta_str,
    })


@app.route("/api/korisnik", methods=["POST"])
def dodaj_korisnika():
    """
    Add a user.

    JSON body:
    {
        "email": "user@example.com",
        "ime": "Marko"
    }
    """
    data = request.get_json(force=True)
    mail = data.get("email")
    ime = data.get("ime")

    if not mail or not ime:
        return jsonify({"error": "email and ime required"}), 400

    doc_ref = admin_db.collection("Korisnici").document()
    doc_ref.set({
        "Email": mail,
        "Ime": ime,
        "brPoena": 3,
        "putanjePutnik": [],
        "putanjeVozac": [],
    })
    return jsonify({"ok": True, "id": doc_ref.id})


# ---------------------------------------------------------------------------
# Get route info (for frontend to display matched ride details)
# ---------------------------------------------------------------------------

@app.route("/api/putanja/<doc_id>", methods=["GET"])
def get_putanja(doc_id):
    """Get a single route by document ID."""
    doc = admin_db.collection("Putanje").document(doc_id).get()
    if not doc.exists:
        return jsonify({"error": "Not found"}), 404
    d = doc.to_dict()
    d["id"] = doc.id
    return jsonify(d)


@app.route("/api/putanje/vozac/<vozac_id>", methods=["GET"])
def get_putanje_vozaca(vozac_id):
    """Get all routes for a driver."""
    docs = admin_db.collection("Putanje").where("idVozaca", "==", vozac_id).stream()
    result = []
    for doc in docs:
        d = doc.to_dict()
        d["id"] = doc.id
        result.append(d)
    return jsonify(result)


@app.route("/api/putanje/putnik/<putnik_id>", methods=["GET"])
def get_putanje_putnika(putnik_id):
    """Get all routes where this user is paired as passenger."""
    docs = admin_db.collection("Putanje").where("idPair", "==", putnik_id).stream()
    result = []
    for doc in docs:
        d = doc.to_dict()
        d["id"] = doc.id
        result.append(d)
    return jsonify(result)


@app.route("/api/putanja/<doc_id>", methods=["DELETE"])
def obrisi_putanju(doc_id):
    """Delete a route entirely (e.g. when driver cancels the ride)."""
    doc_ref = admin_db.collection("Putanje").document(doc_id)
    doc = doc_ref.get()
    if not doc.exists:
        return jsonify({"error": "Not found"}), 404
    doc_ref.delete()
    return jsonify({"ok": True})


@app.route("/api/putanja/<doc_id>/otkaziPar", methods=["POST"])
def otkazi_par(doc_id):
    """Driver cancels a ride — clear the pairing so pickup marker disappears."""
    doc_ref = admin_db.collection("Putanje").document(doc_id)
    doc = doc_ref.get()
    if not doc.exists:
        return jsonify({"error": "Not found"}), 404
    doc_ref.update({
        "idPair": None,
        "pairPoint": None,
        "pairTime": None,
        "datumPutnik": None,
    })
    return jsonify({"ok": True})


@app.route("/api/putanje/ocistiSve", methods=["POST"])
def ocisti_sve():
    """Delete all Putanje documents (admin/debug)."""
    docs = admin_db.collection("Putanje").stream()
    count = 0
    for d in docs:
        admin_db.collection("Putanje").document(d.id).delete()
        count += 1
    return jsonify({"ok": True, "deleted": count})


@app.route("/api/rides/ocistiOtkazane", methods=["POST"])
def ocisti_otkazane_rides():
    """Delete all cancelled rides from Firestore."""
    docs = admin_db.collection("rides").where("status", "==", "cancelled").stream()
    count = 0
    for d in docs:
        admin_db.collection("rides").document(d.id).delete()
        count += 1
    return jsonify({"ok": True, "deleted": count})


# ===========================================================================
# ROAD PROBLEMS
# ===========================================================================

LIKES_FOR_REWARD = 3

@app.route("/api/problems/retroBodovi", methods=["POST"])
def retro_bodovi():
    """Retroactively award points for problems with 3+ likes."""
    docs = admin_db.collection("road_problems").stream()
    awarded = 0
    for d in docs:
        data = d.to_dict()
        likes = data.get("likes", [])
        reporter_id = data.get("reporterId")
        if len(likes) >= LIKES_FOR_REWARD and reporter_id:
            user_ref = admin_db.collection("users").document(reporter_id)
            user_doc = user_ref.get()
            if user_doc.exists:
                from google.cloud.firestore import Increment
                user_ref.update({"points": Increment(3)})
            else:
                user_ref.set({"points": 3})
            awarded += 1
    return jsonify({"ok": True, "awarded": awarded})


@app.route("/api/problems", methods=["GET"])
def get_problems():
    """Get all active road problems (pending + confirmed)."""
    docs = admin_db.collection("road_problems") \
        .where("status", "in", ["pending", "confirmed"]) \
        .stream()
    result = []
    for doc in docs:
        d = doc.to_dict()
        d["id"] = doc.id
        if d.get("createdAt"):
            d["createdAt"] = d["createdAt"].isoformat()
        result.append(d)
    # Sort by createdAt descending client-side
    result.sort(key=lambda x: x.get("createdAt", ""), reverse=True)
    return jsonify(result)


@app.route("/api/problems", methods=["POST"])
def report_problem():
    """
    Report a road problem.

    JSON body:
    {
        "reporterId": "uid",
        "reporterName": "Marko",
        "reporterPhoto": "https://...",
        "description": "Rupa na putu",
        "photoUrl": "",
        "lat": 44.8076,
        "lng": 20.4633
    }
    """
    data = request.get_json(force=True)
    reporter_id = data.get("reporterId")
    description = data.get("description")
    lat = data.get("lat")
    lng = data.get("lng")

    if not all([reporter_id, description, lat is not None, lng is not None]):
        return jsonify({"error": "reporterId, description, lat, lng required"}), 400

    doc_ref = admin_db.collection("road_problems").document()
    doc_ref.set({
        "reporterId": reporter_id,
        "reporterName": data.get("reporterName", ""),
        "reporterPhoto": data.get("reporterPhoto", ""),
        "description": description,
        "photoUrl": data.get("photoUrl", ""),
        "lat": lat,
        "lng": lng,
        "status": "pending",
        "confirmedBy": [reporter_id],
        "createdAt": admin_firestore.SERVER_TIMESTAMP,
    })
    return jsonify({"ok": True, "id": doc_ref.id})


@app.route("/api/problems/<problem_id>/confirm", methods=["POST"])
def confirm_problem(problem_id):
    """
    Confirm a road problem. Requires {"userId": "uid"}.
    After 3 confirmations, status becomes "confirmed".
    """
    data = request.get_json(force=True)
    user_id = data.get("userId")
    if not user_id:
        return jsonify({"error": "userId required"}), 400

    doc_ref = admin_db.collection("road_problems").document(problem_id)
    doc = doc_ref.get()
    if not doc.exists:
        return jsonify({"error": "Problem not found"}), 404

    d = doc.to_dict()
    confirmed_by = d.get("confirmedBy", [])

    if user_id in confirmed_by:
        return jsonify({"error": "Already confirmed"}), 400

    confirmed_by.append(user_id)
    updates = {"confirmedBy": confirmed_by}

    if len(confirmed_by) >= CONFIRMATIONS_NEEDED and d.get("status") == "pending":
        updates["status"] = "confirmed"

    doc_ref.update(updates)
    return jsonify({"ok": True, "confirmations": len(confirmed_by), "status": updates.get("status", d.get("status"))})


@app.route("/api/problems/<problem_id>/resolve", methods=["POST"])
def resolve_problem(problem_id):
    """Mark a problem as resolved."""
    doc_ref = admin_db.collection("road_problems").document(problem_id)
    doc = doc_ref.get()
    if not doc.exists:
        return jsonify({"error": "Problem not found"}), 404
    doc_ref.update({"status": "resolved"})
    return jsonify({"ok": True})


# ===========================================================================
# UTILITY
# ===========================================================================

@app.route("/api/cache/invalidate", methods=["POST"])
def bust_cache():
    invalidate_cache()
    return jsonify({"ok": True})


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "stations": len(get_stations())})


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(debug=True, host="0.0.0.0", port=port)
