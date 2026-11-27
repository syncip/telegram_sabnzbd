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
  der Admin auf Wunsch alle (mit Namen, wer es angefragt hat). Hängt ein Import in Radarr/Sonarr,
  steht er zusammengefasst unter „⚠️ Braucht Eingriff“, mit deutscher Erklärung und wo man klickt.
  „Was läuft gerade?“ zeigt nur Aktuelles, fertige Downloads per Button „✅ Fertige“.
  „Wie ist der Status bei der Suche?“ direkt nach einer Anfrage zeigt nur diesen Titel: Der Bot
  behält nach jeder Aktion die letzte Frage und Antwort (~50 Tokens) für solche Rückfragen.
- **Vorschläge:** „Was soll ich als Nächstes schauen?“ – passend zu deinem Geschmack (TMDB-Empfehlungen,
  ohne was ihr schon habt). Ausgangspunkt ist, was dir gefallen hat: 👍 und ⭐4–5 zählen am meisten, dann in
  Plex zu Ende Geschautes, dann Angefragtes. Antippen = hinzufügen. Wünsche werden beachtet: „nur Filme
  neuer als 2000“, „ab 7 Sterne“, „was Lustiges“, „spannend, unter 90 Minuten“, „wie Dark, aber lustiger“,
  „nur was es auf Deutsch gibt“. Gibt es zu wenig Passendes, füllt der Bot mit den bestbewerteten Titeln
  der Genres oder Jahre auf. Für Abwechslung höchstens ein Teil pro Filmreihe, nicht nur ein Genre und
  ein „💎 Geheimtipp“ (gut bewertet, kaum bekannt); zu jedem Titel steht, warum („wie Arrival“). Was schon
  auf dem Server liegt und du noch nicht gesehen hast, kommt als „📼 Schon auf dem Server“ dazu.
  Der Bot versteht auch, **wobei** du schaust: „was für nebenbei beim Cardio“, „beim Kochen“, „zum
  Einschlafen“, „mit den Kindern“. Dann zählen passende Genres (nebenbei: Komödie, Animation, Reality, Doku;
  nichts Düsteres oder Verschachteltes) und kurze Folgen (nebenbei bis ~35 Min., Einschlafen bis 30 Min.) mehr
  als dein sonstiger Geschmack; die Folgenlänge steht dabei.
- **📚 Bücher (mit Chaptarr):** „Schick mir das Buch Der Schwarm“, „Ich will Limit von Schätzing lesen“.
  Mehrere Treffer: Buttons zur Auswahl (📗 = schon da). Ist das E-Book in Chaptarr, kommt die Datei sofort in
  den Chat (EPUB bevorzugt, bis 50 MB). Sonst fügt der Bot es in Chaptarr hinzu (nur dieses Buch, nicht alle
  des Autors), lässt suchen und schickt die Datei, sobald sie da ist; nach 7 Tagen ohne Treffer sagt er
  Bescheid. Einrichten: `CHAPTARR_URL`, `CHAPTARR_API_KEY` und den Bücher-Ordner nur lesend einbinden
  (`/mnt/user/media/books:/books:ro`, Pfade ggf. mit `BOOK_PATHS` übersetzen). Hörbücher noch nicht.
  Chaptarr läuft überall mit wie Radarr/Sonarr: `/check`, Überwachung, Warnungen, `/downloads` (📚) und
  Speicherplatz.
  - **Format:** Beim ersten Buch fragt der Bot: EPUB, PDF oder Kindle (AZW3) – und merkt es sich. Ändern:
    „Bücher als PDF“ oder „schick es mir für Kindle“. Gibt es das Buch nicht im Format, wandelt der Bot es mit
    Calibre um (ist im Image) und schickt es, wenn es unter 50 MB bleibt; sonst kommt das Original.
  - **Empfehlungen:** „Ich mag die Metro-Bücher, empfiehl mir ähnliche“ – fünf Bücher mit kurzem Grund, je ein
    Button „📥 holen“.
- **Bewerten:** Unter Vorschlägen „👎👁 Bewerten“: 👎 = nicht meins, 👁 = schon gesehen (dann fragt der
  Bot „Wie fandest du’s?“ mit ⭐1–5). Nach einem frisch zu Ende geschauten Film oder einer Serie in Plex
  fragt der Bot das auch (höchstens zwei am Tag, „🔕 Nicht mehr fragen“ beendet es). Sterne aus Plex zählen
  mit, angefangene und lange liegen gelassene Titel etwas negativ. Per Text: „Inception fand ich super“.
  Neue Nutzer bekommen beim ersten Vorschlag ein paar bekannte Titel zum 👍/👎 („Kurz zum Kennenlernen“).
- **Nutzerprofil:** Der Bot merkt sich pro Nutzer dauerhaft, was er mag (SQLite-Datenbank
  `data/profiles.db`). Er lernt ohne KI-Kosten aus jeder Anfrage (Genres und Jahr aus Radarr/Sonarr),
  aus fertigen und gelöschten Titeln und aus „🙅 Nichts davon“ unter Vorschlägen. Dazu kommt, was du
  ihm sagst: „Merk dir, ich mag keine Horrorfilme“, „Schlag mir nur Filme ab 2000 vor“, „Vergiss
  Horror“. Vorschläge bevorzugen deine Lieblingsgenres und lassen Abgelehntes weg. Die KI bekommt
  eine Zeile Profil (~30 Tokens) vor jede Nachricht. **/profil** oder „Was weißt du über mich?“ zeigt
  alles, mit Button „🗑 Alles vergessen“. Entfernt der Admin einen Nutzer, wird sein Profil gelöscht.
  Beim ersten Start übernimmt der Bot die bisherigen Anfragen aus `state.json`.
