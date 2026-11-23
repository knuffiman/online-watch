# online-watch

GitHub Actions prüft jede Minute über die Hypixel-API, ob **mrkioko**, **Knuffiman** und **_gggaming** online sind. Läuft ohne eigenen PC.

**In Discord gibt es nur zwei Dinge:** die **Live-Statusnachricht** (jede Minute aktualisiert: online/offline, Server,
Login/Logout, Spielzeit, Mod-Code) und den **RATTER-Alarm** (mit Pings). Keine Join-/Leave-Nachrichten, keine Join-Pings.
Joins und Leaves stehen sekundengenau in [`sessions.csv`](sessions.csv) hier im Repo.

Pro Spieler und Minute braucht der Bot genau **eine** Hypixel-Anfrage (`/v2/status`). Login- und Logout-Zeiten misst er selbst
(höchstens eine Minute ungenau). Mit der Mod zählt ein Leave sofort (Leave-Code); ohne Mod erst nach 3 Minuten am Stück offline,
damit ein Lobbywechsel keinen falschen Leave auslöst.

Einrichten (einmal): Repo → Settings → Secrets and variables → Actions → New repository secret

- `DISCORD_WEBHOOK` – Webhook-URL (Discord-Kanal → Integrationen → Webhooks)
- `HYPIXEL_KEY` – Hypixel-API-Key (developer.hypixel.net). Ein Gilden-Key beantwortet nur Spieler **in seiner Gilde**;
  für alle anderen steht auf der Live-Karte „not visible: Target is outside this key's guild“.

Optional unter *Variables*: `PLAYERS` mit anderen Spielernamen, durch Komma getrennt.
Starten oder neu starten: Actions → Online Watch → Run workflow (wartet, bis ein laufender Watch-Run fertig ist).

**Datenbank:** [`sessions.csv`](sessions.csv) – eine Zeile pro Join und pro Leave
(Zeit in deutscher Zeit, Spieler, Ereignis, Modus beim Join, Spielzeit beim Leave).

**RATTER-Alarm mit Mod** ([`mod/`](mod), Minecraft 26.2 oder 26.1.2, Fabric): **Eine** Mod für alle Accounts – wer mit ihr
auf einem der Accounts spielt, schickt beim Beitreten auf Hypixel und beim Verlassen einen signierten Code in einen versteckten
Discord-Kanal. Sieht der Bot einen neuen Login und kommt innerhalb von 3 Minuten kein passender Join-Code, gibt es einen
RATTER-Alarm (für alle Accounts in `heartbeat.json`). Einrichten: Secret `HEARTBEAT_WEBHOOK` (Webhook des versteckten Kanals),
dann Actions → Build mod → Run workflow (Minecraft-Version wählen). Die fertige Mod kommt nur in den versteckten Kanal, nie auf GitHub.
