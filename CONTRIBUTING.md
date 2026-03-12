# Contributing to TimeShift Radio

Thanks for your interest in contributing! This project is simple by design — contributions that keep it that way are welcome.

## Getting Started

1. **Clone the repo** and install Python 3.8+
2. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```
3. **Create your config:**
   ```bash
   cp config.example.json config.json
   ```
   Edit `config.json` with your station details.
4. **Run the downloader** (records the stream):
   ```bash
   python downloader.py
   ```
5. **Run the server** (in a separate terminal):
   ```bash
   python server.py
   ```
6. Open `http://localhost:5000` in your browser.

## Adding a New Station

Add a new entry to the `stations` array in `config.json`:

```json
{
  "id": "my-station",
  "name": "My Radio Station",
  "stream_url": "https://stream.example.com/live.mp3",
  "delay_hours": 3,
  "kbps": 128,
  "radio_timezone": "America/New_York"
}
```

The `id` must be lowercase alphanumeric with hyphens only (e.g., `bbc-radio3`). It becomes the URL path and buffer subdirectory name.

## Pull Request Guidelines

- Keep changes focused — one feature or fix per PR
- Test with at least one station running before submitting
- Follow existing code style (no linter enforced, just be consistent)
- Update the README if you add new config options or endpoints

## Reporting Issues

Open a GitHub issue with:
- What you expected to happen
- What actually happened
- Your OS and Python version
- Relevant log output