- **Top-Listen:** „Welche Filme waren die Top 5 2024?“ zeigt die Liste mit Buttons, „Lade die Top 3
  Filme vom Oktober 2025“ lädt sie direkt (Standard-Qualität). Auch „bestbewertet“ oder „nach
  Einspielergebnis“. Was ihr schon habt, ist mit ✅ markiert und hat keinen Button; „⬇️ Alle
  hinzufügen“ holt den Rest auf einmal. Auch pro Studio: „Top 3 Marvel-Filme“ (ohne Jahr = aller
  Zeiten). Braucht `TMDB_API_KEY` – ohne sagt der Bot das direkt.
- **Wunschliste:** „Merk dir Avatar 4“ – gibt es noch keinen Termin, legt der Bot den Titel nicht gleich in
  Radarr an, sondern prüft täglich um 9:30 Uhr und fügt ihn hinzu, sobald ein Termin feststeht (du bekommst
  Bescheid). „Was steht auf meiner Wunschliste?“ zeigt sie.
- **Zusammen schauen:** „Was können Anna und ich zusammen schauen?“ – aus beiden Profilen und
  Plex-Verläufen: was schon da ist und keiner von euch gesehen hat, plus neue Vorschläge als Buttons.
  Genres, die einer von euch meidet, fallen weg.
- **Serien erweitern:** Fragst du eine Serie an, die schon da ist, sagt der Bot, welche Staffeln du
  hast und welche fehlen, und bietet „Alle fehlenden laden“ oder einzelne Staffeln an.
- **Deutsche Fassung:** „Gibt es die aktuelle Staffel South Park schon auf Deutsch?“ zeigt **pro Folge**:
  ✅ bei dir auf Deutsch, ⬇️ deutscher Download auf deinen Indexern, 📁 bei dir nur Englisch,
  🇺🇸 nur Originalfassung gefunden, 🔤 deutscher Folgentitel bekannt, ⏳ noch nicht ausgestrahlt.
  Bei Filmen („Wann kommt Dune 3 auf Deutsch?“) deutscher Kinostart, Digital- und Disc-Termin,
  Streaming und ob du ihn schon auf Deutsch hast. Die Antwort baut der Bot selbst aus den Quellen
  zusammen, die KI formuliert hier nichts um (sie hatte vorher Termine erfunden).
- **Nochmal suchen:** „Such Eine schrecklich nette Familie nochmal“ (oder nur „such nochmal“ nach einer
  Antwort, „such die fehlenden Folgen von Dark“, „Staffel 2 nochmal suchen“) startet für einen Titel, der schon
  in Radarr/Sonarr ist, eine neue Suche – auch unter dem deutschen Titel. Bei Serien nur die fehlenden, schon
  erschienenen Folgen; fehlt eine ganze Staffel, sucht Sonarr zuerst nach einem Staffelpaket. Ist der Titel
  nicht überwacht, schaltet der Bot die Überwachung dafür ein (sonst lädt Sonarr/Radarr nichts) und sagt es.
- **Deutsche Folgen nachladen (Admin):** „Prüf, ob die Folgen von Dark auf Deutsch da sind“ prüft alle
  ausgestrahlten Folgen in der Bibliothek, sucht auf den Indexern deutsche Releases (bei vielen
  Folgen bevorzugt Staffelpakete, alle Staffeln; dauert es über 2½ Minuten, macht „🔎 Weiter suchen“
  beim Rest weiter) und bietet per Button „Alle auf
  Deutsch laden“ oder „Nur fehlende“ an. Dateien in anderer Sprache werden dabei durch die
  deutschen ersetzt. Gibt es noch nichts auf Deutsch, bietet der Bot „🔔 Melden, sobald es Deutsch gibt“ an;
  kennt Sonarr für Folgen keine Datei (obwohl sie in Plex liegen), „🔄 Sonarr neu einlesen“ und prüft danach
  gleich nochmal.
- **Folgen löschen (Admin):** „Lösch die Folgen von Dark, die nicht auf Deutsch sind“ (oder „Staffel 2“,
  „alle Folgen“) zeigt, was mit wie viel GB weg würde, und fragt per Button: löschen und auf Deutsch
  neu suchen, nur löschen (nicht mehr automatisch laden) oder abbrechen. Die KI erkennt nur die
  Serie – Prüfen, Auswählen und Löschen läuft im Code, ohne weitere Tokens.
- **Löschen und Pausieren (Admin):** „Lösch The Office“ fragt per Button nach (inkl. Dateien, nur aus
  Sonarr entfernen, oder abbrechen) und bricht laufende Downloads ab. „Such in 3 Tagen nochmal nach
  Dune 3“ pausiert die Suche in Radarr/Sonarr und startet sie dann automatisch neu. „Weiter mit Dune
  3“ sofort.
