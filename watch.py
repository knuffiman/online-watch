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
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

DEFAULT = "mrkioko, Knuffiman, _gggaming"
STATE = Path(__file__).resolve().parent / "state.json"
# the database: one line per join and per leave, nothing else
LOG = Path(__file__).resolve().parent / "sessions.csv"
EVENTS = []
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
        # overview cards in the same colours as the join / leave cards: green online, red offline
        if overview and "online" in old:
            title = f"🟢 {shown} is online" if online else f"🔴 {shown} is offline"
            color = GREEN if online else RED
        else:
            title, color = f"👀 Now watching {shown}", BLUE
        msg = embed(shown, uuid, title, color,
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

    if "online" in old:
        now = int(time.time() * 1000)
        was = old["online"]
        relogged = online and was and last_in and old.get("lastLogin") and last_in != old["lastLogin"]
        # a whole session between two checks (after an earlier one ended, if they were online)
        between = not online and last_in and old.get("lastLogin") and last_in != old["lastLogin"]
        if (not online and was) or relogged or between:
            if between and was:
                EVENTS.append((last_in - 1, shown, "leave", "", ""))
            if between:
                EVENTS.append((last_in, shown, "join", "", ""))
            end = last_out if last_out and last_in and last_out >= last_in else now
            EVENTS.append((end, shown, "leave", "", played or ""))
        if (online and not was) or relogged:
            EVENTS.append((last_in or now, shown, "join", where, ""))

    state = {"uuid": uuid, "online": online, "where": where if online else None,
             "lastLogin": last_in, "lastLogout": last_out}
    return state, msg, ping


def write_log():
    """Appends the joins and leaves of this run to sessions.csv, oldest first."""
    if not EVENTS:
        return
    from zoneinfo import ZoneInfo
    tz = ZoneInfo("Europe/Berlin")
    new = not LOG.exists()
    with LOG.open("a", encoding="utf-8", newline="") as f:
        if new:
            f.write("time,player,event,mode,played\n")
        for ms, name, event, mode, played in sorted(EVENTS):
            when = datetime.fromtimestamp(ms / 1000, tz).strftime("%Y-%m-%d %H:%M")
            f.write(f"{when},{name},{event},{mode},{played}\n")
    EVENTS.clear()


def main():
    EVENTS.clear()
    webhook = os.environ.get("DISCORD_WEBHOOK", "")
    key = os.environ.get("HYPIXEL_KEY", "")
    overview = os.environ.get("OVERVIEW", "").lower() == "true"
    names = [n.strip() for n in (os.environ.get("PLAYERS") or DEFAULT).split(",") if n.strip()]
    if not webhook or not key:
        print("DISCORD_WEBHOOK oder HYPIXEL_KEY fehlt (Repo-Secrets)")
        return 1
    saved = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}
    # a copy: comparing the new state with a dict changed in place would never see a change
    players = json.loads(json.dumps(saved.get("players", {})))
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
        # "all" or names separated by commas: the real card with live data for each, as if the
        # player had just joined (offline: "was online"), each with its ping under it; the saved
        # state is left alone
        wanted = names if test.lower() == "all" else [t.strip() for t in test.split(",") if t.strip()]
        joined = []
        for t in wanted:
            name = next((n for n in names if n.lower() == t.lower()), t)
            uid = ids.get(name.lower())
            if not uid:
                print(f"TEST_PING: no Discord ID for {name}")
                continue
            _, card, _ = check(name, {"uuid": players.get(name, {}).get("uuid"), "online": False}, key)
            if card is None:  # offline and login times hidden in the API: the status card instead
                _, card, _ = check(name, {"uuid": players.get(name, {}).get("uuid"), "online": False}, key, True)
            card["footer"] = {"text": f"{card['footer']['text']} • test"}
            joined.append((card, uid))
        post(webhook, [], joined)
        print(f"test pings sent: {len(joined)}")
        return 0 if joined else 1
    embeds, joined, errors = [], [], 0
    # Joins and leaves go out at once; anything else (a mode change) waits and goes out at most
    # once an hour, only its newest card per player.
    pending = dict(saved.get("pending", {}))
    last_quiet = saved.get("lastQuiet", 0)
    now = int(time.time())
    for name in names:
        old = players.get(name, {})
        try:
            players[name], msg, ping = check(name, old, key, overview)
        except urllib.error.HTTPError as e:
            print(f"{name}: HTTP {e.code} bei {e.url.split('?')[0]}")
            errors += 1
            continue
        if msg and not overview and msg["title"].startswith("🔁"):
            pending[name] = msg
            print(f"{name}: {msg['title']} (waits for the hourly message)")
        elif msg:
            pending.pop(name, None)
            # only when the player has just come online - not again while they stay online
            if ping and ids.get(name.lower()):
                joined.append((msg, ids[name.lower()]))
            else:
                embeds.append(msg)
            print(f"{name}: {msg['title']}")
        else:
            print(f"{name}: {'online' if players[name]['online'] else 'offline'}, nichts Neues")
    if pending and now - last_quiet >= 3600:
        embeds += [pending[n] for n in names if n in pending]
        pending = {}
        last_quiet = now
    if embeds or joined:
        post(webhook, embeds, joined)
    write_log()

    new = {"players": {n: players[n] for n in names if n in players}}
    if pending:
        new["pending"] = pending
    if last_quiet:
        new["lastQuiet"] = last_quiet
    if new != saved:
        STATE.write_text(json.dumps(new, indent=2) + "\n", encoding="utf-8")
    return 2 if errors == len(names) else 0


