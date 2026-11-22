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


def send(webhook, payload):
    payload = {"username": "Online Watch", "allowed_mentions": {"parse": []}, **payload}
    req = urllib.request.Request(webhook, data=json.dumps(payload).encode("utf-8"), method="POST",
                                 headers={**UA, "Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=20).close()


def post(webhook, embeds, joined=()):
    """The cards first (at most 10 per message); a join card goes on its own, followed by a
    message with nothing but the player's ping right under it - embeds themselves never ping."""
    for i in range(0, len(embeds), 10):
        send(webhook, {"embeds": embeds[i:i + 10]})
    for card, uid in joined:
        send(webhook, {"embeds": [card]})
        send(webhook, {"content": f"<@{uid}>", "allowed_mentions": {"parse": [], "users": [uid]}})


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
    ping = False
    if overview or "online" not in old:
        msg = embed(shown, uuid, f"📋 {shown}" if overview and "online" in old else f"👀 Now watching {shown}", BLUE,
                    [("Status", "🟢 online" if online else "⚫ offline"), ("Mode", where if online else None),
                     ("Last login", login), ("Last logout", logout)])
    elif online and not old["online"]:
        ping = True
        msg = embed(shown, uuid, f"🟢 {shown} is online", GREEN,
                    [("Mode", where or "?"), ("Since", login)])
    elif not online and (old["online"] or last_in != old.get("lastLogin")):
        msg = embed(shown, uuid, f"🔴 {shown} was online", RED,
                    [("Login", login), ("Logout", logout), ("Played", played)])
    elif online and where and old.get("where") and where != old["where"]:
        msg = embed(shown, uuid, f"🔁 {shown} is now playing {where}", GREY, [("Online since", login)])

    state = {"uuid": uuid, "online": online, "where": where if online else None,
             "lastLogin": last_in, "lastLogout": last_out}
    return state, msg, ping


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

    # Discord user IDs to ping, e.g. "mrkioko=123456789012345678, Knuffiman=..."
    ids = {}
    for pair in (os.environ.get("DISCORD_IDS") or "").split(","):
        if "=" in pair:
            k, v = pair.split("=", 1)
            v = "".join(c for c in v if c.isdigit())
            if v:
                ids[k.strip().lower()] = v
    test = (os.environ.get("TEST_PING") or "").strip()
    if test:
        uid = ids.get(test.lower())
        if not uid:
            print(f"TEST_PING: no Discord ID for {test}")
            return 1
        # the real card with live data, as if the player had just joined (offline: "was online");
        # the saved state is left alone
        name = next((n for n in names if n.lower() == test.lower()), test)
        _, card, _ = check(name, {"uuid": players.get(name, {}).get("uuid"), "online": False}, key)
        card["footer"] = {"text": f"{card['footer']['text']} • test"}
        post(webhook, [], [(card, uid)])
        print(f"test ping sent to {test}")
        return 0
    embeds, joined, errors = [], [], 0
    for name in names:
        old = players.get(name, {})
        try:
            players[name], msg, ping = check(name, old, key, overview)
        except urllib.error.HTTPError as e:
            print(f"{name}: HTTP {e.code} bei {e.url.split('?')[0]}")
            errors += 1
            continue
        if msg:
            # only when the player has just come online - not again while they stay online
            if ping and ids.get(name.lower()):
                joined.append((msg, ids[name.lower()]))
            else:
                embeds.append(msg)
            print(f"{name}: {msg['title']}")
        else:
            print(f"{name}: {'online' if players[name]['online'] else 'offline'}, nichts Neues")
    if embeds or joined:
        post(webhook, embeds, joined)

    new = {"players": {n: players[n] for n in names if n in players}}
    if new != saved:
        STATE.write_text(json.dumps(new, indent=2) + "\n", encoding="utf-8")
    return 2 if errors == len(names) else 0


if __name__ == "__main__":
    sys.exit(main())
