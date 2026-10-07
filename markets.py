#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
markets.py - refresh the Treasury Markets panel (data/markets.json).

Runs in GitHub Actions every few hours. Pulls free, official sources only:
  - Policy rates: NY Fed (Fed target range), ECB data API (DFR / MRO),
    Bank of England IADB (Bank Rate); RBI, PBOC and QCB come from the
    hand-maintained data/policy_rates.json.
  - Money markets and yields: NY Fed (SOFR, EFFR), ECB (euro short-term rate),
    US Treasury (daily par yield curve).
  - FX: ECB euro foreign exchange reference rates (USD crosses derived).

Controls: every figure carries its source, its as-of date and a stale flag.
If a source fails, the last good figure is kept with its original date and
flagged stale once it ages; the panel never shows a blank or a guess.
No API keys. Not live market data and not investment advice.
"""
import os, io, csv, json, datetime, xml.etree.ElementTree as ET

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
OUT = os.path.join(DATA, "markets.json")
POLICY = os.path.join(DATA, "policy_rates.json")

UA = {"User-Agent": "Mozilla/5.0 (compatible; TheAITreasurer/1.0; +https://theaitreasurer.com)"}
DAILY_STALE_DAYS = 5      # covers a weekend plus a holiday
POLICY_STALE_DAYS = 60    # hand-maintained rows must be re-checked at least this often

TODAY = datetime.datetime.utcnow().date()
LOG = []


def get(url, **kw):
    r = requests.get(url, headers=UA, timeout=30, **kw)
    r.raise_for_status()
    return r


def iso(d):
    return d.isoformat() if d else None


def age_days(s):
    try:
        return (TODAY - datetime.date.fromisoformat(s)).days
    except Exception:
        return 999


def bp(new, old):
    if new is None or old is None:
        return None
    return round((new - old) * 100)


# ------------------------------------------------------------------ sources
def nyfed(kind):
    """kind: 'secured/sofr' or 'unsecured/effr'. Returns latest two records."""
    j = get("https://markets.newyorkfed.org/api/rates/%s/last/2.json" % kind).json()
    rows = sorted(j["refRates"], key=lambda r: r["effectiveDate"], reverse=True)
    return rows


def ecb_csv(flow_key, n):
    url = "https://data-api.ecb.europa.eu/service/data/%s?lastNObservations=%d&format=csvdata" % (flow_key, n)
    rows = list(csv.DictReader(io.StringIO(get(url).text)))
    rows = [(r["TIME_PERIOD"], float(r["OBS_VALUE"])) for r in rows if r.get("OBS_VALUE") not in (None, "")]
    rows.sort(reverse=True)
    return rows


def treasury_curve():
    """All daily par-yield rows, newest first; reaches into last year when needed for a one-month comparison."""
    rows = []
    for yr in (TODAY.year, TODAY.year - 1):
        url = ("https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
               "daily-treasury-rates.csv/%d/all?type=daily_treasury_yield_curve&field_tdr_date_value=%d&page&_format=csv" % (yr, yr))
        for r in csv.DictReader(io.StringIO(get(url).text)):
            d = datetime.datetime.strptime(r["Date"], "%m/%d/%Y").date()
            rows.append((d, r))
        rows.sort(key=lambda x: x[0], reverse=True)
        if len(rows) >= 2 and rows[-1][0] <= rows[0][0] - datetime.timedelta(days=35):
            break
    return rows


CURVE_TENORS = [("1M", "1 Mo", 1 / 12), ("3M", "3 Mo", 0.25), ("6M", "6 Mo", 0.5), ("1Y", "1 Yr", 1), ("2Y", "2 Yr", 2),
                ("3Y", "3 Yr", 3), ("5Y", "5 Yr", 5), ("7Y", "7 Yr", 7), ("10Y", "10 Yr", 10), ("20Y", "20 Yr", 20), ("30Y", "30 Yr", 30)]


def ecb_fx():
    xml = get("https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist-90d.xml").content
    root = ET.fromstring(xml)
    days = []
    for cube in root.iter():
        if cube.tag.endswith("Cube") and cube.get("time"):
            rates = {c.get("currency"): float(c.get("rate")) for c in cube}
            days.append((cube.get("time"), rates))
    days.sort(reverse=True)
    return days[:2]


def boe_bank_rate():
    url = ("https://www.bankofengland.co.uk/boeapps/database/_iadb-fromshowcolumns.asp?csv.x=yes"
           "&Datefrom=01/Jan/%d&Dateto=now&SeriesCodes=IUDBEDR&CSVF=TN&UsingCodes=Y&VPD=Y&VFD=N" % (TODAY.year - 1))
    rows = [r for r in csv.reader(io.StringIO(get(url).text)) if len(r) >= 2]
    last = rows[-1]
    d = datetime.datetime.strptime(last[0].strip(), "%d %b %Y").date()
    return d, float(last[1])


# ------------------------------------------------------------------ helpers
def keep_or_update(prev, key, build):
    """Try to build a fresh item; on failure keep the previous one (last good)."""
    try:
        item = build()
        item["id"] = key
        return item
    except Exception as e:
        LOG.append("%s: %s" % (key, e))
        return prev.get(key)


def mark(item, max_days):
    if item:
        item["stale"] = age_days(item.get("asof")) > max_days
    return item


def fmt_pct(x, dp=2):
    return "%.*f%%" % (dp, x)


# ------------------------------------------------------------------ build
def build():
    prev_doc = {}
    if os.path.exists(OUT):
        try:
            prev_doc = json.load(open(OUT, encoding="utf-8"))
        except Exception:
            prev_doc = {}
    prev = {}
    for sect in ("policy", "money", "fx"):
        for it in prev_doc.get(sect, []) or []:
            prev[it.get("id")] = it
    if prev_doc.get("curve"):
        prev["curve"] = prev_doc["curve"]

    # ---- money markets and yields
    money = []

    def sofr():
        r = nyfed("secured/sofr")
        return {"name": "SOFR", "value": fmt_pct(r[0]["percentRate"]), "change_bp": bp(r[0]["percentRate"], r[1]["percentRate"]) if len(r) > 1 else None,
                "asof": r[0]["effectiveDate"], "source_name": "Federal Reserve Bank of New York", "source_url": "https://www.newyorkfed.org/markets/reference-rates/sofr"}

    effr_rows = {}

    def effr():
        r = nyfed("unsecured/effr")
        effr_rows["latest"] = r[0]
        return {"name": "Fed funds effective", "value": fmt_pct(r[0]["percentRate"]), "change_bp": bp(r[0]["percentRate"], r[1]["percentRate"]) if len(r) > 1 else None,
                "asof": r[0]["effectiveDate"], "source_name": "Federal Reserve Bank of New York", "source_url": "https://www.newyorkfed.org/markets/reference-rates/effr"}

    def estr():
        r = ecb_csv("EST/B.EU000A2X2A25.WT", 2)
        return {"name": "€STR", "value": fmt_pct(r[0][1], 3), "change_bp": bp(r[0][1], r[1][1]) if len(r) > 1 else None,
                "asof": r[0][0], "source_name": "European Central Bank", "source_url": "https://www.ecb.europa.eu/stats/financial_markets_and_interest_rates/euro_short-term_rate/html/index.en.html"}

    curve_rows = {}

    def ust(col, label):
        def f():
            if "rows" not in curve_rows:
                curve_rows["rows"] = treasury_curve()
            rows = curve_rows["rows"]
            v0 = float(rows[0][1][col]); v1 = float(rows[1][1][col]) if len(rows) > 1 else None
            return {"name": label, "value": fmt_pct(v0), "change_bp": bp(v0, v1), "asof": iso(rows[0][0]),
                    "source_name": "US Treasury", "source_url": "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/TextView?type=daily_treasury_yield_curve"}
        return f

    for key, fn in [("sofr", sofr), ("effr", effr), ("estr", estr),
                    ("ust3m", ust("3 Mo", "US Treasury 3M")), ("ust2y", ust("2 Yr", "US Treasury 2Y")), ("ust10y", ust("10 Yr", "US Treasury 10Y"))]:
        it = mark(keep_or_update(prev, key, fn), DAILY_STALE_DAYS)
        if it:
            money.append(it)

    # ---- US Treasury yield curve: latest vs about one month earlier
    def curve():
        if "rows" not in curve_rows:
            curve_rows["rows"] = treasury_curve()
        rows = curve_rows["rows"]
        d0, r0 = rows[0]
        target = d0 - datetime.timedelta(days=28)
        prev = next(((d, r) for d, r in rows if d <= target), None)
        if not prev:
            raise ValueError("no row a month earlier")
        d1, r1 = prev
        def vals(r):
            return [float(r[col]) if r.get(col) not in (None, "", "N/A") else None for _, col, _ in CURVE_TENORS]
        return {"name": "US Treasury par yield curve", "tenors": [t for t, _, _ in CURVE_TENORS],
                "years": [y for _, _, y in CURVE_TENORS], "today": vals(r0), "prev": vals(r1),
                "asof": iso(d0), "prev_asof": iso(d1), "source_name": "US Treasury",
                "source_url": "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/TextView?type=daily_treasury_yield_curve"}
    curve_item = mark(keep_or_update(prev, "curve", curve), DAILY_STALE_DAYS)

    # ---- FX (ECB reference rates, USD crosses derived)
    fx = []
    days = {}

    def pair(key, label, fn, dp):
        def f():
            if "d" not in days:
                days["d"] = ecb_fx()
            (d0, r0), (d1, r1) = days["d"][0], days["d"][1]
            v0, v1 = fn(r0), fn(r1)
            return {"name": label, "value": "%.*f" % (dp, v0), "change_pct": round((v0 / v1 - 1) * 100, 2), "asof": d0,
                    "source_name": "ECB reference rates (USD crosses derived)", "source_url": "https://www.ecb.europa.eu/stats/policy_and_exchange_rates/euro_reference_exchange_rates/html/index.en.html"}
        return f

    pairs = [
        ("eurusd", "EUR/USD", lambda r: r["USD"], 4),
        ("gbpusd", "GBP/USD", lambda r: r["USD"] / r["GBP"], 4),
        ("usdjpy", "USD/JPY", lambda r: r["JPY"] / r["USD"], 2),
        ("usdinr", "USD/INR", lambda r: r["INR"] / r["USD"], 2),
        ("usdcny", "USD/CNY", lambda r: r["CNY"] / r["USD"], 4),
        ("audusd", "AUD/USD", lambda r: r["USD"] / r["AUD"], 4),
    ]
    for key, label, fn, dp in pairs:
        it = mark(keep_or_update(prev, key, pair(key, label, fn, dp)), DAILY_STALE_DAYS)
        if it:
            fx.append(it)

    # ---- policy rates
    seeds = json.load(open(POLICY, encoding="utf-8"))["rates"]
    policy = []
    for s in seeds:
        p = dict(prev.get(s["id"]) or {})
        # hand-maintained fields always come from the seed file (it is the record of truth)
        base = {k: s.get(k) for k in ("bank", "label", "source_name", "source_url")}
        value, change_bp, since, checked = s["value"], s.get("change_bp"), s.get("decided"), s.get("checked")
        if s.get("auto"):
            # keep any auto-detected change from earlier runs if newer than the seed
            if p.get("since") and (not since or p["since"] > since):
                value, change_bp, since = p["value"], p.get("change_bp"), p["since"]
            try:
                if s["id"] == "fed":
                    r = effr_rows.get("latest") or nyfed("unsecured/effr")[0]
                    new = "%.2f–%.2f%%" % (r["targetRateFrom"], r["targetRateTo"])
                    obs = r["effectiveDate"]
                    old_top = _num(value.split("–")[-1])
                    if new != value:
                        change_bp, since = bp(r["targetRateTo"], old_top), obs
                    value = new
                elif s["id"] == "ecb":
                    dfr = ecb_csv("FM/B.U2.EUR.4F.KR.DFR.LEV", 1)[0]
                    mro = ecb_csv("FM/B.U2.EUR.4F.KR.MRR_FR.LEV", 1)[0]
                    new = "%.2f / %.2f%%" % (dfr[1], mro[1])
                    if new != value:
                        change_bp, since = bp(dfr[1], _num(value.split("/")[0])), dfr[0]
                    value = new
                elif s["id"] == "boe":
                    d, v = boe_bank_rate()
                    new = fmt_pct(v)
                    if new != value:
                        change_bp, since = bp(v, _num(value)), iso(d)
                    value = new
                checked = iso(TODAY)
            except Exception as e:
                LOG.append("%s: %s" % (s["id"], e))
                checked = p.get("checked") or checked
        item = dict(base, id=s["id"], value=value, change_bp=change_bp, since=since, checked=checked, asof=checked)
        policy.append(mark(item, POLICY_STALE_DAYS))

    doc = {
        "generated_utc": datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
        "policy": policy,
        "money": money,
        "fx": fx,
        "curve": curve_item,
        "notes": "Figures as published by the sources shown, on the dates shown. The Qatari riyal is pegged at QAR 3.64 per USD. Not live market data and not investment advice.",
        "errors": LOG,
    }
    return doc


def _num(s):
    return float(str(s).replace("%", "").strip())


def main():
    os.makedirs(DATA, exist_ok=True)
    doc = build()
    if not doc["money"] and not doc["fx"]:
        raise SystemExit("markets: no data at all; leaving previous file untouched. Errors: %s" % doc["errors"])
    # only rewrite when something other than the run timestamp changed (avoids empty deploys)
    if os.path.exists(OUT):
        try:
            old = json.load(open(OUT, encoding="utf-8"))
            if {k: v for k, v in old.items() if k != "generated_utc"} == {k: v for k, v in doc.items() if k != "generated_utc"}:
                print("markets: no change since last run; file left as is.")
                return
        except Exception:
            pass
    tmp = OUT + ".tmp"
    json.dump(doc, open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    os.replace(tmp, OUT)
    print("markets.json written: %d policy, %d money, %d fx; %d source errors" % (len(doc["policy"]), len(doc["money"]), len(doc["fx"]), len(LOG)))
    for e in LOG:
        print("  source error -", e)


if __name__ == "__main__":
    main()
