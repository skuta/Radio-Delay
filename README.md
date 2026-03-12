# TimeShift Radio

A web-based internet radio player with time-delayed playback. Records a live radio stream to a local buffer and plays it back with a configurable delay — for example, 9 hours back, so you can listen to US primetime radio during European evening hours.

Supports multiple radio stations simultaneously.

## Features

- **Time-shifted playback** — listen to any point from live to 24+ hours ago
- **Multi-station support** — record and play back multiple stations at once
- **Minute-by-minute buffering** — stream is saved as per-minute MP3 chunks
- **Hot-swap config** — change delay, stream URL, or add stations without restarting
- **Auto-cleanup** — old buffer files are deleted automatically (26h+ retention)
- **Reconnection with backoff** — recovers from network errors automatically
- **Monitoring dashboard** — real-time stats page with uptime, errors, and system info
- **PLS/M3U support** — auto-resolves playlist URLs to direct stream URLs

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Copy and edit config
cp config.example.json config.json

# Start the downloader (records streams)
python downloader.py

# Start the server (in a separate terminal)
python server.py
```

Open `http://localhost:5000` in your browser.

## Configuration

Edit `config.json` to add your stations:

```json
{
    "stations": [
        {
            "id": "my-station",
            "name": "My Radio Station",
            "stream_url": "https://stream.example.com/live.mp3",
            "delay_hours": 6,
            "kbps": 128,
            "radio_timezone": "America/New_York"
        }
    ],
    "server": {
        "host": "0.0.0.0",
        "port": 5000,
        "timezone": "Europe/London"
    }
}
```

### Station Parameters

| Parameter | Description | Example |
|---|---|---|
| `id` | Unique slug (lowercase, hyphens) — used in URLs and buffer paths | `"bbc-radio3"` |
| `name` | Display name shown in the UI | `"BBC Radio 3"` |
| `stream_url` | Live stream URL (direct MP3, PLS, or M3U) | `"https://stream.example.com/live.mp3"` |
| `delay_hours` | Default delay in hours | `9` |
| `kbps` | Stream bitrate (for playback speed calculation) | `128` |
| `radio_timezone` | Timezone of the radio station | `"America/Los_Angeles"` |

### Server Parameters

| Parameter | Description | Default |
|---|---|---|
| `host` | Bind address | `"0.0.0.0"` |
| `port` | HTTP port | `5000` |
| `timezone` | Server/listener timezone (used for delay suggestions) | `"Europe/London"` |

Changes to `config.json` take effect immediately — no restart needed.

**Backward compatibility:** The old single-station flat config format is still supported and auto-migrated.

## API

| Endpoint | Description |
|---|---|
| `GET /` | Station selector (or player if single station) |
| `GET /station/<id>/` | Player UI for a specific station |
| `GET /station/<id>/stream?delay=N` | Audio stream with N-hour delay (float) |
| `GET /station/<id>/status` | JSON status (delay, buffer availability, timezones) |
| `GET /station/<id>/stats` | Detailed JSON stats (uptime, errors, system) |
| `GET /station/<id>/stats.html` | Monitoring dashboard |
| `GET /api/stations` | List all configured stations |
| `GET /health` | Health check |

Legacy routes (`/stream`, `/status`, `/stats`) redirect to the first configured station.

## Docker

```bash
docker-compose up -d
```

See [Dockerfile](Dockerfile) and [docker-compose.yml](docker-compose.yml).

## Project Structure

```
delayedradio/
├── server.py            # Flask server, streaming, status endpoints
├── downloader.py        # Background stream recorder (one thread per station)
├── index.html           # Web player UI
├── stats.html           # Monitoring dashboard
├── config.json          # Configuration (hot-swap, not committed)
├── config.example.json  # Example configuration template
├── requirements.txt     # Python dependencies
├── Dockerfile
├── docker-compose.yml
└── buffer/              # Auto-generated MP3 buffer (per-station subdirs)
    └── <station-id>/
        └── YYYYMMDD_HHMM.mp3
```

## Notes

- Buffer retains at least **26 hours** of audio per station, regardless of delay setting
- Missing minutes (recording gaps) are filled with silence to avoid disconnecting clients
- Each station at 128 kbps uses ~1.4 GB of disk for 26 hours of buffer

## License

[MIT](LICENSE)

---

> Built for personal use with [Claude Code](https://claude.com/claude-code) by Anthropic.
> Tested on Windows with Laragon; also runs in Docker.
