# -*- coding: utf-8 -*-
"""
Rapport automatique or / argent poste sur Discord (GitHub Actions).
Les horaires sont en heure de Paris : ete comme hiver, seul le bon creneau envoie.
"""
import os
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools"))
import market  # noqa: E402
import discord_api as dc  # noqa: E402

SLOTS = {"07:45": "☀️ Rapport avant Londres", "14:00": "🇺🇸 Rapport avant New York"}
NEWS_RSS = ("https://news.google.com/rss/search?q=gold+price+OR+%22federal+reserve%22+OR+xauusd+when:1d"
            "&hl=en-US&gl=US&ceid=US:en")


def slot_from_schedule(sched):
    """'45 5 * * 1-5' (UTC) -> '07:45' si c'est bien 7h45 a Paris aujourd'hui, sinon None."""
    if not sched:
        return "manuel"
    mi, hr = sched.split()[:2]
    now = datetime.now(timezone.utc)
    t = now.replace(hour=int(hr), minute=int(mi), second=0, microsecond=0).astimezone(market.PARIS)
    hm = t.strftime("%H:%M")
    return hm if hm in SLOTS else None


def headlines(n=4):
    try:
        import requests
        r = requests.get(NEWS_RSS, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        items = ET.fromstring(r.content).findall(".//item")[:n]
        out = []
        for it in items:
            title = (it.findtext("title") or "").strip()
            src = (it.findtext("source") or "").strip()
            if src and title.endswith(" - " + src):
                title = title[: -len(src) - 3]
            out.append("• %s%s" % (title[:140], (" (%s)" % src) if src else ""))
        return out
    except Exception as e:
        return ["• titres indisponibles (%s)" % str(e)[:60]]


def tf(o, name):
    for t in o.get("timeframes", []):
        if t["timeframe"] == name:
            return t
    return {}


def violence(o, mac, cal):
    """Signale ce qui a 'bouscule' le marche."""
    w = []
    v = o.get("variation_jour_pct")
    if v is not None and abs(v) >= 1.5:
        w.append("Or %+.1f %% depuis la cloture d'hier : mouvement fort" % v)
    c = o.get("atr_jour_consomme_pct")
    if c is not None and c >= 70:
        w.append("Déjà %d %% de l'amplitude moyenne journalière consommée" % c)
    vix = mac.get("VIX", {})
    if vix.get("var_jour_pct") is not None and vix["var_jour_pct"] >= 10:
        w.append("VIX %+.0f %% : stress sur les marchés" % vix["var_jour_pct"])
    dxy = mac.get("DXY", {})
    if dxy.get("var_jour_pct") is not None and abs(dxy["var_jour_pct"]) >= 0.6:
        w.append("Dollar (DXY) %+.2f %% : mouvement inhabituel" % dxy["var_jour_pct"])
    today = market.now_paris().strftime("%d/%m")
    fortes = [e for e in cal.get("evenements_usd", []) if e["impact"] == "FORT" and today in e["heure_paris"]
              and e["dans_minutes"] >= -60]
    for e in fortes:
        w.append("Annonce FORTE %s : %s" % (e["heure_paris"][-5:], e["evenement"]))
    if not o.get("donnee_fraiche", True):
        w.append("Données de prix en retard : prudence")
    return w


def build(slot):
    o = market.metal("gold")
    s = market.metal("silver")
    mac = market.macro()
    cal = market.calendar(hours=30)
    n = market.now_paris()
    title = SLOTS.get(slot, "📊 Rapport (manuel)")
    if "erreur" in o:
        return "%s — %s\n⚠️ Données or indisponibles : %s" % (title, n.strftime("%d/%m %H:%M"), o["erreur"])
    lv = o["niveaux"]
    d1, h4, h1 = tf(o, "D1"), tf(o, "H4"), tf(o, "H1")
    L = ["**%s — %s**" % (title, n.strftime("%a %d/%m %H:%M")), ""]
    warn = violence(o, mac, cal)
    if warn:
        L.append("⚠️ **À savoir**")
        L += ["• " + x for x in warn]
        L.append("")
    L.append("**Or** %.1f (%+.2f %%) · données %s · future COMEX, ajoute ton écart broker" % (
        o["dernier"], o.get("variation_jour_pct") or 0, o["heure_donnee"][-9:-4]))
    if "erreur" not in s:
        L.append("**Argent** %.2f (%+.2f %%) · ratio or/argent %.1f" % (
            s["dernier"], s.get("variation_jour_pct") or 0, o["dernier"] / s["dernier"]))
    L.append("")
    L.append("**Structure**")
    L.append("• D1 : %s · RSI %s · %s" % (d1.get("structure", {}).get("tendance"), d1.get("rsi14"), d1.get("position_vs_ema")))
    L.append("• H4 : %s · ATR %s" % (h4.get("structure", {}).get("tendance"), h4.get("atr14")))
    L.append("• H1 : %s · ATR %s" % (h1.get("structure", {}).get("tendance"), h1.get("atr14")))
    L.append("")
    L.append("**Niveaux clés (future)**")
    order = [("semaine_haut", "Haut semaine"), ("veille_haut", "Haut veille"), ("asie_haut", "Haut Asie"),
             ("londres_haut", "Haut Londres"), ("londres_bas", "Bas Londres"), ("asie_bas", "Bas Asie"),
             ("veille_bas", "Bas veille"), ("semaine_bas", "Bas semaine")]
    pts = [(lv[k], nom) for k, nom in order if lv.get(k) is not None]
    pts.append((o["dernier"], "◀ PRIX"))
    seen = set()
    for p, nom in sorted(pts, key=lambda x: -x[0]):
        if (p, nom) in seen:
            continue
        seen.add((p, nom))
        L.append(("• **%.1f  %s**" if nom == "◀ PRIX" else "• %.1f  %s") % (p, nom))
    for k, nom in (("sommets_egaux_M15", "Sommets égaux (liquidité)"), ("creux_egaux_M15", "Creux égaux (liquidité)")):
        if lv.get(k):
            L.append("• %s : %s" % (nom, ", ".join("%.1f" % x for x in lv[k])))
    L.append("")
    L.append("**Macro** : " + (" · ".join(mac.get("lecture_or", [])) or "indisponible"))
    L.append("")
    ev = [e for e in cal.get("evenements_usd", []) if 0 <= e["dans_minutes"] <= 30 * 60]
    L.append("**Annonces USD (30 h)**" + (" — %s" % cal["source"] if cal.get("source") else ""))
    if cal.get("erreur"):
        L.append("• " + cal["erreur"][:100])
    elif not ev:
        L.append("• aucune annonce moyenne/forte")
    for e in ev[:6]:
        L.append("• %s %s %s%s" % (e["heure_paris"][-11:], "🔴" if e["impact"] == "FORT" else "🟠", e["evenement"],
                                  (" (prév. %s)" % e["prevision"]) if e.get("prevision") else ""))
    L.append("")
    L.append("**Titres des dernières 24 h**")
    L += headlines()
    L.append("")
    L.append("_Données Yahoo (~10 min de retard). Pour un plan : tape « prépa » au bot quand ton PC est allumé._")
    return "\n".join(L)


def main():
    sched = os.environ.get("SCHEDULE", "").strip()
    slot = slot_from_schedule(sched)
    if slot is None:
        print("Pas le bon creneau pour Paris (heure d'ete/hiver), on passe.")
        return
    if slot != "manuel" and market.now_paris().weekday() >= 5:
        return
    text = build(slot)
    ch = os.environ.get("REPORT_CHANNEL_ID") or os.environ.get("ALERT_CHANNEL_ID")
    if "--print" in sys.argv or not ch:
        print(text)
        return
    dc.send(ch.strip(), text)
    print("Rapport envoye (%d caracteres)" % len(text))


if __name__ == "__main__":
    main()
