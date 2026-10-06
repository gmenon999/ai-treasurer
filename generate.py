#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
generate.py - build today's edition of The AI Treasurer Briefing.

Runs in GitHub Actions. It researches the day's AI / Treasury / Finance news
through OpenRouter's web search, writes the edition in the house template and
voice, saves a dated archive copy, updates index.html (the live landing page),
and prints the edition number for the commit step. Pushing is done by the
workflow; Netlify publishes on push.

ONE SECRET: OPENROUTER_API_KEY  (search runs through OpenRouter; no Exa key).
Optional env: BRIEF_RESEARCH_MODEL, BRIEF_EDITORIAL_MODEL, BRIEF_SEARCH_ENGINE,
BRIEF_MAX_SEARCHES, BRIEF_TZ_OFFSET_HOURS (default 3 = Qatar).

Guardrails (baked into the prompts): original-expression summaries, short
attributed quotes only, a real source URL found via search for every item,
no paywalled reproduction, not investment advice, exclude anything negative to
Qatar/QatarEnergy/Woqod.
"""
import os, sys, re, json, time, html, datetime, shutil, argparse

try:
    import requests
except ImportError:
    requests = None

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(HERE, "edition_template.html")
INDEX = os.path.join(HERE, "index.html")
EDITIONS_DIR = os.path.join(HERE, "editions")
STATE = os.path.join(EDITIONS_DIR, "state.json")

API_URL = "https://openrouter.ai/api/v1/chat/completions"
RESEARCH_MODEL = os.environ.get("BRIEF_RESEARCH_MODEL", "anthropic/claude-haiku-4.5")
EDITORIAL_MODEL = os.environ.get("BRIEF_EDITORIAL_MODEL", "anthropic/claude-opus-5")
SEARCH_ENGINE = os.environ.get("BRIEF_SEARCH_ENGINE", "exa")
MAX_SEARCHES = int(os.environ.get("BRIEF_MAX_SEARCHES", "10"))
TZ_OFFSET = int(os.environ.get("BRIEF_TZ_OFFSET_HOURS", "3"))  # Qatar = UTC+3
EXCLUDED = ["reddit.com", "quora.com", "medium.com", "substack.com"]

BEATS = [
    ("ai_agentic",   "AI & Agentic Automation in Treasury",
     "autonomous/agentic AI inside corporate treasury and cash management; AI copilots for treasurers; named vendor or bank deployments"),
    ("treasury_tech","Treasury Technology, Data & Connectivity",
     "treasury management systems (TMS), ERP-treasury, APIs/connectivity, real-time data, cash-forecasting tech"),
    ("cash",         "Cash, Liquidity & Working Capital",
     "corporate liquidity, cash visibility and pooling, working-capital and trapped-cash trends, money-market/investment of corporate cash"),
    ("payments",     "Payments & Transaction Banking",
     "ISO 20022, real-time/instant and cross-border payments, transaction-banking product launches from major banks"),
    ("regulation",   "Regulation, Controls & the CFO Agenda",
     "AI governance and controls in finance, ICFR/audit, SEC/PCAOB, EU AI Act, CFO-office compliance"),
    ("risk",         "Treasury Risk - FX, Rates & Commodities",
     "corporate FX and interest-rate hedging, commodity exposure, funding and counterparty risk for treasurers"),
    ("digital",      "Digital Assets, Tokenisation & Settlement",
     "stablecoins and tokenised deposits for corporate payments/treasury, on-chain settlement, bank digital-asset infrastructure"),
    ("markets_macro","Markets & Macro for Treasurers",
     "central-bank rate decisions and outlook, government bond yields, major FX moves - framed for a corporate treasurer"),
]
BEAT_KEYS = [b[0] for b in BEATS]
LEAD_TAB_LABEL = {"ai": "AI &amp; Technology", "treasury": "Treasury &amp; Payments", "markets": "Risk &amp; Markets"}


def esc(x):
    return html.escape(str(x or ""), quote=False)


# ---------------------------------------------------------------- OpenRouter
def _key():
    k = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not k:
        sys.exit("OPENROUTER_API_KEY is not set (GitHub Actions repository secret of that name).")
    return k


def _search_tool():
    p = {"engine": SEARCH_ENGINE, "max_uses": MAX_SEARCHES, "max_results": 6,
         "max_characters": 2500, "excluded_domains": EXCLUDED}
    return {"type": "openrouter:web_search", "parameters": p}


def _ask(prompt, model, max_tokens=9000, search=True, attempts=3):
    body = {"model": model, "messages": [{"role": "user", "content": prompt}], "max_tokens": max_tokens}
    if search:
        body["tools"] = [_search_tool()]
        body["max_tool_calls"] = MAX_SEARCHES
    headers = {"Authorization": "Bearer " + _key(), "Content-Type": "application/json",
               "HTTP-Referer": "https://theaitreasurer.com", "X-Title": "The AI Treasurer"}
    last = None
    for attempt in range(1, attempts + 1):
        try:
            r = requests.post(API_URL, headers=headers, json=body, timeout=300)
            if r.status_code == 200:
                data = r.json()
                if "choices" not in data:
                    raise ValueError("no choices: " + json.dumps(data)[:300])
                return data["choices"][0]["message"]["content"] or ""
            if r.status_code != 429 and r.status_code < 500:
                raise RuntimeError("OpenRouter %d: %s" % (r.status_code, r.text[:400]))
            last = RuntimeError("OpenRouter %d" % r.status_code)
        except Exception as exc:  # noqa
            last = exc
        if attempt < attempts:
            time.sleep(4 * attempt)
    raise last


def _json(raw, label="response"):
    m = re.search(r"```(?:json)?\s*(.+?)```", raw, re.S)
    if m:
        raw = m.group(1)
    starts = [i for i in (raw.find("{"), raw.find("[")) if i != -1]
    if not starts:
        print("--- %s: no JSON. Raw ---\n%s\n---" % (label, raw[:1500]))
        raise ValueError("%s: no JSON" % label)
    return json.JSONDecoder().raw_decode(raw[min(starts):])[0]


# ---------------------------------------------------------------- prompts
GUARD = (
    "GUARDRAILS (absolute): summarise every item in your OWN words (no copying "
    "sentences from the source); at most one short quoted phrase under 20 words "
    "if essential. Every item needs a REAL url you actually found via search - "
    "never invent a url; if you cannot verify one, drop the item. Neutral, "
    "factual tone with concrete names, numbers and dates. Prefer the last 7 "
    "days. No investment advice. Exclude anything negative toward Qatar, "
    "QatarEnergy or Woqod."
)


def research(date_human):
    beats_spec = "\n".join("- %s (key \"%s\"): %s" % (t, k, d) for k, t, d in BEATS)
    prompt = (
        "You are the research desk for The AI Treasurer, a daily briefing for CFOs and corporate "
        "treasurers. Today is %s. Search the web for the most important recent developments and "
        "return STRICT JSON only (no prose) with this exact shape:\n"
        "{\n"
        '  "beats": { "<key>": [ {"headline": "...", "summary": "...", '
        '"sources": [{"name":"Publication","url":"https://..."}] } ] },\n'
        '  "dashboard": [ {"value":"4.5-4.75%","label":"short label","src":"source + date"} ],\n'
        '  "events": [ {"when":"16-18 Sep 2026","place":"City - status","title":"...","blurb":"...",'
        '"source":{"name":"...","url":"https://..."}} ]\n'
        "}\n\n"
        "BEATS (produce 2-3 items each, newest/most material first):\n%s\n\n"
        "dashboard: exactly 6 treasury-relevant figures (rates, FX, stablecoin/market, payments "
        "adoption, liquidity) each with a short label and a source+date in 'src'. "
        "events: 2-3 real treasury conferences/deadlines (e.g. Sibos, EuroFinance, AFP, ISO 20022 "
        "dates), with a real source url.\n\n"
        "summary = 30-45 words, original wording. %s" % (date_human, beats_spec, GUARD)
    )
    data = _json(_ask(prompt, RESEARCH_MODEL, search=True), "research")
    # validate
    beats = data.get("beats", {})
    for k in BEAT_KEYS:
        items = beats.get(k) or []
        items = [it for it in items if it.get("headline") and it.get("summary") and (it.get("sources"))]
        if not items:
            raise ValueError("research: beat '%s' has no usable items" % k)
        beats[k] = items[:3]
    data["beats"] = beats
    data["dashboard"] = (data.get("dashboard") or [])[:6]
    data["events"] = (data.get("events") or [])[:3]
    if len(data["dashboard"]) < 4 or not data["events"]:
        raise ValueError("research: dashboard/events incomplete")
    return data


def editorial(res, date_human):
    flat = []
    for k, t, _ in BEATS:
        for it in res["beats"][k]:
            flat.append("[%s] %s - %s" % (t, it["headline"], it["summary"]))
    prompt = (
        "You are the editor of The AI Treasurer (plain-English, controls-first voice for CFOs and "
        "treasurers). Today is %s. Based ONLY on the items below, return STRICT JSON:\n"
        "{\n"
        '  "editor_note": ["paragraph one", "paragraph two"],\n'
        '  "lead_stories": [ {"headline":"...","blurb":"1-2 sentences","tab":"ai|treasury|markets"} ]\n'
        "}\n\n"
        "editor_note: exactly 2 short paragraphs, no greeting, no salutation, tying the day's items to "
        "the site's thesis that speed is easy but every automated step must leave a trail an auditor "
        "can follow. Do NOT mention any personal name. lead_stories: exactly 3, each pointing to the "
        "tab where the detail sits (tab is one of ai, treasury, markets). Plain, concrete, no advice.\n\n"
        "ITEMS:\n%s" % (date_human, "\n".join(flat))
    )
    data = _json(_ask(prompt, EDITORIAL_MODEL, search=False), "editorial")
    note = data.get("editor_note") or []
    leads = data.get("lead_stories") or []
    if len(note) < 2 or len(leads) < 3:
        raise ValueError("editorial: note/leads incomplete")
    data["editor_note"] = note[:2]
    data["lead_stories"] = leads[:3]
    return data


# ---------------------------------------------------------------- rendering
def _sources_html(sources, prefix="Via "):
    links = []
    for s in sources:
        name, url = esc(s.get("name")), s.get("url", "#")
        links.append('<a href="%s" target="_blank" rel="noopener">%s</a>' % (html.escape(url, quote=True), name))
    if not links:
        return ""
    joined = links[0] if len(links) == 1 else " and ".join([", ".join(links[:-1]), links[-1]]) if len(links) > 2 else " and ".join(links)
    return prefix + joined


def _items_html(items):
    out = []
    for it in items:
        out.append('<div class="item"><h4>%s</h4><p>%s</p><div class="cite">%s</div></div>'
                   % (esc(it["headline"]), esc(it["summary"]), _sources_html(it.get("sources", []))))
    return "\n        ".join(out)


def render(content, date_human, edition_n, archive_entries):
    s = open(TEMPLATE, encoding="utf-8").read()
    # dateline
    dateline = '<span><b>%s</b></span>\n      <span>Edition No. %d</span>' % (esc(date_human), edition_n)
    s = s.replace("<!--DATELINE-->", dateline)
    # editor note
    note = content["editor_note"]
    s = s.replace("<!--EDITOR_NOTE-->", "<p>%s</p>\n        <p>%s</p>" % (esc(note[0]), esc(note[1])))
    # dashboard
    cells = []
    for c in content["dashboard"]:
        cells.append('<div class="cell"><div class="val">%s</div><div class="lbl">%s</div><div class="src">%s</div></div>'
                     % (esc(c.get("value")), esc(c.get("label")), esc(c.get("src"))))
    s = s.replace("<!--DASHBOARD-->", "\n          ".join(cells))
    # lead stories
    leads = []
    for L in content["lead_stories"]:
        tab = L.get("tab", "ai")
        label = LEAD_TAB_LABEL.get(tab, "AI &amp; Technology")
        leads.append('<div class="item"><h4>%s</h4><p>%s</p><div class="cite">See <a href="#" onclick="showTab(\'%s\');return false;">%s &rarr;</a></div></div>'
                     % (esc(L["headline"]), esc(L["blurb"]), tab, label))
    s = s.replace("<!--LEAD-->", "\n      ".join(leads))
    # beats
    for k in BEAT_KEYS:
        s = s.replace("<!--BEAT_%s-->" % k, _items_html(content["beats"][k]))
    # events
    evs = []
    for e in content["events"]:
        src = e.get("source") or {}
        cite = ""
        if src.get("url"):
            cite = ('<div class="cite" style="margin-top:6px;font-size:13px;color:var(--navy-soft);">Via '
                    '<a href="%s" target="_blank" rel="noopener" style="border-bottom:1px solid var(--gold);text-decoration:none;color:var(--navy);">%s</a></div>'
                    % (html.escape(src.get("url"), quote=True), esc(src.get("name"))))
        evs.append('<div class="event"><div class="when">%s<small>%s</small></div><div><h4>%s</h4><p>%s</p>%s</div></div>'
                   % (esc(e.get("when")), esc(e.get("place")), esc(e.get("title")), esc(e.get("blurb")), cite))
    s = s.replace("<!--EVENTS-->", "\n      ".join(evs))
    # archive
    s = s.replace("<!--ARCHIVE-->", _archive_html(archive_entries))
    return s


def _archive_html(entries):
    # entries: list newest-first of {n, human, month, file, current}
    if not entries:
        return '<p style="font-size:13px;color:var(--navy-soft);">Editions will appear here.</p>'
    cur_month = entries[0]["month"]
    this_month = [e for e in entries if e["month"] == cur_month]
    older = [e for e in entries if e["month"] != cur_month]
    lis = []
    for e in this_month:
        href = "/" if e.get("current") else e["file"]
        tail = " &middot; current edition" if e.get("current") else ""
        lis.append('<li><span class="no">Edition No. %d</span> <span class="dt"><a href="%s" style="color:var(--navy);text-decoration:none;border-bottom:1px solid var(--gold);">%s</a>%s</span></li>'
                   % (e["n"], href, esc(e["human"]), tail))
    out = '<div class="arch-month">%s <small>&middot; current month</small></div>\n      <ul class="edlist">\n        %s\n      </ul>' % (esc(cur_month), "\n        ".join(lis))
    if older:
        olis = ['<li><span class="no">Edition No. %d</span> <span class="dt"><a href="%s" style="color:var(--navy);text-decoration:none;border-bottom:1px solid var(--gold);">%s</a></span></li>' % (e["n"], e["file"], esc(e["human"])) for e in older]
        out += '\n      <div class="arch-month" style="margin-top:28px;">Earlier editions</div>\n      <ul class="edlist">\n        %s\n      </ul>' % "\n        ".join(olis)
    else:
        out += '\n      <p style="font-size:13px;color:var(--navy-soft);">Earlier months will appear here as the archive grows.</p>'
    return out


# ---------------------------------------------------------------- state
def load_state():
    if os.path.isfile(STATE):
        return json.load(open(STATE, encoding="utf-8"))
    return {"editions": [], "last_n": 0}


def save_state(st):
    os.makedirs(EDITIONS_DIR, exist_ok=True)
    json.dump(st, open(STATE, "w", encoding="utf-8"), indent=2)


def mock_content():
    def it(h, s, n, u):
        return {"headline": h, "summary": s, "sources": [{"name": n, "url": u}]}
    one = [it("Sample headline", "Sample original summary of a treasury development, about forty words long to mirror the real output of the generator so the layout can be checked end to end without calling any API.", "Example", "https://example.com/a")]
    beats = {k: list(one) for k in BEAT_KEYS}
    return {
        "beats": beats,
        "dashboard": [{"value": "4.5-4.75%", "label": "10Y UST range", "src": "Outlook, 2026"}] * 6,
        "events": [{"when": "Soon", "place": "City - upcoming", "title": "A treasury event", "blurb": "Short blurb.", "source": {"name": "Example", "url": "https://example.com"}}],
        "editor_note": ["First paragraph of the editor note for layout testing.", "Second paragraph tying it to controls and the audit trail."],
        "lead_stories": [
            {"headline": "Lead one", "blurb": "Blurb.", "tab": "ai"},
            {"headline": "Lead two", "blurb": "Blurb.", "tab": "treasury"},
            {"headline": "Lead three", "blurb": "Blurb.", "tab": "markets"},
        ],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mock", action="store_true", help="render with canned content (no API)")
    ap.add_argument("--out", default=INDEX, help="output path for the latest edition")
    args = ap.parse_args()

    now = datetime.datetime.utcnow() + datetime.timedelta(hours=TZ_OFFSET)
    date_iso = now.strftime("%Y-%m-%d")
    date_human = now.strftime("%A, %-d %B %Y") if os.name != "nt" else now.strftime("%A, %d %B %Y")
    month_human = now.strftime("%B %Y")

    st = load_state()
    # self-heal: if today's edition already built, exit quietly (lets retry crons no-op)
    if any(e["date"] == date_iso for e in st["editions"]) and not args.mock:
        print("Edition for %s already exists; nothing to do." % date_iso)
        return 0

    edition_n = (st["last_n"] + 1) if not args.mock else (st["last_n"] + 1 or 1)

    if args.mock:
        content = mock_content()
    else:
        res = research(date_human)
        ed = editorial(res, date_human)
        content = dict(res); content.update(ed)

    # archive entries (newest first): this new edition marked current
    entries = [{"n": edition_n, "date": date_iso, "human": date_human, "month": month_human,
                "file": "/editions/%s.html" % date_iso, "current": True}]
    for e in sorted(st["editions"], key=lambda x: x["date"], reverse=True):
        e = dict(e); e["current"] = False
        entries.append(e)

    page = render(content, date_human, edition_n, entries)

    if args.mock:
        out = args.out if args.out != INDEX else "/tmp/mock_index.html"
        open(out, "w", encoding="utf-8").write(page)
        print("MOCK wrote", out, "bytes", len(page))
        return 0

    # write latest + dated archive copy
    open(args.out, "w", encoding="utf-8").write(page)
    os.makedirs(EDITIONS_DIR, exist_ok=True)
    shutil.copyfile(args.out, os.path.join(EDITIONS_DIR, "%s.html" % date_iso))
    # update state
    st["editions"].append({"n": edition_n, "date": date_iso, "human": date_human,
                           "month": month_human, "file": "/editions/%s.html" % date_iso})
    st["last_n"] = edition_n
    save_state(st)
    print("Edition %d - %s" % (edition_n, date_human))
    return 0


if __name__ == "__main__":
    sys.exit(main())
