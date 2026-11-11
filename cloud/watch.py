# -*- coding: utf-8 -*-
"""
Surveillant d'alertes (tourne sur GitHub Actions toutes les ~5-15 min).

Lit les lignes d'alerte postees par TOI ou par TON BOT dans les salons configures :
    ALERTE XAU 4053-4061
    ALERTE XAU 4053-4061 ecart -6.1 (vente)
    ALERTE XAG 31.2-31.4
    ALERTE XAU 4100              (un seul prix)
'ecart' = prix broker - prix future (donne par le bot). Sans ecart : prix du future.
Une alerte vit 24 h. Pour l'annuler : supprime le message Discord.
Quand la zone est touchee : message dans le salon d'alertes avec un ping.
"""
import os
import re
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools"))
import market  # noqa: E402
import discord_api as dc  # noqa: E402

PAT = re.compile(
    r"ALERTE\s+(XAU|XAG|OR|ARGENT|GOLD|SILVER)\w*\s+(\d+(?:[.,]\d+)?)(?:\s*(?:-|–|à|a|/)\s*(\d+(?:[.,]\d+)?))?"
    r"(?:[^\n]*?[ée]cart\s*([+-]?\s*\d+(?:[.,]\d+)?))?([^\n]*)",
    re.IGNORECASE,
)
SYMBOLS = {"XAU": "GC=F", "OR": "GC=F", "GOLD": "GC=F", "XAG": "SI=F", "ARGENT": "SI=F", "SILVER": "SI=F"}
NAMES = {"GC=F": "XAUUSD", "SI=F": "XAGUSD"}
TTL_HOURS = 24


def num(s):
    return float(s.replace(" ", "").replace(",", "."))


def parse_alerts(messages, allowed_authors, max_age_h=TTL_HOURS):
    now = datetime.now(timezone.utc)
    out = []
    for m in messages:
        if m.get("author", {}).get("id") not in allowed_authors:
            continue
        ts = datetime.fromisoformat(m["timestamp"].replace("Z", "+00:00"))
        if now - ts > timedelta(hours=max_age_h):
            continue
        for k, mt in enumerate(PAT.finditer(m.get("content", ""))):
            a, b = num(mt.group(2)), num(mt.group(3)) if mt.group(3) else None
            lo, hi = (min(a, b), max(a, b)) if b is not None else (a, a)
            ecart = num(mt.group(4)) if mt.group(4) else 0.0
            note = (mt.group(5) or "").strip(" *_`|-")[:60]
            out.append({
                "key": "%s-%d" % (m["id"], k), "symbol": SYMBOLS[mt.group(1).upper()],
                "lo": lo, "hi": hi, "ecart": ecart, "note": note, "created": ts,
            })
    return out


def already_done(alert_msgs, bot_id):
    done = set()
    for m in alert_msgs:
        if m.get("author", {}).get("id") == bot_id:
            done.update(re.findall(r"\[id:([\d-]+)\]", m.get("content", "")))
    return done


def check(alert, bars):
    """Premiere bougie 5 min apres la creation qui touche la zone (prix broker estime)."""
    b = bars[bars.index > alert["created"].astimezone(bars.index.tz)]
    if b is None or b.empty:
        return None
    hi = b["High"] + alert["ecart"]
    lo = b["Low"] + alert["ecart"]
    hit = b[(hi >= alert["lo"]) & (lo <= alert["hi"])]
    if hit.empty:
        return None
    t = hit.index[0]
    return {"t": t, "last": float(b["Close"].iloc[-1]) + alert["ecart"]}


def main():
    if not market.market_open_now() and "--force" not in sys.argv:
        print("Marche ferme, rien a surveiller.")
        return
    user = os.environ.get("DISCORD_USER_ID", "").strip()
    alert_ch = os.environ.get("ALERT_CHANNEL_ID", "").strip()
    scan = [c.strip() for c in (os.environ.get("CHAT_CHANNEL_ID", "") + "," + alert_ch).split(",") if c.strip()]
    if not (user and alert_ch):
        raise SystemExit("Secrets DISCORD_USER_ID et ALERT_CHANNEL_ID obligatoires")
    bot_id = dc.me()["id"]
    msgs, alert_msgs = [], []
    for ch in dict.fromkeys(scan):
        lst = dc.recent_messages(ch, 100)
        msgs += lst
        if ch == alert_ch:
            alert_msgs = lst
    alerts = parse_alerts(msgs, {user, bot_id})
    done = already_done(alert_msgs, bot_id)
    alerts = [a for a in alerts if a["key"] not in done]
    print("%d alerte(s) active(s)" % len(alerts))
    bars = {}
    for a in alerts:
        if a["symbol"] not in bars:
            bars[a["symbol"]] = market.fetch(a["symbol"], "5m", "2d")
        if bars[a["symbol"]] is None:
            continue
        r = check(a, bars[a["symbol"]])
        if not r:
            continue
        zone = ("%g" % a["lo"]) if a["lo"] == a["hi"] else ("%g – %g" % (a["lo"], a["hi"]))
        src = "prix broker estimé" if a["ecart"] else "prix future COMEX"
        txt = ("🔔 **%s a touché ta zone %s**%s\n"
               "Touchée vers %s · prix actuel ~%.2f (%s)\n"
               "Alerte posée le %s · vérifie sur MT5 avant d'agir.  [id:%s]") % (
            NAMES[a["symbol"]], zone, (" — " + a["note"]) if a["note"] else "",
            r["t"].astimezone(market.PARIS).strftime("%H:%M"), r["last"], src,
            a["created"].astimezone(market.PARIS).strftime("%d/%m %H:%M"), a["key"])
        dc.send(alert_ch, txt, ping_user=user)
        print("Alerte envoyee :", a["key"])


if __name__ == "__main__":
    main()
