# online-watch

GitHub Actions prüft alle 30 Minuten über die Hypixel-API, wann **mrkioko**, **Knuffiman** und **_gggaming** zuletzt online waren,
und schreibt Änderungen in einen Discord-Kanal. Läuft ohne eigenen PC.

Einrichten (einmal): Repo → Settings → Secrets and variables → Actions → New repository secret

- `DISCORD_WEBHOOK` – Webhook-URL (Discord-Kanal → Integrationen → Webhooks)
- `HYPIXEL_KEY` – Hypixel-API-Key (developer.hypixel.net)

Optional unter *Variables*: `PLAYERS` mit anderen Spielernamen, durch Komma getrennt.
Sofort testen: Actions → Online Watch → Run workflow.
