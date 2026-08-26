# Daily Financial Intelligence

A morning brief for a chartered accountant in India. One screen, read in five minutes, ranked by what each item changes for *you* rather than by which regulator issued it.

Setup is in **[QUICK_START.md](QUICK_START.md)**.

---

## How it works

```
03:10 UTC  GitHub Actions wakes up
           ├─ fetch ~33 sources in parallel, 20s timeout each, failures skipped
           ├─ keep the last 26 hours
           ├─ cluster duplicates, keep the most authoritative source of each
           ├─ one model call: rank, write the implication, flag real actions
           ├─ diff against yesterday, mark what carried over
           ├─ write data/today.json + data/archive/
           ├─ email the same brief
           └─ commit data/ back to the repo
09:00 IST  GitHub Pages serves it; the PWA on your phone picks it up
```

There is no server and no database. The whole thing is a scheduled script that writes a JSON file, and a static page that reads it. Nothing to maintain, nothing to pay for except a few cents a day of model usage.

## What each piece is

| Path | What it does |
|---|---|
| `index.html` | The whole app — markup, styling and logic in one file |
| `sw.js` | Caches the shell so it opens offline; always tries the network for the brief first |
| `collector/sources.yaml` | Every source, its trust weight and its CA bias. **This is the file to edit.** |
| `collector/run.py` | Fetch, dedupe, score, diff, write, email |
| `.github/workflows/daily.yml` | The 9 AM schedule |
| `data/today.json` | What the app reads |

## Sources, and the four that aren't here

The original plan listed Reuters, the Financial Times, the Wall Street Journal and Bloomberg. None of them are in `sources.yaml`, and it's better you know why now than discover four silent failures later.

Reuters withdrew its public RSS feeds in 2020, and its terms prohibit automated collection. The FT, WSJ and Bloomberg are paywalled, and their terms prohibit it too. Code that claimed to read them would either break immediately or quietly put you on the wrong side of a licence agreement. If you want them, the honest routes are a personal subscription you read directly, or their commercial licensing desks.

What replaces them: for global wire coverage, AP and CNBC. For everything that actually matters to your work, the primary sources are better than the wires anyway — you want the RBI circular, not a paraphrase of it.

Two additions worth knowing about:

**PIB** is the biggest single win and wasn't in the original list. The Press Information Bureau carries Ministry of Finance, CBDT, CBIC and GST Council announcements, usually hours before those departments update their own websites, and it has a real RSS feed while most of them don't.

**Scraped regulators.** CBDT, CBIC, MCA, ICAI and IBBI publish no feed at all, so those five are HTML scrapes driven by CSS selectors in `sources.yaml`. Scrapes break when a site is redesigned. When one does, open the page, find the list element, and update the `item` / `title` / `link` selectors. Nothing else needs to change, and a broken scrape never breaks the run.

## Deduplication

Two stages, because one isn't enough.

The lexical stage catches syndicated copy: identical canonical URLs, plus IDF-weighted cosine similarity over stemmed title words, plus a rule that three shared *rare* words on the same day means one event. That last rule matters more than it sounds. Two outlets covering one story often share only four words out of twenty, which sinks cosine similarity — but the four they share are "brent", "supply", "importers", never "the" and "market".

The model stage catches the rest. It sees every surviving candidate and is told to merge anything describing one event, preferring the regulator over the newspaper. Genuine paraphrase is a semantic problem and lexical methods will never fully solve it.

## Tuning it

Everything below is an environment variable, settable per-run or in the workflow.

| Variable | Default | Effect |
|---|---|---|
| `DFI_MAX_ITEMS` | `14` | Ceiling on the brief. The model may return fewer and is told that's fine. |
| `DFI_WINDOW_HOURS` | `26` | Lookback. The overlap past 24h stops late-night items falling through the gap. |
| `DFI_MODEL` | `claude-sonnet-5` | Model names change; check current ones at docs.claude.com. |

To reweight the brief, edit `sources.yaml`:

- **`weight`** (1–10) is how much you trust the source, and decides which outlet wins when several cover one story.
- **`ca_bias`** (0–2) pushes a source's stories up the ranking for your work specifically. Raise it on the GST and direct tax feeds during filing season.

To change what counts as important, edit `PROMPT` in `run.py`. That string is where the editorial judgement lives — rule 3 in particular is what stops it inventing work for you. If the brief starts feeling padded, tighten rule 5 before you touch anything else.

## Running it locally

```bash
pip install -r collector/requirements.txt

python collector/run.py --offline --no-ai --no-email   # fixtures, no network
python collector/run.py --no-email                     # real fetch, no send
python collector/run.py                                # everything

python -m http.server 8000                             # then open localhost:8000
```

`--offline` runs against `collector/fixtures/`, which is also what seeds the sample brief you see before the first real run.

## What this doesn't do

It scores relevance by machine, and it will sometimes be wrong in both directions — a 4 that mattered, a 9 that didn't. Every item links to its source because the ranking is a reading order, not a substitute for reading the circular. Don't act on a summary.

The market strip is delayed end-of-day data from a free endpoint. It's context for a morning read, not a quote you should rely on.

The archive keeps 60 days and then deletes. If you want it permanently, that's what the emails are for.
