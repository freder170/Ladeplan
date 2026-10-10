#!/usr/bin/env python3
"""
Ladeplan – styring af Zaptec efter DK1-priser og Ioniq 5's batteri-%.

Kører hvert kvarter i GitHub Actions:
  1. Henter spotpriser (DK1) og TREFOR El-nets tarif fra Energi Data Service
  2. Læser plan.json (ugeplan, engangsmål, manuel ladning, indstillinger) fra appen
  3. Læser batteri-% fra bilen via Bluelink (hvis login er sat op)
  4. Vælger de billigste timer frem mod deadline
  5. Tænder/slukker Zaptec, hvis tilstanden skal ændres
  6. Skriver state.json, som appen viser

Alle logins kommer fra miljøvariabler (GitHub Secrets) – aldrig fra filer.
"""
import json, os, sys, math, datetime as dt
from zoneinfo import ZoneInfo
import requests

TZ = ZoneInfo("Europe/Copenhagen")
NOW = dt.datetime.now(TZ)
EDS = "https://api.energidataservice.dk/dataset/"
log = lambda *a: print(f"[{NOW:%H:%M}]", *a, flush=True)


# ---------------------------------------------------------------- plan.json
def load_plan():
    default = {
        "plan": [{"time": "07:00" if i < 5 else "10:00", "pct": 80, "on": True} for i in range(7)],
        "override": None, "manual": None, "soc_manual": None,
        "settings": {"battery": 84, "power": 11, "loss": 10, "gasel": 0.06, "transport": 0.125, "tax": 0.73},
    }
    try:
        with open("plan.json") as f:
            p = json.load(f)
        for k, v in default.items():
            p.setdefault(k, v)
        for k, v in default["settings"].items():
            p["settings"].setdefault(k, v)
        return p
    except FileNotFoundError:
        return default


def load_state():
    try:
        with open("state.json") as f:
            return json.load(f)
    except Exception:
        return {}


# ---------------------------------------------------------------- priser
def is_winter(d): return d.month >= 10 or d.month <= 3


def fallback_tariff(d):
    h, w = d.hour, is_winter(d)
    if h < 6:  return 0.13
    if h < 17: return 0.39 if w else 0.19
    if h < 21: return 1.16 if w else 0.51
    return 0.39 if w else 0.19


def load_tariffs():
    """TREFOR El-net, C-kunde, tidsdifferentieret. Returnerer {winter:[24], summer:[24]} eller None."""
    try:
        flt = json.dumps({"ChargeOwner": ["TREFOR El-net A/S"], "ChargeType": ["D03"]})
        r = requests.get(EDS + "DatahubPricelist", params={"filter": flt, "sort": "ValidFrom desc", "limit": 40}, timeout=20)
        recs = r.json().get("records", [])
        def prices(rec): return [rec.get(f"Price{i}") for i in range(1, 25)]
        recs = [x for x in recs if any(p is not None and p != prices(x)[0] for p in prices(x))]
        def pick(date):
            for x in recs:
                vf = dt.datetime.fromisoformat(x["ValidFrom"]).replace(tzinfo=TZ)
                vt = dt.datetime.fromisoformat(x["ValidTo"]).replace(tzinfo=TZ) if x.get("ValidTo") else None
                if vf <= date and (vt is None or vt > date):
                    return x
        cur = pick(NOW)
        if not cur:
            return None
        other = pick(NOW.replace(day=1) + dt.timedelta(days=185)) or cur
        arr = lambda x: [float(p or 0) for p in prices(x)]
        t = {"winter": arr(cur), "summer": arr(other)} if is_winter(NOW) else {"winter": arr(other), "summer": arr(cur)}
        log("TREFOR-tarif hentet")
        return t
    except Exception as e:
        log("Tarif kunne ikke hentes, bruger skøn:", e)
        return None


