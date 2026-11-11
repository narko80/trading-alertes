# -*- coding: utf-8 -*-
"""Envoi / lecture de messages Discord via l'API REST (token du bot, secrets GitHub)."""
import os
import time
import requests

API = "https://discord.com/api/v10"


def _headers():
    tok = os.environ.get("DISCORD_TOKEN", "").strip()
    if not tok:
        raise SystemExit("Secret DISCORD_TOKEN manquant")
    return {"Authorization": "Bot " + tok, "Content-Type": "application/json",
            "User-Agent": "TradingCockpit (github-actions, 1.0)"}


def _req(method, url, **kw):
    for _ in range(4):
        r = requests.request(method, url, headers=_headers(), timeout=20, **kw)
        if r.status_code == 429:
            time.sleep(float(r.json().get("retry_after", 2)) + 0.5)
            continue
        if r.status_code >= 400:
            raise SystemExit("Discord %s %s -> %s %s" % (method, url, r.status_code, r.text[:300]))
        return r.json() if r.text else None
    raise SystemExit("Discord : trop de requetes")


def send(channel_id, text, ping_user=None):
    """Envoie text (decoupe en morceaux de < 2000 caracteres)."""
    if ping_user:
        text = "<@%s> " % ping_user + text
    chunks, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > 1900:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        chunks.append(cur)
    for i, c in enumerate(chunks):
        _req("POST", "%s/channels/%s/messages" % (API, channel_id), json={
            "content": c,
            "allowed_mentions": {"users": [ping_user] if (ping_user and i == 0) else []},
        })


def recent_messages(channel_id, limit=100):
    return _req("GET", "%s/channels/%s/messages" % (API, channel_id), params={"limit": limit}) or []


def me():
    return _req("GET", API + "/users/@me")
