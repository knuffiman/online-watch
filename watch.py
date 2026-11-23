"""Prüft jede Minute, ob Minecraft-Accounts auf Hypixel online sind, und meldet Joins und Leaves
in Discord (als Embed mit Kopf, Status und Zeiten). Läuft in GitHub Actions
(.github/workflows/watch.yml); der Stand liegt in state.json im Repo.

Pro Spieler und Runde genau eine Hypixel-Anfrage: der Online-Status (/v2/status). Login- und
Logout-Zeiten misst der Bot selbst (wann er jemanden online / wieder offline sieht). Bei jedem neuen
Login wartet er auf den Join-Code der Online-Watch-Mod (heartbeat.py); kommt keiner: RATTER-Alarm.

Secrets (Repo → Settings → Secrets and variables → Actions):
  DISCORD_WEBHOOK  - Webhook-URL des Discord-Kanals
  HYPIXEL_KEY      - Hypixel-API-Key (developer.hypixel.net; ein Gilden-Key gilt nur für die Gilde)
  HEARTBEAT_WEBHOOK - Webhook des versteckten Kanals mit den Join-Codes der Mod
Variable (optional): PLAYERS - Spielernamen, durch Komma getrennt (Standard siehe DEFAULT)
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

import heartbeat
from datetime import datetime, timezone
from pathlib import Path

DEFAULT = "mrkioko, Knuffiman, _gggaming"
STATE = Path(__file__).resolve().parent / "state.json"
# the database: one line per join and per leave, nothing else
LOG = Path(__file__).resolve().parent / "sessions.csv"
EVENTS = []
LIVE = {}  # the current status card of each player, for the pinned live message
UA = {"User-Agent": "online-watch/1.1"}
GREEN, RED, GREY, BLUE = 0x43B581, 0xF04747, 0x99AAB5, 0x5865F2


PAUSE = [0]  # Hypixel said "too many requests": no Hypixel requests until then (time.time())
REQ = {}  # Hypixel requests per hour ("<hour>": n), kept in state.json - only to see the usage
# leaving needs this many rounds in a row offline: a lobby or server switch can show a player
# offline for a moment
OFF_ROUNDS = 3


def count(n=1):
    hour = str(int(time.time()) // 3600)
    REQ[hour] = REQ.get(hour, 0) + n


def today():
    """Hypixel requests in the last 24 hours."""
    now = int(time.time()) // 3600
    return sum(v for k, v in REQ.items() if now - int(k) < 24)


class Throttled(Exception):
    pass


class Denied(Exception):
    """Hypixel refused this player (e.g. a guild key and a player outside the guild)."""


def get(url, headers=None):
    if "hypixel.net" in url and time.time() < PAUSE[0]:
        raise Throttled(f"paused for {PAUSE[0] - time.time():.0f} s more")
    req = urllib.request.Request(url, headers={**UA, **(headers or {})})
    if "hypixel.net" in url:
        count()
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 429 and "hypixel.net" in url:
            h = e.headers
            try:
                body = e.read().decode("utf-8", "replace")[:200]
            except Exception:
                body = ""
            wait = int(h.get("Retry-After") or h.get("RateLimit-Reset") or 60)
            PAUSE[0] = time.time() + max(30, min(wait, 3600))
            print(f"Hypixel HTTP 429 ({url.split('?')[0].split('/v2/')[-1]}): limit {h.get('RateLimit-Limit')}, "
                  f"left {h.get('RateLimit-Remaining')}, reset in {h.get('RateLimit-Reset')} s, "
                  f"retry-after {h.get('Retry-After')}; {body} -> pausing {PAUSE[0] - time.time():.0f} s")
            raise Throttled("429") from None
        if e.code == 403 and "hypixel.net" in url:
            try:
                cause = json.loads(e.read().decode("utf-8", "replace")).get("cause") or "forbidden"
            except Exception:
                cause = "forbidden"
            raise Denied(cause) from None
        raise


def send(webhook, payload, method="POST", url=None):
    payload = {"username": "Online Watch", "allowed_mentions": {"parse": []}, **payload}
    if method != "POST":
        payload.pop("username")  # an edited message keeps its name
    req = urllib.request.Request(url or webhook, data=json.dumps(payload).encode("utf-8"), method=method,
                                 headers={**UA, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        body = r.read()
    return json.loads(body) if body else None


def live(webhook, msg_id, bottom=False):
    """The live message, always the last one in the channel: edited in place every round; when
    other cards were sent (bottom), the old one is deleted and a new one sent below them.
    If it is gone (deleted by hand), a new one is sent; its id is kept in state.json."""
    if not LIVE:
        return msg_id
    now = int(time.time())
    payload = {"content": f"📡 **Live status** – updated every minute (last: <t:{now}:T>, <t:{now}:R>)",
               "embeds": list(LIVE.values())[:10]}
    base = webhook.split("?")[0]
    if msg_id and bottom:
        try:
            req = urllib.request.Request(f"{base}/messages/{msg_id}", method="DELETE", headers=UA)
            urllib.request.urlopen(req, timeout=20).close()
        except urllib.error.HTTPError as e:
            if e.code != 404:
                print(f"could not delete the old live message: HTTP {e.code}")
        msg_id = None
    if msg_id:
        try:
            send(webhook, payload, "PATCH", f"{base}/messages/{msg_id}")
            return msg_id
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
            print("live message gone, sending a new one")
    sep = "&" if "?" in webhook else "?"
    msg = send(webhook, payload, url=f"{webhook}{sep}wait=true")
    print(f"live message sent: {msg['id']}")
    return msg["id"]


def post(webhook, embeds, joined=()):
    """The cards first (at most 10 per message); a join card goes on its own, followed by a
    message with nothing but the player's ping right under it - embeds themselves never ping.
    joined: (card, Discord user id or None, player); returns {player: id of their join message},
    so that the card can be edited later (the mod's code verified after it was sent)."""
    for i in range(0, len(embeds), 10):
        send(webhook, {"embeds": embeds[i:i + 10]})
    sent = {}
    sep = "&" if "?" in webhook else "?"
    for card, uid, *who in joined:
        msg = send(webhook, {"embeds": [card]}, url=f"{webhook}{sep}wait=true")
        if who and msg:
            sent[who[0]] = msg.get("id")
        if uid:
            send(webhook, {"content": f"<@{uid}>", "allowed_mentions": {"parse": [], "users": [uid]}})
    return sent


MOD_FIELD = "Mod code"


def mod_status(st):
    """The line about the mod's join code for the current login, or None (no mod / offline)."""
    if st.get("hbWait"):
        return f"⏳ waiting for the code (max {int(os.environ.get('HB_WAIT') or 180) // 60} min)"
    if st.get("hbFail") and st.get("hbFail") == st.get("lastLogin"):
        return "❌ no code – RATTER ALARM"
    t, login = st.get("hbLast"), st.get("lastLogin")
    if t and login and abs(t - login) <= 180000:
        return f"✅ verified ({ts(t, 'T')})"
    return None


def set_mod_field(card, text):
    """Puts the mod-code line on a card (replacing an old one), just above "Checked"."""
    fields = [f for f in card["fields"] if f["name"] != MOD_FIELD]
    at = next((i for i, f in enumerate(fields) if f["name"] == "Checked"), len(fields))
    fields.insert(at, {"name": MOD_FIELD, "value": text, "inline": False})
    card["fields"] = fields
    return card


def edit_card(webhook, msg_id, card):
    """Edits a message the bot sent before (the join card, once the code is in)."""
    try:
        send(webhook, {"embeds": [card]}, "PATCH", f"{webhook.split('?')[0]}/messages/{msg_id}")
    except Exception as e:
        print(f"could not edit the join card: {e!r}")


def ts(ms, style="f"):
    """Discord-Zeitstempel: zeigt sich bei jedem in seiner eigenen Zeitzone."""
    return f"<t:{ms // 1000}:{style}>"


def span(ms):
    m = round(ms / 60000)
    return f"{m // 60} h {m % 60} min" if m >= 60 else f"{m} min"


def logged(shown):
    """Playtime already in sessions.csv for a player (to start the total when it is new)."""
    total = 0
    if LOG.exists():
        for line in LOG.read_text(encoding="utf-8").splitlines()[1:]:
            parts = line.split(",")
            if len(parts) >= 5 and parts[1].lower() == shown.lower() and parts[4]:
                p = parts[4].replace(" min", "").split(" h ")
                total += (int(p[0]) * 60 + int(p[1]) if len(p) == 2 else int(p[0])) * 60000
    return total


def playtime(old, last_in, last_out, shown):
    """Total playtime since tracking began, kept in state.json: each finished session
    (logout - login, as the bot saw them) counts once."""
    if "total" in old and old.get("v") == 2:
        total, counted = old["total"], old.get("counted")
    else:  # first time: what sessions.csv already has
        total = logged(shown)
        counted = last_in if last_in == old.get("lastLogin") else None
    if last_in and last_out and last_out >= last_in and last_in != counted:
        total += last_out - last_in
        counted = last_in
    return total, counted


# Hypixel only gives the game, mode and map, no server number; Skyblock modes get readable names
SKYBLOCK = {"dynamic": "Private Island", "hub": "Hub", "farming_1": "The Barn", "garden": "Garden",
            "foraging_1": "The Park", "foraging_2": "Galatea", "mining_1": "Gold Mine",
            "mining_2": "Deep Caverns", "mining_3": "Dwarven Mines", "crystal_hollows": "Crystal Hollows",
            "mineshaft": "Glacite Mineshaft", "combat_1": "Spider's Den", "combat_3": "The End",
            "crimson_isle": "Crimson Isle", "fishing_1": "Backwater Bayou", "dungeon_hub": "Dungeon Hub",
            "dungeon": "Dungeons", "kuudra": "Kuudra", "rift": "The Rift", "winter": "Jerry's Workshop",
            "dark_auction": "Dark Auction"}


def server(session):
    """The server a player is on, as far as the API says: game, area and map."""
    game, mode, where_map = session.get("gameType"), session.get("mode"), session.get("map")
    if not game:
        return None
    parts = [game.replace("_", " ").title()]
    if mode:
        if game == "SKYBLOCK":
            parts.append(SKYBLOCK.get(mode.lower(), mode.replace("_", " ").title()))
        elif mode.upper() == "LOBBY":
            parts.append("Lobby")
        else:
            parts.append(mode.replace("_", " ").title())
    if where_map and where_map not in parts:
        parts.append(where_map)
    return " – ".join(parts)


def has_mod(name):
    return bool(os.environ.get("HEARTBEAT_WEBHOOK")) and any(k.lower() == name.lower() for k in heartbeat.ids())


def mod_leave(name, uuid, old, last_in):
    """The time of a fresh leave code from the mod for this session (ms), else None.
    Only asked when Hypixel shows a player offline who was online: one read of the hidden channel."""
    if os.environ.get("LOOP", "").lower() != "true" or not has_mod(name):
        return None
    try:
        codes = heartbeat.read_codes(name, uuid)
    except Exception as e:  # cannot read it: the usual OFF_ROUNDS rounds decide
        print(f"{name}: codes not readable: {e!r}")
        return None
    t, joined = codes.get("leave"), codes.get("join")
    # newer than the last leave used, not older than this session (a little slack: the bot sees a
    # login up to a minute late), and the latest event (no join after it)
    if (t and t > (old.get("hbLeave") or 0) and (not last_in or t >= last_in - 180000)
            and (not joined or t >= joined)):
        return t
    return None


def code_check(name, old, st, now_ms):
    """RATTER alarm: a new login on Hypixel needs a join code from the Online Watch mod within
    HB_WAIT seconds (only for players in heartbeat.json). Keeps hbWait / hbLast in the state and
    returns the alarm card or None."""
    if not has_mod(name):
        return None
    for k in ("hbWait", "hbLast", "hbFail"):
        if old.get(k):
            st[k] = old[k]
    login = st.get("lastLogin")
    if st["online"] and (not old.get("online") or (login and old.get("lastLogin") and login != old["lastLogin"])):
        st["hbWait"] = {"since": now_ms, "login": login}
    wait = st.get("hbWait")
    if not wait:
        return None
    ref = wait.get("login") or wait["since"]
    try:
        t = heartbeat.read_code(name, st["uuid"])
    except Exception as e:  # cannot read the code: never an alarm for that, try again next round
        print(f"{name}: join code not readable: {e!r}")
        t = None
        if now_ms - wait["since"] < 15 * 60000:
            return None
    if t and t > (st.get("hbLast") or 0) and abs(t - ref) <= 180000:
        st["hbLast"] = t
        st.pop("hbWait", None)
        print(f"{name}: join code OK")
        return None
    if now_ms - wait["since"] < int(os.environ.get("HB_WAIT") or 180) * 1000:
        return None
    st.pop("hbWait", None)
    st["hbFail"] = st.get("lastLogin")  # this login had no code (shown on the cards)
    card = embed(name, st["uuid"], f"🚨 RATTER ALARM – {name}", RED, [
        ("Login", ts(ref) + " (" + ts(ref, "R") + ")"),
        ("Server", st.get("server") or ("online" if st["online"] else "already offline again"))])
    card["description"] = (f"**{name} joined Hypixel, but the Online Watch mod sent no code.**\n"
                           "If that was not you on your own PC with the mod: change your Microsoft "
                           "password now, log out all devices (account.microsoft.com) and check your mods.")
    return card


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
    """New state of a player and the card for it (None when nothing happened).
    One Hypixel request: the status. Login and logout times are when the bot saw the player come
    online and go offline (at most a minute late), so the player endpoint is never needed."""
    uuid, shown = old.get("uuid"), old.get("shown")
    if not uuid:
        profile = get(f"https://api.mojang.com/users/profiles/minecraft/{name}")
        uuid, shown = profile["id"], profile.get("name")
    shown = shown or name
    session = get(f"https://api.hypixel.net/v2/status?uuid={uuid}", {"API-Key": key}).get("session") or {}
    pulled = int(time.time() * 1000)  # when the data was fetched from Hypixel, shown on every card
    online = bool(session.get("online"))
    where = " / ".join(x.replace("_", " ").title() for x in (session.get("gameType"), session.get("mode")) if x)
    first = "online" not in old
    was = bool(old.get("online"))
    last_in, last_out = old.get("lastLogin"), old.get("lastLogout")

    # Switching lobbies or servers can show a player offline for a moment: only OFF_ROUNDS rounds
    # in a row offline count as leaving (the logout is the first of them). Until then they stay
    # online - no leave card, and no second join ping afterwards.
    # A leave code from the mod (sent when disconnecting) ends the session at once, at its time.
    miss, off_at = 0, None
    left = mod_leave(name, uuid, old, last_in) if not online and was and not overview else None
    if left:
        last_out = left
        print(f"{name}: leave code from the mod, left at once")
    elif not online and was and not overview:
        miss = old.get("miss", 0) + 1
        off_at = old.get("offAt") or pulled
        if miss < OFF_ROUNDS:
            online, where = True, old.get("where") or ""
            print(f"{name}: offline for a moment ({miss}/{OFF_ROUNDS}), still counted as online")
        else:
            last_out, miss, off_at = off_at, 0, None
    elif online and old.get("miss"):
        print(f"{name}: back online after {old['miss']} round(s), same session")
    if online and (first or not was):
        last_in = pulled  # a new login, seen now
    on = (server(session) or old.get("server")) if online else None  # kept during a lobby switch

    login = ts(last_in) + " (" + ts(last_in, "R") + ")" if last_in else None
    logout = ts(last_out) + " (" + ts(last_out, "R") + ")" if last_out else None
    played = span(last_out - last_in) if last_in and last_out and last_out > last_in else None

    msg = None
    ping = False
    if overview or first:
        # overview cards in the same colours as the join / leave cards: green online, red offline
        if overview and not first:
            title = f"🟢 {shown} is online" if online else f"🔴 {shown} is offline"
            color = GREEN if online else RED
        else:
            title, color = f"👀 Now watching {shown}", BLUE
        msg = embed(shown, uuid, title, color,
                    [("Status", "🟢 online" if online else "⚫ offline"), ("Mode", where if online else None),
                     ("Online since" if online else "Last login", login),
                     ("Last logout", None if online else logout)])
    elif online and not was:
        # a ping once per login
        ping = last_in != old.get("pinged")
        msg = embed(shown, uuid, f"🟢 {shown} is online", GREEN, [("Mode", where or "?"), ("Since", login)])
    elif not online and was:
        msg = embed(shown, uuid, f"🔴 {shown} was online", RED,
                    [("Login", login), ("Logout", logout), ("Played", played)])
    # a server or mode change sends nothing: it only shows on the live card

    if not first and not overview:
        if online and not was:
            EVENTS.append((last_in, shown, "join", where, ""))
        elif not online and was:
            EVENTS.append((last_out, shown, "leave", "", played or ""))

    total, counted = playtime(old, last_in, last_out, shown)
    status = embed(shown, uuid, f"🟢 {shown} is online" if online else f"🔴 {shown} is offline",
                   GREEN if online else RED,
                   [("Status", "🟢 online" if online else "⚫ offline"), ("Server", on),
                    ("Online since" if online else "Last login", login),
                    ("Last logout", None if online else logout)])
    # the live card: the total including the session going on right now
    running = pulled - last_in if online and last_in and last_in != counted else 0
    status["fields"].append({"name": "Total played", "value": span(total + running) + " since tracking began",
                             "inline": False})
    LIVE[name] = status
    for card in (msg, status):
        if card:
            card["fields"].append({"name": "Checked", "value": ts(pulled, "T") + " (" + ts(pulled, "R") + ")",
                                   "inline": False})
    state = {"uuid": uuid, "shown": shown, "online": online, "where": where if online else None,
             "server": on, "lastLogin": last_in, "lastLogout": last_out, "total": total,
             "counted": counted, "v": 2, "pinged": last_in if ping else old.get("pinged"),
             "statusAt": pulled}
    if left or old.get("hbLeave"):
        state["hbLeave"] = left or old["hbLeave"]
    if miss:
        state.update({"miss": miss, "offAt": off_at})
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


def alarm(webhook, title, card, uid, text):
    """An alarm card, then the player pinged several times."""
    send(webhook, {"content": f"🚨🚨🚨 **{title}** 🚨🚨🚨", "embeds": [card]})
    for i in range(int(os.environ.get("RAT_PINGS") or 3)):
        if uid:
            send(webhook, {"content": f"🚨 <@{uid}> {text}", "allowed_mentions": {"parse": [], "users": [uid]}})


def main():
    EVENTS.clear()
    LIVE.clear()
    webhook = os.environ.get("DISCORD_WEBHOOK", "")
    key = os.environ.get("HYPIXEL_KEY", "")
    overview = os.environ.get("OVERVIEW", "").lower() == "true"
    watching = os.environ.get("LOOP", "").lower() == "true"
    names = [n.strip() for n in (os.environ.get("PLAYERS") or DEFAULT).split(",") if n.strip()]
    if not webhook or not key:
        print("DISCORD_WEBHOOK oder HYPIXEL_KEY fehlt (Repo-Secrets)")
        return 1
    saved = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}
    if not REQ:
        REQ.update(saved.get("req", {}))
    # a copy: comparing the new state with a dict changed in place would never see a change
    players = json.loads(json.dumps(saved.get("players", {})))

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
        # player had just joined (offline: their status card), each with its ping under it; the
        # saved state is left alone
        wanted = names if test.lower() == "all" else [t.strip() for t in test.split(",") if t.strip()]
        joined = []
        for t in wanted:
            name = next((n for n in names if n.lower() == t.lower()), t)
            uid = ids.get(name.lower())
            if not uid:
                print(f"TEST_PING: no Discord ID for {name}")
                continue
            old = {k: v for k, v in players.get(name, {}).items() if k in ("uuid", "shown", "lastLogin", "lastLogout")}
            try:
                _, card, _ = check(name, {**old, "online": False}, key)
                if card is None:  # offline: the status card instead
                    _, card, _ = check(name, {**old, "online": False}, key, True)
            except (Denied, Throttled) as e:
                print(f"TEST_PING: {name}: {e}")
                continue
            card["footer"] = {"text": f"{card['footer']['text']} • test"}
            joined.append((card, uid))
        EVENTS.clear()
        post(webhook, [], joined)
        print(f"test pings sent: {len(joined)}")
        return 0 if joined else 1

    embeds, joined, ratters, errors = [], [], [], 0
    for name in names:
        old = players.get(name, {})
        try:
            players[name], msg, ping = check(name, old, key, overview)
        except (Denied, Throttled, urllib.error.HTTPError) as e:
            why = f"HTTP {e.code}" if isinstance(e, urllib.error.HTTPError) else str(e)
            print(f"{name}: not checked: {why}")
            errors += 1
            # the live card says why, instead of showing an old status as if it were current
            LIVE[name] = embed(old.get("shown") or name, old.get("uuid") or "", f"⚠️ {old.get('shown') or name}",
                               GREY, [("Status", f"not visible: {why}")])
            continue
        st = players[name]
        if watching:
            card = code_check(name, old, st, int(time.time() * 1000))
            if card:
                ratters.append((card, ids.get(name.lower())))
                print(f"{name}: RATTER ALARM (no join code)")
        code = mod_status(st) if watching and st["online"] else None
        if code:
            set_mod_field(LIVE[name], code)
        # the join card of this login was sent while the code was still awaited: edit it now
        was_waiting = old.get("hbWait") or {}
        if was_waiting.get("msg") and not st.get("hbWait") and was_waiting.get("login") == st.get("lastLogin"):
            edit_card(webhook, was_waiting["msg"],
                      set_mod_field(was_waiting["card"], mod_status(st) or "❌ no code – RATTER ALARM"))
            print(f"{name}: join card edited: {mod_status(st)}")
        if msg:
            if code and msg["title"].startswith("🟢"):
                set_mod_field(msg, code)
            # only when the player has just come online - not again while they stay online;
            # a join card still waiting for the code goes on its own, to be edited later
            uid = ids.get(name.lower()) if ping else None
            if uid or st.get("hbWait"):
                joined.append((msg, uid, name))
            else:
                embeds.append(msg)
            print(f"{name}: {msg['title']}" + (f" ({code})" if code else ""))
        else:
            print(f"{name}: {'online' if st['online'] else 'offline'}, nichts Neues")
    for card, uid in ratters:
        alarm(webhook, "RATTER ALARM", card, uid, "RATTER ALARM – someone else may be on your account!")
    # new cards, or the first round of a run (e.g. after a test ping): the live message goes to
    # the bottom again
    moved = bool(embeds or joined or ratters) or os.environ.pop("LIVE_TO_BOTTOM", "") == "1"
    if embeds or joined:
        for name, msg_id in post(webhook, embeds, joined).items():
            wait = players[name].get("hbWait")
            if wait and msg_id:  # remember the join card, to show the code once it is verified
                card = next(c for c, _, n in joined if n == name)
                wait.update({"msg": msg_id, "card": card})
    write_log()
    live_id = saved.get("live")
    if watching:  # only the watching run keeps the live message
        order = {n: LIVE[n] for n in names if n in LIVE}  # always in the order of PLAYERS
        LIVE.clear()
        LIVE.update(order)
        try:
            live_id = live(webhook, live_id, moved)
        except Exception as e:
            print(f"live message failed: {e!r}")
    LIVE.clear()

    if not watching:
        return 2 if errors == len(names) else 0  # manual runs only report, the state stays as it is
    new = {"players": {n: players[n] for n in names if n in players}}
    hour = int(time.time()) // 3600
    new["req"] = {k: v for k, v in sorted(REQ.items()) if hour - int(k) < 25}
    if live_id:
        new["live"] = live_id
    if new != saved:
        STATE.write_text(json.dumps(new, indent=2) + "\n", encoding="utf-8")
    if time.time() < PAUSE[0]:
        print(f"Hypixel requests paused for {PAUSE[0] - time.time():.0f} s (too many requests)")
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