def load_prices():
    """Timepriser DK1 i kr/kWh (ex moms) for de seneste 7 døgn og frem. [(datetime, spot, est)]"""
    start = (NOW - dt.timedelta(days=7)).date().isoformat()
    end = (NOW + dt.timedelta(days=2)).date().isoformat()
    flt = json.dumps({"PriceArea": ["DK1"]})
    recs = []
    try:
        r = requests.get(EDS + "DayAheadPrices", params={"start": start, "end": end, "filter": flt, "sort": "TimeDK asc", "limit": 0}, timeout=30)
        recs = [(x["TimeDK"], x["DayAheadPriceDKK"] / 1000) for x in r.json()["records"]]
    except Exception:
        r = requests.get(EDS + "Elspotprices", params={"start": start, "end": end, "filter": flt, "sort": "HourDK asc", "limit": 0}, timeout=30)
        recs = [(x["HourDK"], x["SpotPriceDKK"] / 1000) for x in r.json()["records"]]
    agg = {}
    for t, p in recs:
        d = dt.datetime.fromisoformat(t).replace(tzinfo=TZ).replace(minute=0, second=0, microsecond=0)
        s = agg.setdefault(d, [0, 0]); s[0] += p; s[1] += 1
    hours = sorted((d, s[0] / s[1], False) for d, s in agg.items())
    # skøn for timer uden pris
    if hours:
        by_h = [[] for _ in range(24)]
        for d, p, _ in hours:
            if d < NOW: by_h[d.hour].append(p)
        avg_h = [sum(a) / len(a) if a else None for a in by_h]
        known = [x for x in avg_h if x is not None]
        overall = sum(known) / len(known)
        last24 = hours[-24:]
        shift = sum(p for _, p, _ in last24) / len(last24) - overall
        t = hours[-1][0] + dt.timedelta(hours=1)
        horizon = (NOW + dt.timedelta(days=2)).replace(hour=0, minute=0, second=0, microsecond=0)
        while t < horizon:
            base = avg_h[t.hour] if avg_h[t.hour] is not None else overall
            hours.append((t, base + shift * 0.5, True))
            t += dt.timedelta(hours=1)
    log(f"{len(hours)} timepriser, heraf {sum(1 for h in hours if h[2])} skønnede")
    return hours


def full_price(spot, d, S, tariffs):
    tar = (tariffs["winter" if is_winter(d) else "summer"][d.hour]) if tariffs else fallback_tariff(d)
    return (spot + S["gasel"] + tar + S["transport"] + S["tax"]) * 1.25


# ---------------------------------------------------------------- Bluelink (Hyundai)
def read_soc(prev_state):
    """Returnerer (soc, plugged, charging, updated_iso) eller (None,...) hvis ikke sat op."""
    user, pw, pin = os.getenv("BLUELINK_USER"), os.getenv("BLUELINK_PASSWORD"), os.getenv("BLUELINK_PIN", "")
    # Bluelink er slået fra som standard (Hyundais servere smider app-sessionen ud ved parallelle logins).
    # Sæt secret BLUELINK_ENABLED=ja for at bruge det alligevel.
    if os.getenv("BLUELINK_ENABLED", "").lower() not in ("ja", "yes", "true", "1") or not user or not pw:
        return None, None, None, None
    try:
        from hyundai_kia_connect_api import VehicleManager
        vm = VehicleManager(region=1, brand=2, username=user, password=pw, pin=pin)  # 1=Europa, 2=Hyundai
        vm.check_and_refresh_token()
        # cached: spørger Hyundais server, ikke bilen – skåner 12V-batteriet.
        # Hver time (eller mens der lades) beder vi bilen om friske tal.
        force = NOW.minute < 15 or (prev_state.get("charger") or {}).get("charging")
        if force:
            vm.check_and_force_update_vehicles(force_refresh_interval=600)
        else:
            vm.update_all_vehicles_with_cached_state()
        v = next(iter(vm.vehicles.values()))
        soc = v.ev_battery_percentage
        log(f"Bluelink: {v.name} {soc} % (plugged={v.ev_battery_is_plugged_in}, charging={v.ev_battery_is_charging})")
        return soc, bool(v.ev_battery_is_plugged_in), bool(v.ev_battery_is_charging), NOW.isoformat()
    except Exception as e:
        log("Bluelink fejlede:", e)
        return None, None, None, None


