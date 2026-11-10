#!/usr/bin/env python3
"""Build The AI Treasurer Insights from one list: data/insights.json.

Run after adding or editing an article entry:
    python3 insights.py            # rebuild everything
    python3 insights.py --check    # validate only, change nothing (used by CI)

It rewrites only the marked regions it owns:
  * Home cards (latest 3) between INSIGHTS-HOME markers in edition_template.html and index.html
  * /insights/index.html (the full list) and /insights/feed.xml (RSS)
  * Insights URLs between INSIGHTS-SITEMAP markers in sitemap.xml
Article pages themselves are hand-written under /articles/<slug>/index.html.
"""
import datetime as dt
import html
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data", "insights.json")
SITE = "https://theaitreasurer.com"
HOME_FILES = ("edition_template.html", "index.html")
HOME_START, HOME_END = "<!--INSIGHTS-HOME-START-->", "<!--INSIGHTS-HOME-END-->"
MAP_START, MAP_END = "<!--INSIGHTS-SITEMAP-START-->", "<!--INSIGHTS-SITEMAP-END-->"
HOME_CARDS = 3

e = lambda s: html.escape(str(s), quote=True)


def load():
    doc = json.load(open(DATA, encoding="utf-8"))
    topics, arts, errs = doc["topics"], doc["articles"], []
    seen = set()
    for a in arts:
        for k in ("slug", "title", "summary", "date", "topic", "minutes"):
            if not a.get(k):
                errs.append("%s: missing %s" % (a.get("slug", "?"), k))
        if a.get("slug") in seen:
            errs.append("%s: duplicate slug" % a["slug"])
        seen.add(a.get("slug"))
        if a.get("topic") not in topics:
            errs.append("%s: topic %r is not in the topic list" % (a.get("slug"), a.get("topic")))
        try:
            a["_d"] = dt.date.fromisoformat(a["date"])
        except Exception:
            errs.append("%s: date must be YYYY-MM-DD" % a.get("slug"))
        if not os.path.isfile(os.path.join(HERE, "articles", str(a.get("slug")), "index.html")):
            errs.append("%s: articles/%s/index.html not found" % (a.get("slug"), a.get("slug")))
    if errs:
        sys.exit("insights.json problems:\n  " + "\n  ".join(errs))
    return sorted(arts, key=lambda a: (a["_d"], a["slug"]), reverse=True)


def long_date(d):
    return "%d %s %d" % (d.day, d.strftime("%B"), d.year)


def short_date(d):
    return "%d %s %d" % (d.day, d.strftime("%b"), d.year)


def home_cards(arts):
    out = []
    for a in arts[:HOME_CARDS]:
        out.append(
            '        <a class="art-card" href="/articles/%s/"><span class="art-meta"><span class="art-topic">%s</span> &middot; %s &middot; %d min read</span>'
            '<span class="art-t">%s</span><span class="art-d">%s</span><span class="art-go">Read the article</span></a>'
            % (e(a["slug"]), e(a["topic"]), short_date(a["_d"]), a["minutes"], e(a["title"]), e(a["summary"])))
    return "\n" + "\n".join(out) + "\n        "


def insights_page(arts):
    items = []
    for a in arts:
        items.append(
            '        <li class="ins-item">\n'
            '          <p class="ins-meta"><span class="ins-topic">%s</span> &middot; <time datetime="%s">%s</time> &middot; <span class="nw">%d min read</span></p>\n'
            '          <h2 class="ins-t"><a href="/articles/%s/">%s</a></h2>\n'
            '          <p class="ins-d">%s</p>\n'
            '        </li>' % (e(a["topic"]), a["date"], long_date(a["_d"]), a["minutes"], e(a["slug"]), e(a["title"]), e(a["summary"])))
    return PAGE.replace("<!--ITEMS-->", "\n".join(items))


