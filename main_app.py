#!/usr/bin/env python3
from flask import Flask, request, jsonify, send_from_directory
from pathlib import Path
import subprocess, json

app = Flask(__name__, static_folder="/opt/sonixscape/webui", static_url_path="")

# Preset directory
PRESET_DIR = Path("/opt/sonixscape/webui/presets")
PRESET_DIR.mkdir(parents=True, exist_ok=True)

# ? Serve index.html as homepage
@app.route('/')
def home():
    return send_from_directory(app.static_folder, "index.html")

# ? Serve static files automatically (JS, CSS, images, etc.)
@app.route('/<path:filename>')
def static_files(filename):
    return send_from_directory(app.static_folder, filename)

# -------------------------------------------------------------------
# API routes
# -------------------------------------------------------------------

@app.route('/api/info')
def api_info():
    try:
        hostname = subprocess.check_output(["hostname"], text=True).strip()
        uptime = subprocess.check_output(["uptime","-p"], text=True).strip()
        meminfo = subprocess.check_output(["free","-h"], text=True).splitlines()[1]
        mem_used, mem_total = meminfo.split()[2], meminfo.split()[1]
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    return jsonify({
        "hostname": hostname,
        "uptime": uptime,
        "memory": f"{mem_used} used / {mem_total} total",
        "status": "Online and Ready"
    })

@app.route('/api/presets')
def list_presets():
    files = [f.name for f in PRESET_DIR.glob("*.json")]
    return jsonify(files)

@app.route('/api/presets/<name>', methods=['GET'])
def get_preset(name):
    path = PRESET_DIR / f"{name}.json"
    if not path.exists():
        return jsonify({"error": "Preset not found"}), 404
    return send_from_directory(PRESET_DIR, f"{name}.json")

@app.route('/api/presets/<name>', methods=['POST'])
def save_preset(name):
    data = request.json
    if not data:
        return jsonify({"error": "No data"}), 400
    path = PRESET_DIR / f"{name}.json"
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
    return jsonify({"success": True, "preset": name})

@app.route('/api/presets/<name>', methods=['DELETE'])
def delete_preset(name):
    path = PRESET_DIR / f"{name}.json"
    if path.exists():
        path.unlink()
        return jsonify({"success": True})
    return jsonify({"error": "Preset not found"}), 404

# -------------------------------------------------------------------
# Run if executed directly (systemd overrides port/host)
# -------------------------------------------------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=False)