# a name for every git command: a rebase writes commits too (without it every push failed
# as soon as someone else had pushed to main)
BOT = {"GIT_AUTHOR_NAME": "online-watch", "GIT_AUTHOR_EMAIL": "online-watch@users.noreply.github.com",
       "GIT_COMMITTER_NAME": "online-watch", "GIT_COMMITTER_EMAIL": "online-watch@users.noreply.github.com"}


def git(*args):
    r = subprocess.run(["git", *args], cwd=STATE.parent, capture_output=True, text=True,
                       env={**os.environ, **BOT})
    if r.returncode and args[0] not in ("status", "rev-parse"):
        print(f"git {args[0]} failed: {(r.stderr or r.stdout).strip()[:300]}")
    return r


def blobs():
    """Ids of the code files at HEAD: when they change, the loop restarts with the new code."""
    return git("rev-parse", "HEAD:watch.py", "HEAD:.github/workflows/watch.yml").stdout


SAVED = [time.time()]


def save(now=False):
    """Commits and pushes state.json / sessions.csv: at once when a join or leave was logged,
    otherwise at most every 10 minutes (the playtime of hidden players changes every minute)."""
    changed = git("status", "--porcelain", "state.json", "sessions.csv").stdout
    if not changed.strip():
        return
    if not now and "sessions.csv" not in changed and time.time() - SAVED[0] < 600:
        return
    SAVED[0] = time.time()
    git("add", *[f for f in ("state.json", "sessions.csv") if (STATE.parent / f).exists()])
    git("commit", "-q", "-m", "state")
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
    os.environ["LIVE_TO_BOTTOM"] = "1"
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
            print(f"Hypixel requests in the last 24 h: {today()}")
            save(True)  # a pull needs a clean tree
            git("pull", "-q", "--rebase")
            if blobs() != code:
                print("code changed, restarting")
                break
        time.sleep(max(1, every - (time.time() - start)))
    save(True)
    dispatch()
    return 0


if __name__ == "__main__":
    if os.environ.get("LOOP", "").lower() == "true":
        sys.exit(loop(int(os.environ.get("LOOP_MINUTES") or 345), int(os.environ.get("EVERY") or 60)))
    sys.exit(main())
