import os
import re
import time
import json
import logging
import threading
import glob
import shutil
import psutil
from flask import Flask, Response, jsonify, send_from_directory, request, redirect
from datetime import datetime, timedelta

logging.basicConfig(
    format='%(asctime)s [%(name)s] %(levelname)s %(message)s',
    level=logging.INFO,
)
log = logging.getLogger("server")

app = Flask(__name__)
BUFFER_DIR = "buffer"
CONFIG_FILE = "config.json"

os.makedirs(BUFFER_DIR, exist_ok=True)

# Per-station streaming state
_server_start_time = time.time()
_station_state_lock = threading.Lock()
_station_state = {}

DOWNLOADER_STATS_FALLBACK = {
    "written_at": None,
    "downloader_start_time": None,
    "buffer_uptime_seconds": 0.0,
    "buffer_downtime_seconds": 0.0,
    "reconnect_count": 0,
    "downloaded_bytes": 0,
    "timeout_errors": 0,
    "network_errors": 0,
    "resolver_failures": 0,
    "buffer_session_active": False,
    "buffer_session_start": None,
    "reconnect_history": [],
}


def _get_station_state(station_id):
    """Get or create per-station streaming state."""
    with _station_state_lock:
        if station_id not in _station_state:
            _station_state[station_id] = {
                "client_count": 0,
                "client_lock": threading.Lock(),
                "stats_lock": threading.Lock(),
                "sent_bytes": 0,
                "silence_chunks_sent": 0,
            }
        return _station_state[station_id]


def _inc_station(station_id, key, amount=1):
    state = _get_station_state(station_id)
    with state["stats_lock"]:
        state[key] += amount


def load_config():
    """Load current config with hot-swap support. Handles both old and new format."""
    try:
        with open(CONFIG_FILE, 'r') as f:
            raw = json.load(f)
    except Exception:
        return {
            "stations": [{
                "id": "default",
                "name": "Radio Station",
                "stream_url": "https://128.mp3.pls.kdfc.live",
                "delay_hours": 9,
                "kbps": 128,
                "radio_timezone": "America/Los_Angeles",
            }],
            "server": {
                "host": "0.0.0.0",
                "port": 5000,
                "timezone": "Europe/Bratislava",
            }
        }

    # Backward compatibility: old flat format
    if "stations" not in raw:
        station = {
            "id": "default",
            "name": "Radio Station",
            "stream_url": raw.get("stream_url"),
            "delay_hours": raw.get("delay_hours", 9),
            "kbps": raw.get("kbps", 128),
            "radio_timezone": raw.get("radio_timezone", "America/Los_Angeles"),
        }
        return {
            "stations": [station],
            "server": {
                "host": "0.0.0.0",
                "port": raw.get("port", 5000),
                "timezone": raw.get("server_timezone", "Europe/Bratislava"),
            }
        }
    return raw


def validate_config(config):
    """Validate config and log warnings. Returns True if usable."""
    seen_ids = set()
    for station in config.get("stations", []):
        sid = station.get("id", "")
        if not re.match(r'^[a-z0-9-]+$', sid):
            log.error("Station id '%s' is invalid (must be lowercase alphanumeric with hyphens)", sid)
            return False
        if sid in seen_ids:
            log.error("Duplicate station id: '%s'", sid)
            return False
        seen_ids.add(sid)
        if not station.get("stream_url"):
            log.error("Station '%s' is missing stream_url", sid)
            return False
    return True


def get_station(station_id):
    """Find a station config by id, or None."""
    config = load_config()
    for s in config.get("stations", []):
        if s["id"] == station_id:
            return s
    return None


def get_first_station_id():
    """Get the first station's id for backward-compat routes."""
    config = load_config()
    stations = config.get("stations", [])
    return stations[0]["id"] if stations else "default"


def station_buffer_dir(station_id):
    return os.path.join(BUFFER_DIR, station_id)


def station_stats_file(station_id):
    return os.path.join(station_buffer_dir(station_id), "downloader_stats.json")


def _read_downloader_stats(station_id):
    try:
        with open(station_stats_file(station_id), 'r') as f:
            return json.load(f)
    except Exception:
        return DOWNLOADER_STATS_FALLBACK.copy()


def count_missing_minutes(station_id):
    """Count missing minutes in the buffer (gaps in recording)."""
    try:
        buf_dir = station_buffer_dir(station_id)
        files = glob.glob(os.path.join(buf_dir, "*.mp3"))
        if len(files) < 2:
            return 0
        times = set()
        for f in files:
            basename = os.path.basename(f).replace(".mp3", "")
            try:
                times.add(datetime.strptime(basename, "%Y%m%d_%H%M"))
            except ValueError:
                pass
        if len(times) < 2:
            return 0
        t_min = min(times)
        t_max = max(times)
        missing = 0
        current = t_min
        while current <= t_max:
            if current not in times:
                missing += 1
            current += timedelta(minutes=1)
        return missing
    except Exception:
        return 0


