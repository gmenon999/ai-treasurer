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
HIST = os.path.join(DATA, "policy_history.json")
MKT_HIST = os.path.join(DATA, "market_history.json")
HIST_START = datetime.date(2022, 1, 1)   # chart window: the full 2022-26 hiking and cutting cycle

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
    rows = []
    for yr in (TODAY.year, TODAY.year - 1):
        url = ("https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
               "daily-treasury-rates.csv/%d/all?type=daily_treasury_yield_curve&field_tdr_date_value=%d&page&_format=csv" % (yr, yr))
        for r in csv.DictReader(io.StringIO(get(url).text)):
            d = datetime.datetime.strptime(r["Date"], "%m/%d/%Y").date()
            rows.append((d, r))
        if len(rows) >= 2:
            break
    rows.sort(key=lambda x: x[0], reverse=True)
    return rows[:2]


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


# ------------------------------------------------------------- rate history
# Official daily series, reduced to the dates the rate changed (step points).
# Each series also records the last observation date, so the chart runs to "today".
def _steps(rows):
    """rows: ascending [(iso_date, value, ...)] -> only the rows where the value changed."""
    out = []
    for r in rows:
        if not out or tuple(out[-1][1:]) != tuple(r[1:]):
            out.append(list(r))
    return out


def hist_fed(start):
    url = ("https://markets.newyorkfed.org/api/rates/unsecured/effr/search.json?startDate=%s&endDate=%s"
           % (start.isoformat(), TODAY.isoformat()))
    rows = sorted((r["effectiveDate"], round(r["targetRateTo"], 4), round(r["targetRateFrom"], 4))
                  for r in get(url).json()["refRates"] if r.get("targetRateTo") is not None)
    return {"bank": "Fed", "label": "Fed funds target range (upper bound)", "range": True,
            "source_name": "Federal Reserve Bank of New York", "source_url": "https://www.newyorkfed.org/markets/reference-rates/effr",
            "points": _steps(rows), "asof": rows[-1][0]}


def hist_ecb(start):
    # The ECB key-rate series records only the dates a rate changed, so read from earlier,
    # carry the rate in force on `start` into the window, and run the line to today.
    url = ("https://data-api.ecb.europa.eu/service/data/FM/B.U2.EUR.4F.KR.DFR.LEV?startPeriod=%s&format=csvdata"
           % min(start, datetime.date(2014, 1, 1) if start <= HIST_START else start - datetime.timedelta(days=3660)).isoformat())
    rows = sorted((r["TIME_PERIOD"], round(float(r["OBS_VALUE"]), 4))
                  for r in csv.DictReader(io.StringIO(get(url).text)) if r.get("OBS_VALUE") not in (None, ""))
    st = start.isoformat()
    before = [r for r in rows if r[0] <= st]
    rows = ([(st, before[-1][1])] if before else []) + [r for r in rows if r[0] > st]
    return {"bank": "ECB", "label": "Deposit facility rate",
            "source_name": "European Central Bank", "source_url": "https://data.ecb.europa.eu/data/datasets/FM/FM.B.U2.EUR.4F.KR.DFR.LEV",
            "points": _steps(rows), "asof": TODAY.isoformat()}


def hist_boe(start):
    url = ("https://www.bankofengland.co.uk/boeapps/database/_iadb-fromshowcolumns.asp?csv.x=yes"
           "&Datefrom=%s&Dateto=now&SeriesCodes=IUDBEDR&CSVF=TN&UsingCodes=Y&VPD=Y&VFD=N" % start.strftime("%d/%b/%Y"))
    rows = []
    for r in csv.reader(io.StringIO(get(url).text)):
        try:
            rows.append((datetime.datetime.strptime(r[0].strip(), "%d %b %Y").date().isoformat(), round(float(r[1]), 4)))
        except Exception:
            continue          # header or blank line
    rows.sort()
    return {"bank": "BoE", "label": "Bank Rate",
            "source_name": "Bank of England", "source_url": "https://www.bankofengland.co.uk/boeapps/database/Bank-Rate.asp",
            "points": _steps(rows), "asof": rows[-1][0]}


