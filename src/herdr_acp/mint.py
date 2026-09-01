"""Mint a Buzz agent identity owned by Josh, with no desktop involved.

    python -m herdr_acp.mint --name <agent-name> [--channel <id>]...

Generates a keypair, signs a NIP-OA `auth` tag with the owner key from
~/.config/buzz-acp/owner.env, publishes the agent's profile (the buzz CLI attaches the
tag to every event when BUZZ_AUTH_TAG is set), joins the channels, and writes
~/.config/buzz-acp/agents/<name>.env for buzz-acp / the pane env.

Stdlib only: bech32 + BIP-340 Schnorr below (reference algorithm), checked against the
NIP-OA test vector in `python -m herdr_acp.mint --selfcheck`.
"""

import argparse
import hashlib
import json
import os
import secrets
import subprocess
import sys

# ---- secp256k1 / BIP-340 (reference implementation, trimmed) --------------------------
P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
G = (0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798,
     0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8)


def _add(p1, p2):
    if p1 is None:
        return p2
    if p2 is None:
        return p1
    if p1[0] == p2[0] and p1[1] != p2[1]:
        return None
    if p1 == p2:
        lam = 3 * p1[0] * p1[0] * pow(2 * p1[1], P - 2, P) % P
    else:
        lam = (p2[1] - p1[1]) * pow(p2[0] - p1[0], P - 2, P) % P
    x3 = (lam * lam - p1[0] - p2[0]) % P
    return x3, (lam * (p1[0] - x3) - p1[1]) % P


def _mul(p, n):
    r = None
    for i in range(256):
        if (n >> i) & 1:
            r = _add(r, p)
        p = _add(p, p)
    return r


def _tagged(tag: str, *parts: bytes) -> bytes:
    h = hashlib.sha256(tag.encode()).digest()
    return hashlib.sha256(h + h + b"".join(parts)).digest()


def pubkey(seckey: bytes) -> bytes:
    return _mul(G, int.from_bytes(seckey, "big"))[0].to_bytes(32, "big")


def schnorr_sign(msg: bytes, seckey: bytes, aux: bytes = bytes(32)) -> bytes:
    d0 = int.from_bytes(seckey, "big")
    assert 1 <= d0 < N and len(msg) == 32
    Pt = _mul(G, d0)
    d = d0 if Pt[1] % 2 == 0 else N - d0
    t = (d ^ int.from_bytes(_tagged("BIP0340/aux", aux), "big")).to_bytes(32, "big")
    k0 = int.from_bytes(_tagged("BIP0340/nonce", t, Pt[0].to_bytes(32, "big"), msg), "big") % N
    assert k0
    R = _mul(G, k0)
    k = k0 if R[1] % 2 == 0 else N - k0
    e = int.from_bytes(_tagged("BIP0340/challenge", R[0].to_bytes(32, "big"),
                               Pt[0].to_bytes(32, "big"), msg), "big") % N
    sig = R[0].to_bytes(32, "big") + ((k + e * d) % N).to_bytes(32, "big")
    assert schnorr_verify(msg, Pt[0].to_bytes(32, "big"), sig)
    return sig


def _lift_x(x: int):
    if x >= P:
        return None
    y = pow((x ** 3 + 7) % P, (P + 1) // 4, P)
    if y * y % P != (x ** 3 + 7) % P:
        return None
    return x, y if y % 2 == 0 else P - y


def schnorr_verify(msg: bytes, pk: bytes, sig: bytes) -> bool:
    Pt = _lift_x(int.from_bytes(pk, "big"))
    r, s = int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:], "big")
    if Pt is None or r >= P or s >= N:
        return False
    e = int.from_bytes(_tagged("BIP0340/challenge", sig[:32], pk, msg), "big") % N
    R = _add(_mul(G, s), _mul(Pt, N - e))
    return R is not None and R[1] % 2 == 0 and R[0] == r


# ---- bech32 (nsec/npub) ----------------------------------------------------------------
_B32 = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def _polymod(values):
    gen = [0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3]
    chk = 1
    for v in values:
        b = chk >> 25
        chk = (chk & 0x1FFFFFF) << 5 ^ v
        for i in range(5):
            chk ^= gen[i] if (b >> i) & 1 else 0
    return chk


def _hrp_expand(hrp):
    return [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]


def _convertbits(data, frombits, tobits, pad=True):
    acc = bits = 0
    out = []
    maxv = (1 << tobits) - 1
    for v in data:
        acc = (acc << frombits) | v
        bits += frombits
        while bits >= tobits:
            bits -= tobits
            out.append((acc >> bits) & maxv)
    if pad and bits:
        out.append((acc << (tobits - bits)) & maxv)
    return out


def bech32_decode(s: str) -> tuple[str, bytes]:
    s = s.lower()
    hrp, data = s.rsplit("1", 1)
    vals = [_B32.index(c) for c in data]
    assert _polymod(_hrp_expand(hrp) + vals) == 1, "bad bech32 checksum"
    return hrp, bytes(_convertbits(vals[:-6], 5, 8, False))


def bech32_encode(hrp: str, data: bytes) -> str:
    vals = _convertbits(data, 8, 5)
    chk = _polymod(_hrp_expand(hrp) + vals + [0] * 6) ^ 1
    return hrp + "1" + "".join(_B32[v] for v in vals + [(chk >> 5 * (5 - i)) & 31 for i in range(6)])


