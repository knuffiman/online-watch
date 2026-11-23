"""Join and leave codes from the Online Watch mod (mod/): one mod for all watched accounts. On
Hypixel it sends a signed code per join and per leave into ONE shared message in a hidden Discord
channel (HEARTBEAT_WEBHOOK): its content is {"<player>": {"join": code, "leave": code}, ...}, and
the mod only ever changes its own player's entry. The bot reads that message back: a new login
without a matching join code raises a RATTER alarm; a leave code lets the bot count the leave at once.

A second message in the channel holds the mod jars (both Minecraft versions); every Build mod run
edits it instead of posting new ones.

heartbeat.json (public, no secrets): {"codes": id, "mods": id, "players": [...], "legacy": {...}}
- "legacy": the old per-player code messages of earlier mods, still read until every jar is new.

The key of each account is derived from the DISCORD_WEBHOOK secret, so no extra secret is needed:
the bot works it out at runtime, the mod workflow builds the keys of all accounts into the jar.

  python3 heartbeat.py setup [<player>,...]      the messages (heartbeat.json) + mod settings
  python3 heartbeat.py upload <jar> [<jar>...]   puts the built jars into the mod message
"""
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid as uuidlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
IDS = HERE / "heartbeat.json"
PROPS = HERE / "mod" / "src" / "main" / "resources" / "onlinewatch.properties"
UA = {"User-Agent": "online-watch/1.1"}
CODES_NOTE = ("🔒 **Online Watch – codes of all accounts.** The mod writes the join and leave codes into "
              "this message. **Do not delete or edit it** – without it no code arrives and the bot cannot "
              "check any login.")


def key(player):
    master = os.environ["DISCORD_WEBHOOK"].encode()
    return hmac.new(master, f"onlinewatch-heartbeat|{player.lower()}".encode(), hashlib.sha256).digest()


def hook():
    return os.environ.get("HEARTBEAT_WEBHOOK", "").split("?")[0]


def ids():
    """heartbeat.json; the old format (player -> message id) counts as legacy messages."""
    data = json.loads(IDS.read_text(encoding="utf-8")) if IDS.exists() else {}
    if data and "players" not in data:
        data = {"players": list(data), "legacy": data}
    return data


def has(player):
    """Is the player one the mod is built for (so a join code is expected)?"""
    return any(p.lower() == player.lower() for p in ids().get("players", []))


def message_id(player=None):
    """The id of the shared code message."""
    return ids().get("codes")


