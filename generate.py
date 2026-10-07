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
EDITORIAL_MODEL = os.environ.get("BRIEF_EDITORIAL_MODEL", "anthropic/claude-opus-5.5")
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
    ("financial_crime","Sanctions, AML & KYC",
     "sanctions regime changes (OFAC, UN, EU, UK), AML/KYC rules, FATF updates and enforcement that affect corporate payments, bank onboarding and counterparty screening"),
    ("reporting",    "Accounting & Reporting Standards",
     "IFRS / US GAAP developments relevant to treasury (IFRS 9 hedge accounting, IAS 7 supplier-finance disclosures, IFRS 18, IFRS 16), ISSB sustainability reporting, audit-regulator actions"),
]
# Newer, narrower sections: if research finds nothing material, show a quiet note instead of failing the edition.
OPTIONAL_BEATS = {"financial_crime", "reporting"}
BEAT_KEYS = [b[0] for b in BEATS]
LEAD_TAB_LABEL = {"ai": "AI &amp; Technology", "treasury": "Treasury &amp; Payments", "markets": "Risk &amp; Markets", "regulation": "Regulation &amp; Compliance"}


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


def _balanced_prefix(t):
    """Return the first balanced {...} or [...] substring, ignoring trailing text."""
    depth = 0; instr = False; esc = False
    for i, ch in enumerate(t):
        if instr:
            if esc: esc = False
            elif ch == "\\": esc = True
            elif ch == '"': instr = False
            continue
        if ch == '"': instr = True
        elif ch in "{[": depth += 1
        elif ch in "}]":
            depth -= 1
            if depth == 0:
                return t[:i + 1]
    return None


def _json(raw, label="response"):
    """Tolerant JSON extraction: strips fences/prose, trailing commas, trailing text."""
    txt = raw or ""
    m = re.search(r"```(?:json)?\s*(.+?)```", txt, re.S)
    if m:
        txt = m.group(1)
    starts = [i for i in (txt.find("{"), txt.find("[")) if i != -1]
    if not starts:
        print("--- %s: no JSON found. Raw model output ---\n%s\n--- end ---" % (label, (raw or "")[:3000]))
        raise ValueError("%s: no JSON in model response" % label)
    txt = txt[min(starts):]
    cands = []
    bal = _balanced_prefix(txt)
    if bal:
        cands.append(bal)
    last = max(txt.rfind("}"), txt.rfind("]"))
    if last != -1:
        cands.append(txt[:last + 1])
    cands.append(txt)
    # also try each candidate with trailing commas removed
    cands += [re.sub(r",(\s*[}\]])", r"\1", c) for c in list(cands)]
    for c in cands:
        try:
            return json.loads(c)
        except Exception:
            try:
                return json.JSONDecoder().raw_decode(c)[0]
            except Exception:
                continue
    print("--- %s: unparseable JSON. Raw model output ---\n%s\n--- end ---" % (label, (raw or "")[:3000]))
    raise ValueError("%s: unparseable JSON" % label)


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
        '  "events": [ {"when":"16-18 Sep 2026","place":"City - status","title":"...","blurb":"...",'
        '"source":{"name":"...","url":"https://..."}} ]\n'
        "}\n\n"
        "BEATS (produce 2-3 items each, newest/most material first):\n%s\n\n"
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
        if not items and k not in OPTIONAL_BEATS:
            raise ValueError("research: beat '%s' has no usable items" % k)
        beats[k] = items[:3]
    data["beats"] = beats
    data["events"] = (data.get("events") or [])[:3]
    if not data["events"]:
        raise ValueError("research: events incomplete")
    return data