def parse_key(s: str) -> bytes:
    s = s.strip()
    if s.startswith("nsec1"):
        hrp, raw = bech32_decode(s)
        assert hrp == "nsec" and len(raw) == 32
        return raw
    return bytes.fromhex(s)


# ---- NIP-OA ----------------------------------------------------------------------------
def auth_tag(owner_sec: bytes, agent_pub: bytes, conditions: str = "") -> str:
    preimage = f"nostr:agent-auth:{agent_pub.hex()}:{conditions}".encode()
    sig = schnorr_sign(hashlib.sha256(preimage).digest(), owner_sec)
    return json.dumps(["auth", pubkey(owner_sec).hex(), conditions, sig.hex()], separators=(",", ":"))


# ---- the command -----------------------------------------------------------------------
CFG = os.path.expanduser("~/.config/buzz-acp")


def _env_file(path: str) -> dict:
    out = {}
    for line in open(path):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k.removeprefix("export ").strip()] = v.strip().strip('"').strip("'")
    return out


def _buzz(env: dict, *args: str) -> dict:
    merged = {k: v for k, v in {**os.environ, **env}.items() if v != ""}  # "" unsets (BUZZ_AUTH_TAG)
    r = subprocess.run(["buzz", *args], env=merged, capture_output=True, text=True)
    if r.returncode:
        raise SystemExit(f"buzz {' '.join(args[:2])} failed: {r.stderr.strip() or r.stdout.strip()}")
    return json.loads(r.stdout) if r.stdout.strip().startswith(("{", "[")) else {}


def mint(name: str, channels: list[str], about: str | None) -> str:
    owner = _env_file(os.path.join(CFG, "owner.env"))
    owner_sec = parse_key(owner["BUZZ_OWNER_NSEC"])
    relay = owner.get("BUZZ_RELAY_URL") or _env_file(os.path.join(CFG, "agent.env"))["BUZZ_RELAY_URL"]
    agent_sec = secrets.token_bytes(32)
    agent_pub = pubkey(agent_sec)
    env = {
        "BUZZ_PRIVATE_KEY": agent_sec.hex(),
        "BUZZ_PUBLIC_KEY": agent_pub.hex(),
        "BUZZ_AUTH_TAG": auth_tag(owner_sec, agent_pub),
        "BUZZ_RELAY_URL": relay,
    }
    _buzz(env, "users", "set-profile", "--name", name, *(["--about", about] if about else []))
    # Owner adds the agent with role `bot`: that role is what Buzz's UIs key "agent" features on.
    owner_env = {"BUZZ_PRIVATE_KEY": owner_sec.hex(), "BUZZ_RELAY_URL": relay, "BUZZ_AUTH_TAG": ""}
    for ch in channels:
        _buzz(owner_env, "channels", "add-member", "--channel", ch, "--pubkey", agent_pub.hex(), "--role", "bot")
    os.makedirs(os.path.join(CFG, "agents"), exist_ok=True)
    path = os.path.join(CFG, "agents", f"{name}.env")
    with open(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        for k, v in env.items():
            f.write(f"{k}='{v}'\n")
    return path


def _selfcheck() -> None:
    owner = (1).to_bytes(32, "big")
    agent_pub = pubkey((2).to_bytes(32, "big"))
    assert pubkey(owner).hex() == "79be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798"
    assert agent_pub.hex() == "c6047f9441ed7d6d3045406e95c07cd85c778e4b8cef3ca7abac09b95c709ee5"
    tag = json.loads(auth_tag(owner, agent_pub, "kind=1&created_at<1713957000"))
    msg = hashlib.sha256(b"nostr:agent-auth:" + agent_pub.hex().encode() + b":kind=1&created_at<1713957000").digest()
    assert msg.hex() == "08cdecd55af4c28d3801fd69615dcf5cc04fab3bc134b38a840bf157197069a6"
    vec = "8b7df2575caf0a108374f8471722b233c53f9ff827a8b0f91861966c3b9dd5cb2e189eae9f49d72187674c2f5bd244145e10ff86c9f257ffe65a1ee5f108b369"
    assert schnorr_verify(msg, pubkey(owner), bytes.fromhex(vec)), "spec vector sig must verify"
    assert schnorr_verify(msg, pubkey(owner), bytes.fromhex(tag[3])), "our sig must verify"
    assert bech32_decode(bech32_encode("nsec", owner))[1] == owner
    if os.path.exists(os.path.join(CFG, "owner.env")):
        sec = parse_key(_env_file(os.path.join(CFG, "owner.env"))["BUZZ_OWNER_NSEC"])
        print("owner pubkey:", pubkey(sec).hex())
    print("mint ok")


def main() -> None:
    ap = argparse.ArgumentParser(prog="herdr-acp mint")
    ap.add_argument("--name")
    ap.add_argument("--channel", action="append", default=[], help="channel id to join (repeatable)")
    ap.add_argument("--about")
    ap.add_argument("--selfcheck", action="store_true")
    a = ap.parse_args()
    if a.selfcheck:
        return _selfcheck()
    if not a.name:
        ap.error("--name required")
    print(mint(a.name, a.channel, a.about))


if __name__ == "__main__":
    main()