# ---------------------------------------------------------------- Zaptec
class Zaptec:
    BASE = "https://api.zaptec.com"
    CMD_STOP, CMD_RESUME = 506, 507   # StopChargingFinal, ResumeCharging

    def __init__(self):
        self.user, self.pw = os.getenv("ZAPTEC_USER"), os.getenv("ZAPTEC_PASSWORD")
        self.charger_id = os.getenv("ZAPTEC_CHARGER_ID")  # valgfri – ellers første lader
        self.token = None

    def enabled(self): return bool(self.user and self.pw)

    def login(self):
        r = requests.post(self.BASE + "/oauth/token", data={"grant_type": "password", "username": self.user, "password": self.pw}, timeout=20)
        r.raise_for_status()
        self.token = r.json()["access_token"]

    def _h(self): return {"Authorization": f"Bearer {self.token}"}

    def charger(self):
        r = requests.get(self.BASE + "/api/chargers", headers=self._h(), timeout=20); r.raise_for_status()
        chargers = r.json().get("Data", [])
        if not chargers:
            raise RuntimeError("Ingen ladere på kontoen")
        c = next((c for c in chargers if c["Id"] == self.charger_id), chargers[0]) if self.charger_id else chargers[0]
        return c

    def state(self, cid):
        r = requests.get(f"{self.BASE}/api/chargers/{cid}/state", headers=self._h(), timeout=20); r.raise_for_status()
        obs = {o["StateId"]: o.get("ValueAsString") for o in r.json()}
        mode = int(obs.get(710) or 0)   # 1 frakoblet, 2 tilsluttet/venter, 3 lader, 5 tilsluttet/færdig-pauset
        power = float(obs.get(513) or 0) / 1000 if obs.get(513) else None  # TotalChargePower W
        session_kwh = float(obs[553]) if obs.get(553) not in (None, "") else None  # TotalChargePowerSession kWh
        return {"mode": mode, "plugged": mode != 1 if mode else None, "charging": mode == 3, "power_kw": power, "session_kwh": session_kwh}

    def command(self, cid, cmd):
        r = requests.post(f"{self.BASE}/api/chargers/{cid}/sendCommand/{cmd}", headers=self._h(), timeout=20)
        r.raise_for_status()

    def start(self, cid):
        """Prøver i rækkefølge: genoptag (507) → godkend session → start (501). Returnerer hvad der virkede."""
        tried = []
        for name, fn in (("genoptag", lambda: self.command(cid, self.CMD_RESUME)),
                         ("godkend", lambda: requests.post(f"{self.BASE}/api/chargers/{cid}/authorizecharge", headers=self._h(), timeout=20).raise_for_status()),
                         ("start", lambda: self.command(cid, 501))):
            try:
                fn(); return name
            except Exception as e:
                tried.append(f"{name}: {str(e)[:60]}")
        raise RuntimeError(" | ".join(tried))


# ---------------------------------------------------------------- ladeplan
def next_deadline(P):
    o = P.get("override")
    if o and o["at"] / 1000 > NOW.timestamp():
        return dt.datetime.fromtimestamp(o["at"] / 1000, TZ), o["pct"], "engangs"
    for add in range(8):
        d = (NOW + dt.timedelta(days=add)).date()
        row = P["plan"][d.weekday()]
        if not row.get("on"): continue
        h, m = map(int, row["time"].split(":"))
        dl = dt.datetime.combine(d, dt.time(h, m), TZ)
        if dl > NOW:
            return dl, row["pct"], "ugeplan"
    return None, None, None


def compute(P, soc, hours, tariffs):
    S = P["settings"]
    manual = P.get("manual")
    dl, pct, kind = next_deadline(P)
    if manual:
        if soc is not None and soc >= manual["pct"]:
            manual = None
        else:
            dl, pct, kind = NOW + dt.timedelta(hours=48), manual["pct"], "manuel"
    if dl is None or soc is None:
        return {"charge_now": False, "reason": "ingen plan" if dl is None else "ingen batteri-%", "blocks": [], "deadline": None}
    need_kwh = max(0, pct - soc) / 100 * S["battery"] / (1 - S["loss"] / 100)
    need_h = need_kwh / S["power"]
    cand = []
    for t, spot, est in hours:
        end = min(t + dt.timedelta(hours=1), dl)
        start = max(t, NOW)
        frac = (end - start).total_seconds() / 3600
        if frac > 0.01:
            cand.append({"t": t, "start": start, "end": end, "frac": frac, "price": full_price(spot, t, S, tariffs), "est": est})
    cand.sort(key=(lambda c: c["start"]) if manual else (lambda c: c["price"]))
    left, chosen = need_h, []
    for c in cand:
        if left <= 0: break
        use = min(c["frac"], left); left -= use
        chosen.append({**c, "use": use, "end": c["start"] + dt.timedelta(hours=use)})
    chosen.sort(key=lambda c: c["start"])
    charge_now = any(c["start"] <= NOW < c["end"] for c in chosen)
    blocks = []
    for c in chosen:
        if blocks and abs((blocks[-1]["end"] - c["start"]).total_seconds()) < 900:  # sammenlæg huller under 15 min
            blocks[-1]["end"] = c["end"]
        else:
            blocks.append({"start": c["start"], "end": c["end"]})
    return {
        "charge_now": charge_now, "reason": kind, "need_kwh": round(need_kwh, 1), "target": pct,
        "deadline": dl.isoformat(), "short": left > 0.05,
        "cost": round(sum(c["use"] * S["power"] * c["price"] for c in chosen), 2),
        "blocks": [{"start": b["start"].isoformat(), "end": b["end"].isoformat()} for b in blocks],
        "manual_cleared": P.get("manual") is not None and manual is None,
    }