- **Echt Deutsch?** Geprüft wird die **Tonspur**, die Radarr/Sonarr beim Import aus der Datei lesen
  – nicht der Release-Name. So fallen Folgen auf, die „GERMAN“ heißen, aber englisch sind („🏷 heißt
  Deutsch, Tonspur aber nicht“). „Ist Eine schrecklich nette Familie wirklich auf Deutsch?“ oder „Ist
  Dune auf Deutsch?“ bietet dann ein **anderes** deutsches Release als Ersatz an (nie dasselbe noch
  einmal) und löscht die alte Datei erst nach deinem Okay.
- **🎧 Reinhören:** Die Markierung der Tonspur kann falsch sein – alte DVD-Rips markieren Deutsch oft als
  Englisch. Heißen alle Folgen einer Serie „Deutsch“, ist aber keine Spur als Deutsch markiert, zeigt die
  Mediathek sie als „❔ vermutlich nur falsch markiert“. Mit „🎧 Reinhören“ schneidet der Bot (lokal,
  ffmpeg) 20-Sekunden-Ausschnitte bei 25, 50 und 72 % der Laufzeit aus – nie Intro oder Abspann – und lässt
  nur diese von OpenAI-Spracherkennung prüfen (ca. 0,2 Cent pro Ausschnitt; zwei gleiche Ergebnisse
  reichen). Bei Serien, die überall gleich aussehen, entscheiden 3 Folgen vom Anfang, aus der Mitte und vom
  Ende; sonst wird jede verdächtige Folge geprüft. Was Deutsch ist, merkt sich der Bot
  (`data/german_ok.json`); echte englische Folgen bekommen den Button „Deutsch suchen und ersetzen“.
  Braucht die Medien-Ordner im Container (nur lesend) und `MEDIA_PATHS`. Ohne gibt es stattdessen
  „✅ Ist Deutsch“ zum Bestätigen nach eigenem Anhören.
- **Deutsch-Benachrichtigung:** „Sag mir, wenn Dune 3 auf Deutsch kommt“ – der Bot prüft täglich um 9 Uhr
  (Filme: deutsche Termine und Streaming laut TMDB, Serien: neue deutsche Folgen auf deinen Indexern),
  startet dann die Suche oder bietet die Folgen per Button an. Auch als Button „🔔 Bei Deutsch melden“
  unter „nichts gefunden“.
- **Kalender:** „Was kommt diese Woche?“ oder /kalender – neue Folgen und Filmtermine, auf Wunsch nur
  deine Titel. **Speicherplatz:** „Wie viel Platz ist noch?“ oder /speicher; Admins werden gewarnt, wenn
  weniger als `DISK_WARN_GB` frei ist.
- **Wochenrückblick:** „Schick mir jeden Sonntag einen Wochenrückblick“ – was fertig wurde, was in der
  Woche kommt, drei Vorschläge. „Keinen Wochenrückblick mehr“ beendet ihn.
- **Plex:** Jeder verbindet sein eigenes Konto mit /plex: der Bot zeigt einen Code, den gibst du auf
  plex.tv/link ein – dein Passwort sieht der Bot nie. Danach: „Wo war ich bei Dark?“ (mit Button „In
  Plex öffnen“), „Hab ich Dune gesehen?“, „Was schaue ich gerade?“. Alle 6 Stunden gleicht der Bot ab,
  was du geschaut hast; das fließt in Profil und Vorschläge ein.
- **Mediathek (Admin):** /mediathek oder „Was kann weg?“ zeigt
  ⏳ was fehlt (in Radarr/Sonarr, aber nicht geladen – Button „alle fehlenden suchen“),
  ❓ was geladen, aber nicht in Plex ist (Button „Plex scannen“),
  ✅ was alle mit verknüpftem Plex gesehen haben, 💤 was seit `UNWATCHED_DAYS` Tagen niemand
  angefangen hat (beides nach Größe, mit Lösch-Buttons) und 🗣 was nicht wirklich auf Deutsch ist.
- **Qualitätsprofile (Admin):** „Neues Qualitätsprofil 4K nur Englisch“ oder „Profil 1080p mindestens
  Deutsch und Englisch + Originalsprache“. Der Bot zeigt, was er verstanden hat, und legt es nach
  Bestätigung in Radarr und Sonarr an (Sprachen als Custom Formats „Sprache: Deutsch“ usw., braucht
  Sonarr v4). 2K wird als 1080p angelegt, weil Radarr/Sonarr kein 1440p kennen.
- **Neu in Plex:** Jeden Abend um 18 Uhr bekommt jeder mit verknüpftem Plex, was seit gestern dazukam
  – Folgen pro Serie zusammengefasst, Lieblingsgenres zuerst (⭐), gemiedene Genres fehlen, mit
  „▶️ In Plex öffnen“. Abschalten per Button oder in /plex.
- **Webseiten beobachten (Admin):** „Überwache https://… nach neuen Filmen“. Alle `SITE_CHECK_HOURS` Stunden
  schaut der Bot nach. Hat sich nichts geändert, kostet das nichts. Sonst gehen nur die **neuen** Zeilen
  (max. 4.000 Zeichen) an die KI, die nur Titel herausschreiben darf. Was schon da ist, abgelehnt wurde
  oder ein gemiedenes Genre hat, fällt weg; der Rest kommt als Buttons, sortiert nach Geschmack.
  „🙅 Nichts davon“ lernt mit. Max. 5 Seiten pro Nutzer. Seiten, die nur per JavaScript laden, gehen nicht.