def delayed_stream_generator(station_id, requested_delay=None):
    """Read delayed audio data and simulate a live stream for the client."""
    station = get_station(station_id)
    if not station:
        return

    if requested_delay is not None:
        try:
            delay_hours = float(requested_delay)
        except ValueError:
            delay_hours = station.get("delay_hours", 9)
    else:
        delay_hours = station.get("delay_hours", 9)

    chunk_size = 8192
    kbps = station.get("kbps", 128)
    bytes_per_second = (kbps * 1000) // 8
    sleep_interval = chunk_size / bytes_per_second
    buf_dir = station_buffer_dir(station_id)

    while True:
        target_time = datetime.now() - timedelta(hours=delay_hours)
        target_minute_str = target_time.strftime("%Y%m%d_%H%M")
        filename = os.path.join(buf_dir, f"{target_minute_str}.mp3")

        if requested_delay is None:
            station = get_station(station_id)
            if station and station.get("delay_hours", delay_hours) != delay_hours:
                delay_hours = station.get("delay_hours")
                continue

        if os.path.exists(filename) and os.path.getsize(filename) > 0:
            try:
                with open(filename, 'rb') as f:
                    while True:
                        if requested_delay is None:
                            station = get_station(station_id)
                            if station and station.get("delay_hours", delay_hours) != delay_hours:
                                break

                        chunk = f.read(chunk_size)
                        if not chunk:
                            break
                        _inc_station(station_id, "sent_bytes", len(chunk))
                        yield chunk
                        time.sleep(sleep_interval * 0.9)
            except Exception as e:
                log.warning("Error reading buffer for station '%s': %s", station_id, e)
                time.sleep(1)
        else:
            _inc_station(station_id, "silence_chunks_sent")
            yield b'\x00' * 100
            time.sleep(1)


# ─── Station-specific routes ───

@app.route('/station/<station_id>/')
def station_player(station_id):
    if not get_station(station_id):
        return jsonify({"error": "Station not found"}), 404
    return send_from_directory('.', 'index.html')


@app.route('/station/<station_id>/stats.html')
def station_stats_page(station_id):
    if not get_station(station_id):
        return jsonify({"error": "Station not found"}), 404
    return send_from_directory('.', 'stats.html')


@app.route('/station/<station_id>/stream')
def station_stream(station_id):
    station = get_station(station_id)
    if not station:
        return jsonify({"error": "Station not found"}), 404

    custom_delay = request.args.get('delay')
    state = _get_station_state(station_id)

    def tracked_generator():
        with state["client_lock"]:
            state["client_count"] += 1
            log.info("[%s] Client connected. Active: %d", station_id, state["client_count"])
        try:
            yield from delayed_stream_generator(station_id, custom_delay)
        finally:
            with state["client_lock"]:
                state["client_count"] -= 1
                log.info("[%s] Client disconnected. Active: %d", station_id, state["client_count"])

    return Response(tracked_generator(), mimetype="audio/mpeg", headers={
        "Cache-Control": "no-cache"
    })


@app.route('/station/<station_id>/status')
def station_status(station_id):
    station = get_station(station_id)
    if not station:
        return jsonify({"error": "Station not found"}), 404

    config = load_config()
    buf_dir = station_buffer_dir(station_id)

    max_available_delay = 0.0
    try:
        files = glob.glob(os.path.join(buf_dir, "*.mp3"))
        if files:
            oldest_file = min(files)
            basename = os.path.basename(oldest_file).replace(".mp3", "")
            oldest_time = datetime.strptime(basename, "%Y%m%d_%H%M")
            age_timedelta = datetime.now() - oldest_time
            max_available_delay = max(0.0, age_timedelta.total_seconds() / 3600.0)
    except Exception as e:
        log.warning("[%s] Error checking available buffer: %s", station_id, e)

    return jsonify({
        "status": "online",
        "station_id": station["id"],
        "station_name": station["name"],
        "delay_hours": station.get("delay_hours", 0),
        "stream_url": station.get("stream_url"),
        "max_available_delay_hours": max_available_delay,
        "server_timezone": config.get("server", {}).get("timezone", "Europe/London"),
        "radio_timezone": station.get("radio_timezone", "America/Los_Angeles"),
    })


