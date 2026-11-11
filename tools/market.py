#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
market.py : donnees de marche en LECTURE SEULE pour Claude Code.

Aucun acces broker, aucun ordre, aucun identifiant. Sources publiques :
  - Yahoo Finance (via yfinance) : or GC=F, argent SI=F, DXY, US10Y, VIX
  - Calendrier economique ForexFactory (JSON public de la semaine)

Usage :
  python tools/market.py snapshot                 # tout (or, argent, macro, calendrier)
  python tools/market.py snapshot --xau 2651.20   # + aligne les niveaux sur le prix de TON broker
  python tools/market.py gold | silver | macro | calendar
"""
import argparse
import json
import sys
import warnings
warnings.filterwarnings("ignore")
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
    PARIS = ZoneInfo("Europe/Paris")
except Exception:  # Windows sans tzdata
    PARIS = timezone(timedelta(hours=1))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

try:
    import pandas as pd
    import numpy as np
except ImportError:
    print(json.dumps({"erreur": "pandas manquant : pip install -r tools/requirements.txt"}))
    sys.exit(1)

METALS = {"gold": ("GC=F", "Or (future COMEX GC=F)"), "silver": ("SI=F", "Argent (future COMEX SI=F)")}
MACRO = {
    "DXY": "DX-Y.NYB",
    "US10Y": "^TNX",
    "VIX": "^VIX",
    "SP500_fut": "ES=F",
}
CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
STALE_MINUTES = 45


# ----------------------------------------------------------------- utilitaires
def now_paris():
    return datetime.now(PARIS)


def fmt_ts(ts):
    if ts is None:
        return None
    if getattr(ts, "tzinfo", None) is None:
        ts = ts.tz_localize("UTC") if hasattr(ts, "tz_localize") else ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(PARIS).strftime("%Y-%m-%d %H:%M %Z")


def rnd(x, n=2):
    try:
        if x is None or (isinstance(x, float) and np.isnan(x)):
            return None
        return round(float(x), n)
    except Exception:
        return None


def market_open_now():
    """Or/argent COMEX : ferme du vendredi ~23h au dimanche ~23h (Paris), pause quotidienne ~23h-00h."""
    n = now_paris()
    wd, h = n.weekday(), n.hour
    if wd == 5:
        return False
    if wd == 4 and h >= 23:
        return False
    if wd == 6 and h < 23:
        return False
    return True


def fetch(symbol, interval, period):
    import yfinance as yf
    df = yf.Ticker(symbol).history(interval=interval, period=period, auto_adjust=False, prepost=True)
    if df is None or df.empty:
        return None
    df = df[["Open", "High", "Low", "Close", "Volume"]].dropna(subset=["Close"])
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    df.index = df.index.tz_convert(PARIS)
    return df


def resample(df, rule):
    agg = {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}
    return df.resample(rule, label="left", closed="left").agg(agg).dropna(subset=["Close"])


# ----------------------------------------------------------------- indicateurs
def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def rsi(s, n=14):
    d = s.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = up / dn.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def atr(df, n=14):
    pc = df["Close"].shift()
    tr = pd.concat([df["High"] - df["Low"], (df["High"] - pc).abs(), (df["Low"] - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def swings(df, k=3):
    """Pivots confirmes : plus haut/bas que les k bougies de chaque cote."""
    h, l = df["High"].values, df["Low"].values
    highs, lows = [], []
    for i in range(k, len(df) - k):
        if h[i] == max(h[i - k:i + k + 1]):
            highs.append((df.index[i], h[i]))
        if l[i] == min(l[i - k:i + k + 1]):
            lows.append((df.index[i], l[i]))
    return highs, lows


def structure(df, k=3):
    highs, lows = swings(df, k)
    out = {
        "derniers_sommets": [{"t": fmt_ts(t), "prix": rnd(p)} for t, p in highs[-3:]],
        "derniers_creux": [{"t": fmt_ts(t), "prix": rnd(p)} for t, p in lows[-3:]],
        "tendance": "indeterminee",
        "cassure": None,
    }
    if len(highs) >= 2 and len(lows) >= 2:
        hh = highs[-1][1] > highs[-2][1]
        hl = lows[-1][1] > lows[-2][1]
        if hh and hl:
            out["tendance"] = "haussiere (HH + HL)"
        elif not hh and not hl:
            out["tendance"] = "baissiere (LH + LL)"
        else:
            out["tendance"] = "range / transition (" + ("HH" if hh else "LH") + " + " + ("HL" if hl else "LL") + ")"
        last = df["Close"].iloc[-1]
        if last > highs[-1][1]:
            out["cassure"] = "cloture au-dessus du dernier sommet " + str(rnd(highs[-1][1]))
        elif last < lows[-1][1]:
            out["cassure"] = "cloture sous le dernier creux " + str(rnd(lows[-1][1]))
    return out


def fvgs(df, max_items=3):
    """Fair value gaps non combles (3 bougies)."""
    res = []
    hi, lo = df["High"].values, df["Low"].values
    for i in range(2, len(df)):
        if lo[i] > hi[i - 2]:
            zone = (hi[i - 2], lo[i])
            if df["Low"].iloc[i + 1:].min() > zone[0] if i + 1 < len(df) else True:
                res.append({"type": "haussier", "de": rnd(zone[0]), "a": rnd(zone[1]), "t": fmt_ts(df.index[i - 1])})
        elif hi[i] < lo[i - 2]:
            zone = (hi[i], lo[i - 2])
            if df["High"].iloc[i + 1:].max() < zone[1] if i + 1 < len(df) else True:
                res.append({"type": "baissier", "de": rnd(zone[0]), "a": rnd(zone[1]), "t": fmt_ts(df.index[i - 1])})
    return res[-max_items:]


def tf_summary(df, name, k=3):
    c = df["Close"]
    last = c.iloc[-1]
    e20, e50, e200 = ema(c, 20).iloc[-1], ema(c, 50).iloc[-1], ema(c, 200).iloc[-1] if len(c) >= 200 else np.nan
    return {
        "timeframe": name,
        "bougies": len(df),
        "derniere_bougie": fmt_ts(df.index[-1]),
        "cloture": rnd(last),
        "ema20": rnd(e20), "ema50": rnd(e50), "ema200": rnd(e200),
        "position_vs_ema": ("au-dessus" if last > e50 else "en-dessous") + " EMA50",
        "rsi14": rnd(rsi(c).iloc[-1], 1),
        "atr14": rnd(atr(df).iloc[-1]),
        "structure": structure(df, k),
        "fvg_non_combles": fvgs(df.tail(120)),
    }


# ----------------------------------------------------------------- niveaux
def key_levels(m15, d1):
    lv = {}
    if d1 is not None and len(d1) >= 2:
        today = now_paris().date()
        past = d1[d1.index.date < today] if (d1.index.date == today).any() else d1.iloc[:-1]
        if len(past):
            p = past.iloc[-1]
            lv["veille_haut"] = rnd(p["High"]); lv["veille_bas"] = rnd(p["Low"]); lv["veille_cloture"] = rnd(p["Close"])
        n0 = now_paris()
        week_start = pd.Timestamp((n0 - timedelta(days=n0.weekday())).replace(hour=0, minute=0, second=0, microsecond=0))
        wk = d1[d1.index >= week_start]
        if len(wk):
            lv["semaine_haut"] = rnd(wk["High"].max()); lv["semaine_bas"] = rnd(wk["Low"].min())
        prevwk = d1[d1.index < week_start].tail(5)
        if len(prevwk):
            lv["sem_prec_haut"] = rnd(prevwk["High"].max()); lv["sem_prec_bas"] = rnd(prevwk["Low"].min())
    if m15 is not None and len(m15):
        day = m15[m15.index.date == m15.index[-1].date()]
        asia = day[(day.index.hour >= 1) & (day.index.hour < 8)]          # ~ Tokyo, heure de Paris
        london = day[(day.index.hour >= 8) & (day.index.hour < 13)]
        ny = day[(day.index.hour >= 14) & (day.index.hour < 22)]
        for nom, s in (("asie", asia), ("londres", london), ("new_york", ny)):
            if len(s):
                lv[nom + "_haut"] = rnd(s["High"].max()); lv[nom + "_bas"] = rnd(s["Low"].min())
        lv["jour_ouverture"] = rnd(day["Open"].iloc[0]) if len(day) else None
        # sommets / creux egaux (liquidite) sur les dernieres 48h en M15
        recent = m15.tail(192)
        hs, ls = swings(recent, 2)
        tol = float(atr(recent).iloc[-1]) * 0.15 if len(recent) > 20 else 0.5
        eqh = [rnd(a[1]) for i, a in enumerate(hs) for b in hs[i + 1:] if abs(a[1] - b[1]) <= tol]
        eql = [rnd(a[1]) for i, a in enumerate(ls) for b in ls[i + 1:] if abs(a[1] - b[1]) <= tol]
        if eqh: lv["sommets_egaux_M15"] = sorted(set(eqh))[-3:]
        if eql: lv["creux_egaux_M15"] = sorted(set(eql))[:3]
    return lv


def session_now():
    h = now_paris().hour
    if 1 <= h < 8: return "Asie"
    if 8 <= h < 14: return "Londres" + (" (ouverture 8h-10h)" if h < 10 else "")
    if 14 <= h < 17: return "Overlap Londres / New York (forte liquidite)"
    if 17 <= h < 22: return "New York apres-midi"
    return "Faible liquidite (fin NY / rollover)"


def apply_offset(obj, off):
    """Ajoute l'ecart broker a tous les prix (cles de prix connues)."""
    PRICE_KEYS = {"prix", "cloture", "ema20", "ema50", "ema200", "de", "a"}
    if isinstance(obj, dict):
        return {k: (rnd(v + off) if (isinstance(v, (int, float)) and v is not None and
                                     (k in PRICE_KEYS or k.endswith("_haut") or k.endswith("_bas")
                                      or k.startswith("veille_") or k == "jour_ouverture" or k == "dernier"))
                    else apply_offset(v, off)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [rnd(x + off) if isinstance(x, (int, float)) else apply_offset(x, off) for x in obj]
    return obj


# ----------------------------------------------------------------- blocs
def metal(key, broker_price=None):
    sym, label = METALS[key]
    out = {"instrument": label, "symbole": sym}
    try:
        m15 = fetch(sym, "15m", "5d")
        h1 = fetch(sym, "60m", "60d")
        d1 = fetch(sym, "1d", "1y")
    except Exception as e:
        return {"instrument": label, "erreur": "telechargement impossible : " + str(e)}
    if m15 is None or h1 is None or d1 is None:
        return {"instrument": label, "erreur": "aucune donnee recue de Yahoo"}

    last_ts = m15.index[-1]
    age = (now_paris() - last_ts.to_pydatetime()).total_seconds() / 60
    last = float(m15["Close"].iloc[-1])
    out["dernier"] = rnd(last)
    out["heure_donnee"] = fmt_ts(last_ts)
    out["age_minutes"] = int(age)
    out["marche_ouvert"] = market_open_now()
    out["donnee_fraiche"] = bool(age <= STALE_MINUTES or not out["marche_ouvert"])
    if not out["donnee_fraiche"]:
        out["ALERTE"] = "Donnee de plus de %d min alors que le marche est ouvert : ne pas donner de niveau d'entree precis." % int(age)
    prev_close = float(d1["Close"].iloc[-2]) if len(d1) >= 2 else None
    out["variation_jour_pct"] = rnd((last / prev_close - 1) * 100) if prev_close else None
    h4 = resample(h1, "4h")
    out["timeframes"] = [tf_summary(d1, "D1", 3), tf_summary(h4, "H4", 2), tf_summary(h1, "H1", 3), tf_summary(m15, "M15", 3)]
    out["niveaux"] = key_levels(m15, d1)
    out["atr_jour_consomme_pct"] = None
    try:
        today = m15[m15.index.date == m15.index[-1].date()]
        adr = float(atr(d1).iloc[-2])
        out["atr_jour_consomme_pct"] = rnd((today["High"].max() - today["Low"].min()) / adr * 100, 0)
    except Exception:
        pass

    if broker_price:
        off = float(broker_price) - last
        out = apply_offset(out, off)
        out["ecart_broker_applique"] = rnd(off)
        out["note_prix"] = "Niveaux convertis au prix spot de ton broker (ecart %+.2f). " % off
    else:
        out["note_prix"] = ("Prix du FUTURE COMEX, pas le spot de ton broker : l'ecart est en general de "
                            "quelques dollars. Donne ton prix MT5 (--xau / --xag) pour aligner les niveaux.")
    return out


def macro():
    out = {}
    for name, sym in MACRO.items():
        try:
            d = fetch(sym, "1d", "1mo")
            h = fetch(sym, "60m", "5d")
            if d is None:
                out[name] = {"erreur": "pas de donnee"}
                continue
            last = float((h if h is not None else d)["Close"].iloc[-1])
            prev = float(d["Close"].iloc[-2])
            wk = float(d["Close"].iloc[-6]) if len(d) >= 6 else None
            out[name] = {
                "dernier": rnd(last, 3),
                "heure_donnee": fmt_ts((h if h is not None else d).index[-1]),
                "var_jour_pct": rnd((last / prev - 1) * 100),
                "var_5j_pct": rnd((last / wk - 1) * 100) if wk else None,
            }
            age_h = (now_paris() - (h if h is not None else d).index[-1].to_pydatetime()).total_seconds() / 3600
            if age_h > 3:
                out[name]["note"] = "derniere cotation il y a %d h (marche US ferme) : variation = celle de la derniere seance" % int(age_h)
            if name == "US10Y":
                out[name]["var_jour_pb"] = rnd((last - prev) * 100, 1)
        except Exception as e:
            out[name] = {"erreur": str(e)}
    # lecture simple pour l'or
    lect = []
    try:
        if out["DXY"]["var_jour_pct"] is not None:
            lect.append("dollar " + ("en baisse -> soutien or" if out["DXY"]["var_jour_pct"] < -0.15 else
                                     "en hausse -> pression sur l'or" if out["DXY"]["var_jour_pct"] > 0.15 else "stable"))
        if out["US10Y"].get("var_jour_pb") is not None:
            pb = out["US10Y"]["var_jour_pb"]
            lect.append("taux 10 ans " + ("en baisse -> soutien or" if pb < -3 else "en hausse -> pression sur l'or" if pb > 3 else "stables"))
        if out["VIX"]["dernier"] is not None:
            lect.append("VIX %.1f %s" % (out["VIX"]["dernier"], "(stress, demande refuge)" if out["VIX"]["dernier"] > 22 else ""))
    except Exception:
        pass
    out["lecture_or"] = lect
    return out


def calendar(hours=48):
    import os, time
    cache = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache_calendrier.json")
    events, source = None, "en ligne"
    try:
        if os.path.exists(cache) and time.time() - os.path.getmtime(cache) < 3600:
            with open(cache, encoding="utf-8") as f:
                events = json.load(f)
            source = "cache local (< 1 h)"
    except Exception:
        events = None
    if events is None:
        try:
            import requests
            r = requests.get(CALENDAR_URL, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            events = r.json()
            with open(cache, "w", encoding="utf-8") as f:
                json.dump(events, f)
        except Exception as e:
            if os.path.exists(cache):
                with open(cache, encoding="utf-8") as f:
                    events = json.load(f)
                age = int((time.time() - os.path.getmtime(cache)) / 3600)
                source = "cache local (%d h, site indisponible)" % age
            else:
                return {"erreur": "calendrier indisponible : " + str(e)[:120],
                        "conseil": "verifier forexfactory.com avant toute annonce US (reessayer dans 5 min)"}
    n = now_paris()
    res = []
    for ev in events:
        if ev.get("country") != "USD" or ev.get("impact") not in ("High", "Medium"):
            continue
        try:
            t = datetime.fromisoformat(ev["date"]).astimezone(PARIS)
        except Exception:
            continue
        if n - timedelta(hours=6) <= t <= n + timedelta(hours=hours):
            res.append({
                "heure_paris": t.strftime("%a %d/%m %H:%M"),
                "dans_minutes": int((t - n).total_seconds() / 60),
                "impact": "FORT" if ev["impact"] == "High" else "moyen",
                "evenement": ev.get("title"),
                "prevision": ev.get("forecast") or None,
                "precedent": ev.get("previous") or None,
            })
    res.sort(key=lambda x: x["dans_minutes"])
    proche = [e for e in res if e["impact"] == "FORT" and -15 <= e["dans_minutes"] <= 60]
    return {
        "source": source,
        "evenements_usd": res,
        "ALERTE_annonce": ("Annonce FORTE dans moins d'1h : " + ", ".join(e["evenement"] for e in proche)) if proche else None,
    }


def main():
    ap = argparse.ArgumentParser(description="Donnees marche lecture seule (or/argent)")
    ap.add_argument("cmd", nargs="?", default="snapshot", choices=["snapshot", "gold", "silver", "macro", "calendar"])
    ap.add_argument("--xau", type=float, help="prix XAUUSD actuel de ton broker, pour aligner les niveaux")
    ap.add_argument("--xag", type=float, help="prix XAGUSD actuel de ton broker")
    a = ap.parse_args()

    try:
        import yfinance  # noqa: F401
    except ImportError:
        print(json.dumps({"erreur": "yfinance manquant : pip install -r tools/requirements.txt"}, ensure_ascii=False))
        sys.exit(1)

    out = {"genere_a": now_paris().strftime("%Y-%m-%d %H:%M %Z"), "session_actuelle": session_now()}
    if a.cmd in ("snapshot", "gold"):
        out["or"] = metal("gold", a.xau)
    if a.cmd in ("snapshot", "silver"):
        out["argent"] = metal("silver", a.xag)
    if a.cmd in ("snapshot", "macro", "gold"):
        out["macro"] = macro()
    if a.cmd in ("snapshot", "calendar", "gold", "silver"):
        out["calendrier"] = calendar()
    print(json.dumps(out, ensure_ascii=False, indent=1, default=str))


if __name__ == "__main__":
    main()