- **Nachfragen zu Gesehenem (Admin):** Hast du in Plex einen Film oder Staffeln einer Serie zu Ende
  geschaut, fragt der Bot hin und wieder (Standard: alle 3 Tage höchstens 3 Fragen, frühestens 2 Tage nach
  dem Anschauen, abends um 19 Uhr): „Du hast Dune gesehen – löschen?“ Buttons: Löschen (bei Serien die
  gesehenen Staffeln, bei beendeten und komplett gesehenen auch die ganze Serie), Behalten (fragt dazu nicht
  mehr), Später fragen (in 14 Tagen), 🛡 Nie löschen. Ohne Sperre, wie beim Medien-Manager. Ändern: „Frag mich
  nur alle 7 Tage“, „Frag mich nicht mehr nach Gesehenem“, „Was habe ich gesehen, das weg kann?“ (sofort).
- **Medien-Manager (Admin):** Aufräumen auf Befehl, in deinen Worten: „Lösche alle Filme, die länger als
  180 Tage da sind und die ich nicht gesehen habe“, „Lösche alle Filme, die ich gesehen habe“, „Die 10
  größten Serien, die ich nie angefangen habe“, „Horrorfilme unter 6 Sterne“. „Gesehen“ heißt: von dir (dein
  Plex-Verlauf). Die KI macht daraus nur einen Filter, welche Titel passen, rechnet der Bot selbst aus
  Radarr/Sonarr und Plex. Immer zuerst eine Liste mit Größe: „🗑 Alle löschen“, „👆 Einzeln durchgehen“
  (Löschen · Behalten · 🛡 Nie löschen) oder „Abbrechen“. Bei Serien fragt der Bot per Button, ob einzelne
  Folgen, ganze Staffeln oder ganze Serien weg sollen. Gelöscht wird direkt über Radarr/Sonarr mit den
  Dateien, aber **ohne Sperre**: alles kann jederzeit wieder hinzugefügt werden. Gelöschte Folgen und
  Staffeln werden nicht mehr überwacht (sonst lädt Sonarr sie gleich wieder), „such nochmal“ holt sie zurück.
  Nie in der Liste: „🛡 Nie löschen“-Titel („Dune nie löschen“), was jemand anderes gerade schaut und was
  jemand anderes in den letzten 30 Tagen angefragt hat. „Was frisst am meisten Platz?“ zeigt freien
  Speicher, die größten Titel und wie viel Gesehenes / lange Ungesehenes ausmacht.
- **Duplikat-Finder (Admin):** „Such doppelte Filme“ findet in Plex denselben Film oder dieselbe Folge mehrfach
  – geprüft wird der Inhalt, nicht Dateiname oder Größe: Plex hat die Versionen demselben Titel zugeordnet
  (bzw. gleiche TMDB-ID, gleiche Staffel und Folge) und die Laufzeit ist gleich (Standard ab 97 %,
  einstellbar: „Duplikate ab 95 % Ähnlichkeit“). Etwas weniger ähnlich (vielleicht Extended Cut): der Bot
  fragt einzeln. Behalten wird zuerst die Sprache (Standard Deutsch vor Englisch), dann die Auflösung (Standard
  die höchste) – änderbar: „Bei Duplikaten 1080p vor 4K behalten“, „Auflösung ist wichtiger als Sprache“.
  Gelöscht wird über Plex (dort unter Einstellungen → Bibliothek „Löschen von Medien erlauben“ einschalten),
  danach lesen Radarr/Sonarr den Titel neu ein. Nur anschauen: „Zeig mir die Duplikate“ oder der Button
  „📋 Alle anzeigen“ – die ganze Liste seitenweise, je Eintrag was weg soll und was bleibt, mit Dateinamen;
  gelöscht wird nur per Button.
- **„Arbeitest du noch?“:** Läuft etwas Langes (Duplikate, Medien-Manager, Vorschläge), beantwortet der Bot
  Fragen wie „Arbeitest du noch?“, „Läuft das noch?“, „Hängst du?“ sofort und ohne KI: woran er arbeitet, seit
  wann und welcher Schritt gerade läuft. Andere Nachrichten in der Zeit bekommen kurz „Ich bin noch bei … –
  deine Nachricht ist gleich danach dran“.
- **Aufräumregeln (Admin):** „Lösch Filme, die alle gesehen haben, nach 30 Tagen“, „Serien, die keiner
  nach einem Jahr angeschaut hat, nur wenn weniger als 100 GB frei sind“. Täglich um 11 Uhr kommt eine
  Liste mit „🗑 Alle löschen“ und „🛡 Behalten“ (wird nie wieder vorgeschlagen). Nur wenn du ausdrücklich
  „automatisch“ sagst, löscht der Bot selbst und meldet es. /regeln zeigt und löscht Regeln.
- **Freigaben (Admin):** „Wünsche von Anna erst freigeben“, „… wenn es Horrorfilme sind“, „Wünsche
  von allen erst freigeben, wenn der Download größer als 20 GB ist“. Ohne Größe wird nichts hinzugefügt,
  bis du „✅ Freigeben“ tippst. Mit Größe (nur Filme) legt der Bot den Film ohne Download an, schaut, welches
  Release Radarr nehmen würde, und fragt nur, wenn es größer ist – bis dahin ist der Film
  „nicht überwacht“, Radarr lädt ihn also nicht von selbst. Ablehnen entfernt ihn wieder. Der Nutzer
  bekommt in beiden Fällen Bescheid. Admins brauchen nie eine Freigabe. /freigaben zeigt Regeln und
  offene Wünsche.
