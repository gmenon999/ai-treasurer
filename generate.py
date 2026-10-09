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
HISTORY = os.path.join(EDITIONS_DIR, "history.json")  # recent stories and notes, for no-repeat checks
HISTORY_DAYS = 14

API_URL = "https://openrouter.ai/api/v1/chat/completions"
RESEARCH_MODEL = os.environ.get("BRIEF_RESEARCH_MODEL", "anthropic/claude-haiku-4.5")
EDITORIAL_MODEL = os.environ.get("BRIEF_EDITORIAL_MODEL", "anthropic/claude-opus-5.5")
SEARCH_ENGINE = os.environ.get("BRIEF_SEARCH_ENGINE", "exa")
MAX_SEARCHES = int(os.environ.get("BRIEF_MAX_SEARCHES", "10"))
TZ_OFFSET = int(os.environ.get("BRIEF_TZ_OFFSET_HOURS", "3"))  # Qatar = UTC+3
CHECK_MODEL = os.environ.get("BRIEF_CHECK_MODEL", "anthropic/claude-sonnet-5.5")  # independent checker: never the drafting model
TAKES_ON = (os.environ.get("BRIEF_TAKES") or "on").strip().lower() not in ("off", "false", "0", "no")
TAKES_LOG = os.path.join(HERE, "editions", "takes-log.json")
COST = {"usd": 0.0}  # running OpenRouter cost for this run (from the usage block)
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
# Which briefing tab each beat is shown under (must match edition_template.html panels).
BEAT_TAB = {"ai_agentic": "ai", "treasury_tech": "ai", "cash": "treasury", "payments": "treasury",
            "risk": "markets", "digital": "markets", "markets_macro": "markets",
            "regulation": "regulation", "financial_crime": "regulation", "reporting": "regulation"}
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
    body = {"model": model, "messages": [{"role": "user", "content": prompt}], "max_tokens": max_tokens,
            "usage": {"include": True}}
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
                try:
                    COST["usd"] += float((data.get("usage") or {}).get("cost") or 0)
                except (TypeError, ValueError):
                    pass
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
    "days; for the narrower sections (sanctions and accounting standards) look back up to 30 days and always return at least one item with its date in the summary. "
    "No investment advice. Exclude anything negative toward Qatar, "
    "QatarEnergy or Woqod."
)


def research(date_human, history=None):
    covered = []
    for d in (history or {}).get("days", [])[-HISTORY_DAYS:]:
        covered += [it.get("headline", "") for it in d.get("items", [])]
    covered_txt = "\n".join("- " + h for h in covered[-80:]) or "- (none yet)"
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
        "NO REPEATS: the stories below already ran in the last two weeks. Do not return them again. "
        "Only include a follow-up if something materially new has happened, and then the headline must "
        "say what is new.\n%s\n\n"
        "summary = 30-45 words, original wording. %s" % (date_human, beats_spec, covered_txt, GUARD)
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


# ---------------------------------------------------------------- no-repeat memory
import difflib


def load_history():
    try:
        return json.load(open(HISTORY, encoding="utf-8"))
    except Exception:
        return {"days": []}


