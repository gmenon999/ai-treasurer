# The AI Treasurer

Source for **theaitreasurer.com** — a plain-English perspective on AI, automation
and control in corporate Treasury, by Gopakumar Menon, CA, CPA, CTP.

## Structure
- `edition_template.html` — the homepage design (editorial grid, briefing,
  framework, articles, Get in Touch). `generate.py` fills its markers every
  morning and writes `index.html`, so **design changes go in the template**.
- `index.html` — the live homepage and today's edition (generated; don't hand-edit).
- `editions/` — dated archive copies plus `state.json` (edition numbering).
- `img/` — site photography (Unsplash licence; credited in the footer).
  Image rules: people in executive attire only, otherwise abstract designs or
  objects that match the subject; files are committed here, never hotlinked.
- `subscribed.html` — thank-you page for the Netlify newsletter form.
- `welcome.html` — retired earlier homepage; redirects to `/`.
- `netlify.toml` — Netlify publishes the repo root as-is (no build step).

## Market view
Five short points of commentary sit directly under the Markets heading. `generate.py`
writes them each morning from the official figures in `data/markets.json` (with
four-week changes from `data/market_history.json`) and the day's Markets & Risk
news. Every point cites its sources; any figure not in a cited fact fails a local
check, then an independent model checks the rest. One redraft; if it still fails,
the block is left out and Markets publishes as normal. Switch off with the
repository variable `BRIEF_MARKET_VIEW=off`. Result and cost are logged in
`editions/takes-log.json`.

## Publishing
Netlify is linked to this repo's `main` branch and republishes on every push,
so pushing is publishing. A new briefing edition is added each day and committed
here; the archive keeps the current month's editions.

## Notes on sources
The briefing links to third-party publishers and summarises each item in its own
words; it does not reproduce source articles. Figures are as reported on the
dates shown and are not live market data. Nothing here is financial or
professional advice.