- **Aktivität (Admin):** /aktivitaet oder „Was haben die Nutzer gefragt?“ – Anfragen der letzten Tage
  mit Ergebnis (✅ geklappt, 🤷 nichts gefunden, ⚠️ Problem, ❌ Fehler), Erfolgsquote, meistgenutzte
  Funktionen, pro Nutzer und die letzten Probleme mit Fehlertext. Bei echten Fehlern (Absturz, KI antwortet
  nicht) bekommst du sofort eine Nachricht. Wird 90 Tage aufbewahrt; Nutzer sehen in /hilfe, dass du das
  siehst.
- **Überwachung (Admin):** Alle `MONITOR_MINUTES` Minuten prüft der Bot Radarr, Sonarr, Chaptarr, SABnzbd,
  TMDB und Plex (ohne KI, kostet nichts). Fällt einer zweimal hintereinander aus, kommt „🚨 Sonarr ist nicht
  erreichbar“, wenn er zurück ist „✅ läuft wieder (war 12 Min. weg)“.
- **Sonarr/Radarr/Chaptarr-Warnungen (Admin):** Bei jeder Überwachungsrunde liest der Bot die Warnungen von
  Sonarr/Radarr/Chaptarr (System → Status) und meldet neue einmal, mit Erklärung auf Deutsch; verschwindet eine,
  kommt „✅ behoben“. Harmloses behebt er selbst: Ist ein Indexer oder Download-Client wegen Fehlern
  gesperrt, testet er ihn neu und lässt Sonarr/Radarr den Status prüfen – und sagt, ob es geholfen hat.
  Zusätzlich prüft er (nur Sonarr/Radarr), ob Indexer nur Hauptkategorien (5000/5100) haben; dann gibt es den Button
  „🔧 Unterkategorien eintragen“ (ändert die Indexer-Einstellung, deshalb nur auf Knopfdruck). `/check`
  zeigt die aktuellen Warnungen mit. Damit es nicht nervt: gemeldet wird erst, was zwei Runden hintereinander
  da ist, alles in einer Nachricht, ohne Link-Vorschau; was er selbst behoben hat, meldet er nicht. Mit
  „🔕 … nicht mehr melden“ schweigt eine Warnung, bis sie einmal weg war.
- **Suche ohne Ergebnis:** Startet der Bot eine Suche, wartet er im Hintergrund, bis Sonarr/Radarr fertig
  sind. Wurde nichts geladen, prüft er sofort selbst, warum, und sagt es (statt erst nach 30 Minuten):
  Titel/Staffel nicht überwacht? Liefern die Indexer gar nichts (dann auch: sind nur Hauptkategorien wie
  5000/5100 eingetragen, ohne Unterkategorien)? Oder gibt es Releases, die alle abgelehnt werden – mit
  Qualität, Sprache und Grund („3× DVD (Deutsch), Qualität nicht im Profil“). Dazu die üblichen Buttons.
- **Nur in anderer Sprache:** Gibt es einen Titel bei deinen Indexern nur z. B. auf Koreanisch und
  Englisch, sagt der Bot das direkt – und dass dein gewähltes Profil (etwa „1080p Deutsch“) dafür im
  Zweifel nicht passt. Buttons: „⬇️ Auf Koreanisch laden“, „⬇️ Auf Englisch laden“, „⬇️ Auf Multi laden“,
  „🔔 Auf Deutsch warten“, „🗑 Wieder löschen“. Geladen wird das beste Release der Sprache, möglichst nah an
  deiner Qualität (nie darüber), bei Serien das Staffelpaket. Weitere Staffeln sucht Sonarr weiter mit dem
  Profil – willst du alles in der Sprache, stell die Serie in Sonarr auf ein Profil ohne Deutsch-Pflicht.
- **Viele Folgen auf einmal:** Lädt Sonarr eine ganze Staffel oder Serie, kommt eine Meldung beim Start
  und eine Zusammenfassung, wenn alles durch ist („✅ 24 von 24 Folgen fertig importiert (S01E01–S02E12)“) –
  nicht eine pro Folge.
- **Version:** /version zeigt die laufende Version und ob auf GitHub eine neuere liegt; einmal am Tag
  sagt der Bot dir von selbst Bescheid. Bei einem privaten Repo braucht er dafür `UPDATE_TOKEN`
  (GitHub → Settings → Developer settings → Fine-grained token, nur dieses Repo, „Contents: Read-only“). Nach einem Update meldet er „🆕 Bot aktualisiert: 1.6.0 → 1.7.0“
  mit den Neuerungen.
- **Buttons nach Neustart:** Offene Auswahlen (Treffer, Staffeln, Qualität) werden gespeichert
  (`data/buttons.json`) und funktionieren auch nach einem Update des Containers.
- **Wenig Nerv:** Wird nichts gefunden, kommt **eine** Nachricht (nach 30 Minuten) mit Buttons
  „⏸ In 3 Tagen nochmal“, „⏸ In 7 Tagen nochmal“, „🗑 Löschen“, „👍 Okay“. Danach sucht
  Radarr/Sonarr still weiter, die nächste Nachricht kommt erst, wenn ein Download startet.
  `NO_RELEASE_NOTICE_MINUTES: "0"` schaltet sie ganz ab.
