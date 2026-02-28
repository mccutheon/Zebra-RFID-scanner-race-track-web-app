# rfid_app.py - UPDATED WITH CONFIG FILE + DYNAMIC ANTENNAS + dBm TX POWER
import json
import os
import random
import threading
import time
import logging
import csv
from io import StringIO
from flask import Flask, render_template, jsonify, request, Response

# ====================== LOAD CONFIG ======================
config = {}
if os.path.exists('config.txt'):
    with open('config.txt') as f:
        for line in f:
            if '=' in line:
                k, v = line.strip().split('=', 1)
                config[k.strip()] = v.strip()

READER_IP = config.get('READER_IP', '192.168.2.154')

# Default per-antenna powers (dBm)
default_powers = {1: 10, 2: 20, 3: 30, 4: 30}
ant_powers = {}
for ant in range(1, 5):
    key = f'ANT{ant}_POWER'
    dbm = float(config.get(key, default_powers[ant]))
    if not (10 <= dbm <= 30):
        print(f"ERROR: {key} must be between 10 and 30 dBm")
        exit(1)
    ant_powers[ant] = dbm

DATA_FILE = 'rfid_data.json'
RACE_FILE = 'race_data.json'

NAMES = ["tom", "dick", "harry", "moe", "chicko"]
last_detection = {}

def load_profiles():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE) as f: return json.load(f)
    return []

def save_profiles(data):
    with open(DATA_FILE, 'w') as f: json.dump(data, f, indent=2)

def load_race():
    if os.path.exists(RACE_FILE):
        with open(RACE_FILE) as f: return json.load(f)
    return {"session_name": "Moto 1", "start_time": None, "is_running": False, "paused_elapsed": 0.0, "laps": []}

def save_race(data):
    with open(RACE_FILE, 'w') as f: json.dump(data, f, indent=2)

def get_name(rfid):
    for p in load_profiles():
        if p['rfid_id'] == rfid: return p['player_name']
    return rfid[:8] + "..."

# ====================== RFID READER ======================
try:
    from sllurp.llrp import LLRPReaderConfig, LLRPReaderClient, LLRP_DEFAULT_PORT
except ImportError:
    print("ERROR: pip install sllurp")
    exit(1)

def run_reader():
    logging.getLogger('sllurp').setLevel(logging.INFO)

    def tag_callback(reader, reports):
        for tag in reports:
            b = (tag.get('EPC-96') or 
                 (tag.get('EPCData', {}).get('EPC') if isinstance(tag.get('EPCData'), dict) else None) or 
                 tag.get('EPC'))
            if not b: continue
            rfid = b.hex().upper() if isinstance(b, (bytes, bytearray)) else str(b).upper()

            # Antenna detection
            antenna = 1
            if 'AntennaID' in tag:
                val = tag['AntennaID']
                if isinstance(val, (list, tuple)) and val:
                    antenna = val[0]
                elif isinstance(val, int):
                    antenna = val

            race = load_race()
            if antenna == 1:
                add_checkin(rfid)
            elif race.get("is_running"):
                handle_lap(rfid)

    # Assume max 4 antennas for FX7500 - change if needed
    num_ant = 4  # Hardcoded for FX7500 - change if needed
    antennas = list(range(1, num_ant + 1))
    tx_power = {}
    for ant in antennas:
        dbm = ant_powers.get(ant, 30)
        # Convert dBm to table index (FX7500: index 0 = 30 dBm, step 0.25 dBm down to index 80 = 10 dBm)
        tx_index = int((30 - dbm) * 4)
        tx_power[ant] = tx_index
        print(f"   Antenna {ant}: {dbm} dBm (index {tx_index})")

    # Pass as config_dict
    config_dict = {
        'antennas': antennas,
        'tx_power': tx_power,
        'report_every_n_tags': 1
    }
    config = LLRPReaderConfig(config_dict)

    reader = LLRPReaderClient(READER_IP, LLRP_DEFAULT_PORT, config)

    try:
        reader.connect()
        print("🚀 Reader connected!")
        reader.add_tag_report_callback(tag_callback)
        reader.startInventory()
        print(f"✅ Inventory started on {num_ant} antennas")
        reader.join(None)
    except Exception as e:
        print(f"❌ Reader error: {e}")

def add_checkin(rfid):
    data = load_profiles()
    if any(e['rfid_id'] == rfid for e in data): return
    next_id = max([e['profile_id'] for e in data] + [0]) + 1
    entry = {"profile_id": next_id, "rfid_id": rfid, "player_name": random.choice(NAMES), "driver_number": random.randint(0,100)}
    data.append(entry)
    save_profiles(data)
    print(f"✅ CHECK-IN → #{next_id} {entry['player_name']}")

