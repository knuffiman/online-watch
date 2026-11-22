"""Prüft in Abständen, wann ein Minecraft-Account zuletzt auf Hypixel online war, und meldet
Änderungen in Discord. Läuft in GitHub Actions (.github/workflows/watch.yml); der Stand liegt in
state.json im Repo.

Secrets (Repo → Settings → Secrets and variables → Actions):
  DISCORD_WEBHOOK  - Webhook-URL des Discord-Kanals
  HYPIXEL_KEY      - Hypixel-API-Key (developer.hypixel.net)
Variable (optional): PLAYER - Spielername, Standard Knuffiman
"""
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

STATE = Path(__file__).resolve().parent / "state.json"
UA = {"User-Agent": "online-watch/1.0"}
TZ = ZoneInfo("Europe/Berlin")


def get(url, headers=None):
    req = urllib.request.Request(url, headers={**UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def post(webhook, text):
    body = json.dumps({"content": text, "username": "Online Watch"}).encode("utf-8")
    req = urllib.request.Request(webhook, data=body, method="POST",
                                 headers={**UA, "Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=20).close()


def clock(ms):
    return datetime.fromtimestamp(ms / 1000, TZ).strftime("%d.%m. %H:%M")


def span(ms):
    m = round(ms / 60000)
    return f"{m // 60} h {m % 60} min" if m >= 60 else f"{m} min"


def main():
    webhook = os.environ.get("DISCORD_WEBHOOK", "")
    key = os.environ.get("HYPIXEL_KEY", "")
    name = os.environ.get("PLAYER") or "Knuffiman"
    if not webhook or not key:
        print("DISCORD_WEBHOOK oder HYPIXEL_KEY fehlt (Repo-Secrets)")
        return 1
    state = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}
    if state.get("name") != name or not state.get("uuid"):
        state = {"name": name, "uuid": get(f"https://api.mojang.com/users/profiles/minecraft/{name}")["id"]}
    uuid = state["uuid"]

    auth = {"API-Key": key}
    session = get(f"https://api.hypixel.net/v2/status?uuid={uuid}", auth).get("session") or {}
    player = get(f"https://api.hypixel.net/v2/player?uuid={uuid}", auth).get("player") or {}
    online = bool(session.get("online"))
    where = " / ".join(x for x in (session.get("gameType"), session.get("mode")) if x)
    last_in, last_out = player.get("lastLogin"), player.get("lastLogout")

    first = "online" not in state
    msg = None
    if first:
        msg = (f"👀 Beobachte **{name}** - gerade {'online' if online else 'offline'}"
               + (f", zuletzt eingeloggt {clock(last_in)}" if last_in else "")
               + (f", zuletzt ausgeloggt {clock(last_out)}" if last_out else ""))
    elif online and not state["online"]:
        msg = f"🟢 **{name}** ist online" + (f" ({where})" if where else "") + (f" - seit {clock(last_in)}" if last_in else "")
    elif not online and (state["online"] or last_in != state.get("lastLogin")):
        msg = f"🔴 **{name}** war online"
        if last_in:
            msg += f" - Login {clock(last_in)}"
        if last_out and last_in and last_out > last_in:
            msg += f", Logout {clock(last_out)} ({span(last_out - last_in)})"
        elif last_out:
            msg += f", zuletzt ausgeloggt {clock(last_out)}"
    if msg:
        post(webhook, msg)
        print("gesendet:", msg)
    else:
        print(f"{name}: {'online' if online else 'offline'}, nichts Neues")
    state.update(online=online, lastLogin=last_in, lastLogout=last_out,
                 checked=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    # nur bei Änderungen speichern, sonst entsteht bei jedem Lauf ein Commit
    old = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}
    if {k: v for k, v in old.items() if k != "checked"} != {k: v for k, v in state.items() if k != "checked"}:
        STATE.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code} bei {e.url.split('?')[0]}")
        sys.exit(2)