- **Nutzerverwaltung:** Du bist Admin. Wer dem Bot schreibt, landet als Anfrage bei dir
  (✅ Freigeben / ❌ Ablehnen) und bekommt **einmal** die Antwort, dass der Admin gefragt ist. Bis zur
  Freigabe antwortet der Bot nicht mehr, nichts davon erreicht die KI (kostet also nichts), und auch
  Buttons gehen nicht. Nach der Freigabe bekommt der Nutzer Bescheid. Gesperrte hören gar nichts.
  Du bekommst eine Nachricht, wenn andere etwas hinzufügen.
- **Limits:** pro Nutzer oder für alle: KI-Nachrichten pro Tag/Monat, Tokens oder Kosten pro Tag oder Monat,
  dazu ein Gesamtbudget für den ganzen Bot (z. B. „gesamt max. 200.000 Tokens pro Tag“). Ab 80 % und bei
  100 % eines Gesamtlimits bekommst du einmal Bescheid.
- **Limit-Empfehlung:** „Welches Limit sollte ich setzen?“ oder `/limit empfehlung` – der Bot schaut sich
  den Verbrauch der letzten 30 Tage an (Schnitt, Spitzentag, aktive Nutzer) und schlägt Gesamt- und
  Standardlimits mit Sicherheitsaufschlag vor (×1,5, bei weniger als 14 Tagen Daten ×2, unter 7 Tagen ×3,
  plus 0,1 je weiterem aktiven Nutzer). Per Button übernimmst du alles, nur Gesamt oder nur Standard. Und was jemand hinzufügen darf: Filme, Serien, Staffeln oder Folgen pro
  Stunde, Tag, Woche oder Monat („Sarah höchstens 3 Filme pro Tag“, „Standard 40 Folgen pro Woche“,
  `/limit Sarah filme_tag 3`). Das gilt auch für „such nochmal“. Ist ein Wunsch zu groß (eine Serie mit
  mehr Folgen, als noch gehen), sagt der Bot, wie viel noch geht, und schlägt z. B. nur eine Staffel vor.
  Was der Admin freigibt, zählt nicht gegen das Limit.
- **Rechte verteilen:** „Sarah darf ihre Filme und Serien löschen“ – sie darf dann löschen, was sie selbst
  angefragt hat, sonst nichts. Weitere Rechte: alles löschen, eigene Titel pausieren, Deutsch-Reparatur und
  Folgen aufräumen, Medien-Manager (Achtung: alle Titel), Webseiten beobachten, keine Limits. „Was darf
  Sarah?“ oder ihre Nutzerkarte zeigt es, „Sarah darf nicht mehr löschen“ nimmt es zurück. Der Nutzer
  bekommt eine Nachricht, wenn sich seine Rechte ändern. Admin-Ansichten (Statistik, Nutzer, Regeln,
  Freigaben …) bleiben immer beim Admin.
- **Wer darf was:** Nutzer suchen und **fügen hinzu** (Filme, Serien, fehlende Staffeln), dazu alles, was
  nur zeigt: Vorschläge, Top-Listen, Deutsch-Info, Kalender, eigene Downloads, Wunschliste, Plex.
  Löschen, Pausieren, Folgen austauschen, Webseiten beobachten und selbstgebaute Werkzeuge gibt es nur
  für Admins (oder mit einem Recht, s. o.) – die KI bekommt diese Werkzeuge bei Nutzern gar nicht erst, der
  Code prüft es bei jedem Aufruf noch einmal, und Buttons gelten nur in dem Chat, für den sie gedacht sind. Unter „Nichts gefunden“ sehen Nutzer nur „🔔 Bei Deutsch melden“ und „👍 Okay“.
- **Höchstqualität:** „Anna darf höchstens 1080p“ oder „Standard höchstens 1080p“ (gilt für alle Nutzer
  ohne eigene Grenze; SD, 720, 1080, 4K oder „aus“). Nutzer sehen dann nur Qualitätsprofile bis zu dieser
  Auflösung; ist das Standardprofil höher, nimmt der Bot das beste erlaubte. Admins haben keine Grenze.
- **Spam-Schutz:** Wer in einer Minute mehr als 8 Nachrichten schickt, 4× dasselbe in 10 Minuten schreibt
  oder über 25 Titel in einer Stunde hinzufügt, wird **vorsorglich gesperrt**; du bekommst sofort eine
  Meldung mit „🔓 Entsperren“. **Warnungen** (an Nutzer und dich, höchstens einmal am Tag): 10 Titel in einer
  Stunde, 80 % eines Limits, Limit erreicht, oder abends: weit mehr Anfragen als die anderen (3× der Median).
  Alles läuft im Code, ohne Tokens. `SPAM_CHECK: "0"` schaltet es ab. Admins werden nie geprüft.
- **Monatsbericht (Admin):** Am 1. um 9:15 Uhr: Kosten und Anfragen (mit Vergleich zum Vormonat), Nutzer
  nach Kosten, neue und gesperrte Nutzer, hinzugefügte Titel, Erfolgsquote und häufigste Probleme, Sperren
  und Warnungen, neue Werkzeuge. `/monatsbericht` zeigt den letzten Monat, `/monatsbericht jetzt` den
  laufenden.
