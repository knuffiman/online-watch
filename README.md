# online-watch

GitHub Actions prüft jede Minute über die Hypixel-API, wann **mrkioko**, **Knuffiman** und **_gggaming** zuletzt online waren,
und schreibt Änderungen in einen Discord-Kanal (dazu eine angepinnte Live-Statusnachricht, die jede Minute aktualisiert wird). Läuft ohne eigenen PC.

Einrichten (einmal): Repo → Settings → Secrets and variables → Actions → New repository secret

- `DISCORD_WEBHOOK` – Webhook-URL (Discord-Kanal → Integrationen → Webhooks)
- `HYPIXEL_KEY` – Hypixel-API-Key (developer.hypixel.net)

Optional unter *Variables*: `PLAYERS` mit anderen Spielernamen, durch Komma getrennt.
Sofort testen: Actions → Online Watch → Run workflow.

**Datenbank:** [`sessions.csv`](sessions.csv) – eine Zeile pro Join und pro Leave
(Zeit in deutscher Zeit, Spieler, Ereignis, Modus beim Join, Spielzeit beim Leave).

**RATTER-Alarm mit Mod** ([`mod/`](mod), Minecraft 26.2, Fabric): Die Mod schickt bei jedem Beitreten auf Hypixel
einmal einen signierten Code in einen versteckten Discord-Kanal. Sieht der Bot einen neuen Login ohne passenden Code,
kommt ein RATTER-Alarm. Einrichten: Secret `HEARTBEAT_WEBHOOK` (Webhook des versteckten Kanals), dann
Actions → Build mod → Run workflow (Spielername). Die fertige Mod kommt nur in den versteckten Kanal, nie auf GitHub.