def feed(arts):
    now = dt.datetime.now(dt.timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")
    items = []
    for a in arts:
        pub = dt.datetime(a["_d"].year, a["_d"].month, a["_d"].day, 6, 0, tzinfo=dt.timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")
        url = "%s/articles/%s/" % (SITE, a["slug"])
        items.append(
            "    <item>\n      <title>%s</title>\n      <link>%s</link>\n      <guid isPermaLink=\"true\">%s</guid>\n"
            "      <pubDate>%s</pubDate>\n      <category>%s</category>\n      <description>%s</description>\n    </item>"
            % (e(a["title"]), url, url, pub, e(a["topic"]), e(a["summary"])))
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">\n  <channel>\n'
            '    <title>The AI Treasurer: Insights</title>\n    <link>%s/insights/</link>\n'
            '    <atom:link href="%s/insights/feed.xml" rel="self" type="application/rss+xml"/>\n'
            '    <description>Practical views for CFOs and Treasurers, by Gopakumar Menon.</description>\n'
            '    <language>en-gb</language>\n    <lastBuildDate>%s</lastBuildDate>\n%s\n  </channel>\n</rss>\n'
            % (SITE, SITE, now, "\n".join(items)))


def sitemap_block(arts):
    newest = arts[0]["date"] if arts else dt.date.today().isoformat()
    rows = ["  <url>\n    <loc>%s/insights/</loc>\n    <lastmod>%s</lastmod>\n    <changefreq>weekly</changefreq>\n    <priority>0.8</priority>\n  </url>" % (SITE, newest)]
    for a in arts:
        rows.append("  <url>\n    <loc>%s/articles/%s/</loc>\n    <lastmod>%s</lastmod>\n    <changefreq>monthly</changefreq>\n    <priority>0.8</priority>\n  </url>" % (SITE, a["slug"], a["date"]))
    return "\n" + "\n".join(rows) + "\n  "


def replace_region(text, start, end, body, name):
    pat = re.compile(re.escape(start) + r".*?" + re.escape(end), re.S)
    if len(pat.findall(text)) != 1:
        sys.exit("%s: expected exactly one %s ... %s region" % (name, start, end))
    return pat.sub(lambda m: start + body + end, text)


def write(path, text):
    full = os.path.join(HERE, path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    old = open(full, encoding="utf-8").read() if os.path.exists(full) else None
    if old != text:
        open(full, "w", encoding="utf-8").write(text)
        print("updated", path)


def main():
    arts = load()
    check = "--check" in sys.argv
    outputs = {}
    for f in HOME_FILES:
        outputs[f] = replace_region(open(os.path.join(HERE, f), encoding="utf-8").read(), HOME_START, HOME_END, home_cards(arts), f)
    outputs["sitemap.xml"] = replace_region(open(os.path.join(HERE, "sitemap.xml"), encoding="utf-8").read(), MAP_START, MAP_END, sitemap_block(arts), "sitemap.xml")
    outputs["insights/index.html"] = insights_page(arts)
    if check:
        print("insights check ok: %d article(s)" % len(arts))
        return
    outputs["insights/feed.xml"] = feed(arts)
    for path, text in outputs.items():
        write(path, text)


PAGE = r'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#0D1A2C">
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<link rel="icon" type="image/png" sizes="32x32" href="/icon-32.png">
<link rel="apple-touch-icon" href="/icon-180.png">
<link rel="alternate" type="application/rss+xml" title="The AI Treasurer: Insights" href="/insights/feed.xml">
<title>Insights | The AI Treasurer</title>
<meta name="description" content="Practical views for CFOs and Treasurers on Treasury, Finance and AI, by Gopakumar Menon.">
<link rel="canonical" href="https://theaitreasurer.com/insights/">
<meta property="og:title" content="Insights | The AI Treasurer">
<meta property="og:description" content="Practical views for CFOs and Treasurers, by Gopakumar Menon.">
<meta property="og:type" content="website">
<meta property="og:url" content="https://theaitreasurer.com/insights/">
<meta property="og:image" content="https://theaitreasurer.com/img/ait-og-gopakumar.jpg">
<meta name="twitter:card" content="summary_large_image">
<!-- Generated by insights.py from data/insights.json. Do not edit by hand. -->
<style>
  :root { --navy: #14263F; --navy-deep: #0D1A2C; --on-dark: #DCE2EA; --on-dark-soft: #9FACBE; --gold-light: #E9CB85; --white: #FFFFFF;
    --serif: Charter, "Bitstream Charter", "Sitka Text", Cambria, Georgia, serif;
    --sans: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif; }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--navy-deep); color: var(--white); font-family: var(--sans); font-size: 18px; line-height: 1.6; -webkit-font-smoothing: antialiased; }
  a { color: var(--white); }
  a:focus-visible { outline: 3px solid var(--gold-light); outline-offset: 3px; }
  .wrap { max-width: 1240px; margin: 0 auto; padding: 0 20px; }
  header.band { border-bottom: 1px solid rgba(255,255,255,0.12); }
  header.band .wrap { display: flex; justify-content: space-between; align-items: center; gap: 16px; padding-top: 16px; padding-bottom: 16px; }
  .mark { display: inline-flex; align-items: center; gap: 10px; color: var(--white); text-decoration: none; font-family: var(--serif); font-weight: 700; font-size: 19px; }
  .mark svg { width: 30px; height: 30px; }
  .home { color: var(--white); font-size: 15px; font-weight: 600; text-decoration: none; border-bottom: 1px solid var(--gold-light); }
  main { padding: 64px 0 72px; }
  h1 { font-family: var(--serif); font-size: clamp(34px, 4.4vw, 48px); line-height: 1.08; letter-spacing: -0.01em; margin: 0 0 10px; }
  .lede { margin: 0 0 34px; font-size: 18px; color: var(--on-dark); }
  .ins-list { list-style: none; margin: 0; padding: 0; max-width: 800px; border-top: 1px solid rgba(255,255,255,0.16); }
  .ins-item { padding: 26px 0 28px; border-bottom: 1px solid rgba(255,255,255,0.16); }
  .ins-meta { margin: 0 0 8px; font-size: 13px; letter-spacing: 0.04em; color: var(--on-dark-soft); }
  .ins-topic { color: var(--gold-light); font-weight: 700; letter-spacing: 0.08em; text-transform: uppercase; font-size: 12px; }
  .nw { white-space: nowrap; }
  .ins-t { font-family: var(--serif); font-size: clamp(22px, 2.6vw, 28px); line-height: 1.22; margin: 0 0 8px; }
  .ins-t a { text-decoration: none; }
  .ins-t a:hover { text-decoration: underline; text-decoration-color: var(--gold-light); text-underline-offset: 4px; }
  .ins-d { margin: 0; font-size: 17px; line-height: 1.55; color: var(--on-dark); max-width: 62ch; }
  .feed { margin: 28px 0 0; font-size: 14px; color: var(--on-dark-soft); }
  .feed a { color: var(--on-dark); }
  footer { border-top: 1px solid rgba(255,255,255,0.12); padding: 22px 0 40px; font-size: 13px; color: var(--on-dark-soft); }
  footer a { color: var(--on-dark); }
</style>
</head>
<body>
<header class="band">
  <div class="wrap">
    <a class="mark" href="/"><svg viewBox="0 0 100 100" aria-hidden="true"><rect width="100" height="100" rx="22" fill="#ffffff"/><polygon points="50,20 82,46 82,58 50,32 18,58 18,46" fill="#2E6AF0"/><polygon points="50,48 82,74 82,86 50,60 18,86 18,74" fill="#14263F"/></svg><span>The AI Treasurer</span></a>
    <a class="home" href="/">Home</a>
  </div>
</header>
<main id="main">
  <div class="wrap">
    <h1>Insights</h1>
    <p class="lede">Practical views for CFOs and Treasurers.</p>
    <ol class="ins-list">
<!--ITEMS-->
    </ol>
  </div>
</main>
<footer>
  <div class="wrap"><p>&copy; The AI Treasurer &middot; Views are personal and not financial or professional advice &middot; <a href="/">theaitreasurer.com</a></p></div>
</footer>
</body>
</html>
'''

if __name__ == "__main__":
    main()