- **Plex-Sammlungen (Admin):** „Mach eine Sammlung Marvel“ legt in Plex eine Sammlung mit den passenden
  Titeln an (pro Bibliothek). „Automatische Sammlungen pro Nutzer an“ pflegt täglich „Wünsche von Anna“
  usw.; es wird nur ergänzt, nie entfernt. Braucht das Plex-Konto des Server-Besitzers.
- **Selbstgebaute Werkzeuge (Admin):** Fragst du etwas, wofür es kein Werkzeug gibt („Welche Filme mit
  Keanu Reeves habe ich noch nicht?“), bietet der Bot an, sich eins zu bauen – mit geschätzten Kosten
  vorher (mit `gpt-6.1-terra` etwa 0,5–1,5 Cent pro Bau). Ein stärkeres Modell (`RECIPE_MODEL`) schreibt ein
  **Rezept**: keine Programmierung, sondern ein paar lesende Abfragen an TMDB, Radarr oder Sonarr plus
  Filter, Sortierung und Textvorlage. Der Bot prüft es gegen eine feste Liste erlaubter Abfragen (nur
  lesen), macht einen Probelauf und zeigt dir Ergebnis und echte Kosten: „✅ Behalten“ oder „🗑 Verwerfen“.
  Behaltene Werkzeuge nutzt die normale KI ab dann direkt, das kostet nur die normale Frage. /rezepte
  listet sie, „Lösch das Werkzeug …“ entfernt eins.

Die Sprach-KI ist austauschbar: **OpenAI** (Standard `gpt-6-luna`), **Gemini**, **Claude** oder alles
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
6. Starten – das fertige Image `ghcr.io/syncip/telegram_sabnzbd:latest` wird geholt, nichts muss gebaut werden:
   ```bash
   docker compose up -d
   docker compose logs -f
   ```
   **Update:** `docker compose up -d` holt die neueste Version (`pull_policy: always`). Eine feste Version
   geht mit `:1.8.1` statt `:latest`. Das Image baut GitHub Actions bei jedem Push nach `main` – erst
   laufen alle Tests, dann wird gebaut (amd64 und arm64).
   Lieber selbst bauen? In `docker-compose.yml` `image`/`pull_policy` gegen `build: .` tauschen und
   `docker compose up -d --build`.

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
- `/stumm [30 | 2h | 1d]` – Download-Meldungen eine Weile stumm (Standard 60 Min., auch per Text: „Nerv mich
  10 Minuten nicht“, „3 Tage Ruhe“). Danach kommt eine kurze Zusammenfassung, was in der Zeit war. `/laut` –
  sofort wieder an
- `/meldungen` – welche Download-Meldungen du dauerhaft bekommst, per Button an/aus: Start, fertig (Serien:
  eine Zusammenfassung am Ende statt jeder Folge), Probleme, nichts gefunden. Geht auch per Text: „Keine Info
  mehr, wenn ein Download startet, nur wenn er fertig ist“. Ist etwas unklar, fragt der Bot nach.
- `/version` – laufende Version und welches KI-Modell der Bot nutzt (oder einfach fragen: „Welche Version hast du?“)
- `/hilfe` – Beispiele

Nur für den Admin:

- `/admin` – Übersicht auf einen Blick: Version, Modelle (mit Preis und Abschalt-Warnung), Nutzer, Kosten
  und Budget, Erfolgsquote der letzten 24 Std., Offenes (Zugangsanfragen, Freigaben, Sperren), Dienste –
  mit Buttons zu allen Admin-Ansichten und zum direkten Freigeben wartender Nutzer
- `/nutzer` – alle Nutzer mit Rolle, Verbrauch, Limits und Höchstqualität; Buttons zum Freigeben, Sperren,
  Entfernen und ℹ️ für die **Nutzerkarte** (Verbrauch, Limits mit Herkunft, Wünsche, letzte Titel,
  Erfolgsquote, Warnungen, Plex, Wunschliste) – auch per Text: „Zeig mir Anna“
- `/statistik` – Nutzerzahl, aktive Nutzer, Anfragen, KI-Aufrufe, Tokens und Kosten (heute, Monat,
  gesamt, Ø pro Anfrage), Budget-Balken, die letzten Monate und alles pro Nutzer
- `/check` – prüft Telegram, KI, Radarr, Sonarr, Chaptarr (falls eingerichtet), SABnzbd und TMDB: ✅/❌ mit Version, Antwortzeit und
  verständlichem Fehler („API-Key falsch (401)“, „nicht erreichbar – URL/Netzwerk prüfen“). Die KI-Prüfung
  kostet einen Mini-Aufruf (~60 Tokens).
- `/aktivitaet [Name]` – was Nutzer fragen, was geklappt hat, wo es Probleme gab
- `/freigaben` – Freigabe-Regeln und offene Wünsche
- `/regeln` – Aufräumregeln
- `/version` – Version, Update verfügbar?, Neuerungen
- `/monatsbericht [jetzt]` – Monatsbericht (letzter oder laufender Monat)
- `/rezepte` – selbstgebaute Werkzeuge
- `/log [Name] [Anzahl]` – **Chat-Log**: was der Bot bei der letzten Nachricht (oder den letzten n) gemacht
  hat – Nachricht, jeder KI-Schritt (Modell, Dauer, Tokens), jedes Werkzeug mit Argumenten und Ergebnis,
  jede Anfrage an Sonarr/Radarr mit Status und Dauer, und was Sonarr/Radarr nach der Suche melden
  („Season search completed. 0 reports downloaded.“). `/log Anna` zeigt Annas letzte Nachricht.