def save_history(h, date_iso, content):
    day = {"date": date_iso,
           "note_headline": content.get("note_headline", ""),
           "note_opening": (content.get("editor_note") or [""])[0][:220],
           "items": []}
    for k in BEAT_KEYS:
        for it in content["beats"].get(k) or []:
            urls = [x.get("url") for x in it.get("sources") or [] if x.get("url")]
            day["items"].append({"headline": it.get("headline", ""), "urls": urls})
    days = [d for d in h.get("days", []) if d.get("date") != date_iso] + [day]
    cutoff = (datetime.date.fromisoformat(date_iso) - datetime.timedelta(days=HISTORY_DAYS)).isoformat()
    h = {"days": [d for d in days if d.get("date", "") >= cutoff]}
    os.makedirs(EDITIONS_DIR, exist_ok=True)
    json.dump(h, open(HISTORY, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


def _norm(t):
    return re.sub(r"[^a-z0-9 ]", "", (t or "").lower())


def _similar(a, b):
    return difflib.SequenceMatcher(None, _norm(a), _norm(b)).ratio() >= 0.72


def dedupe(res, history):
    """Drop repeats: the same story twice today, or a story already run in the last two weeks.
    A required beat always keeps at least one item."""
    seen_urls, seen_heads = set(), []
    for d in history.get("days", []):
        for it in d.get("items", []):
            seen_urls.update(it.get("urls") or []); seen_heads.append(it.get("headline", ""))
    today_urls, today_heads, dropped = set(), [], []
    for k in BEAT_KEYS:
        kept, fallback = [], []
        for it in res["beats"].get(k) or []:
            urls = set(x.get("url") for x in it.get("sources") or [] if x.get("url"))
            h = it.get("headline", "")
            repeat_today = bool(urls & today_urls) or any(_similar(h, x) for x in today_heads)
            repeat_past = bool(urls & seen_urls) or any(_similar(h, x) for x in seen_heads)
            if repeat_today or repeat_past:
                dropped.append(h)
                if not repeat_today:
                    fallback.append((it, urls, h))
                continue
            kept.append(it); today_urls |= urls; today_heads.append(h)
        if not kept and fallback:
            it, urls, h = fallback[0]  # never leave a section empty: reuse a recent story, never a same-day duplicate
            kept = [it]; today_urls |= urls; today_heads.append(h); dropped.remove(h)
        res["beats"][k] = kept
    if dropped:
        print("dedupe: dropped %d repeat(s): %s" % (len(dropped), "; ".join(dropped)[:400]))
    return res


def fill_empty(res, date_human, history):
    """No section is ever published empty: for any beat left with no items, run a focused search with a longer
    look-back (60 days, then 180), accept the freshest verified item, and say the date in the summary."""
    spec = {k: (t, d) for k, t, d in BEATS}
    for k in BEAT_KEYS:
        if res["beats"].get(k):
            continue
        t, d = spec[k]
        got = []
        for days in (60, 180):
            prompt = (
                "You are the research desk for The AI Treasurer. Today is %s. Search the web for the most recent "
                "material developments, up to the last %d days, for this section of a briefing for CFOs and corporate "
                "treasurers: %s - %s.\nReturn STRICT JSON only: {\"items\": [{\"headline\": \"...\", \"summary\": \"...\", "
                "\"sources\": [{\"name\": \"Publication\", \"url\": \"https://...\"}]}]} with 1-2 items, newest first. "
                "summary = 30-45 words, original wording, and it must state the date of the development (for example "
                "'On 17 June ...'). Never return an empty list: choose the most relevant development you can verify. %s"
                % (date_human, days, t, d, GUARD))
            try:
                data = _json(_ask(prompt, RESEARCH_MODEL, search=True), "fill %s" % k)
            except Exception as e:
                print("fill_empty: %s attempt failed (%s)" % (k, e))
                continue
            got = [it for it in (data.get("items") or []) if it.get("headline") and it.get("summary") and it.get("sources")]
            if got:
                break
        if not got:
            raise ValueError("research: section '%s' is empty even after a 180-day search; not publishing an empty section" % k)
        res["beats"][k] = got[:2]
        print("fill_empty: filled '%s' with %d item(s)" % (k, len(res["beats"][k])))
    return res


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
    "- Always write 'Treasury' with a capital T and 'Finance' with a capital F when they mean the function, "
    "team or profession (e.g. 'Finance Teams', 'the Finance function'). 'financial' stays lower case.\n"
    "- No hype, no advice, no personal names of the author."
)

BANNED = [r"\btoday['\u2019]?s items\b", r"\btoday['\u2019]?s stories\b", r"\bitems show\b", r"\bstories show\b",
          r"\bthis week['\u2019]?s news\b", r"\bkey takeaways?\b", r"\bin summary\b", r"\bever-evolving\b",
          r"\btransformative\b", r"\brevolutioni[sz]e\b", r"\bthis briefing\b", r"\bin this edition\b",
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
    # House rule: Treasury with a capital T and Finance with a capital F (function/profession).
    # Leaves URLs and 'financial' untouched.
    t = re.sub(r"\btreasury\b", "Treasury", t or "")
    return re.sub(r"(?<![/.\w-])finance\b(?![\w-]*\.)", "Finance", t)


def editorial(res, date_human, history=None):
    recent = (history or {}).get("days", [])[-5:]
    recent_notes = "\n".join("- %s | %s" % (d.get("note_headline", ""), d.get("note_opening", "")[:120]) for d in recent) or "- (none yet)"
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
        '  "note_headline": "the insight in one line",\n'
        '  "editor_note": ["paragraph one", "paragraph two"],\n'
        '  "lead_stories": [ {"topic":"2-4 word topic","headline":"...","blurb":"1-2 sentences","tab":"ai|treasury|markets|regulation"} ],\n'
        '  "items": [ {"n": 1, "headline": "...", "summary": "..."} ]\n'
        "}\n\n"
        "note_headline: the single insight of the day as a declarative headline a CFO would repeat, "
        "8-14 words, no colon, no question, no date, no 'today'. It states a conclusion, not a topic "
        "(good: 'Treasury agents now act on their own, and controls are only starting to catch up'; "
        "bad: 'AI agents in Treasury').\n"
        "editor_note: exactly 2 paragraphs, 2-4 sentences and 35-60 words each, written answer-first like a McKinsey "
        "executive summary. Paragraph one: the shift, stated as a claim in the first sentence, then the "
        "two or three strongest pieces of evidence (named firms, figures). Paragraph two: the so-what for "
        "a CFO or treasurer, through the lens of control, cash or risk, ending on one sharp line. Never "
        "open a paragraph with 'Today', 'This week', 'In', 'As' or 'With', and never mention the briefing, "
        "items, stories or edition.\n"
        "SCOPE: the note speaks to the whole finance function, not Treasury alone. Write for the CFO, "
        "the controller and the treasurer together: where the items allow, pair a Treasury development "
        "with one for accounting, reporting, audit or the wider finance team, and make the so-what "
        "apply to Finance as a whole. Prefer 'Treasury and Finance' over 'Treasury' when framing.\n"
        "Recent notes (do NOT reuse their angle, headline wording or opening):\n%s\n"
        "lead_stories: exactly 3, the most material items, each pointing to the tab where the detail sits "
        "(tab is one of ai, treasury, markets, regulation). topic: 2-4 words, title case (e.g. 'Agentic "
        "Treasury Controls', 'ISO 20022 Migration'); never 'Lead story'.\n"
        "items: rewrite EVERY numbered item (same n). headline: under 14 words, specific, no clickbait, no "
        "trailing full stop. summary: 30-45 words, what happened, the key number, and why a treasurer "
        "should care. Do not copy the source wording.\n\n"
        "%s\n\nITEMS:\n%s" % (date_human, recent_notes, STYLE, "\n".join(flat))
    )
    prompt, data = base, None
    for attempt in range(3):
        data = _json(_ask(prompt, EDITORIAL_MODEL, max_tokens=12000, search=False), "editorial")
        note = data.get("editor_note") or []
        leads = data.get("lead_stories") or []
        if len(note) < 2 or len(leads) < 3:
            raise ValueError("editorial: note/leads incomplete")
        texts = list(note[:2]) + [L.get("blurb", "") + " " + L.get("headline", "") for L in leads[:3]]
        texts += [(x.get("headline", "") + " " + x.get("summary", "")) for x in (data.get("items") or [])]
        texts.append(data.get("note_headline", ""))
        problems = ["banned phrases: " + ", ".join(h) for h in [_lint(texts)] if h]
        problems += _note_problems(data, recent)
        if not problems:
            break
        print("editorial: redraft (%s)" % "; ".join(problems))
        prompt = base + ("\n\nYOUR PREVIOUS DRAFT FAILED THE QUALITY CHECK: %s. Rewrite to fix every point." % "; ".join(problems))
    data["editor_note"] = [_cap_treasury(p) for p in data["editor_note"][:2]]
    data["note_headline"] = _cap_treasury((data.get("note_headline") or "").strip().rstrip("."))
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


# ---------------------------------------------------------------- The Treasurer's Take
# Two lines under every item: "Focus: <lens>" (the one point that matters most, through the lens that
# fits the story) and "Our view". Written from the full source article, checked by an independent model,
# dropped (never published) if it fails. Fail-safe: any error here leaves the news untouched.
LENSES = ["Controls & audit", "Regulation & compliance", "Accounting & reporting", "Cash & liquidity",
          "Risk", "Payments & operations", "Cost & value"]
LENS_GUIDE = (
    "- Controls & audit: approvals, segregation of duties, audit trail, evidence, AI governance. Name the control-trail "
    "step (Capture, Match, Flag, Approve, Post) or foundation (Govern, Access, Change, Monitor) and the auditor's question.\n"
    "- Regulation & compliance: a rule, regulator action, deadline, sanctions or AML/KYC duty. Say who must act and by when.\n"
    "- Accounting & reporting: IFRS / US GAAP treatment, disclosure, classification or measurement.\n"
    "- Cash & liquidity: visibility, forecasting, pooling, funding, working capital, trapped cash.\n"
    "- Risk: FX, rates, commodity, counterparty or settlement exposure and how to limit it.\n"
    "- Payments & operations: payment rails, settlement timing, process, connectivity, systems.\n"
    "- Cost & value: fees, efficiency, return on technology spend, the business case."
)
SRC_CHARS = 9000


def _page_text(url):
    """Readable text of a source page, or '' if it cannot be read (paywall, consent wall, PDF, error)."""
    try:
        r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0 (compatible; TheAITreasurerBot/1.0; +https://theaitreasurer.com)"})
        if r.status_code != 200 or "html" not in (r.headers.get("content-type") or "").lower():
            return ""
        t = r.text[:2000000]
    except Exception:
        return ""
    t = re.sub(r"(?is)<(script|style|noscript|svg|nav|header|footer|form|aside)\b.*?</\1>", " ", t)
    paras = [html.unescape(re.sub(r"(?s)<[^>]+>", " ", m)) for m in re.findall(r"(?is)<p\b[^>]*>(.*?)</p>", t)]
    paras = [re.sub(r"\s+", " ", x).strip() for x in paras]
    body = " ".join(x for x in paras if len(x) > 40)
    if len(body) < 800:
        body = re.sub(r"\s+", " ", html.unescape(re.sub(r"(?s)<[^>]+>", " ", t))).strip()
    return body[:SRC_CHARS]


def _source_for(it):
    """Full text of the item's first readable source that is clearly about the story; '' if none."""
    marks = set(re.findall(r"\b(?:[A-Z][A-Za-z0-9&.-]{2,}|\d[\d.,]*%?)\b", it.get("summary", "") + " " + it.get("headline", "")))
    marks -= {"The", "This", "That", "Treasury", "Finance", "With", "From", "And"}
    for src in (it.get("sources") or [])[:2]:
        txt = _page_text(src.get("url", ""))
        if len(txt) >= 1200 and sum(1 for m in marks if m in txt) >= min(3, len(marks)):
            return txt
    return ""


def _shingles(t, n=8):
    w = re.findall(r"[a-z0-9]+", (t or "").lower())
    return {" ".join(w[i:i + n]) for i in range(len(w) - n + 1)}


def _local_problems(x, it, src):
    """Mechanical checks that need no model: lens, length, banned words, copying."""
    out = []
    if x.get("lens") not in LENSES:
        out.append("lens not in the list")
    words = len((x.get("focus", "") + " " + x.get("view", "")).split())
    if not 22 <= words <= 50:
        out.append("length %d words (target 30-40)" % words)
    hits = _lint([x.get("focus", ""), x.get("view", "")])
    if hits:
        out.append("banned phrases: " + ", ".join(hits))
    if _shingles(x.get("focus", "") + " " + x.get("view", "")) & (_shingles(src) | _shingles(it.get("summary", ""))):
        out.append("copies wording from the source or summary")
    if difflib.SequenceMatcher(None, _norm(x.get("focus")), _norm(it.get("summary"))).ratio() > 0.6:
        out.append("restates the summary")
    return out


def _take_prompt(rows, fixes=None):
    blocks = []
    for n, it, src in rows:
        blk = "### ITEM %d\nHeadline: %s\nSummary: %s\nSOURCE TEXT:\n%s" % (n, it["headline"], it["summary"], src)
        if fixes and fixes.get(n):
            blk += "\nYOUR PREVIOUS TAKE FAILED THE CHECK: %s. Fix every point." % fixes[n]
        blocks.append(blk)
    return (
        "You write 'The Treasurer's Take' for The AI Treasurer, a controls-first daily read for CFOs, controllers "
        "and group treasurers. For each item below, write two lines (30-40 words in total):\n"
        "1. focus: the ONE point in this story that matters most to Treasury and Finance, through the single lens "
        "that fits it best. Do not restate the summary; say what follows from it. One or two sentences.\n"
        "2. view: one sentence. A clear position or one concrete action a treasurer could take. Not hedged.\n\n"
        "LENSES (pick exactly one, copied exactly; the story decides, never force a lens):\n%s\n\n"
        "RULES: use ONLY facts in the SOURCE TEXT; add no figures, names or dates that are not there. Original "
        "wording, no quotes. Brand voice ('we'), no personal name. Not investment or professional advice; no "
        "buy/sell or vendor recommendations. Nothing negative toward Qatar, QatarEnergy or Woqod.\n\n%s\n\n"
        "Return STRICT JSON only: {\"takes\": [{\"n\": 1, \"lens\": \"...\", \"focus\": \"...\", \"view\": \"...\"}]}\n\n%s"
        % (LENS_GUIDE, STYLE, "\n\n".join(blocks)))


def _check_prompt(rows, drafts):
    blocks = []
    for n, it, src in rows:
        x = drafts[n]
        blocks.append("### ITEM %d\nSOURCE TEXT:\n%s\nNEWS SUMMARY: %s\nTAKE: [Focus: %s] %s | Our view: %s"
                      % (n, src, it["summary"], x.get("lens"), x.get("focus"), x.get("view")))
    return (
        "You are the independent checker for The AI Treasurer. You did not write these takes. Check each one "
        "strictly against its SOURCE TEXT and fail it if ANY test fails:\n"
        "1. Every fact, figure, name and date in the take is supported by the source. Nothing invented or overstated.\n"
        "2. The lens is the one that genuinely fits the story's key point (lenses: %s). Not forced.\n"
        "3. The focus adds a point beyond the news summary rather than repeating it.\n"
        "4. The view takes a clear position or gives a concrete action.\n"
        "5. No investment or professional advice, no buy/sell or vendor recommendation, nothing negative toward "
        "Qatar, QatarEnergy or Woqod, no hype.\n"
        "Return STRICT JSON only: {\"checks\": [{\"n\": 1, \"pass\": true, \"reasons\": \"\"}]} with a short, "
        "specific reason for every fail.\n\n%s" % (", ".join(LENSES), "\n\n".join(blocks)))


def _draft(rows, fixes=None):
    data = _json(_ask(_take_prompt(rows, fixes), EDITORIAL_MODEL, max_tokens=8000, search=False), "takes")
    out = {}
    for x in data.get("takes") or []:
        try:
            n = int(x.get("n"))
        except (TypeError, ValueError):
            continue
        x = {"lens": (x.get("lens") or "").strip(), "focus": _cap_treasury((x.get("focus") or "").strip()),
             "view": _cap_treasury((x.get("view") or "").strip())}
        if x["focus"] and x["view"]:
            out[n] = x
    return out


def _check(rows, drafts):
    """Local checks, then the independent model. Returns {n: reasons} for every failure."""
    fails = {}
    for n, it, src in rows:
        if n not in drafts:
            fails[n] = "no take returned"
            continue
        p = _local_problems(drafts[n], it, src)
        if p:
            fails[n] = "; ".join(p)
    todo = [r for r in rows if r[0] not in fails]
    if todo:
        data = _json(_ask(_check_prompt(todo, drafts), CHECK_MODEL, max_tokens=4000, search=False), "check")
        verdict = {}
        for c in data.get("checks") or []:
            try:
                verdict[int(c.get("n"))] = c
            except (TypeError, ValueError):
                pass
        for n, _, _ in todo:
            c = verdict.get(n)
            if not c or c.get("pass") is not True:
                fails[n] = (c or {}).get("reasons") or "no verdict from checker"
    return fails


def takes(content, date_iso):
    log = {"date": date_iso, "items": [], "cost_usd": None}
    if not TAKES_ON:
        print("takes: switched off (BRIEF_TAKES)")
        return content, log
    start_cost = COST["usd"]
    rows, slots = [], {}
    for k in BEAT_KEYS:
        for i, it in enumerate(content["beats"].get(k) or []):
            n = len(slots) + 1
            slots[n] = (k, i)
            src = _source_for(it)
            if src:
                rows.append((n, it, src))
            else:
                log["items"].append({"headline": it["headline"], "status": "no source text", "reasons": "source not readable"})
    try:
        drafts = _draft(rows) if rows else {}
        fails = _check(rows, drafts) if rows else {}
        if fails:  # one redraft for the failures only, then re-check them
            redo = [r for r in rows if r[0] in fails]
            drafts.update(_draft(redo, fails))
            fails = _check(redo, drafts)
    except Exception as e:
        print("takes: stopped (%s); edition publishes without takes" % e)
        log["items"].append({"headline": "(all)", "status": "error", "reasons": str(e)[:300]})
        log["cost_usd"] = round(COST["usd"] - start_cost, 4)
        return content, log
    for n, it, _ in rows:
        k, i = slots[n]
        if n in fails:
            log["items"].append({"headline": it["headline"], "status": "dropped", "lens": drafts.get(n, {}).get("lens", ""),
                                 "reasons": str(fails[n])[:300]})
        else:
            content["beats"][k][i]["take"] = drafts[n]
            log["items"].append({"headline": it["headline"], "status": "published", "lens": drafts[n]["lens"]})
    log["cost_usd"] = round(COST["usd"] - start_cost, 4)
    pub = sum(1 for x in log["items"] if x["status"] == "published")
    print("takes: %d published of %d items; cost $%.4f" % (pub, len(slots), log["cost_usd"]))
    return content, log


def save_takes_log(log, edition_cost):
    try:
        data = json.load(open(TAKES_LOG, encoding="utf-8"))
    except Exception:
        data = {"days": []}
    log = dict(log); log["edition_cost_usd"] = round(edition_cost, 4)
    days = [d for d in data.get("days", []) if d.get("date") != log["date"]] + [log]
    data["days"] = days[-60:]
    os.makedirs(os.path.dirname(TAKES_LOG), exist_ok=True)
    json.dump(data, open(TAKES_LOG, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


# ---------------------------------------------------------------- Market view (5 points under the Markets heading)
# Five short points of commentary, written ONLY from the official figures in data/markets.json (with 4-week
# changes from data/market_history.json) and the day's Markets & Risk news items. Every point cites its sources.
# Local number check + independent model check; one redraft; if it still fails the block is simply left out.
MARKETS_JSON = os.path.join(HERE, "data", "markets.json")
MARKET_HISTORY = os.path.join(HERE, "data", "market_history.json")
MV_ON = (os.environ.get("BRIEF_MARKET_VIEW") or "on").strip().lower() not in ("off", "false", "0", "no")
MV_START, MV_END = "<!--MV-START-->", "<!--MV-END-->"


def _fmt_bp(x):
    return ("%+d bp" % round(x)) if x is not None else "n/a"


def market_facts(content):
    """Numbered fact list for the model: {ref: {"text": ..., "name": source name, "url": source url}}."""
    facts = {}
    try:
        mk = json.load(open(MARKETS_JSON, encoding="utf-8"))
    except Exception:
        mk = {}
    try:
        hist = json.load(open(MARKET_HISTORY, encoding="utf-8")).get("series", {})
    except Exception:
        hist = {}
    for r in mk.get("policy") or []:
        if r.get("stale") or not r.get("id"):
            continue
        move = ""
        if r.get("change_bp") is not None and r.get("since"):
            move = "; last move %s, effective %s" % (_fmt_bp(r["change_bp"]), r["since"])
        facts["p_" + r["id"]] = {"text": "%s %s: %s%s (as of %s)" % (r.get("bank"), r.get("label"), r.get("value"), move, r.get("asof")),
                                 "name": r.get("source_name"), "url": r.get("source_url")}
    for g in ("money", "fx"):
        for r in mk.get(g) or []:
            if r.get("stale") or not r.get("id"):
                continue
            if g == "money":
                chg = "day change %s" % _fmt_bp(r.get("change_bp")) if r.get("change_bp") is not None else ""
            else:
                chg = "day change %+.2f%%" % r["change_pct"] if r.get("change_pct") is not None else ""
            four = ""
            pts = (hist.get(r["id"]) or {}).get("points") or []
            if len(pts) >= 5:
                now_v, then_d, then_v = pts[-1][1], pts[-5][0], pts[-5][1]
                if g == "money":
                    four = "; change since %s: %s" % (then_d, _fmt_bp((now_v - then_v) * 100))
                elif then_v:
                    four = "; change since %s: %+.1f%%" % (then_d, (now_v / then_v - 1) * 100)
            facts[r["id"]] = {"text": "%s: %s (%s, as of %s%s)" % (r.get("name"), r.get("value"), chg, r.get("asof"), four),
                              "name": r.get("source_name"), "url": r.get("source_url")}
    m = {r.get("id"): r for r in mk.get("money") or [] if not r.get("stale")}
    try:
        if "ust10y" in m and "ust2y" in m:
            sp = (float(m["ust10y"]["value"].rstrip("%")) - float(m["ust2y"]["value"].rstrip("%"))) * 100
            facts["curve"] = {"text": "US Treasury 10Y minus 2Y spread: %+d bp (as of %s)" % (round(sp), m["ust10y"].get("asof")),
                              "name": m["ust10y"].get("source_name"), "url": m["ust10y"].get("source_url")}
    except (ValueError, AttributeError):
        pass
    n = 0
    for k in ("markets_macro", "risk", "digital"):
        for it in (content.get("beats") or {}).get(k) or []:
            src = (it.get("sources") or [{}])[0]
            if not src.get("url"):
                continue
            n += 1
            facts["n%d" % n] = {"text": "NEWS: %s. %s" % (it.get("headline"), it.get("summary")),
                                "name": src.get("name"), "url": src.get("url")}
    return facts


_MONTHS = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*"


def _nums(t):
    """Figures in a text, ignoring dates (8 October, October 8, 2026-10-08) and tenors (10-year, 3M, 2Y)."""
    t = (t or "").replace(",", "")
    t = re.sub(r"\d{4}-\d{2}-\d{2}", " ", t)
    t = re.sub(r"\b\d{1,2}(?:st|nd|rd|th)?\s+" + _MONTHS, " ", t)
    t = re.sub(_MONTHS + r"\s+\d{1,2}\b", " ", t)
    t = re.sub(r"\b\d+(?:-|\s)(?:year|month|week|day)s?\b|\b\d+[YM]\b", " ", t, flags=re.I)
    return set(re.findall(r"\d+(?:\.\d+)?", t))


def _mv_local(points, facts):
    out = []
    if len(points) != 5:
        return ["need exactly 5 points (got %d)" % len(points)]
    for i, p in enumerate(points, 1):
        refs = [r for r in p.get("refs") or [] if r in facts]
        if not refs:
            out.append("point %d cites no valid fact id" % i)
            continue
        w = len((p.get("text") or "").split())
        if not 14 <= w <= 45:
            out.append("point %d is %d words (target 20-35)" % (i, w))
        cited = " ".join(facts[r]["text"] for r in refs).replace("+", " ").replace("-", " ")
        extra = sorted(x for x in _nums(p.get("head", "") + " " + p.get("text", "")) - _nums(cited)
                       if x not in ("2026", "2027"))
        if extra:
            out.append("point %d uses figures not in its cited facts: %s" % (i, ", ".join(extra)))
        hits = _lint([p.get("head", ""), p.get("text", ""), p.get("view", "")])
        if hits:
            out.append("point %d banned phrases: %s" % (i, ", ".join(hits)))
        vw = len((p.get("view") or "").split())
        if not 10 <= vw <= 40:
            out.append("point %d AI Treasurer view is %d words (target 14-32)" % (i, vw))
        if p.get("tone") not in ("bad", "good", "watch") or not (p.get("effect") or "").strip() or not (p.get("area") or "").strip():
            out.append("point %d needs area, effect and tone (bad/good/watch)" % i)
        vx = sorted(x for x in _nums(p.get("view", "")) - _nums(cited) if x not in ("2026", "2027"))
        if vx:
            out.append("point %d view uses figures not in its cited facts: %s" % (i, ", ".join(vx)))
    return out


def _mv_prompt(facts, date_human, fixes=None):
    lst = "\n".join("[%s] %s" % (k, v["text"]) for k, v in facts.items())
    p = (
        "You write the Market view for The AI Treasurer, a controls-first daily read for CFOs and corporate "
        "treasurers. Today is %s. Write EXACTLY 5 points of commentary on rates, money markets and FX for a "
        "corporate Treasury and Finance team, using ONLY the facts below.\n\n"
        "Each point: head = 2-5 word label (e.g. 'Curve shape', 'Dollar funding', 'Rupee pressure'); text = 1-2 "
        "sentences, 20-35 words: the move with its figure and date basis, then what it means for funding, "
        "investing surplus cash, FX exposure or hedging. refs = the fact ids you used (every figure you write "
        "must appear in a cited fact; do not calculate new numbers).\n"
        "Also give each point an AI Treasurer view: view = ONE sentence, 14-32 words, the Treasury question or step "
        "to consider in response, framed as something to check or consider, never as an instruction to trade or buy "
        "a product; any figure in it must appear in a cited fact; area = 2-4 words naming the Treasury area "
        "(e.g. 'Term borrowing'); effect = 2-3 words, the effect on a corporate Treasury (e.g. 'Cost up', "
        "'Yield up', 'Hedge cost up', 'Event risk'); tone = bad, good or watch.\n"
        "Order: most material first. Cover different ground: policy rates, short-term/money-market rates, the "
        "yield curve, FX, and (if a news fact supports it) the outlook. No two points on the same series.\n"
        "RULES: commentary, not a forecast; no buy/sell, hedging-product or trade recommendations; not investment "
        "advice. Neutral and factual about every central bank; nothing negative toward Qatar, QatarEnergy or "
        "Woqod (the QCB and the riyal peg may be mentioned factually). Brand voice, no personal name.\n\n%s\n\n"
        "Return STRICT JSON only: {\"points\": [{\"head\": \"...\", \"text\": \"...\", \"refs\": [\"ust10y\"], \"view\": \"...\", \"area\": \"...\", \"effect\": \"...\", \"tone\": \"watch\"}]}\n\n"
        "FACTS:\n%s" % (date_human, STYLE, lst))
    if fixes:
        p += "\n\nYOUR PREVIOUS DRAFT FAILED THE CHECK: %s. Fix every point." % fixes
    return p


def _mv_check(points, facts):
    blocks = []
    for i, x in enumerate(points, 1):
        cited = "\n".join("  [%s] %s" % (r, facts[r]["text"]) for r in x.get("refs") or [] if r in facts)
        blocks.append("### POINT %d\n%s: %s\nAI TREASURER VIEW: %s\nCITED FACTS:\n%s" % (i, x.get("head"), x.get("text"), x.get("view"), cited))
    prompt = (
        "You are the independent checker for The AI Treasurer. You did not write these points. Fail a point if ANY "
        "test fails:\n1. Every figure, date, name and direction (up/down, hike/cut) matches its CITED FACTS exactly. "
        "Nothing invented, overstated or miscalculated.\n2. The implication drawn is reasonable for a corporate "
        "treasurer and does not present a forecast as fact.\n3. No investment advice, no buy/sell or product "
        "recommendation, no hype, nothing negative toward Qatar, QatarEnergy or Woqod.\n4. The AI TREASURER VIEW is a "
        "consideration or check that follows from the point, not an instruction to trade, hedge with a named product "
        "or place money; it is not investment advice and adds no unsupported figure.\n"
        "Return STRICT JSON only: {\"checks\": [{\"n\": 1, \"pass\": true, \"reasons\": \"\"}]} with a short, "
        "specific reason for every fail.\n\n%s" % "\n\n".join(blocks))
    data = _json(_ask(prompt, CHECK_MODEL, max_tokens=3000, search=False), "market view check")
    fails = []
    got = {}
    for c in data.get("checks") or []:
        try:
            got[int(c.get("n"))] = c
        except (TypeError, ValueError):
            pass
    for i in range(1, len(points) + 1):
        c = got.get(i)
        if not c or c.get("pass") is not True:
            fails.append("point %d: %s" % (i, (c or {}).get("reasons") or "no verdict"))
    return fails


def market_view(content, date_human):
    """Returns {"points": [...], "log": {...}}; points is [] when switched off or when the check fails."""
    log = {"status": "off", "reasons": ""}
    if not MV_ON:
        return {"points": [], "log": log}
    start = COST["usd"]
    try:
        facts = market_facts(content)
        if len(facts) < 8:
            raise ValueError("too few market facts (%d)" % len(facts))
        fixes, points = None, []
        for attempt in range(2):
            data = _json(_ask(_mv_prompt(facts, date_human, fixes), EDITORIAL_MODEL, max_tokens=4000, search=False), "market view")
            points = [{"head": _cap_treasury((x.get("head") or "").strip().rstrip(".:")),
                       "text": _cap_treasury((x.get("text") or "").strip()),
                       "refs": [r for r in (x.get("refs") or []) if r in facts],
                       "view": _cap_treasury((x.get("view") or "").strip()),
                       "area": _cap_treasury((x.get("area") or "").strip()),
                       "effect": _cap_treasury((x.get("effect") or "").strip()),
                       "tone": (x.get("tone") or "").strip().lower()} for x in data.get("points") or []]
            problems = _mv_local(points, facts)
            if not problems:
                problems = _mv_check(points, facts)
            if not problems:
                break
            print("market view: %s (%s)" % ("redraft" if attempt == 0 else "dropped", "; ".join(problems)[:400]))
            fixes = "; ".join(problems)
        if problems:
            log = {"status": "dropped", "reasons": "; ".join(problems)[:400]}
            points = []
        else:
            for p in points:
                p["sources"] = []
                for r in p["refs"]:
                    s = {"name": facts[r]["name"], "url": facts[r]["url"]}
                    if s["url"] and s not in p["sources"]:
                        p["sources"].append(s)
            log = {"status": "published", "reasons": ""}
    except Exception as e:
        print("market view: stopped (%s); Markets publishes without commentary" % e)
        log, points = {"status": "error", "reasons": str(e)[:300]}, []
    log["cost_usd"] = round(COST["usd"] - start, 4)
    print("market view: %s; cost $%.4f" % (log["status"], log["cost_usd"]))
    return {"points": points, "log": log}


def market_view_html(points, date_human):
    if not points:
        return MV_START + MV_END
    lis = []
    for p in points:
        src = _sources_html(p.get("sources") or [], prefix="Source: ")
        vw = ('<div class="mv-view" data-tone="%s"><div class="mv-vh"><b>AI Treasurer view</b><span>%s</span><span class="mv-eff">%s</span></div>'
              '<p>%s</p></div>' % (esc(p.get("tone") or "watch"), esc(p.get("area") or ""), esc(p.get("effect") or ""), esc(p["view"]))) if p.get("view") else ""
        lis.append('<li><p><b>%s.</b> %s</p>%s%s</li>' % (esc(p["head"]), esc(p["text"]), vw,
                   ('<span class="mv-src">%s</span>' % src) if src else ""))
    return (MV_START + '<section class="mv" aria-labelledby="mv-h"><h4 id="mv-h">Market view<small>Five points for '
            'Treasury, %s</small></h4><ol>%s</ol><p class="mv-note">Commentary on the official figures below, as '
            'published on the dates shown. Not a forecast and not investment advice.</p></section>' % (esc(date_human), "".join(lis))
            + MV_END)


def market_view_only(date_human, date_iso):
    """Write today's Market view into the published edition without re-running the news."""
    page = open(INDEX, encoding="utf-8").read()
    if MV_START not in page:
        print("market view: today's page has no Market view slot (built from an older template); run with --force")
        return 1
    content = {"beats": {k: [] for k in BEAT_KEYS}}
    for m in ITEM_RE.finditer(page):
        k = m.group(2)
        if k in content["beats"]:
            srcs = [{"name": html.unescape(re.sub(r"<[^>]+>", "", n)), "url": html.unescape(u)}
                    for u, n in re.findall(r'<a href="([^"]+)"[^>]*>(.*?)</a>', m.group(8))]
            content["beats"][k].append({"headline": html.unescape(m.group(4)), "summary": html.unescape(m.group(5)), "sources": srcs})
    mv = market_view(content, date_human)
    page = re.sub(re.escape(MV_START) + ".*?" + re.escape(MV_END), lambda _: market_view_html(mv["points"], date_human), page, flags=re.S)
    open(INDEX, "w", encoding="utf-8").write(page)
    arch = os.path.join(EDITIONS_DIR, "%s.html" % date_iso)
    if os.path.exists(arch):
        shutil.copyfile(INDEX, arch)
    return 0


def _note_problems(data, recent):
    """McKinsey-standard gate for the note: answer-first, tight, and not a rerun of recent notes."""
    out = []
    head = (data.get("note_headline") or "").strip()
    note = data.get("editor_note") or []
    if not head:
        out.append("note_headline missing")
    else:
        n = len(head.split())
        if n < 7 or n > 16:
            out.append("note_headline must be 8-14 words (got %d)" % n)
        if re.match(r"^(today|this week)\b", head, re.I) or head.endswith("?") or ":" in head:
            out.append("note_headline must be a declarative claim with no colon, question or 'today'")
    for i, p in enumerate(note[:2]):
        if re.match(r"^\s*(today|this week|in |as |with )", p, re.I):
            out.append("paragraph %d opens weakly ('%s')" % (i + 1, p.split()[0] if p.split() else ""))
        sents = [x for x in re.split(r"(?<=[.!?])\s+", p.strip()) if x]
        if len(sents) > 4:
            out.append("paragraph %d has %d sentences (max 4)" % (i + 1, len(sents)))
        if len(p.split()) > 65:
            out.append("paragraph %d is %d words (max 60)" % (i + 1, len(p.split())))
    blob = (head + " " + " ".join(note)).lower()
    if not re.search(r"\b(finance|financial reporting|cfo|controller|close|audit|accounting|icfr)\b", blob):
        out.append("note speaks only to Treasury; frame it for the whole finance function (CFO, controller, reporting)")
    for d in recent:
        if head and d.get("note_headline") and _similar(head, d["note_headline"]):
            out.append("note_headline repeats a recent note ('%s')" % d["note_headline"]); break
        if note and d.get("note_opening") and _similar(note[0][:160], d["note_opening"][:160]):
            out.append("opening repeats a recent note"); break
    # no phrase of 5+ words used twice inside the note
    words = _norm(" ".join(note)).split()
    grams = [" ".join(words[i:i + 5]) for i in range(max(0, len(words) - 4))]
    dup = sorted(set(g for g in grams if grams.count(g) > 1))
    if dup:
        out.append("repeated phrasing: " + dup[0])
    return out


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


def _hnorm(h):
    return re.sub(r"[^a-z0-9]+", " ", (h or "").lower()).strip()


def _take_html(t):
    if not t:
        return ""
    lens = esc(t.get("lens"))
    return ('<div class="take"><p><b>Focus: %s</b> &mdash; %s</p><p class="view"><b>Our view</b> &mdash; %s</p></div>'
            % (lens, esc(t.get("focus")), esc(t.get("view"))))


def _items_html(items, beat=""):
    if not items:
        return '<p class="pillar-sub" style="margin:0;">No material developments today.</p>'
    out = []
    for i, it in enumerate(items):
        out.append('<div class="item" id="s-%s-%d"><h4>%s</h4><p>%s</p>%s<div class="cite">%s</div></div>'
                   % (beat, i, esc(it["headline"]), esc(it["summary"]), _take_html(it.get("take")),
                      _sources_html(it.get("sources", []))))
    return "\n        ".join(out)


def render(content, date_human, edition_n, archive_entries):
    s = open(TEMPLATE, encoding="utf-8").read()
    # dateline
    dateline = '<span><b>%s</b></span>' % esc(date_human)
    s = s.replace("<!--DATELINE-->", dateline)
    # editor note
    note = content["editor_note"]
    head = content.get("note_headline") or ""
    s = s.replace("<!--EDITOR_NOTE-->", ('<h3 class="note-h">%s</h3>\n        ' % esc(head) if head else "") + "<p>%s</p>\n        <p>%s</p>" % (esc(note[0]), esc(note[1])))
    # dashboard
    cells = []
    for c in content.get("dashboard") or []:
        cells.append('<div class="cell"><div class="val">%s</div><div class="lbl">%s</div><div class="src">%s</div></div>'
                     % (esc(c.get("value")), esc(c.get("label")), esc(c.get("src"))))
    s = s.replace("<!--DASHBOARD-->", "\n          ".join(cells))
    # lead stories
    # Locate each lead in the beats, so its link opens the tab (and story) where it actually appears.
    where = {}
    for k in BEAT_KEYS:
        for i, it in enumerate(content["beats"].get(k) or []):
            where.setdefault(_hnorm(it.get("headline")), (k, i))
    leads = []
    for L in content["lead_stories"]:
        tab = L.get("tab", "ai")
        anchor = ""
        hit = where.get(_hnorm(L.get("headline")))
        if hit:
            tab = BEAT_TAB.get(hit[0], tab)
            anchor = "s-%s-%d" % hit
        label = LEAD_TAB_LABEL.get(tab, "AI &amp; Technology")
        topic = (L.get("topic") or "").strip() or LEAD_TAB_LABEL.get(tab, "AI &amp; Technology").replace("&amp;", "&")
        go = "showTab(\'%s\',\'%s\');return false;" % (tab, anchor)
        leads.append('<div class="item"><div class="topic">%s</div><h4><a class="lead-link" href="#" onclick="%s">%s</a></h4><div class="cite">See <a href="#" onclick="%s">%s &rarr;</a></div></div>'
                     % (esc(topic), go, esc(L["headline"]), go, label))
    s = s.replace("<!--LEAD-->", "\n      ".join(leads))
    # beats
    for k in BEAT_KEYS:
        s = s.replace("<!--BEAT_%s-->" % k, _items_html(content["beats"][k], k))
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
    # market view
    s = s.replace("<!--MARKET_VIEW-->", market_view_html(content.get("market_view") or [], date_human))
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
    one[0]["take"] = {"lens": "Payments & operations",
                      "focus": "Intercompany cash that took days now moves in hours, so forecasting and FX timing have to catch up.",
                      "view": "Speed is the easy part; prove who controls the wallet before the next transfer."}
    beats = {k: list(one) for k in BEAT_KEYS}
    return {
        "beats": beats,
        "dashboard": [{"value": "4.5-4.75%", "label": "10Y UST range", "src": "Outlook, 2026"}] * 6,
        "events": [{"when": "Soon", "place": "City - upcoming", "title": "A treasury event", "blurb": "Short blurb.", "source": {"name": "Example", "url": "https://example.com"}}],
        "note_headline": "Sample insight headline for the mock edition, eight words long",
        "market_view": [{"head": "Curve shape", "text": "Sample point of market commentary, about thirty words, with a figure and what it means for funding, surplus cash or hedging so the layout can be checked end to end.", "view": "Sample AI Treasurer view: the check or step to consider, in one sentence.", "area": "Term borrowing", "effect": "Cost up", "tone": "watch", "sources": [{"name": "US Treasury", "url": "https://home.treasury.gov/"}]}] * 5,
        "editor_note": ["First paragraph of the editor note for layout testing.", "Second paragraph tying it to controls and the audit trail."],
        "lead_stories": [
            {"topic": "Agentic Treasury Controls", "headline": "Lead one", "blurb": "Blurb.", "tab": "ai"},
            {"headline": "Lead two", "blurb": "Blurb.", "tab": "treasury"},
            {"headline": "Lead three", "blurb": "Blurb.", "tab": "markets"},
        ],
    }


ITEM_RE = re.compile(r'(<div class="item" id="s-([a-z_]+)-(\d+)"><h4>(.*?)</h4><p>(.*?)</p>)(<div class="take">.*?</div></div>)?(<div class="cite">(.*?)</div></div>)', re.S)


def takes_only(date_iso):
    """Add takes to the edition already published today, without re-running the news."""
    page = open(INDEX, encoding="utf-8").read()
    content = {"beats": {k: [] for k in BEAT_KEYS}}
    for m in ITEM_RE.finditer(page):
        k = m.group(2)
        if k not in content["beats"]:
            continue
        srcs = [{"name": html.unescape(re.sub(r"<[^>]+>", "", n)), "url": html.unescape(u)}
                for u, n in re.findall(r'<a href="([^"]+)"[^>]*>(.*?)</a>', m.group(8))]
        content["beats"][k].append({"headline": html.unescape(m.group(4)), "summary": html.unescape(m.group(5)),
                                    "sources": srcs, "_id": "s-%s-%s" % (k, m.group(3))})
    content, tlog = takes(content, date_iso)
    by_id = {it["_id"]: it.get("take") for k in BEAT_KEYS for it in content["beats"][k]}

    def put(m):
        t = by_id.get("s-%s-%s" % (m.group(2), m.group(3)))
        return m.group(1) + (_take_html(t) if t else (m.group(6) or "")) + m.group(7)
    page = ITEM_RE.sub(put, page)
    open(INDEX, "w", encoding="utf-8").write(page)
    arch = os.path.join(EDITIONS_DIR, "%s.html" % date_iso)
    if os.path.exists(arch):
        shutil.copyfile(INDEX, arch)
    save_takes_log(tlog, COST["usd"])
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mock", action="store_true", help="render with canned content (no API)")
    ap.add_argument("--out", default=INDEX, help="output path for the latest edition")
    ap.add_argument("--force", action="store_true", help="rebuild today's edition even if it already exists")
    ap.add_argument("--market-view-only", action="store_true", help="refresh the Market view on today's published edition; news unchanged")
    ap.add_argument("--takes-only", action="store_true", help="add takes to today's published edition; news unchanged")
    args = ap.parse_args()

    now = datetime.datetime.utcnow() + datetime.timedelta(hours=TZ_OFFSET)
    date_iso = now.strftime("%Y-%m-%d")
    date_human = now.strftime("%A, %-d %B %Y") if os.name != "nt" else now.strftime("%A, %d %B %Y")
    month_human = now.strftime("%B %Y")

    if args.takes_only:
        return takes_only(date_iso)
    if args.market_view_only:
        return market_view_only(date_human, date_iso)

    st = load_state()
    # self-heal: if today's edition already built, exit quietly (lets retry crons no-op)
    if any(e["date"] == date_iso for e in st["editions"]) and not args.mock:
        if not args.force:
            print("Edition for %s already exists; nothing to do." % date_iso)
            return 0
        # rebuild: drop today's entry (keeping its number) so it is replaced, not duplicated
        today = [e for e in st["editions"] if e["date"] == date_iso]
        st["editions"] = [e for e in st["editions"] if e["date"] != date_iso]
        st["last_n"] = min(e["n"] for e in today) - 1
        print("Rebuilding edition for %s" % date_iso)

    edition_n = (st["last_n"] + 1) if not args.mock else (st["last_n"] + 1 or 1)

    if args.mock:
        content = mock_content()
    else:
        history = load_history()
        history["days"] = [d for d in history.get("days", []) if d.get("date") != date_iso]  # a rebuild must not dedupe against itself
        res = fill_empty(dedupe(research(date_human, history), history), date_human, history)
        ed = editorial(res, date_human, history)
        content = dict(res); content.update(ed)
        content, tlog = takes(content, date_iso)
        mv = market_view(content, date_human)
        content["market_view"] = mv["points"]
        tlog["market_view"] = mv["log"]

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
    save_history(load_history(), date_iso, content)
    if not args.mock:
        save_takes_log(tlog, COST["usd"])
    print("Edition %d - %s" % (edition_n, date_human))
    return 0


if __name__ == "__main__":
    sys.exit(main())