STYLE = (
    "HOUSE STYLE (The AI Treasurer, written for CFOs and group treasurers):\n"
    "- Write like a senior Treasury practitioner briefing a CFO: confident, precise, plain English. "
    "British spelling (organisation, tokenisation, programme).\n"
    "- Lead with the insight, never with the page. Never refer to the briefing itself: no 'today's items', "
    "'today's stories', 'this briefing', 'in this edition', 'the items below', 'this week's news shows'.\n"
    "- Each paragraph opens with a clear claim a CFO would care about, then the evidence (named firms, "
    "figures, dates), then what it means for control, cash or risk.\n"
    "- Active voice, varied sentence length, no filler. Avoid: 'it is worth noting', 'in today's fast-paced', "
    "'landscape', 'game-changer', 'unlock', 'leverage' (as a verb), 'delve', 'navigate', 'robust', "
    "'seamless', 'cutting-edge', 'paradigm', 'Moreover', 'Furthermore', 'In conclusion'.\n"
    "- Do not open two sentences in a row with the same word. Do not list more than three names in a row.\n"
    "- Always write 'Treasury' with a capital T when it means the function or profession.\n"
    "- No hype, no advice, no personal names of the author."
)

BANNED = [r"\btoday'?s items\b", r"\btoday'?s stories\b", r"\bthis briefing\b", r"\bin this edition\b",
          r"\bthe items (below|above)\b", r"\blandscape\b", r"\bgame[- ]changer\b", r"\bdelve\b",
          r"\bseamless(ly)?\b", r"\bcutting[- ]edge\b", r"\bparadigm\b", r"\bit is worth noting\b",
          r"\bmoreover\b", r"\bfurthermore\b", r"\bin conclusion\b"]


def _lint(texts):
    hits = set()
    for t in texts:
        for pat in BANNED:
            m = re.search(pat, t or "", re.I)
            if m:
                hits.add(m.group(0))
    return sorted(hits)


def _cap_treasury(t):
    # House rule: Treasury with a capital T (function/profession). Leaves URLs untouched.
    return re.sub(r"\btreasury\b", "Treasury", t or "")


def editorial(res, date_human):
    flat, slots = [], []
    for k, t, _ in BEATS:
        for i, it in enumerate(res["beats"][k]):
            slots.append((k, i))
            flat.append('%d. [%s] %s - %s' % (len(slots), t, it["headline"], it["summary"]))
    base = (
        "You are the editor of The AI Treasurer, a controls-first daily read for CFOs and corporate "
        "treasurers. Today is %s. Using ONLY the facts in the numbered items below (add nothing new, "
        "keep every figure, name and date exactly), return STRICT JSON:\n"
        "{\n"
        '  "editor_note": ["paragraph one", "paragraph two"],\n'
        '  "lead_stories": [ {"topic":"2-4 word topic","headline":"...","blurb":"1-2 sentences","tab":"ai|treasury|markets|regulation"} ],\n'
        '  "items": [ {"n": 1, "headline": "...", "summary": "..."} ]\n'
        "}\n\n"
        "editor_note: exactly 2 paragraphs of 3-4 sentences each, no greeting. Paragraph one names the "
        "single most important shift across the items and backs it with two or three specific examples. "
        "Paragraph two draws the control implication: speed is easy, but every automated step must leave "
        "a trail an auditor can follow. End on a sharp, quotable line, not a summary.\n"
        "lead_stories: exactly 3, the most material items, each pointing to the tab where the detail sits "
        "(tab is one of ai, treasury, markets, regulation). topic: 2-4 words, title case (e.g. 'Agentic "
        "Treasury Controls', 'ISO 20022 Migration'); never 'Lead story'.\n"
        "items: rewrite EVERY numbered item (same n). headline: under 14 words, specific, no clickbait, no "
        "trailing full stop. summary: 30-45 words, what happened, the key number, and why a treasurer "
        "should care. Do not copy the source wording.\n\n"
        "%s\n\nITEMS:\n%s" % (date_human, STYLE, "\n".join(flat))
    )
    prompt, data = base, None
    for attempt in range(2):
        data = _json(_ask(prompt, EDITORIAL_MODEL, max_tokens=12000, search=False), "editorial")
        note = data.get("editor_note") or []
        leads = data.get("lead_stories") or []
        if len(note) < 2 or len(leads) < 3:
            raise ValueError("editorial: note/leads incomplete")
        texts = list(note[:2]) + [L.get("blurb", "") + " " + L.get("headline", "") for L in leads[:3]]
        texts += [(x.get("headline", "") + " " + x.get("summary", "")) for x in (data.get("items") or [])]
        hits = _lint(texts)
        if not hits:
            break
        prompt = base + ("\n\nYOUR PREVIOUS DRAFT USED BANNED PHRASES: %s. Rewrite without them." % ", ".join(hits))
    data["editor_note"] = [_cap_treasury(p) for p in data["editor_note"][:2]]
    data["lead_stories"] = data["lead_stories"][:3]
    for L in data["lead_stories"]:
        L["headline"] = _cap_treasury(L.get("headline", "")); L["blurb"] = _cap_treasury(L.get("blurb", ""))
    # merge polished item text back; keep researched text if a rewrite is missing or empty
    rewrites = {}
    for x in data.get("items") or []:
        try:
            rewrites[int(x.get("n"))] = x
        except (TypeError, ValueError):
            pass
    for n, (k, i) in enumerate(slots, start=1):
        it = res["beats"][k][i]
        rw = rewrites.get(n) or {}
        if rw.get("headline") and rw.get("summary"):
            it["headline"], it["summary"] = rw["headline"].rstrip("."), rw["summary"]
        it["headline"], it["summary"] = _cap_treasury(it["headline"]), _cap_treasury(it["summary"])
    data.pop("items", None)
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
    if not items:
        return '<p class="pillar-sub" style="margin:0;">No material developments today.</p>'
    out = []
    for it in items:
        out.append('<div class="item"><h4>%s</h4><p>%s</p><div class="cite">%s</div></div>'
                   % (esc(it["headline"]), esc(it["summary"]), _sources_html(it.get("sources", []))))
    return "\n        ".join(out)


