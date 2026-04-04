from flask import Flask, render_template, request, jsonify
from firebase_admin import credentials, firestore
from datetime import datetime
from datetime import timedelta
from funkcije import *
import firebase_admin
import requests


app = Flask(__name__)
cred = credentials.Certificate("PraviKljuc.json")
firebase_admin.initialize_app(cred)

db = firestore.client()

# Home route
@app.route("/")
def home():
    return None

"""
@app.route("/api/user/<name>/<int:age>", methods=["GET"])
def get_user(name, age):
    return jsonify({
        "name": name,
        "age": age
    })
"""

@app.route("/dodajputanju", methods=["POST"])
def dodaj_putanju():
    data = request.get_json()
    id_vozaca = data.get("id_vozaca")
    vreme = data.get("vreme")
    points = data.get("tacke")
    vozac_datum = data.get("datumVozac")
    db.collection("Putanje").document().set({
        "idPair": None,
        "idVozaca": id_vozaca,
        "vreme": vreme,
        "listaTacaka": points,
        "pairPoint": None,
        "pairTime": None,
        "datumVozac": vozac_datum,
        "datumPutnik": None
    })
    return "OK"


@app.route("/poveziputnika", methods=["POST"])
def poveziputnika():
    """
    Treba da pronadje putniku odgovarajuceg vozaca
    vozacevo vreme polaska, tacke njegovog puta i da sacuva to u bazu a request ce da vrati OK
    Onda kada na frontendu treba videti sta se desilo, pozove se GET request koji dobije ove podatke
    """
    data = request.get_json()
    id_putnika = data.get("id_putnika")
    putnik_datum = data.get("datumPutnik")
    putnik_vreme = data.get("pairTime")  # expected as a comparable format, e.g., "14:30"
    putnik_end_point = list(map(float, data.get("putnik_end_point").split(", ")))
    putnik_start_point = list(map(float, data.get("putnik_start_point").split(", ")))

    putanje = db.collection("Putanje").stream()

    best_score = float('inf')
    best_route = None

    for putanja in putanje:
        putanja_dict = putanja.to_dict()
        putanja_dict["id"] = putanja.id  # include document ID

        datum_vozac = putanja_dict["datumVozac"]
        vreme_vozac = putanja_dict["vreme"]  # expected as "HH:MM" string

        if datum_vozac != putnik_datum:
            continue
        if vreme_vozac <= putnik_vreme:  # driver must depart **before** passenger
            continue
        print("vremena su dobra")
        if putanja_dict["idPair"]:
            continue
        print("idPair je prazan")
        tacke = [list(map(float, tacka.split(", "))) for tacka in putanja_dict["listaTacaka"]]

        start_p, d1, start_idx = closest_point_on_path(tacke, putnik_start_point)
        end_p, d2, end_idx = closest_point_on_path(tacke, putnik_end_point)

        print(f"start idx: {start_idx}, end idx: {end_idx}")

        if start_idx > end_idx:
            continue
        score = (d1 + d2) / 2

        if score < best_score:
            best_score = score
            best_route = putanja

    print("Best score:", best_score)
    print("Best route:", best_route)

    if best_route is not None:
        db.collection("Putanje").document(best_route.id).update({
            "idPair": id_putnika,
            "pairPoint": None,
            "pairTime": None
        })
        return "OK"
    else:
        return "Fail"


@app.route("/odaberiTacku", methods=["POST"])
def odaberi_tacku():
    """
    Treba da odredi vreme kada putnik treba da dodje na lokaciju koju je izabrao
    """
    data = request.get_json()
    tacka_putnika = data.get("tacka_putnika")
    id_putanje = data.get("id_putanje")
    prva_tacka_putanje = data.get("prva_tacka_putanje")
    doc_ref = db.collection("Putanje").document(id_putanje)
    putanja = doc_ref.get()

    url = "https://maps.googleapis.com/maps/api/directions/json"
    params = {
        "origin": prva_tacka_putanje,
        "destination": tacka_putnika,
        "key": "AIzaSyAk2_QgVLnfPgduvmY8N9p_ug0arX3IClk"
    }

    res = requests.get(url, params=params)
    data_mape = res.json()
    vreme_puta_sec = data_mape["routes"][0]["legs"][0]["duration"]["value"]
    td = timedelta(seconds=vreme_puta_sec)
    total_seconds = int(td.total_seconds())
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    vreme_puta_str = f"{hours:02d}:{minutes:02d}"
    konacno_vreme = add_times_simple(vreme_puta_str, putanja.to_dict()["vreme"])
    db.collection("Putanje").document(id_putanje).update({
        "pairPoint": tacka_putnika,
        "pairTime": konacno_vreme
    })
    return "OK"

@app.route("/dodajkorisnika", methods=["POST"])
def dodaj_korisnika():
    data = request.get_json()
    mail = data.get("email")
    ime = data.get("ime")
    db.collection("Korisnici").document().set({
        "Email": mail,
        "Ime": ime,
        "brPoena": 3,
        "putanjePutnik": [],
        "putanjeVozac": []
    })
    return "OK"

if __name__ == "__main__":
    app.run(debug=True)
