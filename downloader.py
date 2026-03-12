import os
import re
import time
import json
import logging
import threading
import glob
import shutil
import requests
from datetime import datetime, timedelta

logging.basicConfig(
    format='%(asctime)s [%(name)s] %(levelname)s %(message)s',
    level=logging.INFO,
)

BUFFER_DIR = "buffer"
CONFIG_FILE = "config.json"


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
    log = logging.getLogger("config")
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


def migrate_flat_buffer(station_id):
    """If old flat buffer/*.mp3 files exist, move them into buffer/<station_id>/."""
    flat_files = glob.glob(os.path.join(BUFFER_DIR, "*.mp3"))
    if not flat_files:
        return
    target_dir = os.path.join(BUFFER_DIR, station_id)
    os.makedirs(target_dir, exist_ok=True)
    log = logging.getLogger("migrate")
    log.info("Migrating %d buffer files to %s/", len(flat_files), target_dir)
    for f in flat_files:
        dest = os.path.join(target_dir, os.path.basename(f))
        shutil.move(f, dest)
    # Also move stats file if it exists
    old_stats = os.path.join(BUFFER_DIR, "downloader_stats.json")
    if os.path.exists(old_stats):
        shutil.move(old_stats, os.path.join(target_dir, "downloader_stats.json"))
    log.info("Buffer migration complete.")