# ---------------------------------------------------------------- main
def main():
    P = load_plan()
    prev = load_state()
    tariffs = load_tariffs()
    try:
        hours = load_prices()
    except Exception as e:
        log("Priser kunne ikke hentes:", e)
        hours = []
    if not hours:  # uden priser gør vi ingenting ved laderen, men melder fejl til appen
        prev.update({"updated": NOW.isoformat(), "error": "Priser kunne ikke hentes fra Energi Data Service"})
        with open("state.json", "w") as f:
            json.dump(prev, f, ensure_ascii=False, indent=2, default=str)
        return

    # --- Zaptec: læs status først (bruges både til batteri-skøn og styring)
    z = Zaptec()
    charger_state, st, cid = {"error": "Zaptec ikke sat op"}, None, None
    if z.enabled():
        try:
            z.login()
            c = z.charger(); cid = c["Id"]
            st = z.state(cid)
            charger_state = {"name": c.get("Name"), "id": cid, **st, "error": None}
            log("Zaptec-status:", {k: st.get(k) for k in ("mode", "plugged", "charging", "power_kw", "session_kwh")},
                "(mode 1=frakoblet, 2=tilsluttet/venter, 3=lader, 5=tilsluttet/pauset)")
        except Exception as e:
            charger_state = {"error": str(e)[:120]}
            log("Zaptec fejlede:", e)

    # --- Batteri-%: Bluelink hvis slået til, ellers skøn = indtastet % + kWh målt af Zaptec
    soc, plugged, car_charging, soc_updated = read_soc(prev)
    soc_source = "bluelink" if soc is not None else None
    est = dict(prev.get("soc_est") or {})
    sm = P.get("soc_manual") or {}
    if sm.get("at") and sm.get("at") != est.get("base_at"):   # ny indtastning i appen → nyt udgangspunkt
        est = {"base_soc": float(sm["soc"]), "base_at": sm["at"], "kwh": 0.0, "last_session_kwh": None}
        log(f"Nyt udgangspunkt fra appen: {sm['soc']} %")
    if est.get("base_soc") is not None and st:
        prev_t = prev.get("updated")
        mins = (NOW - dt.datetime.fromisoformat(prev_t)).total_seconds() / 60 if prev_t else 0
        sk, last = st.get("session_kwh"), est.get("last_session_kwh")
        if sk is not None:
            if last is not None:
                est["kwh"] += (sk - last) if sk >= last else sk   # faldt tælleren, er det en ny session
            est["last_session_kwh"] = sk
        elif st.get("charging") and st.get("power_kw") and 0 < mins < 60:
            est["kwh"] += st["power_kw"] * mins / 60               # fallback: effekt × tid
        if st.get("plugged") is False:
            plugged = False
    if soc is None and est.get("base_soc") is not None:
        S = P["settings"]
        soc = min(100.0, round(est["base_soc"] + est["kwh"] * (1 - S["loss"] / 100) / S["battery"] * 100, 1))
        soc_source = "zaptec"
        soc_updated = NOW.isoformat()
        log(f"Batteri-skøn: {est['base_soc']} % + {est['kwh']:.1f} kWh → {soc} %")
    elif soc is None:
        soc = prev.get("soc"); soc_source = "ukendt"
        log("Ingen batteri-% – tast den i appen, når bilen sættes til")

    result = compute(P, soc, hours, tariffs)
    log(f"Plan: {result['reason']} → lad nu = {result['charge_now']}  blokke = {len(result['blocks'])}")

    # --- Zaptec: tænd/sluk hvis nødvendigt
    action = None
    if st is not None:
        try:
            if st["plugged"] is False:
                action = "bil ikke tilsluttet"
            elif result["charge_now"] and not st["charging"]:
                action = "startet (" + z.start(cid) + ")"
            elif not result["charge_now"] and st["charging"]:
                z.command(cid, Zaptec.CMD_STOP); action = "stoppet"
            else:
                action = "uændret"
            log("Zaptec →", action)
        except Exception as e:
            charger_state["error"] = str(e)[:120]
            log("Zaptec-kommando fejlede:", e)

    state = {
        "updated": NOW.isoformat(),
        "soc": soc, "soc_updated": soc_updated, "soc_source": soc_source, "soc_est": est,
        "car_plugged": plugged, "car_charging": car_charging,
        "charger": charger_state, "action": action,
        "plan": result,
        "tariff_source": "TREFOR El-net (live)" if tariffs else "skøn",
        # priser til appen (browseren må ikke selv kalde Energi Data Service)
        "hours": [{"t": t.isoformat(), "spot": round(spot, 5), "est": est} for t, spot, est in hours],
        "tariffs": tariffs,
    }
    with open("state.json", "w") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, default=str)

    # ryd manuel ladning i plan.json når målet er nået, så appen også ser det
    if result.get("manual_cleared"):
        P["manual"] = None
        with open("plan.json", "w") as f:
            json.dump(P, f, ensure_ascii=False, indent=2)
        log("Manuel ladning afsluttet – mål nået")


if __name__ == "__main__":
    main()
