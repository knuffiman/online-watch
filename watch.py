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
PROBLEMS = []  # what went wrong with Hypixel in this round (for the failsafe)
LAST = {}  # the newest live card of each player, for the fast updates between rounds
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
    payload = {"content": f"📡 **Live status** – updated every {os.environ.get('EVERY') or 60} s (last: <t:{now}:T>, <t:{now}:R>)",
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



MOD_FIELD = "Key"


def mod_status(st):
    """Only whether the mod's key (join code) of the current login was checked, or None (no mod /
    offline). The code itself never goes into the channel."""
    if st.get("hbGone"):
        return "❌ not checked"
    if st.get("hbWait"):
        return "⏳ checking…"
    if st.get("hbFail") and st.get("hbFail") == st.get("lastLogin"):
        return "❌ not checked"
    t, login = st.get("hbLast"), st.get("lastLogin")
    if t and login and abs(t - login) <= 180000:
        return "✅ checked"
    return None


def set_mod_field(card, text):
    """Puts the mod-code line on a card (replacing an old one), just above "Checked"."""
    fields = [f for f in card["fields"] if f["name"] != MOD_FIELD]
    at = next((i for i, f in enumerate(fields) if f["name"] == "Checked"), len(fields))
    fields.insert(at, {"name": MOD_FIELD, "value": text, "inline": False})
    card["fields"] = fields
    return card



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
    return bool(os.environ.get("HEARTBEAT_WEBHOOK")) and heartbeat.has(name)


def mod_leave(name, uuid, old, last_in):
    """The time of a fresh leave code from the mod for this session (ms), else None.
    Only asked when Hypixel shows a player offline who was online: one read of the hidden channel."""
    if not has_mod(name):
        return None
    try:
        codes = heartbeat.read_codes(name, uuid)
    except heartbeat.MessageGone:  # shown on the live card; the usual OFF_ROUNDS rounds decide
        return None
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


def stale_online(name, uuid, old, now_ms):
    """Hypixel shows online, but the mod's leave code ended the session less than 5 minutes ago and
    no join code came after it: Hypixel has not caught up yet."""
    t = old.get("hbLeave")
    if not t or now_ms - t > 5 * 60000 or not has_mod(name):
        return False
    try:
        joined = heartbeat.read_codes(name, uuid).get("join")
    except Exception as e:  # cannot tell: believe Hypixel
        print(f"{name}: codes not readable: {e!r}")
        return False
    return not joined or joined < t


def fast_leaves():
    """Between the rounds (every FAST seconds): the leave codes of the players online with the mod.
    Only the hidden Discord channel is read, never Hypixel. {player: time of the leave code}"""
    if not os.environ.get("HEARTBEAT_WEBHOOK") or not STATE.exists():
        return {}
    players = json.loads(STATE.read_text(encoding="utf-8")).get("players", {})
    found = {}
    for name, p in players.items():
        if p.get("online") and p.get("uuid") and has_mod(name):
            t = mod_leave(name, p["uuid"], p, p.get("lastLogin"))
            if t:
                found[name] = t
    return found


def fast_joins():
    """Between the rounds: the players offline on the card whose mod just sent a new join code, so
    that the join shows on the live card within seconds (Hypixel only shows it in the next round,
    and sometimes later). The code is signed, so it is the player's own login. {player: its time}"""
    if not os.environ.get("HEARTBEAT_WEBHOOK") or not STATE.exists():
        return {}
    players = json.loads(STATE.read_text(encoding="utf-8")).get("players", {})
    found, now = {}, time.time() * 1000
    for name, p in players.items():
        if "online" in p and p.get("uuid") and has_mod(name):
            try:
                codes = heartbeat.read_codes(name, p["uuid"])
            except Exception:  # read again in the next round
                continue
            t = codes.get("join")
            seen = max(p.get("lastLogout") or 0, p.get("hbLeave") or 0, p.get("hbLast") or 0, codes.get("leave") or 0)
            if p["online"]:
                # still shown online (e.g. joined without the mod, then rejoined with it before the
                # bot saw the leave): a join code clearly after this login is a new session
                seen = max(seen, (p.get("lastLogin") or 0) + 20000)
            if t and t > seen and now - t < 5 * 60000:
                found[name] = t
    return found


def join_ok(st, t):
    """Is t (a join code's time) the code of the login the bot waits for?"""
    wait = st.get("hbWait") or {}
    ref = wait.get("login") or wait.get("since")
    return bool(t and ref and t > (st.get("hbLast") or 0) and abs(t - ref) <= 180000)


def fast_codes():
    """Between the rounds: the players whose join code arrived while the bot waits for it, so that
    the live card shows "verified" within seconds. Hidden channel only, never Hypixel."""
    if not os.environ.get("HEARTBEAT_WEBHOOK") or not STATE.exists():
        return set()
    players = json.loads(STATE.read_text(encoding="utf-8")).get("players", {})
    found = set()
    for name, p in players.items():
        if p.get("online") and p.get("hbWait") and p.get("uuid") and has_mod(name):
            try:
                if join_ok(p, heartbeat.read_code(name, p["uuid"])):
                    found.add(name)
            except Exception:  # read again in the next round
                pass
    return found


def code_check(name, old, st, now_ms):
    """RATTER alarm: a new login on Hypixel needs a join code from the Online Watch mod within
    HB_WAIT seconds (only for players in heartbeat.json). Keeps hbWait / hbLast in the state and
    returns the alarm card or None."""
    if not has_mod(name):
        return None
    for k in ("hbWait", "hbLast", "hbFail", "hbGone"):
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
        st.pop("hbGone", None)
    except heartbeat.MessageGone as e:
        # deleted by hand: a setup problem, not a stolen account - no alarm, a warning on the live
        # card until Build mod made a new message (heartbeat.json) and the new jar is in
        if st.get("hbGone") != str(e):
            print(f"{name}: code message {e} was deleted - run Build mod and install the new jar")
        st["hbGone"] = str(e)
        st.pop("hbWait", None)
        return None
    except Exception as e:  # cannot read the code: never an alarm for that, try again next round
        print(f"{name}: join code not readable: {e!r}")
        t = None
        if now_ms - wait["since"] < 15 * 60000:
            return None
    if join_ok(st, t):
        st["hbLast"] = t
        st.pop("hbWait", None)
        print(f"{name}: join code OK")
        return None
    if now_ms - wait["since"] < int(os.environ.get("HB_WAIT") or 60) * 1000:
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


def check(name, old, key, leave_at=None, join_at=None):
    """New state of a player and what happened ("join", "leave" or None).
    One Hypixel request: the status. Login and logout times are when the bot saw the player come
    online and go offline (at most a minute late) or, with the mod, the time of its leave code.
    leave_at: a leave code the fast check between rounds found - then Hypixel is not asked at all.
    join_at: the same for a new join code: the player counts as online from its time.
    Nothing is sent from here: joins and leaves only go into sessions.csv and the live card."""
    uuid, shown = old.get("uuid"), old.get("shown")
    if not uuid:
        profile = get(f"https://api.mojang.com/users/profiles/minecraft/{name}")
        uuid, shown = profile["id"], profile.get("name")
    shown = shown or name
    if leave_at or join_at:
        session = {"online": True} if join_at else {}
    else:
        data = get(f"https://api.hypixel.net/v2/status?uuid={uuid}", {"API-Key": key})
        if not isinstance(data, dict) or not data.get("success") or not isinstance(data.get("session"), dict):
            raise Denied("Hypixel sent no status: " + json.dumps(data)[:120])
        session = data["session"]
    pulled = int(time.time() * 1000)  # when the data was fetched, shown on the live card
    online = bool(session.get("online"))
    where = " / ".join(x.replace("_", " ").title() for x in (session.get("gameType"), session.get("mode")) if x)
    first = "online" not in old
    was = bool(old.get("online"))
    last_in, last_out = old.get("lastLogin"), old.get("lastLogout")

    # Hypixel can still show a player online for a little while after the mod's leave code: that
    # is no new login unless the mod sent a join code after the leave
    if online and not was and not first and not join_at and stale_online(name, uuid, old, pulled):
        online, where = False, ""
        print(f"{name}: Hypixel still shows online after the leave code, counted as offline")

    # Switching lobbies or servers can show a player offline for a moment: only OFF_ROUNDS rounds
    # in a row offline count as leaving (the logout is the first of them). A leave code from the
    # mod (sent when disconnecting or closing the game) ends the session at once, at its time.
    miss, off_at = 0, None
    left = leave_at or (mod_leave(name, uuid, old, last_in) if not online and was else None)
    if left:
        last_out = left
        print(f"{name}: leave code from the mod, left at once")
    elif not online and was:
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
        last_in = join_at or pulled  # a new login: the mod's join code, or seen now
    on = (server(session) or (None if join_at else old.get("server"))) if online else None  # kept during a lobby switch

    login = ts(last_in) + " (" + ts(last_in, "R") + ")" if last_in else None
    logout = ts(last_out) + " (" + ts(last_out, "R") + ")" if last_out else None
    played = span(last_out - last_in) if last_in and last_out and last_out > last_in else None

    event = None
    if not first:
        if online and not was:
            event = "join"
            EVENTS.append((last_in, shown, "join", where, "", ""))
        elif not online and was:
            event = "leave"
            EVENTS.append((last_out, shown, "leave", "", played or "", ""))

    total, counted = playtime(old, last_in, last_out, shown)
    status = embed(shown, uuid, f"🟢 {shown} is online" if online else f"🔴 {shown} is offline",
                   GREEN if online else RED,
                   [("Status", "🟢 online" if online else "⚫ offline"), ("Server", on),
                    ("Online since" if online else "Last login", login),
                    ("Last logout", None if online else logout),
                    ("Last session", played if not online else None)])
    # the live card: the total including the session going on right now
    running = pulled - last_in if online and last_in and last_in != counted else 0
    status["fields"].append({"name": "Total played", "value": span(total + running) + " since tracking began",
                             "inline": False})
    status["fields"].append({"name": "Checked", "value": ts(pulled, "T") + " (" + ts(pulled, "R") + ")",
                             "inline": False})
    LIVE[name] = status
    state = {"uuid": uuid, "shown": shown, "online": online, "where": where if online else None,
             "server": on, "lastLogin": last_in, "lastLogout": last_out, "total": total,
             "counted": counted, "v": 2, "statusAt": pulled}
    if left or old.get("hbLeave"):
        state["hbLeave"] = left or old["hbLeave"]
    if miss:
        state.update({"miss": miss, "offAt": off_at})
    return state, event



HEADER = "time,player,event,mode,played,key"
# leaves before this column existed whose key was checked, worked out from the history of
# state.json (hbLast within 3 minutes of the session's join); every other old leave: not checked
CHECKED_BEFORE = {
    ("2026-11-23 20:43", "Knuffiman"), ("2026-11-23 20:56", "Knuffiman"), ("2026-11-23 21:25", "Knuffiman"),
    ("2026-11-23 21:30", "Knuffiman"), ("2026-11-23 22:05", "mrkioko"), ("2026-11-23 22:51:31", "Knuffiman"),
    ("2026-11-23 22:59:31", "Knuffiman"), ("2026-11-23 23:17:10", "Knuffiman"), ("2026-11-23 23:23:00", "Knuffiman"),
    ("2026-11-23 23:26:38", "Knuffiman")}


def session_code(name, st):
    """Was the mod's key (join code) of this session checked? For the leave line in sessions.csv."""
    login, t = st.get("lastLogin"), st.get("hbLast")
    return "checked" if has_mod(name) and t and login and abs(t - login) <= 180000 else "not checked"


def migrate_log():
    """sessions.csv in the newest layout: a key column with checked / not checked on every leave."""
    if not LOG.exists():
        return
    lines = LOG.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0] == HEADER:
        return
    out = [HEADER]
    for line in lines[1:]:
        if not line:
            continue
        r = (line.split(",") + [""] * 6)[:6]
        if r[2] == "leave":
            r[5] = "checked" if r[5] in ("verified", "checked") or (r[0], r[1]) in CHECKED_BEFORE else "not checked"
        else:
            r[5] = ""
        out.append(",".join(r))
    LOG.write_text("\n".join(out) + "\n", encoding="utf-8")
    print("sessions.csv: key column filled in")