class StationDownloader:
    """Downloads and buffers a single radio station's stream."""

    def __init__(self, station_config):
        self.station_id = station_config["id"]
        self.station_config = station_config
        self.buffer_dir = os.path.join(BUFFER_DIR, self.station_id)
        self.stats_file = os.path.join(self.buffer_dir, "downloader_stats.json")
        self.log = logging.getLogger(f"dl.{self.station_id}")

        self._stats_lock = threading.Lock()
        self._stats = {
            "buffer_uptime_seconds": 0.0,
            "buffer_downtime_seconds": 0.0,
            "reconnect_count": 0,
            "downloaded_bytes": 0,
            "timeout_errors": 0,
            "network_errors": 0,
            "resolver_failures": 0,
            "reconnect_history": [],
        }
        self._buffer_session_start = None
        self._start_time = time.time()
        self._stop_event = threading.Event()

        os.makedirs(self.buffer_dir, exist_ok=True)

    def stop(self):
        """Signal all loops to exit."""
        self._stop_event.set()

    def _inc(self, key, amount=1):
        with self._stats_lock:
            self._stats[key] += amount

    def _add_reconnect(self, reason):
        """Add entry to reconnect history and increment counter."""
        entry = {"ts": datetime.now().isoformat(timespec="seconds"), "reason": reason}
        with self._stats_lock:
            self._stats["reconnect_history"].append(entry)
            if len(self._stats["reconnect_history"]) > 100:
                self._stats["reconnect_history"].pop(0)
            self._stats["reconnect_count"] += 1

    def _end_buffer_session(self):
        """Accumulate active recording session time into stats."""
        with self._stats_lock:
            if self._buffer_session_start is not None:
                self._stats["buffer_uptime_seconds"] += time.time() - self._buffer_session_start
                self._buffer_session_start = None

    def _get_station_config(self):
        """Re-read this station's config for hot-swap support."""
        config = load_config()
        for s in config.get("stations", []):
            if s["id"] == self.station_id:
                return s
        return self.station_config

    def resolve_stream_url(self, url, session):
        """Resolve PLS/M3U playlist URLs to direct stream URLs."""
        try:
            head_r = session.get(url, stream=True, timeout=10)
            head_r.raise_for_status()

            content_type = head_r.headers.get('Content-Type', '').lower()
            if 'audio/mpeg' in content_type or 'audio/aac' in content_type or 'audio/ogg' in content_type:
                return url

            text = session.get(url, timeout=10).text
            if '[playlist]' in text.lower() or text.startswith('#EXTM3U'):
                for line in text.splitlines():
                    line = line.strip()
                    if line.lower().startswith('file'):
                        parts = line.split('=', 1)
                        if len(parts) == 2 and parts[1].startswith('http'):
                            return parts[1]
                    elif line.startswith('http'):
                        return line
        except Exception as e:
            self.log.warning("Error resolving playlist: %s", e)
            self._inc("resolver_failures")
        return url

    def cleanup_old_files(self):
        """Infinite loop that deletes old buffer files."""
        while True:
            try:
                station = self._get_station_config()
                delay_hours = station.get("delay_hours", 9)
                min_keep_hours = 26
                keep_hours = max(min_keep_hours, delay_hours + 2)
                cutoff_time = datetime.now() - timedelta(hours=keep_hours)

                files = glob.glob(os.path.join(self.buffer_dir, "*.mp3"))
                for file in files:
                    basename = os.path.basename(file).replace(".mp3", "")
                    try:
                        file_time = datetime.strptime(basename, "%Y%m%d_%H%M")
                        if file_time < cutoff_time:
                            os.remove(file)
                            self.log.info("Deleted old file: %s", file)
                    except ValueError:
                        pass
            except Exception as e:
                self.log.error("Cleanup error: %s", e)
            if self._stop_event.wait(timeout=300):
                return

    def buffer_stream(self):
        """Infinite loop downloading live stream data and saving it per-minute."""
        session = requests.Session()
        retry_strategy = requests.packages.urllib3.util.retry.Retry(
            total=5,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504]
        )
        adapter = requests.adapters.HTTPAdapter(max_retries=retry_strategy)
        session.mount("http://", adapter)
        session.mount("https://", adapter)

        retry_delay = 5

        while True:
            if self._stop_event.is_set():
                return
            station = self._get_station_config()
            base_stream_url = station.get("stream_url")
            self.log.info("Resolving stream URL from %s...", base_stream_url)
            stream_url = self.resolve_stream_url(base_stream_url, session)
            self.log.info("Connecting to stream: %s", stream_url)

            try:
                r = session.get(stream_url, stream=True, timeout=(10, 30))
                r.raise_for_status()

                ctype = r.headers.get('Content-Type', '')
                if 'text' in ctype.lower():
                    self.log.warning("Received text instead of audio! Content-Type: %s. Retrying.", ctype)
                    retry_start = time.time()
                    self._stop_event.wait(timeout=retry_delay)
                    self._inc("buffer_downtime_seconds", time.time() - retry_start)
                    if self._stop_event.is_set():
                        return
                    retry_delay = min(retry_delay * 2, 60)
                    continue

                retry_delay = 5
                self.log.info("Stream connected successfully.")
                with self._stats_lock:
                    self._buffer_session_start = time.time()

                current_minute = None
                f = None

                for chunk in r.iter_content(chunk_size=8192):
                    if self._stop_event.is_set():
                        if f:
                            f.close()
                        self._end_buffer_session()
                        return
                    if chunk:
                        self._inc("downloaded_bytes", len(chunk))
                        now = datetime.now()
                        this_minute = now.strftime("%Y%m%d_%H%M")
                        if this_minute != current_minute:
                            if f:
                                f.close()
                            current_minute = this_minute
                            filename = os.path.join(self.buffer_dir, f"{this_minute}.mp3")
                            f = open(filename, 'ab')
                        f.write(chunk)

                if f:
                    f.close()
                self._end_buffer_session()
                self._add_reconnect("stream_closed")
                self.log.info("Stream ended by server. Reconnecting...")

            except requests.exceptions.Timeout:
                self._end_buffer_session()
                self._inc("timeout_errors")
                self._add_reconnect("timeout")
                self.log.warning("Stream read timeout. Retrying in %ds", retry_delay)
                retry_start = time.time()
                self._stop_event.wait(timeout=retry_delay)
                self._inc("buffer_downtime_seconds", time.time() - retry_start)
                if self._stop_event.is_set():
                    return
                retry_delay = min(retry_delay * 2, 60)

            except requests.exceptions.RequestException as e:
                self._end_buffer_session()
                self._inc("network_errors")
                self._add_reconnect(f"network: {str(e)[:80]}")
                self.log.warning("Network error: %s. Retrying in %ds", e, retry_delay)
                retry_start = time.time()
                self._stop_event.wait(timeout=retry_delay)
                self._inc("buffer_downtime_seconds", time.time() - retry_start)
                if self._stop_event.is_set():
                    return
                retry_delay = min(retry_delay * 2, 60)

            except Exception as e:
                self._end_buffer_session()
                self._add_reconnect(f"error: {str(e)[:80]}")
                self.log.error("Unexpected error: %s. Retrying in %ds", e, retry_delay)
                retry_start = time.time()
                self._stop_event.wait(timeout=retry_delay)
                self._inc("buffer_downtime_seconds", time.time() - retry_start)
                if self._stop_event.is_set():
                    return
                retry_delay = min(retry_delay * 2, 60)

    def write_stats_loop(self):
        """Write stats snapshot to disk every 10 seconds."""
        while True:
            try:
                with self._stats_lock:
                    snapshot = {
                        "written_at": datetime.now().isoformat(timespec="seconds"),
                        "downloader_start_time": self._start_time,
                        "buffer_uptime_seconds": self._stats["buffer_uptime_seconds"],
                        "buffer_downtime_seconds": self._stats["buffer_downtime_seconds"],
                        "reconnect_count": self._stats["reconnect_count"],
                        "downloaded_bytes": self._stats["downloaded_bytes"],
                        "timeout_errors": self._stats["timeout_errors"],
                        "network_errors": self._stats["network_errors"],
                        "resolver_failures": self._stats["resolver_failures"],
                        "buffer_session_active": self._buffer_session_start is not None,
                        "buffer_session_start": self._buffer_session_start,
                        "reconnect_history": list(self._stats["reconnect_history"]),
                    }
                tmp_path = self.stats_file + ".tmp"
                with open(tmp_path, 'w') as f:
                    json.dump(snapshot, f)
                os.replace(tmp_path, self.stats_file)
            except Exception as e:
                self.log.error("Error writing stats: %s", e)
            if self._stop_event.wait(timeout=10):
                return

    def start(self):
        """Start cleanup + stats threads, then run buffer_stream (blocking)."""
        self.log.info("Starting downloader for station '%s'", self.station_id)
        threading.Thread(target=self.cleanup_old_files, daemon=True, name=f"cleanup-{self.station_id}").start()
        threading.Thread(target=self.write_stats_loop, daemon=True, name=f"stats-{self.station_id}").start()
        self.buffer_stream()

    def start_background(self):
        """Start everything in a background thread."""
        threading.Thread(target=self.start, daemon=True, name=f"dl-{self.station_id}").start()