@app.route('/station/<station_id>/stats')
def station_stats_endpoint(station_id):
    station = get_station(station_id)
    if not station:
        return jsonify({"error": "Station not found"}), 404

    ds = _read_downloader_stats(station_id)
    buf_dir = station_buffer_dir(station_id)
    state = _get_station_state(station_id)

    # Check if downloader is alive (written_at not older than 30s)
    downloader_alive = False
    if ds.get("written_at"):
        try:
            written_dt = datetime.fromisoformat(ds["written_at"])
            downloader_alive = (datetime.now() - written_dt).total_seconds() < 30
        except Exception:
            pass

    # Live uptime delta
    buf_uptime = ds["buffer_uptime_seconds"]
    if ds.get("buffer_session_active") and ds.get("buffer_session_start"):
        buf_uptime += time.time() - ds["buffer_session_start"]
    buf_downtime = ds["buffer_downtime_seconds"]

    total = buf_uptime + buf_downtime
    uptime_pct = (buf_uptime / total * 100) if total > 0 else 100.0

    # Streaming-side stats
    with state["stats_lock"]:
        sent_bytes = state["sent_bytes"]
        silence_chunks_sent = state["silence_chunks_sent"]

    # Buffer directory size
    buf_dir_bytes = sum(
        os.path.getsize(f)
        for f in glob.glob(os.path.join(buf_dir, "*.mp3"))
        if os.path.isfile(f)
    )

    proc = psutil.Process()
    disk = psutil.disk_usage(os.path.abspath(BUFFER_DIR))

    downloader_uptime = 0.0
    if ds.get("downloader_start_time"):
        downloader_uptime = time.time() - ds["downloader_start_time"]

    return jsonify({
        "station": {
            "id": station["id"],
            "name": station["name"],
        },
        "recording": {
            "uptime_seconds": round(buf_uptime, 1),
            "downtime_seconds": round(buf_downtime, 1),
            "uptime_percent": round(uptime_pct, 2),
            "reconnect_count": ds["reconnect_count"],
            "downloaded_mb": round(ds["downloaded_bytes"] / 1024 / 1024, 2),
            "missing_minutes": count_missing_minutes(station_id),
            "buffer_dir_mb": round(buf_dir_bytes / 1024 / 1024, 2),
            "downloader_alive": downloader_alive,
        },
        "playback": {
            "sent_mb": round(sent_bytes / 1024 / 1024, 2),
            "silence_chunks_sent": silence_chunks_sent,
            "active_clients": state["client_count"],
        },
        "errors": {
            "timeout_errors": ds["timeout_errors"],
            "network_errors": ds["network_errors"],
            "resolver_failures": ds["resolver_failures"],
            "reconnect_history": list(reversed(ds["reconnect_history"]))[:20],
        },
        "system": {
            "server_uptime_seconds": round(time.time() - _server_start_time, 1),
            "downloader_uptime_seconds": round(downloader_uptime, 1),
            "ram_mb": round(proc.memory_info().rss / 1024 / 1024, 1),
            "cpu_percent": proc.cpu_percent(interval=0.1),
            "disk_free_gb": round(disk.free / 1024 ** 3, 2),
            "disk_total_gb": round(disk.total / 1024 ** 3, 2),
        }
    })


# ─── General routes ───

@app.route('/api/stations')
def list_stations():
    config = load_config()
    return jsonify([
        {"id": s["id"], "name": s["name"]}
        for s in config.get("stations", [])
    ])


@app.route('/health')
def health():
    return jsonify({"status": "ok"})


@app.route('/')
def index():
    config = load_config()
    stations = config.get("stations", [])
    if len(stations) == 1:
        return redirect(f'/station/{stations[0]["id"]}/')
    return send_from_directory('.', 'index.html')


# ─── Backward-compatible routes (redirect to first station) ───

@app.route('/stream')
def legacy_stream():
    return redirect(f'/station/{get_first_station_id()}/stream?{request.query_string.decode()}')


@app.route('/status')
def legacy_status():
    return station_status(get_first_station_id())


@app.route('/stats')
def legacy_stats():
    return station_stats_endpoint(get_first_station_id())


@app.route('/stats.html')
def legacy_stats_page():
    return redirect(f'/station/{get_first_station_id()}/stats.html')


if __name__ == '__main__':
    config = load_config()
    if not validate_config(config):
        log.error("Invalid configuration. Exiting.")
        exit(1)
    server_cfg = config.get("server", {})
    host = server_cfg.get("host", "0.0.0.0")
    port = server_cfg.get("port", 5000)
    log.info("Starting server on http://%s:%d ...", host, port)
    from waitress import serve
    serve(app, host=host, port=port, threads=100)
