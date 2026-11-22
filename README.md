# online-watch

GitHub Actions prüft alle 30 Minuten über die Hypixel-API, wann **Knuffiman** zuletzt online war,
und schreibt Änderungen in einen Discord-Kanal. Läuft ohne eigenen PC.

Einrichten (einmal): Repo → Settings → Secrets and variables → Actions → New repository secret

- `DISCORD_WEBHOOK` – Webhook-URL (Discord-Kanal → Integrationen → Webhooks)
- `HYPIXEL_KEY` – Hypixel-API-Key (developer.hypixel.net)

Optional unter *Variables*: `PLAYER` für einen anderen Spielernamen.
Sofort testen: Actions → Online Watch → Run workflow.