def overview_all():
    """Test: the overview card of every player, without touching the saved state."""
    key, webhook = os.environ.get("HYPIXEL_KEY", ""), os.environ.get("DISCORD_WEBHOOK", "")
    names = [n.strip() for n in (os.environ.get("PLAYERS") or DEFAULT).split(",") if n.strip()]
    saved = json.loads(STATE.read_text(encoding="utf-8")).get("players", {}) if STATE.exists() else {}
    cards = []
    for name in names:
        try:
            _, card, _ = check(name, {"online": False, **saved.get(name, {})}, key, True)
            cards.append(card)
        except Exception as e:
            print(f"{name}: overview failed: {e!r}")
    EVENTS.clear()
    if cards:
        post(webhook, cards)
        print(f"test overview sent: {len(cards)}")


def git(*args):
    return subprocess.run(["git", *args], cwd=STATE.parent, capture_output=True, text=True)


def blobs():
    """Ids of the code files at HEAD: when they change, the loop restarts with the new code."""
    return git("rev-parse", "HEAD:watch.py", "HEAD:.github/workflows/watch.yml").stdout


def save():
    """Commits and pushes state.json / sessions.csv when they changed."""
    if not git("status", "--porcelain", "state.json", "sessions.csv").stdout.strip():
        return
    git("add", *[f for f in ("state.json", "sessions.csv") if (STATE.parent / f).exists()])
    git("-c", "user.name=online-watch", "-c", "user.email=online-watch@users.noreply.github.com",
        "commit", "-q", "-m", "state")
    for attempt in range(4):
        if git("pull", "-q", "--rebase").returncode != 0:
            git("rebase", "--abort")
        if git("push", "-q").returncode == 0:
            return
        time.sleep(5 * (attempt + 1))
    print("push failed, trying again with the next change")


def dispatch():
    """Starts the next loop run; it waits in the concurrency queue until this one has ended."""
    repo, token = os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GITHUB_TOKEN")
    if not repo or not token:
        return
    api = os.environ.get("GITHUB_API_URL", "https://api.github.com")
    body = {"ref": os.environ.get("GITHUB_REF_NAME", "main"), "inputs": {"loop": "true", "overview": "false"}}
    req = urllib.request.Request(f"{api}/repos/{repo}/actions/workflows/watch.yml/dispatches",
                                 data=json.dumps(body).encode(), method="POST",
                                 headers={**UA, "Authorization": f"Bearer {token}",
                                          "Accept": "application/vnd.github+json"})
    for attempt in range(4):
        try:
            urllib.request.urlopen(req, timeout=20).close()
            print("next run started")
            return
        except Exception as e:  # the hourly schedule restarts it if this keeps failing
            print(f"dispatch failed: {e}")
            time.sleep(10 * (attempt + 1))


def loop(minutes, every):
    """GitHub drops most scheduled runs, so one run keeps checking by itself for almost the
    6 hours a job may take, then starts the next run."""
    end = time.time() + minutes * 60
    code, synced = blobs(), time.time()
    # test: every OVERVIEW_EVERY minutes the card of all players (0 or empty = off)
    test_every = float(os.environ.get("OVERVIEW_EVERY") or 0) * 60
    last_test = 0
    while time.time() < end:
        start = time.time()
        try:
            if main() == 1:
                return 1  # secrets missing: no point in going on
        except Exception as e:  # network trouble etc.: try again next round
            print(f"check failed: {e!r}")
        save()
        if test_every and time.time() - last_test >= test_every:
            last_test = time.time()
            try:
                overview_all()
            except Exception as e:
                print(f"test overview failed: {e!r}")
        if time.time() - synced >= 600:  # new code pushed? restart with it
            synced = time.time()
            git("pull", "-q", "--rebase")
            if blobs() != code:
                print("code changed, restarting")
                break
        time.sleep(max(1, every - (time.time() - start)))
    dispatch()
    return 0


if __name__ == "__main__":
    if os.environ.get("LOOP", "").lower() == "true":
        sys.exit(loop(int(os.environ.get("LOOP_MINUTES") or 345), int(os.environ.get("EVERY") or 60)))
    sys.exit(main())
