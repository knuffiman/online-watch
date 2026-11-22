"""Prüft in Abständen, wann Minecraft-Accounts zuletzt auf Hypixel online waren, und meldet
Änderungen in Discord (als Embed mit Kopf, Status und Zeiten). Läuft in GitHub Actions
(.github/workflows/watch.yml); der Stand liegt in state.json im Repo.

Secrets (Repo → Settings → Secrets and variables → Actions):
  DISCORD_WEBHOOK  - Webhook-URL des Discord-Kanals
  HYPIXEL_KEY      - Hypixel-API-Key (developer.hypixel.net)
Variable (optional): PLAYERS - Spielernamen, durch Komma getrennt (Standard siehe DEFAULT)
"""
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

DEFAULT = "mrkioko, Knuffiman, _gggaming"
STATE = Path(__file__).resolve().parent / "state.json"
UA = {"User-Agent": "online-watch/1.1"}
GREEN, RED, GREY, BLUE = 0x43B581, 0xF04747, 0x99AAB5, 0x5865F2


def get(url, headers=None):
    req = urllib.request.Request(url, headers={**UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def post(webhook, embeds):
    for i in range(0, len(embeds), 10):  # Discord: höchstens 10 Embeds pro Nachricht
        body = json.dumps({"username": "Online Watch", "embeds": embeds[i:i + 10]}).encode("utf-8")
        req = urllib.request.Request(webhook, data=body, method="POST",
                                     headers={**UA, "Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=20).close()


def ts(ms, style="f"):
    """Discord-Zeitstempel: zeigt sich bei jedem in seiner eigenen Zeitzone."""
    return f"<t:{ms // 1000}:{style}>"


def span(ms):
    m = round(ms / 60000)
    return f"{m // 60} h {m % 60} min" if m >= 60 else f"{m} min"


def embed(name, uuid, title, color, fields):
    return {
        "title": title,
        "color": color,
        "thumbnail": {"url": f"https://mc-heads.net/avatar/{uuid}/64"},
        "fields": [{"name": k, "value": v, "inline": True} for k, v in fields if v],
        "footer": {"text": name},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def check(name, old, key, overview=False):
    """Neuer Stand eines Spielers und das Embed dazu (None, wenn sich nichts getan hat)."""
    uuid = old.get("uuid") or get(f"https://api.mojang.com/users/profiles/minecraft/{name}")["id"]
    auth = {"API-Key": key}
    session = get(f"https://api.hypixel.net/v2/status?uuid={uuid}", auth).get("session") or {}
    player = get(f"https://api.hypixel.net/v2/player?uuid={uuid}", auth).get("player") or {}
    online = bool(session.get("online"))
    where = " / ".join(x.replace("_", " ").title() for x in (session.get("gameType"), session.get("mode")) if x)
    last_in, last_out = player.get("lastLogin"), player.get("lastLogout")
    shown = player.get("displayname") or name

    login = ts(last_in) + " (" + ts(last_in, "R") + ")" if last_in else None
    logout = ts(last_out) + " (" + ts(last_out, "R") + ")" if last_out else None
    played = span(last_out - last_in) if last_in and last_out and last_out > last_in else None

    msg = None
    if overview or "online" not in old:
        msg = embed(shown, uuid, f"📋 {shown}" if overview and "online" in old else f"👀 {shown} wird beobachtet", BLUE,
                    [("Status", "🟢 online" if online else "⚫ offline"), ("Modus", where if online else None),
                     ("Letzter Login", login), ("Letzter Logout", logout)])
    elif online and not old["online"]:
        msg = embed(shown, uuid, f"🟢 {shown} ist online", GREEN,
                    [("Modus", where or "?"), ("Seit", login)])
    elif not online and (old["online"] or last_in != old.get("lastLogin")):
        msg = embed(shown, uuid, f"🔴 {shown} war online", RED,
                    [("Login", login), ("Logout", logout), ("Gespielt", played)])
    elif online and where and old.get("where") and where != old["where"]:
        msg = embed(shown, uuid, f"🔁 {shown} spielt jetzt {where}", GREY, [("Online seit", login)])

    state = {"uuid": uuid, "online": online, "where": where if online else None,
             "lastLogin": last_in, "lastLogout": last_out}
    return state, msg


def main():
    webhook = os.environ.get("DISCORD_WEBHOOK", "")
    key = os.environ.get("HYPIXEL_KEY", "")
    overview = os.environ.get("OVERVIEW", "").lower() == "true"
    names = [n.strip() for n in (os.environ.get("PLAYERS") or DEFAULT).split(",") if n.strip()]
    if not webhook or not key:
        print("DISCORD_WEBHOOK oder HYPIXEL_KEY fehlt (Repo-Secrets)")
        return 1
    saved = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}
    players = saved.get("players", {})
    if "name" in saved:  # Stand aus der Version für einen Spieler übernehmen
        players[saved["name"]] = {k: v for k, v in saved.items() if k not in ("name", "checked")}

    embeds, errors = [], 0
    for name in names:
        old = players.get(name, {})
        try:
            players[name], msg = check(name, old, key, overview)
        except urllib.error.HTTPError as e:
            print(f"{name}: HTTP {e.code} bei {e.url.split('?')[0]}")
            errors += 1
            continue
        if msg:
            embeds.append(msg)
            print(f"{name}: {msg['title']}")
        else:
            print(f"{name}: {'online' if players[name]['online'] else 'offline'}, nichts Neues")
    if embeds:
        post(webhook, embeds)

    new = {"players": {n: players[n] for n in names if n in players}}
    if new != saved:
        STATE.write_text(json.dumps(new, indent=2) + "\n", encoding="utf-8")
    return 2 if errors == len(names) else 0


if __name__ == "__main__":
    sys.exit(main())
