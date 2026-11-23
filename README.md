# online-watch

GitHub Actions prüft jede Minute über die Hypixel-API, ob **mrkioko**, **Knuffiman** und **_gggaming** online sind,
und schreibt Joins und Leaves in einen Discord-Kanal (dazu eine Live-Statusnachricht, die jede Minute aktualisiert wird). Läuft ohne eigenen PC.

Pro Spieler und Minute braucht der Bot genau **eine** Hypixel-Anfrage (`/v2/status`). Login- und Logout-Zeiten misst er selbst
(höchstens eine Minute ungenau); erst 3 Minuten am Stück offline zählen als Leave, damit ein Lobbywechsel keinen falschen Leave auslöst.

Einrichten (einmal): Repo → Settings → Secrets and variables → Actions → New repository secret

- `DISCORD_WEBHOOK` – Webhook-URL (Discord-Kanal → Integrationen → Webhooks)
- `HYPIXEL_KEY` – Hypixel-API-Key (developer.hypixel.net). Ein Gilden-Key beantwortet nur Spieler **in seiner Gilde**;
  für alle anderen steht auf der Live-Karte „not visible: Target is outside this key's guild“.

Optional unter *Variables*: `PLAYERS` mit anderen Spielernamen, durch Komma getrennt.
Sofort testen: Actions → Online Watch → Run workflow.

**Datenbank:** [`sessions.csv`](sessions.csv) – eine Zeile pro Join und pro Leave
(Zeit in deutscher Zeit, Spieler, Ereignis, Modus beim Join, Spielzeit beim Leave).

**RATTER-Alarm mit Mod** ([`mod/`](mod), Minecraft 26.2, Fabric): Die Mod schickt bei jedem Beitreten auf Hypixel
einmal einen signierten Code in einen versteckten Discord-Kanal. Sieht der Bot einen neuen Login und kommt innerhalb von
3 Minuten kein passender Code, gibt es einen RATTER-Alarm (nur für Spieler, für die eine Mod gebaut wurde – siehe `heartbeat.json`). Einrichten: Secret `HEARTBEAT_WEBHOOK` (Webhook des versteckten Kanals), dann
Actions → Build mod → Run workflow (Spielername). Die fertige Mod kommt nur in den versteckten Kanal, nie auf GitHub.
