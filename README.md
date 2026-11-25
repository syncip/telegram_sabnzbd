# arrbot – Telegram-Bot für Radarr & Sonarr

Schreib dem Bot ganz normal, was du sehen willst („Staffel 1 und 2 von Dragonball Z“, „Dune, den
neuen“), oder schick einen Link (IMDb, TMDB, TVDB, Trakt, Letterboxd, JustWatch …). Er antwortet auf
Deutsch, nennt die deutschen Titel, bietet bei mehreren Treffern und bei der Staffelwahl Buttons an,
fügt den Titel zu Radarr/Sonarr hinzu und schreibt dir:

- 🎚 Buttons zur Qualität, falls du keine genannt hast (ohne Auswahl nach 60 s das Standardprofil)
- ⬇️ wenn der Download startet, mit ⏱ **Fertig-Uhrzeit aus SABnzbd** („fertig ca. 21:45 Uhr (noch 23 min)“)
- ✅ wenn er fertig und importiert ist
- ❌ wenn er fehlschlägt (mit Grund), ⚠️ wenn der Import hängt
- 🔍 wenn nach 30 Minuten nichts gefunden wurde, mit Grund: 🇩🇪 „Auf Deutsch erst ab 15.01.2027
  (digital)“ oder 🗣 „Es gibt 12 Releases, aber keins auf Deutsch“

Außerdem:

- **/downloads** – laufende, wartende und fertige Downloads mit Fortschrittsbalken, Größe, Tempo und
  Fertig-Uhrzeit, gebaut fürs Handy, mit 🔄-Button zum Aktualisieren. Jeder sieht nur seine eigenen,
  der Admin auf Wunsch alle (mit Namen, wer es angefragt hat).
- **Vorschläge:** „Was soll ich als Nächstes schauen?“ – passend zu dem, was du bisher geladen hast
  (TMDB-Empfehlungen, ohne was ihr schon habt). Antippen = hinzufügen.
- **Top-Listen:** „Welche Filme waren die Top 5 2024?“ zeigt die Liste mit Buttons, „Lade die Top 3
  Filme vom Oktober 2025“ lädt sie direkt (Standard-Qualität). Auch „bestbewertet“ oder „nach
  Einspielergebnis“.
- **Serien erweitern:** Fragst du eine Serie an, die schon da ist, sagt der Bot, welche Staffeln du
  hast und welche fehlen, und bietet „Alle fehlenden laden“ oder einzelne Staffeln an.
- **Deutsche Fassung:** „Gibt es Severance schon auf Deutsch?“, „Wann kommt Dune 3 auf Deutsch?“ –
  deutscher Kinostart, Digital- und Disc-Termin, Streaming in Deutschland.
- **Löschen und Pausieren:** „Lösch The Office“ fragt per Button nach (inkl. Dateien, nur aus
  Sonarr entfernen, oder abbrechen) und bricht laufende Downloads ab. „Such in 3 Tagen nochmal nach
  Dune 3“ pausiert die Suche in Radarr/Sonarr und startet sie dann automatisch neu. „Weiter mit Dune
  3“ sofort. Nutzer können nur ändern, was sie selbst angefragt haben; was mehrere wollten, löscht
  nur der Admin.
- **Wenig Nerv:** Wird nichts gefunden, kommt **eine** Nachricht (nach 30 Minuten) mit Buttons
  „⏸ In 3 Tagen nochmal“, „⏸ In 7 Tagen nochmal“, „🗑 Löschen“, „👍 Okay“. Danach sucht
  Radarr/Sonarr still weiter, die nächste Nachricht kommt erst, wenn ein Download startet.
  `NO_RELEASE_NOTICE_MINUTES: "0"` schaltet sie ganz ab.
- **Nutzerverwaltung:** Du bist Admin. Wer dem Bot schreibt, landet als Anfrage bei dir
  (✅ Freigeben / ❌ Ablehnen). Du bekommst eine Nachricht, wenn andere etwas hinzufügen.
- **Limits:** pro Nutzer oder für alle Anfragen pro Tag/Monat, Tokens oder Kosten pro Monat, dazu ein
  Gesamtbudget für den ganzen Bot.

Die Sprach-KI ist austauschbar: **OpenAI** (z. B. `gpt-4o-mini`), **Gemini**, **Claude** oder alles
OpenAI-kompatible (OpenRouter, Groq, lokal mit Ollama/LM Studio).

## Einrichtung

1. **Bot anlegen:** In Telegram `@BotFather` → `/newbot` → Token kopieren.
2. **KI-Key**, z. B. OpenAI: <https://platform.openai.com/api-keys>.
3. **Radarr/Sonarr-Keys:** jeweils *Settings → General → API Key*.
4. **Optional, aber empfohlen:**
   - **SABnzbd-Key:** *Config → General → API Key* – für Fortschritt, Tempo und Fertig-Uhrzeit.
   - **TMDB-Key** (kostenlos): Konto auf <https://www.themoviedb.org>, dann *Einstellungen → API* →
     „API-Schlüssel“ oder „Lesezugriffs-Token“. Für deutsche Titel, deutsche Termine, Vorschläge und
     Top-Listen.
5. In `docker-compose.yml` unter `environment:` eintragen: `TELEGRAM_BOT_TOKEN`, `ADMIN_USER_ID`
   (deine Telegram-ID, z. B. über `@userinfobot`), `LLM_*`, `RADARR_*`, `SONARR_*`, `SABNZBD_*`,
   `TMDB_API_KEY`. Jede Einstellung ist dort kommentiert.
   **Danach die Datei nicht mehr committen**, sonst landen die Keys im Repo.
   `PUID`/`PGID` müssen zum Besitzer des Daten-Ordners passen: Unraid `99`/`100`, sonst meist `1000`/`1000`.
