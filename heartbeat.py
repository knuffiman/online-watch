"""Join and leave codes from the Online Watch mod (mod/): one mod for all watched accounts. On
Hypixel it sends a signed code per join and per leave into the account's message in a hidden
Discord channel (HEARTBEAT_WEBHOOK); the bot reads that message back. A new login without a matching
join code raises a RATTER alarm; a leave code lets the bot count the leave at once.

The key of each account is derived from the DISCORD_WEBHOOK secret, so no extra secret is needed:
the bot works it out at runtime, the mod workflow builds the keys of all accounts into the jar.

  python3 heartbeat.py setup <player>[,<player>...]  a message per account (heartbeat.json) + mod settings
  python3 heartbeat.py upload <jar> [<players>]      sends the built jar into the hidden channel
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


def read_codes(player, uuid):
    """The newest codes of an account: {"join": ms or None, "leave": ms or None}; each only if genuine.
    Raises on network trouble, so that a failed read never counts as a missing code."""
    msg_id = next((v for k, v in ids().items() if k.lower() == player.lower()), None)
    if not msg_id or not hook():
        return {"join": None, "leave": None}
    msg = request(f"{hook()}/messages/{msg_id}")
    try:
        content = json.loads(msg.get("content") or "")
    except ValueError:
        return {"join": None, "leave": None}
    if not isinstance(content, dict):
        return {"join": None, "leave": None}
    if "join" not in content and "sig" in content:  # an older mod: only the join code itself
        content = {"join": content}
    return {"join": valid(player, uuid, content.get("join")),
            "leave": valid(player, uuid, content.get("leave"), "leave")}


def read_code(player, uuid):
    """The newest join code of an account, if it is genuine: its time in ms, else None."""
    return read_codes(player, uuid)["join"]


def setup(players):
    """Makes sure every account has a message in the hidden channel and writes the mod settings
    with all of them: one mod for every account."""
    known = ids()
    lines = [f"webhook={hook()}", "players=" + ",".join(players)]
    for player in players:
        msg_id = next((v for k, v in known.items() if k.lower() == player.lower()), None)
        if msg_id:
            try:
                request(f"{hook()}/messages/{msg_id}")
            except urllib.error.HTTPError as e:
                if e.code != 404:
                    raise
                msg_id = None
        if not msg_id:
            body = json.dumps({"username": "Online Watch", "content": f"(codes of {player} - leave this message)"})
            msg_id = request(f"{hook()}?wait=true", body.encode(), "POST", {"Content-Type": "application/json"})["id"]
            known = {k: v for k, v in known.items() if k.lower() != player.lower()}
            known[player] = msg_id
            print(f"new code message for {player}: {msg_id}")
        lines += [f"message.{player.lower()}={msg_id}", f"key.{player.lower()}={key(player).hex()}"]
    IDS.write_text(json.dumps(known, indent=2) + "\n", encoding="utf-8")
    PROPS.write_text("\n".join(lines) + "\n", encoding="utf-8")


def upload(jar, players):
    """The jar goes only into the hidden channel: it holds the webhook and the keys."""
    boundary = uuidlib.uuid4().hex
    payload = json.dumps({"username": "Online Watch", "content":
                          f"🧩 **Online Watch mod** (Minecraft 26.2, Fabric) – for {', '.join(players)}\n"
                          "One mod for all these accounts: put it into your `mods` folder (Fabric API is "
                          "needed too) and play with any of them. Replace older Online Watch jars. "
                          "Do not share it outside this channel."})
    data = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"payload_json\"\r\n"
            f"Content-Type: application/json\r\n\r\n{payload}\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"files[0]\"; "
            f"filename=\"onlinewatch.jar\"\r\nContent-Type: application/java-archive\r\n\r\n").encode()
    data += Path(jar).read_bytes() + f"\r\n--{boundary}--\r\n".encode()
    request(hook(), data, "POST", {"Content-Type": f"multipart/form-data; boundary={boundary}"})
    print("mod sent into the hidden channel")


def names(arg=None):
    raw = arg or os.environ.get("PLAYERS") or "mrkioko, Knuffiman, _gggaming"
    return [n.strip() for n in raw.split(",") if n.strip()]


if __name__ == "__main__":
    if not hook() or not os.environ.get("DISCORD_WEBHOOK"):
        sys.exit("HEARTBEAT_WEBHOOK or DISCORD_WEBHOOK missing (repo secrets)")
    if sys.argv[1] == "setup":
        setup(names(sys.argv[2] if len(sys.argv) > 2 else None))
    elif sys.argv[1] == "upload":
        upload(sys.argv[2], names(sys.argv[3] if len(sys.argv) > 3 else None))