INCREMENTAL_DAYS = 45   # daily runs re-read only the recent window and append new moves


def build_history():
    """Policy-rate history for the Markets chart.

    The saved file is the record: each run fetches only the last INCREMENTAL_DAYS for Fed, ECB
    and BoE and appends any new move. A full rebuild from HIST_START happens only when a
    series is missing from the file. A series that fails keeps its last good copy."""
    prev = {}
    if os.path.exists(HIST):
        try:
            prev = json.load(open(HIST, encoding="utf-8")).get("series", {})
        except Exception:
            prev = {}
    series = {}
    for key, fn in (("fed", hist_fed), ("ecb", hist_ecb), ("boe", hist_boe)):
        old = prev.get(key)
        try:
            if old and old.get("points") and not old.get("manual"):
                start = max(HIST_START, datetime.date.fromisoformat(old["asof"]) - datetime.timedelta(days=INCREMENTAL_DAYS))
                new = fn(start)
                # saved moves are never rewritten; only moves dated after the last saved one are added
                last = old["points"][-1][0]
                item = dict(new, points=_steps(old["points"] + [p for p in new["points"] if p[0] > last]))
            else:
                item = fn(HIST_START)
            if len(item["points"]) < 1:
                raise ValueError("no observations")
            series[key] = item
        except Exception as e:
            LOG.append("history %s: %s" % (key, e))
            if old:
                series[key] = old
    # RBI, PBOC and QCB: researched decision histories kept by hand in policy_rates.json
    try:
        for s in json.load(open(POLICY, encoding="utf-8"))["rates"]:
            pts = [list(p) for p in (s.get("history") or []) if p[0] >= HIST_START.isoformat()]
            if pts:
                series[s["id"]] = {"bank": s["bank"], "label": s.get("history_label") or s["label"],
                                   "source_name": s.get("history_source_name") or s["source_name"], "source_url": s["source_url"],
                                   "sources": s.get("history_sources") or {}, "points": pts,
                                   "asof": max(pts[-1][0], s.get("checked") or pts[-1][0]), "manual": True}
    except Exception as e:
        LOG.append("history hand-kept: %s" % e)
    return {"start": HIST_START.isoformat(), "series": series,
            "notes": "Official daily series reduced to the dates each rate changed. Fed shows the target range (upper and lower bound) by effective date."}


# ------------------------------------------------- money-market, yield and FX history
# Weekly (last observation of each ISO week) since HIST_START, for the Markets exhibits.
# The saved file is the record: each run re-reads only the last MKT_WINDOW_WEEKS and
# rebuilds those weeks; older weeks are never rewritten. A full fetch happens only when a
# series is missing from the file. A failing source keeps its last good copy.
MKT_WINDOW_WEEKS = 6
FX_PAIRS = [
    ("eurusd", "EUR/USD", lambda r: r["USD"], 4),
    ("gbpusd", "GBP/USD", lambda r: r["USD"] / r["GBP"], 4),
    ("usdjpy", "USD/JPY", lambda r: r["JPY"] / r["USD"], 2),
    ("usdinr", "USD/INR", lambda r: r["INR"] / r["USD"], 2),
    ("usdcny", "USD/CNY", lambda r: r["CNY"] / r["USD"], 4),
    ("audusd", "AUD/USD", lambda r: r["USD"] / r["AUD"], 4),
]
UST_COLS = [("ust3m", "3 Mo", "US Treasury 3M"), ("ust2y", "2 Yr", "US Treasury 2Y"), ("ust10y", "10 Yr", "US Treasury 10Y")]
SRC_TSY = ("US Treasury", "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/TextView?type=daily_treasury_yield_curve")
SRC_FX = ("ECB euro reference rates (USD crosses derived)", "https://www.ecb.europa.eu/stats/policy_and_exchange_rates/euro_reference_exchange_rates/html/index.en.html")