def write_log():
    """Appends the joins and leaves of this run to sessions.csv, oldest first."""
    if not EVENTS:
        return
    from zoneinfo import ZoneInfo
    tz = ZoneInfo("Europe/Berlin")
    migrate_log()
    new = not LOG.exists()
    with LOG.open("a", encoding="utf-8", newline="") as f:
        if new:
            f.write(HEADER + "\n")
        for ms, name, event, mode, played, code in sorted(EVENTS):
            when = datetime.fromtimestamp(ms / 1000, tz).strftime("%Y-%m-%d %H:%M:%S")
            f.write(f"{when},{name},{event},{mode},{played},{code}\n")
    EVENTS.clear()


def alarm(webhook, title, card, uid, text):
    """An alarm card, then @everyone pinged several times (the player too, if known)."""
    send(webhook, {"content": f"🚨🚨🚨 **{title}** 🚨🚨🚨", "embeds": [card]})
    who = f"<@{uid}> " if uid else ""
    for i in range(int(os.environ.get("RAT_PINGS") or 3)):
        send(webhook, {"content": f"🚨 @everyone {who}{text}",
                       "allowed_mentions": {"parse": ["everyone"], "users": [uid] if uid else []}})


def failsafe(webhook, problems, saved):
    """Hypixel sends errors or nothing: after FAILSAFE_ROUNDS rounds in a row the player in
    ALERT_USER (default Knuffiman, Discord id from DISCORD_IDS) is pinged, again every hour while it
    lasts, and a note without ping comes when it works again. Kept in state.json ("failsafe")."""
    fs = dict(saved.get("failsafe") or {})
    now = int(time.time() * 1000)
    user = (os.environ.get("ALERT_USER") or "Knuffiman").strip()
    uid = next((v.split("=", 1)[1].strip() for v in (os.environ.get("DISCORD_IDS") or "").split(",")
                if "=" in v and v.split("=", 1)[0].strip().lower() == user.lower()), "")
    uid = "".join(c for c in uid if c.isdigit())
    ping = f"<@{uid}> " if uid else ""
    mention = {"parse": [], "users": [uid] if uid else []}
    if problems:
        fs["rounds"] = fs.get("rounds", 0) + 1
        fs.setdefault("since", now)
        fs["why"] = "; ".join(problems)[:900]
        due = not fs.get("sent") or now - fs["sent"] >= 3600000
        if fs["rounds"] >= int(os.environ.get("FAILSAFE_ROUNDS") or 3) and due:
            send(webhook, {"content": f"⚠️ {ping}**Hypixel API problem** since <t:{fs['since'] // 1000}:R> – "
                                      "the watch cannot see the players.\n"
                                      f"```{fs['why']}```",
                           "allowed_mentions": mention})
            fs["sent"] = now
            print(f"failsafe: {user} notified ({fs['why']})")
    elif fs:
        if fs.get("sent"):
            send(webhook, {"content": f"✅ Hypixel API works again (problem since <t:{fs['since'] // 1000}:f>).",
                           "allowed_mentions": {"parse": []}})
            print("failsafe: works again")
        fs = {}
    return fs