6. Starten:
   ```bash
   docker compose up -d --build
   docker compose logs -f
   ```

**Netzwerk:** Der Container muss Radarr, Sonarr und SABnzbd erreichen. Laufen die auch in Docker, häng
den Bot ins gleiche Netzwerk (siehe Kommentar in `docker-compose.yml`) und nutze die Containernamen
(`http://radarr:7878`, `http://sabnzbd:8080`). Sonst die LAN-IP des Servers. Der Bot braucht **keinen**
offenen Port.

**Tipp:** Stell in Radarr unter *Settings → UI → Movie Info Language* „Deutsch“ ein, dann heißen auch
die Ordner und Titel in Radarr deutsch.

## Befehle

Für alle:

- `/downloads` (auch `/status`) – deine Downloads mit Fortschritt
- `/verbrauch` – deine Anfragen, Tokens und Kosten, mit Balken bis zum Limit
- `/neu` – Gespräch zurücksetzen
- `/hilfe` – Beispiele

Nur für den Admin:

- `/nutzer` – alle Nutzer mit Rolle, Verbrauch und Limits; Buttons zum Freigeben, Sperren, Entfernen
- `/limit WER ART WERT` – Limits setzen
  - WER: Name, Telegram-ID, `standard` (alle Nutzer ohne eigenes Limit) oder `gesamt` (ganzer Bot)
  - ART: `tag` (Anfragen/Tag), `monat` (Anfragen/Monat), `tokens` (pro Monat), `kosten` (Dollar/Monat)
  - WERT: Zahl oder `aus`

```
/limit standard tag 10
/limit Anna monat 50
/limit gesamt kosten 1
/limit 123456 tokens aus
```

Ist ein Limit erreicht, antwortet der Bot ohne KI-Aufruf mit einem Hinweis. Buttons und `/downloads`
gehen weiter, die kosten nichts. Standard-Limits gelten nie für den Admin; das Gesamtbudget schon.

## Was kostet das?

Gemessen mit `gpt-4o-mini`: Eine typische Anfrage („lad mir Dune 2“) braucht 2 KI-Aufrufe mit zusammen
etwa 3.200 Tokens rein und 60 raus, also **etwa 0,05 Cent**. Bei 5 Anfragen am Tag sind das rund
**0,08 $ im Monat**. Mit `/limit gesamt kosten 1` bleibt der Bot sicher unter 1 $.

Warum so wenig:

- Die KI findet nur den Titel. Auswahl von Treffer, Staffeln und Qualität, Vorschläge, Top-Listen,
  die Download-Übersicht und alle Benachrichtigungen laufen im Code, ohne weitere KI-Aufrufe.
- Nach einer Aktion endet der Durchgang ohne Abschluss-Antwort der KI, das Gespräch wird geleert.
- System-Prompt und Werkzeugliste sind kurz und immer gleich, OpenAI rechnet sie als Cache günstiger ab.

`gpt-4o-mini` ist für diesen Umfang die richtige Wahl. `gpt-4.1-mini` versteht etwas besser, kostet
aber etwa das Dreifache. Lohnt sich nur, wenn der Bot oft falsch versteht.

## Woher die Daten kommen

| Was | Quelle |
|---|---|
| Suche, Hinzufügen, Download-Ereignisse | Radarr/Sonarr-API |
| Fortschritt, Tempo, Fertig-Uhrzeit | SABnzbd-API (`mode=queue`), über die Download-ID verknüpft |
| Deutsche Titel, Kino/Digital/Disc-Termine in DE, Streaming in DE, Empfehlungen, Top-Listen | TMDB-API (`language=de-DE`, Release-Daten für `DE`, Watch-Provider von JustWatch) |
| Gibt es ein deutsches Release? | Release-Suche von Radarr/Sonarr über deine Indexer (Sprache der Releases) |

Grenzen: Für Serien kennt TMDB keine deutschen Ausstrahlungstermine und keine Info zur Synchro.
Der Bot schließt aus deutschem Streaming-Angebot und deutschen Texten darauf. Seiten wie fernsehserien.de
haben keine öffentliche API und werden deshalb nicht abgefragt.

## Logs

Der Bot schreibt ins Docker-Log (`docker logs arrbot`, Unraid: Log-Symbol im Docker-Tab) und zusätzlich
nach `data/arrbot.log` (rotiert bei 1 MB, 3 alte Dateien). Pro Nachricht steht dort jeder KI-Schritt mit
Dauer und Token-Zahl.

## Wie es funktioniert

```
Telegram ─► bot.py ─► agent.py ─► llm.py (OpenAI / Gemini / Claude)
              │           │
              │           ▼
              │       tools.py ─► arr.py ─► Radarr / Sonarr
              │           └─────► tmdb.py ─► TMDB
              ├─► downloads.py (Übersicht)  ◄── sabnzbd.py ◄── SABnzbd
tracker.py ── alle 60 s: /history/since + /queue + SABnzbd ──► Nachricht an den Chat
```

- Der Bot beobachtet nur, was **über ihn** hinzugefügt oder neu gesucht wurde.
- Zustand liegt in `data/state.json` (beobachtete Titel, fertige Downloads, Anfragen-Verlauf für
  Vorschläge) und `data/users.json` (Nutzer, Limits, Verbrauch). Beides übersteht Neustarts.

## Entwicklung

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt pytest   # Windows; Linux: .venv/bin/pip
.venv/Scripts/python -m pytest -q
```

Die Tests laufen komplett offline mit nachgebautem Radarr/Sonarr/SABnzbd/TMDB und einer simulierten KI.
