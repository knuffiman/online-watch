"""Join codes from the Online Watch mod (mod/): the mod sends one signed code per join on Hypixel
into a message of a hidden Discord channel (HEARTBEAT_WEBHOOK); the bot reads that message back
and raises a RATTER alarm when Hypixel shows a new login without a matching code.

The key of each player is derived from the DISCORD_WEBHOOK secret, so no extra secret is needed:
the bot works it out at runtime, the mod workflow builds it into the jar.

  python3 heartbeat.py setup <player>        message for the player (heartbeat.json) + mod settings
  python3 heartbeat.py upload <player> <jar> sends the built jar into the hidden channel
"""
import hashlib
import hmac
import json
import os
import sys
import urllib.error
import urllib.request
import uuid as uuidlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
IDS = HERE / "heartbeat.json"  # player -> id of their message in the hidden channel (not secret)
PROPS = HERE / "mod" / "src" / "main" / "resources" / "onlinewatch.properties"
UA = {"User-Agent": "online-watch/1.1"}


def key(player):
    master = os.environ["DISCORD_WEBHOOK"].encode()
    return hmac.new(master, f"onlinewatch-heartbeat|{player.lower()}".encode(), hashlib.sha256).digest()


def hook():
    return os.environ.get("HEARTBEAT_WEBHOOK", "").split("?")[0]


def ids():
    return json.loads(IDS.read_text(encoding="utf-8")) if IDS.exists() else {}


def request(url, data=None, method="GET", headers=None):
    req = urllib.request.Request(url, data=data, method=method, headers={**UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=20) as r:
        body = r.read()
    return json.loads(body) if body else None


def read_code(player, uuid):
    """The newest join code of a player, if it is genuine: its time in ms, else None.
    Raises on network trouble, so that a failed read never counts as a missing code."""
    msg_id = next((v for k, v in ids().items() if k.lower() == player.lower()), None)
    if not msg_id or not hook():
        return None
    msg = request(f"{hook()}/messages/{msg_id}")
    try:
        code = json.loads(msg.get("content") or "")
        text = f"{code['p']}|{code['u']}|{code['t']}|{code['n']}"
    except (ValueError, KeyError, TypeError):
        return None
    good = hmac.compare_digest(hmac.new(key(player), text.encode(), hashlib.sha256).hexdigest(), code["sig"])
    if not good or code["p"].lower() != player.lower() or code["u"].replace("-", "") != uuid.replace("-", ""):
        return None
    return int(code["t"])


def setup(player):
    """Makes sure the player has a message in the hidden channel and writes the mod settings."""
    known = ids()
    msg_id = known.get(player)
    if msg_id:
        try:
            request(f"{hook()}/messages/{msg_id}")
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
            msg_id = None
    if not msg_id:
        body = json.dumps({"username": "Online Watch", "content": f"(join codes of {player} - leave this message)"})
        msg_id = request(f"{hook()}?wait=true", body.encode(), "POST", {"Content-Type": "application/json"})["id"]
        known[player] = msg_id
        IDS.write_text(json.dumps(known, indent=2) + "\n", encoding="utf-8")
        print(f"new code message for {player}: {msg_id}")
    PROPS.write_text(f"webhook={hook()}\nmessage={msg_id}\nkey={key(player).hex()}\nplayer={player}\n",
                     encoding="utf-8")


def upload(player, jar):
    """The jar goes only into the hidden channel: it holds the webhook and the key."""
    boundary = uuidlib.uuid4().hex
    payload = json.dumps({"username": "Online Watch", "content":
                          f"🧩 **Online Watch mod for {player}** (Minecraft 26.2, Fabric)\n"
                          "Put it into your `mods` folder (Fabric API is needed too). "
                          "It is made for this account only - do not share it."})
    data = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"payload_json\"\r\n"
            f"Content-Type: application/json\r\n\r\n{payload}\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"files[0]\"; "
            f"filename=\"onlinewatch-{player}.jar\"\r\nContent-Type: application/java-archive\r\n\r\n").encode()
    data += Path(jar).read_bytes() + f"\r\n--{boundary}--\r\n".encode()
    request(hook(), data, "POST", {"Content-Type": f"multipart/form-data; boundary={boundary}"})
    print(f"mod for {player} sent into the hidden channel")


if __name__ == "__main__":
    if not hook() or not os.environ.get("DISCORD_WEBHOOK"):
        sys.exit("HEARTBEAT_WEBHOOK or DISCORD_WEBHOOK missing (repo secrets)")
    if sys.argv[1] == "setup":
        setup(sys.argv[2])
    elif sys.argv[1] == "upload":
        upload(sys.argv[2], sys.argv[3])