- `/debug an|aus` – das Log kommt nach jeder Antwort mit, Suchergebnisse kommen, sobald sie fertig sind
- `/limit WER ART WERT` – Limits setzen
  - WER: Name, Telegram-ID, `standard` (alle Nutzer ohne eigenes Limit) oder `gesamt` (ganzer Bot)
  - ART: `tag` (Anfragen/Tag), `monat` (Anfragen/Monat), `tokens` (pro Monat), `kosten` (Dollar/Monat),
    `tokens_tag` (Tokens/Tag), `kosten_tag` (Dollar/Tag), Titel wie `filme_tag`, `folgen_woche`
  - WERT: Zahl oder `aus`

```
/limit standard tag 10
/limit Anna monat 50
/limit gesamt kosten 1
/limit 123456 tokens aus
/limit gesamt tokens_tag 200000
/limit empfehlung
```

Als Admin kannst du das alles auch einfach schreiben: „Zeig mir die Statistik“, „Setz Annas
Tageslimit auf 5“, „Gesamtbudget 1 Dollar“, „Sperr Bob“, „Gib Anna frei“, „Prüf die APIs“. Diese
Werkzeuge bekommt die KI nur in deinem Chat.

Ist ein Limit erreicht, antwortet der Bot ohne KI-Aufruf mit einem Hinweis. Buttons und `/downloads`
gehen weiter, die kosten nichts. Standard-Limits gelten nie für den Admin; das Gesamtbudget schon.

## Was kostet das?

Gemessen mit `gpt-4o-mini`: Eine typische Anfrage („lad mir Dune 2“) braucht 2 KI-Aufrufe mit zusammen
etwa 3.200 Tokens rein und 60 raus. Mit dem Standard **`gpt-6-luna`** ($0,10 rein, $0,01 zwischengespeichert,
$0,50 raus pro 1 Mio. Tokens) sind das **etwa 0,02–0,035 Cent**, bei 5 Anfragen am Tag **rund 0,04–0,05 $
im Monat** – ein Drittel bis die Hälfte weniger als mit `gpt-4o-mini`. Mit `/limit gesamt kosten 1` bleibt
der Bot sicher unter 1 $. Das Nachdenken steht auf `low` (`LLM_REASONING`); das reicht zum Auswählen der
Werkzeuge und hält die Denk-Tokens klein. Bei OpenAI spricht der Bot die **Responses-API** an – nur dort
dürfen Denk-Modelle wie `gpt-6-luna` nachdenken und gleichzeitig Werkzeuge nutzen. Bei OpenAI wird dabei
nichts gespeichert (`store=false`); das verschlüsselte Nachdenken geht innerhalb einer Frage mit. Andere
OpenAI-kompatible Server (`LLM_BASE_URL`, z. B. Ollama, OpenRouter) laufen weiter über Chat Completions;
mit `LLM_API` (`responses` oder `chat`) lässt sich das erzwingen. Lehnt ein Modell die Denk-Stufe ab,
schaltet der Bot von selbst um.

Warum so wenig:

- Die KI findet nur den Titel. Auswahl von Treffer, Staffeln und Qualität, Vorschläge, Top-Listen,
  die Download-Übersicht und alle Benachrichtigungen laufen im Code, ohne weitere KI-Aufrufe.
- Nach einer Aktion endet der Durchgang ohne Abschluss-Antwort der KI, das Gespräch wird geleert.
- System-Prompt und Werkzeugliste sind kurz und immer gleich, OpenAI rechnet sie als Cache günstiger ab.
- Die KI bekommt nur die Werkzeuge, die zur Nachricht passen (ausgewählt per Stichwort, ohne KI):
  „lad Dune 2“ schickt rund 690 statt 1.900 Tokens Werkzeugbeschreibung. Fehlt doch eins, holt die KI
  sich mit `more_tools` alle.

`gpt-4o-mini` und `gpt-4.1` schaltet OpenAI am 14.01.2027 ab; Nachfolger ist `gpt-6-luna`. Wer ein
altes Modell fest in `LLM_MODEL` eingetragen hat, sollte den Eintrag leeren. `/check` zeigt, ob die KI
antwortet **und** Werkzeuge aufruft – auch für die Werkzeug-KI (`RECIPE_MODEL`).

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
              │       tools.py (+ tools_media / tools_admin / tools_discover / tools_german)
              │           ├─────► arr.py ─► Radarr / Sonarr
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

Wo was liegt, steht kurz in [CLAUDE.md](CLAUDE.md): welche Datei wofür zuständig ist, die Abschnitte in
`bot.py` und eine Checkliste für neue Werkzeuge. Die Werkzeuge der KI sind nach Thema aufgeteilt:
`toolspecs.py` (was die KI sieht), `tools.py` (Kern: Rechte, Buttons, Hinzufügen/Suchen/Löschen),
`tools_media.py` (Mediathek, Duplikate, Gesehenes), `tools_admin.py` (Nutzer, Limits, Rechte, Rezepte),
`tools_discover.py` (Vorschläge, Toplisten, Geschmack), `tools_german.py` (deutsche Tonspur), `tooltypes.py`
(gemeinsame Datentypen).

## Lizenz

MIT – siehe [LICENSE](LICENSE).