def main(leaves=None, verified=(), joins=None):
    """One round: the status of every player, the RATTER check, sessions.csv, the live message.
    The only messages in the channel are the live message and RATTER alarms.
    leaves: {player: time of a leave code} from the fast check between rounds - then only those
    players are updated (without asking Hypixel), the others keep their card from the last round.
    verified: players whose join code arrived between the rounds - only their mod line changes.
    joins: {player: time of a new join code} of players offline until now - they count as joined."""
    joins = joins or {}
    EVENTS.clear()
    LIVE.clear()
    PROBLEMS.clear()
    webhook = os.environ.get("DISCORD_WEBHOOK", "")
    key = os.environ.get("HYPIXEL_KEY", "")
    names = [n.strip() for n in (os.environ.get("PLAYERS") or DEFAULT).split(",") if n.strip()]
    if not webhook or not key:
        print("DISCORD_WEBHOOK oder HYPIXEL_KEY fehlt (Repo-Secrets)")
        return 1
    saved = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}
    if not REQ:
        REQ.update(saved.get("req", {}))
    # a copy: comparing the new state with a dict changed in place would never see a change
    players = json.loads(json.dumps(saved.get("players", {})))

    # Discord user IDs, pinged only with a RATTER alarm, e.g. "mrkioko=123456789012345678, ..."
    ids = {}
    for pair in (os.environ.get("DISCORD_IDS") or "").split(","):
        if "=" in pair:
            k, v = pair.split("=", 1)
            v = "".join(c for c in v if c.isdigit())
            if v:
                ids[k.strip().lower()] = v

    ratters, errors = [], 0
    for name in names:
        old = players.get(name, {})
        if leaves is not None and name not in leaves and name not in joins:
            if name in verified and name in LAST and name in players:
                st = players[name]
                code_check(name, dict(st), st, int(time.time() * 1000))
                code = mod_status(st)
                if code:
                    set_mod_field(LAST[name], code)
            if name in LAST:
                LIVE[name] = LAST[name]
            continue
        try:
            if joins.get(name) and old.get("online"):
                # rejoined with the mod while still shown online: the old session ends first (at the
                # first offline round if Hypixel showed one, else at the join), with its own key state
                end = min(old.get("offAt") or joins[name], joins[name])
                ended, _ = check(name, old, key, leave_at=end)
                for k in ("hbLast", "hbFail", "hbGone"):
                    if old.get(k):
                        ended[k] = old[k]
                for i, e in enumerate(EVENTS):
                    if e[1] == ended.get("shown") and e[2] == "leave" and not e[5]:
                        EVENTS[i] = e[:5] + (session_code(name, old),)
                print(f"{name}: rejoined with the mod, the session before ends at {ts(end, 'T')}")
                old = ended
            players[name], event = check(name, old, key, (leaves or {}).get(name), joins.get(name))
        except (Denied, Throttled, urllib.error.HTTPError, urllib.error.URLError, TimeoutError, ValueError) as e:
            why = f"HTTP {e.code}" if isinstance(e, urllib.error.HTTPError) else (str(e) or type(e).__name__)
            print(f"{name}: not checked: {why}")
            PROBLEMS.append(f"{name}: {why}")
            errors += 1
            # the live card says why, instead of showing an old status as if it were current
            LIVE[name] = embed(old.get("shown") or name, old.get("uuid") or "", f"⚠️ {old.get('shown') or name}",
                               GREY, [("Status", f"not visible: {why}")])
            LAST[name] = LIVE[name]
            continue
        st = players[name]
        card = code_check(name, old, st, int(time.time() * 1000))
        # the leave line in sessions.csv says whether the session's join code was checked
        for i, e in enumerate(EVENTS):
            if e[1] == st.get("shown") and e[2] == "leave" and not e[5]:
                EVENTS[i] = e[:5] + (session_code(name, st),)
        if card:
            ratters.append((card, ids.get(name.lower())))
            print(f"{name}: RATTER ALARM (no join code)")
        code = mod_status(st) if st["online"] else None
        if code:
            set_mod_field(LIVE[name], code)
        if not st["online"] and has_mod(name):
            # offline: whether the key of the last session was checked, next to its playtime
            key_ok = session_code(name, st) == "checked"
            for f in LIVE[name]["fields"]:
                if f["name"] == "Last session":
                    f["value"] += " · ✅ key checked" if key_ok else " · ❌ key not checked"
        LAST[name] = LIVE[name]
        if event:
            print(f"{name}: {event}" + (f" ({code})" if code else ""))
        else:
            print(f"{name}: {'online' if st['online'] else 'offline'}, nichts Neues")
    for card, uid in ratters:
        alarm(webhook, "RATTER ALARM", card, uid, "RATTER ALARM – someone else may be on your account!")
    write_log()
    # after an alarm, or in the first round of a run, the live message goes to the bottom again
    moved = bool(ratters) or os.environ.pop("LIVE_TO_BOTTOM", "") == "1"
    order = {n: LIVE[n] for n in names if n in LIVE}  # always in the order of PLAYERS
    LIVE.clear()
    LIVE.update(order)
    live_id = saved.get("live")
    try:
        live_id = live(webhook, live_id, moved)
    except Exception as e:
        print(f"live message failed: {e!r}")
    LIVE.clear()

    new = {"players": {n: players[n] for n in names if n in players}}
    hour = int(time.time()) // 3600
    new["req"] = {k: v for k, v in sorted(REQ.items()) if hour - int(k) < 25}
    if live_id:
        new["live"] = live_id
    if leaves is None:  # a full Hypixel round
        try:
            fs = failsafe(webhook, PROBLEMS, saved)
        except Exception as e:
            print(f"failsafe message failed: {e!r}")
            fs = saved.get("failsafe") or {}
    else:
        fs = saved.get("failsafe") or {}
    if fs:
        new["failsafe"] = fs
    if new != saved:
        STATE.write_text(json.dumps(new, indent=2) + "\n", encoding="utf-8")
    if time.time() < PAUSE[0]:
        print(f"Hypixel requests paused for {PAUSE[0] - time.time():.0f} s (too many requests)")
    return 2 if errors == len(names) else 0



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


