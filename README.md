# arrbot – Telegram-Bot für Radarr & Sonarr

Schreib dem Bot ganz normal, was du sehen willst („die neueste Staffel von Severance“,
„Dune, den neuen“). Er sucht den Titel, fragt nach, wenn mehrere passen, fügt ihn zu Radarr/Sonarr hinzu
und schreibt dir:

- 🎚 Buttons zur Qualität, falls du keine genannt hast (ohne Auswahl nach 60 s das Standardprofil aus
  `RADARR_QUALITY_PROFILE` / `SONARR_QUALITY_PROFILE`)

- ⬇️ wenn der Download startet (mit Qualität und Indexer)
- ✅ wenn er fertig und importiert ist
- ❌ wenn er fehlschlägt (mit Grund)
- ⚠️ wenn Radarr/Sonarr beim Import hängt (z. B. „manueller Import nötig“)
- 🔍 wenn nach 30 Minuten noch nichts gefunden wurde

Die Sprach-KI ist austauschbar: **Gemini** (kostenloses Kontingent reicht zum Testen), **Claude**
oder alles **OpenAI-kompatible** (OpenAI, OpenRouter, Groq, lokal mit Ollama/LM Studio).

## Einrichtung

1. **Bot anlegen:** In Telegram `@BotFather` → `/newbot` → Token kopieren.
2. **Gemini-Key:** <https://aistudio.google.com/apikey> → „Create API key“.
3. **Radarr/Sonarr-Keys:** jeweils *Settings → General → API Key*.
4. In `docker-compose.yml` unter `environment:` die Werte eintragen: `TELEGRAM_BOT_TOKEN`, `LLM_API_KEY`,
   `RADARR_*`, `SONARR_*` und `ALLOWED_USER_IDS`. Jede Einstellung ist dort kommentiert.
   **Danach die Datei nicht mehr committen**, sonst landen die Keys im Repo.
   Deine Telegram-ID kennst du nicht? Lass `ALLOWED_USER_IDS` erstmal auf einer beliebigen Zahl, starte den
   Bot und schreib ihm – er antwortet mit „Kein Zugriff. Deine Telegram-ID ist …“.
5. Starten:
   ```bash
   docker compose up -d --build
   docker compose logs -f
   ```

**Netzwerk:** Der Container muss Radarr/Sonarr erreichen. Laufen die auch in Docker, häng den Bot ins
gleiche Netzwerk (siehe Kommentar in `docker-compose.yml`) und nutze `http://radarr:7878`. Sonst die
LAN-IP des Servers verwenden. Der Bot braucht **keinen** offenen Port – er holt Telegram-Nachrichten
per Polling und fragt Radarr/Sonarr regelmäßig nach neuen Ereignissen.

## Anbieter wechseln

In `docker-compose.yml` unter `environment:` setzen, danach `docker compose up -d`:

| Anbieter | Einstellungen |
|---|---|
| Gemini (Standard) | `LLM_PROVIDER: "gemini"`, `LLM_API_KEY: "…"` (Modell-Standard `gemini-flash-latest`) |
| Claude | `LLM_PROVIDER: "anthropic"`, `LLM_API_KEY: "sk-ant-…"` (Standard `claude-opus-5-5`) |
| OpenAI | `LLM_PROVIDER: "openai"`, `LLM_API_KEY: "sk-…"`, z. B. `LLM_MODEL: "gpt-4o-mini"` |
| OpenRouter | `LLM_PROVIDER: "openai"`, `LLM_BASE_URL: "https://openrouter.ai/api/v1"`, `LLM_MODEL: "…"` |
| Ollama lokal | `LLM_PROVIDER: "openai"`, `LLM_BASE_URL: "http://ollama:11434/v1"`, `LLM_MODEL: "qwen2.5:14b"` |

Lokale Modelle müssen Tool-Calling können; kleine Modelle (< 7B) verhaspeln sich oft dabei.

## Befehle

- `/status` – laufende Downloads mit Fortschritt
- `/neu` – Gespräch zurücksetzen
- `/hilfe` – Beispiele

## Wie es funktioniert

```
Telegram ─► agent.py ─► llm.py (Gemini / Claude / OpenAI-kompatibel)
                │            ▲
                ▼            │ Werkzeug-Ergebnisse
            tools.py ─► arr.py ─► Radarr / Sonarr
tracker.py ── alle 60 s: /history/since + /queue ──► Nachricht an den Chat
```

- Der Bot beobachtet nur, was **über ihn** hinzugefügt oder neu gesucht wurde. Filme bis sie fertig
  sind, Serien bis `TRACK_DAYS` nach der letzten Aktivität (damit neue Folgen auch gemeldet werden).
- Zustand (beobachtete Titel, Abfrage-Zeitpunkt) liegt in `data/state.json` und übersteht Neustarts.
- SABnzbd braucht keine eigene Anbindung: Radarr/Sonarr kennen den Download-Status bereits.

## Entwicklung

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt pytest   # Windows; Linux: .venv/bin/pip
.venv/Scripts/python -m pytest -q
```

Die Tests laufen komplett offline mit nachgebautem Radarr/Sonarr und einer simulierten KI.