def render(content, date_human, edition_n, archive_entries):
    s = open(TEMPLATE, encoding="utf-8").read()
    # dateline
    dateline = '<span><b>%s</b></span>' % esc(date_human)
    s = s.replace("<!--DATELINE-->", dateline)
    # editor note
    note = content["editor_note"]
    s = s.replace("<!--EDITOR_NOTE-->", "<p>%s</p>\n        <p>%s</p>" % (esc(note[0]), esc(note[1])))
    # dashboard
    cells = []
    for c in content.get("dashboard") or []:
        cells.append('<div class="cell"><div class="val">%s</div><div class="lbl">%s</div><div class="src">%s</div></div>'
                     % (esc(c.get("value")), esc(c.get("label")), esc(c.get("src"))))
    s = s.replace("<!--DASHBOARD-->", "\n          ".join(cells))
    # lead stories
    leads = []
    for L in content["lead_stories"]:
        tab = L.get("tab", "ai")
        label = LEAD_TAB_LABEL.get(tab, "AI &amp; Technology")
        topic = (L.get("topic") or "").strip() or LEAD_TAB_LABEL.get(tab, "AI &amp; Technology").replace("&amp;", "&")
        leads.append('<div class="item"><div class="topic">%s</div><h4>%s</h4><div class="cite">See <a href="#" onclick="showTab(\'%s\');return false;">%s &rarr;</a></div></div>'
                     % (esc(topic), esc(L["headline"]), tab, label))
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
    # entries: list newest-first of {n, human, month, file, current}; grouped under "Month Year"
    if not entries:
        return '<p style="font-size:13px;color:var(--navy-soft);">Briefings will appear here.</p>'
    link = '<a href="%s" style="color:var(--navy);text-decoration:none;border-bottom:1px solid var(--gold);">%s</a>'
    months = []
    for e in entries:
        if not months or months[-1][0] != e["month"]:
            months.append((e["month"], []))
        months[-1][1].append(e)
    blocks = []
    for i, (month, items) in enumerate(months):
        lis = []
        for e in items:
            href = "/" if e.get("current") else e["file"]
            tail = " &middot; today" if e.get("current") else ""
            lis.append('<li><span class="dt">%s%s</span></li>' % (link % (href, esc(e["human"])), tail))
        style = '' if i == 0 else ' style="margin-top:28px;"'
        blocks.append('<div class="arch-month"%s>%s</div>\n      <ul class="edlist">\n        %s\n      </ul>'
                      % (style, esc(month), "\n        ".join(lis)))
    return "\n      ".join(blocks)


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
            {"topic": "Agentic Treasury Controls", "headline": "Lead one", "blurb": "Blurb.", "tab": "ai"},
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