def pull():
    """Gets main from GitHub. Never leaves a half-done rebase behind: on 24 Nov a conflicting pull
    stayed unfinished, state.json kept git's conflict markers and every check failed for 2 hours.
    state.json and sessions.csv are written by this bot only, so on a conflict its own version
    wins (-X theirs: in a rebase, "theirs" are the local commits); sessions.csv is merged line by
    line (.gitattributes: merge=union), so no join or leave is lost."""
    r = git("pull", "-q", "--rebase", "-X", "theirs")
    if r.returncode != 0:
        git("rebase", "--abort")
    return r.returncode == 0


def repair():
    """A rebase left over, or conflict markers in state.json: back to a clean tree."""
    gitdir = STATE.parent / ".git"
    if (gitdir / "rebase-merge").exists() or (gitdir / "rebase-apply").exists():
        print("unfinished rebase found, aborting it")
        git("rebase", "--abort")
    for f in (STATE, LOG):
        if f.exists() and "<<<<<<<" in f.read_text(encoding="utf-8"):
            print(f"conflict markers in {f.name}, taking the last committed version")
            git("checkout", "HEAD", "--", f.name)


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
        pull()
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
    body = {"ref": os.environ.get("GITHUB_REF_NAME", "main"), "inputs": {"loop": "true"}}
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
    # the newest state first: this run may have been started from a commit just before the last
    # one of the run before it (24 Nov: then every push of this run was a conflict)
    repair()
    pull()
    migrate_log()  # once per run; committed with the next save
    while time.time() < end:
        start = time.time()
        repair()
        try:
            if main() == 1:
                return 1  # secrets missing: no point in going on
        except Exception as e:  # network trouble etc.: try again next round
            print(f"check failed: {e!r}")
        save()
        # until the next round: every FAST seconds the mod's leave codes (hidden channel only),
        # so that closing the game shows on the live card within seconds
        fast = float(os.environ.get("FAST") or 10)
        while fast and time.time() - start + fast < every:
            time.sleep(fast)
            try:
                found = fast_leaves()
                joins = {n: t for n, t in fast_joins().items() if n not in found}
                codes = fast_codes() - set(found) - set(joins)
                if found:
                    print(f"leave code(s) between rounds: {', '.join(found)}")
                if joins:
                    print(f"join(s) by the mod's code between rounds: {', '.join(joins)}")
                if codes:
                    print(f"join code(s) between rounds: {', '.join(sorted(codes))}")
                if found or codes or joins:
                    main(found, codes, joins)
                    save(True)
            except Exception as e:
                print(f"fast check failed: {e!r}")
        if time.time() - synced >= 600:  # new code pushed? restart with it
            synced = time.time()
            print(f"Hypixel requests in the last 24 h: {today()}")
            save(True)  # a pull needs a clean tree
            pull()
            if blobs() != code:
                print("code changed, restarting")
                break
        time.sleep(max(1, every - (time.time() - start)))
    save(True)
    dispatch()
    return 0


if __name__ == "__main__":
    # every run watches: a check every minute for almost 6 hours, then the next run
    sys.exit(loop(int(os.environ.get("LOOP_MINUTES") or 345), int(os.environ.get("EVERY") or 60)))