class DownloadSupervisor:
    """Polls config.json and starts/stops StationDownloader instances to match."""

    POLL_INTERVAL = 30

    def __init__(self):
        self.log = logging.getLogger("supervisor")
        self._lock = threading.Lock()
        self._running = {}  # station_id -> {"downloader": ..., "thread": ...}
        self._stop_event = threading.Event()

    def stop(self):
        self._stop_event.set()

    def _start_station(self, station_cfg):
        sid = station_cfg["id"]
        dl = StationDownloader(station_cfg)
        t = threading.Thread(target=dl.start, daemon=True, name=f"dl-{sid}")
        t.start()
        with self._lock:
            self._running[sid] = {"downloader": dl, "thread": t}
        self.log.info("Started downloader for station '%s'.", sid)

    def _stop_station(self, sid):
        with self._lock:
            entry = self._running.pop(sid, None)
        if entry is None:
            return
        entry["downloader"].stop()
        entry["thread"].join(timeout=90)
        if entry["thread"].is_alive():
            self.log.warning("Downloader thread for '%s' did not stop within timeout.", sid)
        else:
            self.log.info("Downloader for station '%s' stopped cleanly.", sid)

    def _reconcile(self):
        config = load_config()
        if not validate_config(config):
            self.log.warning("Config validation failed; skipping reconcile.")
            return
        config_ids = {s["id"] for s in config.get("stations", [])}
        with self._lock:
            running_ids = set(self._running.keys())
        for sid in running_ids - config_ids:
            self.log.info("Station '%s' removed from config; stopping.", sid)
            self._stop_station(sid)
        station_map = {s["id"]: s for s in config.get("stations", [])}
        for sid in config_ids - running_ids:
            self.log.info("Station '%s' added to config; starting.", sid)
            self._start_station(station_map[sid])

    def run(self):
        self.log.info("Supervisor started (poll interval: %ds).", self.POLL_INTERVAL)
        self._reconcile()
        while not self._stop_event.wait(timeout=self.POLL_INTERVAL):
            self._reconcile()
        self.log.info("Supervisor stopping all stations...")
        with self._lock:
            ids = list(self._running.keys())
        for sid in ids:
            self._stop_station(sid)
        self.log.info("Supervisor stopped.")


if __name__ == '__main__':
    log = logging.getLogger("main")
    os.makedirs(BUFFER_DIR, exist_ok=True)

    config = load_config()
    if not validate_config(config):
        log.error("Invalid configuration. Exiting.")
        exit(1)

    stations = config["stations"]

    # Migrate flat buffer if needed (single station with old layout)
    if len(stations) == 1:
        migrate_flat_buffer(stations[0]["id"])

    supervisor = DownloadSupervisor()
    try:
        supervisor.run()
    except KeyboardInterrupt:
        log.info("Interrupted. Shutting down...")
        supervisor.stop()
