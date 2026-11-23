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
