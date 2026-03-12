# TimeShift Radio

Webový prehrávač internetového rádia s možnosťou časového posunu (delay). Server v reálnom čase nahrá live stream do lokálneho bufferu a klientovi ho prehrá s nastaveným oneskorením – napríklad 9 hodín dozadu, čo zodpovedá primetime vysielaniu v európskom čase pri americkom rádiu.

## Ako to funguje

1. **Buffer vlákno** – server sa pripojí na live MP3 stream, stiahne ho a ukladá po minútach do adresára `buffer/` (formát `YYYYMMDD_HHMM.mp3`).
2. **Delayed stream** – keď sa klient pripojí na `/stream?delay=9`, server mu číta súbory z bufferu spred 9 hodín a streamuje ich s príslušnou rýchlosťou.
3. **Cleanup vlákno** – automaticky maže súbory staršie ako 26 hodín (nezávisle od nastaveného delayu).
4. **Hot-swap** – delay aj stream URL je možné zmeniť v `config.json` za behu bez reštartu servera.

## Požiadavky

- Python 3.8+
- Závislosti: `flask`, `requests`, `waitress`

## Inštalácia a spustenie

```bash
# Inštalácia závislostí
pip install -r requirements.txt

# Spustenie servera
python server.py
```

Server bude dostupný na `http://0.0.0.0:5000`.

## Konfigurácia (`config.json`)

| Parameter | Popis | Príklad |
|---|---|---|
| `stream_url` | URL live streamu (priamy MP3, PLS alebo M3U) | `"https://128.mp3.pls.kdfc.live"` |
| `delay_hours` | Predvolené oneskorenie v hodinách | `9` |
| `kbps` | Bitrate streamu (pre výpočet rýchlosti prehrávania) | `128` |

Zmeny v `config.json` sa aplikujú za behu – nie je potrebný reštart.

## API endpointy

| Endpoint | Popis |
|---|---|
| `GET /` | Webový prehrávač (UI) |
| `GET /stream?delay=N` | Audio stream s oneskorením N hodín (float) |
| `GET /status` | JSON so stavom servera a dostupným bufferom |

### Príklad `/status` odpovede

```json
{
  "status": "online",
  "delay_hours": 9,
  "stream_url": "https://128.mp3.pls.kdfc.live",
  "max_available_delay_hours": 25.3
}
```

## UI

Webové rozhranie umožňuje:
- Výber oneskorenia od **Naživo** (0h) až po **−24 hodín** (Včera)
- Tlačidlá pre nedostupné delaye (buffer ešte nebol nahraný) sú automaticky deaktivované
- Rotujúci disk animácia počas prehrávania

## Štruktúra projektu

```
delayedradio/
├── server.py        # Flask server, buffer a streaming logika
├── index.html       # Webový prehrávač (SPA)
├── config.json      # Konfigurácia (hot-swap)
├── requirements.txt # Python závislosti
└── buffer/          # Dočasné MP3 súbory (auto-generované)
```

## Poznámky

- Buffer sa udržuje minimálne **26 hodín** dozadu, aj keď je delay nastavený na 0.
- Ak chýba súbor pre danú minútu (výpadok záznamu), stream pošle ticho namiesto odpojenia klienta.
- Server podporuje PLS aj M3U playlisty – automaticky rozozná a vytiahne priamu stream URL.