def _weekly(rows):
    """rows: [(iso_date, value)] -> last observation of each ISO week, ascending."""
    out = {}
    for d, v in sorted(rows):
        y, w, _ = datetime.date.fromisoformat(d).isocalendar()
        out[(y, w)] = [d, v]
    return [out[k] for k in sorted(out)]


def mh_nyfed(kind, start):
    url = ("https://markets.newyorkfed.org/api/rates/%s/search.json?startDate=%s&endDate=%s"
           % (kind, start.isoformat(), TODAY.isoformat()))
    return [(r["effectiveDate"], round(float(r["percentRate"]), 4)) for r in get(url).json()["refRates"] if r.get("percentRate") is not None]


def mh_estr(start):
    url = "https://data-api.ecb.europa.eu/service/data/EST/B.EU000A2X2A25.WT?startPeriod=%s&format=csvdata" % start.isoformat()
    return [(r["TIME_PERIOD"], round(float(r["OBS_VALUE"]), 4)) for r in csv.DictReader(io.StringIO(get(url).text)) if r.get("OBS_VALUE") not in (None, "")]


def mh_ust(start):
    out = {k: [] for k, _, _ in UST_COLS}
    for yr in range(start.year, TODAY.year + 1):
        url = ("https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
               "daily-treasury-rates.csv/%d/all?type=daily_treasury_yield_curve&field_tdr_date_value=%d&page&_format=csv" % (yr, yr))
        for r in csv.DictReader(io.StringIO(get(url).text)):
            d = datetime.datetime.strptime(r["Date"], "%m/%d/%Y").date()
            if d < start:
                continue
            for k, col, _ in UST_COLS:
                if r.get(col) not in (None, "", "N/A"):
                    out[k].append((d.isoformat(), round(float(r[col]), 4)))
    return out


def mh_fx(start):
    recent = (TODAY - start).days < 85
    url = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist-90d.xml" if recent else "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist.xml"
    root = ET.fromstring(get(url).content)
    out = {k: [] for k, _, _, _ in FX_PAIRS}
    for cube in root.iter():
        if cube.tag.endswith("Cube") and cube.get("time") and cube.get("time") >= start.isoformat():
            rates = {c.get("currency"): float(c.get("rate")) for c in cube}
            for k, _, fn, dp in FX_PAIRS:
                try:
                    out[k].append((cube.get("time"), round(fn(rates), dp + 2)))
                except Exception:
                    pass
    return out