def handle_lap(rfid):
    race = load_race()
    if not race.get("is_running") or not race.get("start_time"): return
    now = time.time()
    if rfid in last_detection and now - last_detection[rfid] < 60: return
    last_detection[rfid] = now

    elapsed = (now - race["start_time"]) + race.get("paused_elapsed", 0)
    name = get_name(rfid)
    rider_entries = [l for l in race["laps"] if l["rfid_id"] == rfid]
    is_first_pass = len(rider_entries) == 0

    if is_first_pass:
        race["laps"].append({
            "rfid_id": rfid, "name": name, "lap_number": 0, "label": "START",
            "lap_time": 0.0, "elapsed": round(elapsed, 2), "timestamp": now
        })
        print(f"🏁 START → {name}")
    else:
        last_lap = rider_entries[-1]
        lap_num = len([l for l in rider_entries if l.get("label") != "START"]) + 1
        lap_time = elapsed - last_lap["elapsed"]
        race["laps"].append({
            "rfid_id": rfid, "name": name, "lap_number": lap_num, "label": f"Lap {lap_num}",
            "lap_time": round(lap_time, 2), "elapsed": round(elapsed, 2), "timestamp": now
        })
        print(f"🏁 Lap {lap_num} → {name} ({lap_time:.2f}s)")

    save_race(race)

# ====================== FLASK APP ======================
app = Flask(__name__, template_folder='templates')

# Check-in routes
@app.route('/')
def index(): return render_template('index.html')

@app.route('/data')
def get_data(): return jsonify(load_profiles())

@app.route('/update', methods=['POST'])
def update_entry():
    profile_id = int(request.form['profile_id'])
    player_name = request.form['player_name']
    driver_number = int(request.form['driver_number'])
    data = load_profiles()
    for e in data:
        if e['profile_id'] == profile_id:
            e['player_name'] = player_name
            e['driver_number'] = driver_number
            break
    save_profiles(data)
    return jsonify(success=True)

@app.route('/delete', methods=['POST'])
def delete_entry():
    profile_id = int(request.form['profile_id'])
    data = [e for e in load_profiles() if e['profile_id'] != profile_id]
    save_profiles(data)
    return jsonify(success=True)

@app.route('/export')
def export_csv():
    data = load_profiles()
    si = StringIO()
    writer = csv.writer(si)
    writer.writerow(['Profile ID', 'Player Name', 'Driver Number', 'RFID ID'])
    for e in data:
        writer.writerow([e['profile_id'], e['player_name'], e['driver_number'], e['rfid_id']])
    return Response(si.getvalue(), mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=checkin.csv"})

# Race routes
@app.route('/race')
def race_page(): return render_template('race.html')

@app.route('/race_data')
def get_race_data(): return jsonify(load_race())

@app.route('/start_race', methods=['POST'])
def start_race():
    race = load_race()
    race["is_running"] = True
    if not race.get("start_time"):
        race["start_time"] = time.time()
    save_race(race)
    return jsonify(success=True)

@app.route('/pause_race', methods=['POST'])
def pause_race():
    race = load_race()
    if race["is_running"] and race.get("start_time"):
        race["paused_elapsed"] += time.time() - race["start_time"]
        race["start_time"] = None
    race["is_running"] = False
    save_race(race)
    return jsonify(success=True)

@app.route('/reset_race', methods=['POST'])
def reset_race():
    save_race({"session_name": "Moto 1", "start_time": None, "is_running": False, "paused_elapsed": 0.0, "laps": []})
    return jsonify(success=True)

@app.route('/finish_race', methods=['POST'])
def finish_race():
    race = load_race()
    race["is_running"] = False
    save_race(race)
    return jsonify(success=True)

@app.route('/new_race', methods=['POST'])
def new_race():
    save_race({"session_name": "Moto 1", "start_time": None, "is_running": False, "paused_elapsed": 0.0, "laps": []})
    return jsonify(success=True)

@app.route('/set_session_name', methods=['POST'])
def set_session_name():
    race = load_race()
    race["session_name"] = request.get_json().get("name", "Moto 1")
    save_race(race)
    return jsonify(success=True)

@app.route('/delete_lap', methods=['POST'])
def delete_lap():
    ts = request.get_json().get("timestamp")
    race = load_race()
    race["laps"] = [l for l in race["laps"] if abs(l["timestamp"] - ts) > 0.01]
    save_race(race)
    return jsonify(success=True)

@app.route('/export_race')
def export_race():
    race = load_race()
    si = StringIO()
    writer = csv.writer(si)
    writer.writerow(['Session', 'Rider', 'Event', 'Lap Time (s)', 'Total Elapsed (s)', 'Timestamp'])
    for lap in race["laps"]:
        writer.writerow([race["session_name"], lap["name"], lap["label"], lap["lap_time"], lap["elapsed"], 
                         time.strftime("%H:%M:%S", time.localtime(lap["timestamp"]))])
    return Response(si.getvalue(), mimetype="text/csv", headers={"Content-Disposition": f"attachment; filename={race['session_name'].replace(' ','_')}_results.csv"})

if __name__ == '__main__':
    threading.Thread(target=run_reader, daemon=True).start()
    time.sleep(4)
    import webbrowser
    webbrowser.open('http://127.0.0.1:5000')
    webbrowser.open('http://127.0.0.1:5000/race')
    print("\n🌐 Both pages opened - Per-Antenna Power Loaded from config.txt")
    app.run(host='0.0.0.0', port=5000, debug=False)