def request(url, data=None, method="GET", headers=None):
    req = urllib.request.Request(url, data=data, method=method, headers={**UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=20) as r:
        body = r.read()
    return json.loads(body) if body else None


class MessageGone(Exception):
    """The shared code message was deleted from the hidden channel: the mod cannot send codes any
    more until Build mod has made a new one (and the new jar is installed)."""


def valid(player, uuid, code, event=None):
    """The time of a code in ms if it is genuine (signature, account, event), else None."""
    try:
        text = f"{code['p']}|{code['u']}|{code['t']}|{code['n']}" + (f"|{event}" if event else "")
        sig = hmac.new(key(player), text.encode(), hashlib.sha256).hexdigest()
        good = hmac.compare_digest(sig, code["sig"]) and code.get("e") == event
    except (KeyError, TypeError):
        return None
    if not good or code["p"].lower() != player.lower() or code["u"].replace("-", "") != uuid.replace("-", ""):
        return None
    return int(code["t"])


def content_of(msg_id):
    """The JSON content of a message (a dict), {} if empty / not JSON; raises HTTPError."""
    msg = request(f"{hook()}/messages/{msg_id}")
    try:
        content = json.loads(msg.get("content") or "")
    except ValueError:
        return {}
    return content if isinstance(content, dict) else {}


CACHE = {}  # the shared message read in the last few seconds: one read per round for all players


def shared():
    msg_id = ids().get("codes")
    if not msg_id:
        return {}
    if CACHE.get("id") == msg_id and time.time() - CACHE.get("at", 0) < 5:
        return CACHE["content"]
    try:
        content = content_of(msg_id)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise MessageGone(msg_id) from None
        raise
    CACHE.update({"id": msg_id, "at": time.time(), "content": content})
    return content


def read_codes(player, uuid):
    """The newest codes of an account: {"join": ms or None, "leave": ms or None}; each only if genuine.
    Raises on network trouble, so that a failed read never counts as a missing code."""
    if not hook():
        return {"join": None, "leave": None}
    entries = []
    gone = None
    in_shared = False
    try:
        entry = next((v for k, v in shared().items() if k.lower() == player.lower()), None)
        if isinstance(entry, dict):
            entries.append(entry)
            in_shared = True
    except MessageGone as e:
        gone = e
    # an older jar still writes the player's own message
    legacy = next((v for k, v in ids().get("legacy", {}).items() if k.lower() == player.lower()), None)
    if legacy:
        try:
            old = content_of(legacy)
            entries.append({"join": old} if "join" not in old and "sig" in old else old)
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
            if not in_shared:
                # never used the new jar, and the old jar's message is deleted: whatever jar is
                # played writes nowhere - a setup problem (install the new jar), not a stolen account
                gone = gone or MessageGone(legacy)
    joins = [t for t in (valid(player, uuid, e.get("join")) for e in entries) if t]
    leaves = [t for t in (valid(player, uuid, e.get("leave"), "leave") for e in entries) if t]
    if gone and not joins and not leaves:
        raise gone
    return {"join": max(joins, default=None), "leave": max(leaves, default=None)}


def read_code(player, uuid):
    """The newest join code of an account, if it is genuine: its time in ms, else None."""
    return read_codes(player, uuid)["join"]


def exists(msg_id):
    if not msg_id:
        return False
    try:
        request(f"{hook()}/messages/{msg_id}")
        return True
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
        return False


def post(payload):
    body = json.dumps({"username": "Online Watch", **payload}).encode()
    return request(f"{hook()}?wait=true", body, "POST", {"Content-Type": "application/json"})["id"]


def setup(players):
    """Makes sure the shared code message and the mod message exist, and writes the mod settings
    with every account: one mod for all of them."""
    data = ids()
    if not exists(data.get("codes")):
        data["codes"] = post({"content": "{}", "embeds": [{"description": CODES_NOTE, "color": 0xF04747}]})
        print(f"new shared code message: {data['codes']}")
    if not exists(data.get("mods")):
        data["mods"] = post({"content": "🧩 **Online Watch mod** – the jars come here."})
        print(f"new mod message: {data['mods']}")
    data["players"] = players
    IDS.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    lines = [f"webhook={hook()}", f"message={data['codes']}", "players=" + ",".join(players)]
    lines += [f"key.{p.lower()}={key(p).hex()}" for p in players]
    PROPS.write_text("\n".join(lines) + "\n", encoding="utf-8")


def upload(jars, players):
    """The jars replace the ones in the mod message (edited, never a new message). They go only into
    the hidden channel: they hold the webhook and the keys."""
    msg_id = ids().get("mods")
    boundary = uuidlib.uuid4().hex
    files, attachments = [], []
    for i, jar in enumerate(jars):
        stem = Path(jar).stem  # onlinewatch-1.1.0+mc26.1.2
        mc = stem.split("+mc")[-1] if "+mc" in stem else "26.2"
        files.append((f"onlinewatch-mc{mc}.jar", Path(jar).read_bytes()))
        attachments.append({"id": i, "filename": f"onlinewatch-mc{mc}.jar"})
    versions = ", ".join(a["filename"].removeprefix("onlinewatch-mc").removesuffix(".jar") for a in attachments)
    now = int(time.time())
    content = (f"🧩 **Online Watch mod** (Fabric) – one jar for all accounts: {', '.join(players)}\n"
               f"Minecraft **{versions}**: take the jar of your version, put it into your `mods` folder "
               "(Fabric API is needed too) and play with any of these accounts. Replace older Online "
               f"Watch jars. Do not share it outside this channel.\nBuilt <t:{now}:f>.")
    payload = json.dumps({"content": content, "attachments": attachments})
    data = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"payload_json\"\r\n"
            f"Content-Type: application/json\r\n\r\n{payload}\r\n").encode()
    for i, (name, blob) in enumerate(files):
        data += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"files[{i}]\"; filename=\"{name}\"\r\n"
                 "Content-Type: application/java-archive\r\n\r\n").encode() + blob + b"\r\n"
    data += f"--{boundary}--\r\n".encode()
    request(f"{hook()}/messages/{msg_id}", data, "PATCH", {"Content-Type": f"multipart/form-data; boundary={boundary}"})
    print(f"mod message updated: {versions}")


def names(arg=None):
    raw = arg or os.environ.get("PLAYERS") or "mrkioko, Knuffiman, _gggaming"
    return [n.strip() for n in raw.split(",") if n.strip()]


if __name__ == "__main__":
    if not hook() or not os.environ.get("DISCORD_WEBHOOK"):
        sys.exit("HEARTBEAT_WEBHOOK or DISCORD_WEBHOOK missing (repo secrets)")
    if sys.argv[1] == "setup":
        setup(names(sys.argv[2] if len(sys.argv) > 2 else None))
    elif sys.argv[1] == "upload":
        upload(sys.argv[2:], names())