def build_market_history():
    prev = {}
    if os.path.exists(MKT_HIST):
        try:
            prev = json.load(open(MKT_HIST, encoding="utf-8")).get("series", {})
        except Exception:
            prev = {}

    def start_for(keys):
        """Full fetch if any series is missing; otherwise the Monday MKT_WINDOW_WEEKS before the oldest last point."""
        if any(not (prev.get(k) or {}).get("points") for k in keys):
            return HIST_START
        last = min(datetime.date.fromisoformat(prev[k]["points"][-1][0]) for k in keys)
        st = last - datetime.timedelta(weeks=MKT_WINDOW_WEEKS)
        return max(HIST_START, st - datetime.timedelta(days=st.weekday()))

    def merge(key, meta, rows, start):
        old = [p for p in (prev.get(key) or {}).get("points", []) if p[0] < start.isoformat()]
        pts = old + _weekly([r for r in rows if r[0] >= start.isoformat()])
        if not pts:
            raise ValueError("no observations")
        return dict(meta, points=pts, asof=pts[-1][0])

    series = {}
    jobs = [
        (["sofr"], lambda st: {"sofr": mh_nyfed("secured/sofr", st)}, {"sofr": dict(name="SOFR", short="SOFR", group="money", unit="%", dp=2, source_name="Federal Reserve Bank of New York", source_url="https://www.newyorkfed.org/markets/reference-rates/sofr")}),
        (["effr"], lambda st: {"effr": mh_nyfed("unsecured/effr", st)}, {"effr": dict(name="Fed funds effective rate", short="EFFR", group="money", unit="%", dp=2, source_name="Federal Reserve Bank of New York", source_url="https://www.newyorkfed.org/markets/reference-rates/effr")}),
        (["estr"], lambda st: {"estr": mh_estr(st)}, {"estr": dict(name="€STR", short="€STR", group="money", unit="%", dp=3, source_name="European Central Bank", source_url="https://www.ecb.europa.eu/stats/financial_markets_and_interest_rates/euro_short-term_rate/html/index.en.html")}),
        ([k for k, _, _ in UST_COLS], mh_ust, {k: dict(name=lab, short=lab.replace("US Treasury ", "US "), group="money", unit="%", dp=2, source_name=SRC_TSY[0], source_url=SRC_TSY[1]) for k, _, lab in UST_COLS}),
        ([k for k, _, _, _ in FX_PAIRS], mh_fx, {k: dict(name=lab, short=lab, group="fx", unit="", dp=dp, source_name=SRC_FX[0], source_url=SRC_FX[1]) for k, lab, _, dp in FX_PAIRS}),
    ]
    for keys, fetch, metas in jobs:
        try:
            st = start_for(keys)
            got = fetch(st)
            for k in keys:
                try:
                    series[k] = merge(k, metas[k], got.get(k) or [], st)
                except Exception as e:
                    LOG.append("market history %s: %s" % (k, e))
                    if prev.get(k):
                        series[k] = prev[k]
        except Exception as e:
            LOG.append("market history %s: %s" % ("/".join(keys), e))
            for k in keys:
                if prev.get(k):
                    series[k] = prev[k]
    return {"start": HIST_START.isoformat(), "frequency": "weekly (last observation of each week)", "series": series}


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

    curve = {}

    def ust(col, label):
        def f():
            if "rows" not in curve:
                curve["rows"] = treasury_curve()
            rows = curve["rows"]
            v0 = float(rows[0][1][col]); v1 = float(rows[1][1][col]) if len(rows) > 1 else None
            return {"name": label, "value": fmt_pct(v0), "change_bp": bp(v0, v1), "asof": iso(rows[0][0]),
                    "source_name": "US Treasury", "source_url": "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/TextView?type=daily_treasury_yield_curve"}
        return f

    for key, fn in [("sofr", sofr), ("effr", effr), ("estr", estr),
                    ("ust3m", ust("3 Mo", "US Treasury 3M")), ("ust2y", ust("2 Yr", "US Treasury 2Y")), ("ust10y", ust("10 Yr", "US Treasury 10Y"))]:
        it = mark(keep_or_update(prev, key, fn), DAILY_STALE_DAYS)
        if it:
            money.append(it)

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
    # rate history for the policy-rate chart (its own file; written only when it changed)
    hist = build_history()
    if hist["series"]:
        old_h = None
        if os.path.exists(HIST):
            try:
                old_h = json.load(open(HIST, encoding="utf-8"))
            except Exception:
                old_h = None
        if old_h != hist:
            json.dump(hist, open(HIST + ".tmp", "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
            os.replace(HIST + ".tmp", HIST)
            print("policy_history.json written: %s" % ", ".join("%s %d moves" % (k, len(v["points"])) for k, v in hist["series"].items()))
    # money-market, yield and FX history for the Markets exhibits
    mh = build_market_history()
    if mh["series"]:
        old_m = None
        if os.path.exists(MKT_HIST):
            try:
                old_m = json.load(open(MKT_HIST, encoding="utf-8"))
            except Exception:
                old_m = None
        if old_m != mh:
            json.dump(mh, open(MKT_HIST + ".tmp", "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
            os.replace(MKT_HIST + ".tmp", MKT_HIST)
            print("market_history.json written: %d series" % len(mh["series"]))
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
