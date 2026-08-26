#!/usr/bin/env python3
"""
Daily Financial Intelligence — collector.

Builds one JSON file, data/today.json, which the PWA reads. Runs on a schedule
from GitHub Actions. Every stage tolerates failure: a dead source is logged and
skipped, a missing API key falls back to heuristic scoring, a broken mailer
still leaves the digest published.

    python run.py                 # live run
    python run.py --offline       # use collector/fixtures, no network
    python run.py --no-ai         # skip the model, heuristic scoring only
    python run.py --no-email      # build but do not send
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import smtplib
import sys
import textwrap
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from io import StringIO
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import feedparser
import requests
import yaml

ROOT = Path(__file__).resolve().parent.parent
CONF = Path(__file__).resolve().parent / "sources.yaml"
DATA = ROOT / "data"
IST = timezone(timedelta(hours=5, minutes=30))

WINDOW_HOURS = int(os.environ.get("DFI_WINDOW_HOURS", "26"))
MAX_ITEMS = int(os.environ.get("DFI_MAX_ITEMS", "14"))
MODEL = os.environ.get("DFI_MODEL") or ""
UA = "DailyFinancialIntelligence/1.0 (personal digest; +https://github.com/)"

BUCKETS = [
    ("must_know", "What you need to know today"),
    ("ca_work", "What could affect your CA work"),
    ("india_economy", "India · economy and business"),
    ("global", "Global developments that reach India"),
    ("markets", "Market context"),
    ("watchlist", "Worth monitoring, not yet urgent"),
]

STOP = set(
    """a an the of to in on for and or with by from at as is are was were be been
    after over into new says said will may could would its it this that than then
    up down amid ahead has have had not but""".split()
)


# ───────────────────────────────── fetching ─────────────────────────────────


def canonical(url: str) -> str:
    """Strip tracking noise so the same story from two links dedupes."""
    try:
        p = urlparse(url)
        q = "&".join(
            kv
            for kv in p.query.split("&")
            if kv and not kv.split("=")[0].lower().startswith(("utm_", "fbclid", "gclid", "cmp"))
        )
        return urlunparse((p.scheme, p.netloc.lower().replace("www.", ""), p.path.rstrip("/"), "", q, ""))
    except Exception:
        return url


def parse_when(entry) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            try:
                return datetime(*t[:6], tzinfo=timezone.utc)
            except Exception:
                pass
    return None


def clean(html: str, limit: int = 600) -> str:
    text = re.sub(r"<[^>]+>", " ", html or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def fetch_feed(src: dict, session: requests.Session) -> list[dict]:
    for url in src.get("urls", []):
        try:
            r = session.get(url, timeout=20, headers={"User-Agent": UA})
            r.raise_for_status()
            parsed = feedparser.parse(r.content)
            if not parsed.entries:
                continue
            out = []
            for e in parsed.entries[:40]:
                link = e.get("link") or ""
                title = clean(e.get("title", ""), 300)
                if not title or not link:
                    continue
                out.append(
                    {
                        "title": title,
                        "url": link,
                        "canonical": canonical(link),
                        "summary": clean(e.get("summary", "") or e.get("description", "")),
                        "published": (parse_when(e) or datetime.now(timezone.utc)).isoformat(),
                        "source_id": src["id"],
                        "source": src["name"],
                        "tier": src["tier"],
                        "weight": src["weight"],
                        "ca_bias": src.get("ca_bias", 0),
                        "region": src.get("region", "global"),
                    }
                )
            return out
        except Exception as exc:  # noqa: BLE001
            log(f"  · {src['id']} {url[:48]}… {type(exc).__name__}")
    return []


def fetch_scrape(src: dict, session: requests.Session) -> list[dict]:
    """Selector-based scrape for regulators that publish no feed."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return []
    cfg = src["scrape"]
    try:
        r = session.get(cfg["url"], timeout=25, headers={"User-Agent": UA})
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        base = urlparse(cfg["url"])
        out = []
        for node in soup.select(cfg["item"])[:60]:
            a = node.select_one(cfg["link"])
            if not a or not a.get("href"):
                continue
            title = clean(a.get_text(), 300)
            if len(title) < 18:
                continue
            href = a["href"]
            if href.startswith("/"):
                href = f"{base.scheme}://{base.netloc}{href}"
            elif not href.startswith("http"):
                continue
            out.append(
                {
                    "title": title,
                    "url": href,
                    "canonical": canonical(href),
                    "summary": "",
                    # scraped listings rarely carry a reliable date; assume today and
                    # let the yesterday-diff suppress anything already seen.
                    "published": datetime.now(timezone.utc).isoformat(),
                    "source_id": src["id"],
                    "source": src["name"],
                    "tier": src["tier"],
                    "weight": src["weight"],
                    "ca_bias": src.get("ca_bias", 0),
                    "region": src.get("region", "india"),
                    "undated": True,
                }
            )
        return out
    except Exception as exc:  # noqa: BLE001
        log(f"  · {src['id']} scrape failed: {type(exc).__name__}")
        return []


def collect(conf: dict, offline: bool) -> tuple[list[dict], dict]:
    if offline:
        fx = Path(__file__).resolve().parent / "fixtures" / "sample_items.json"
        items = json.loads(fx.read_text())
        return items, {"ok": len({i["source_id"] for i in items}), "failed": 0, "offline": True}

    session = requests.Session()
    items, ok, failed = [], 0, 0
    log("fetching sources")
    with ThreadPoolExecutor(max_workers=10) as pool:
        jobs = {}
        for src in conf["sources"]:
            fn = fetch_scrape if "scrape" in src else fetch_feed
            jobs[pool.submit(fn, src, session)] = src
        for fut in as_completed(jobs):
            src = jobs[fut]
            try:
                got = fut.result()
            except Exception:  # noqa: BLE001
                got = []
            if got:
                ok += 1
                items.extend(got)
            else:
                failed += 1
    log(f"  {len(items)} raw items · {ok} sources ok · {failed} unavailable")
    return items, {"ok": ok, "failed": failed, "offline": False}


def market_strip(conf: dict, offline: bool) -> list[dict]:
    if offline or "markets" not in conf:
        return []
    m = conf["markets"]
    syms = ",".join(i["symbol"] for i in m["instruments"])
    try:
        r = requests.get(m["endpoint"].format(symbols=syms), timeout=15, headers={"User-Agent": UA})
        r.raise_for_status()
        rows = list(csv.DictReader(StringIO(r.text)))
        labels = {i["symbol"].lower(): i["label"] for i in m["instruments"]}
        out = []
        for row in rows:
            try:
                close, open_ = float(row["Close"]), float(row["Open"])
            except (ValueError, KeyError, TypeError):
                continue
            pct = ((close - open_) / open_ * 100) if open_ else 0.0
            out.append(
                {
                    "label": labels.get(row["Symbol"].lower(), row["Symbol"]),
                    "value": f"{close:,.2f}",
                    "change": round(pct, 2),
                }
            )
        return out
    except Exception as exc:  # noqa: BLE001
        log(f"  · markets unavailable: {type(exc).__name__}")
        return []


# ───────────────────────────────── clustering ─────────────────────────────────


def tokens(title: str) -> set[str]:
    """Content words, lightly stemmed so importer/importers collide."""
    out = set()
    for w in re.findall(r"[a-z0-9]+", title.lower()):
        if w in STOP or len(w) < 3:
            continue
        if len(w) > 4 and w.endswith("ies"):
            w = w[:-3] + "y"
        elif len(w) > 4 and w.endswith(("es", "ed")):
            w = w[:-2]
        elif len(w) > 3 and w.endswith("s"):
            w = w[:-1]
        out.add(w)
    return out


def build_idf(items: list[dict]) -> tuple[dict[str, float], dict[str, int]]:
    """Two headlines about the same event rarely share phrasing, but they almost
    always share the rare words — 'brent', 'repo', a company name. Weighting by
    inverse document frequency lets those carry the match while boilerplate
    like 'india' or 'market' counts for almost nothing."""
    import math

    n = max(len(items), 1)
    df: dict[str, int] = {}
    for it in items:
        for tok in it["_tok"]:
            df[tok] = df.get(tok, 0) + 1
    return {tok: math.log(1 + n / c) for tok, c in df.items()}, df


def similarity(a: dict, b: dict, idf: dict[str, float]) -> float:
    import math

    ta, tb = a["_tok"], b["_tok"]
    if not ta or not tb:
        return 0.0
    shared = sum(idf.get(t, 1.0) ** 2 for t in ta & tb)
    na = math.sqrt(sum(idf.get(t, 1.0) ** 2 for t in ta))
    nb = math.sqrt(sum(idf.get(t, 1.0) ** 2 for t in tb))
    return shared / (na * nb) if na and nb else 0.0


def rare_overlap(a: dict, b: dict, df: dict[str, int], cutoff: int) -> bool:
    """Cosine punishes paraphrase: two outlets covering one event often share only
    a handful of words out of twenty. But the words they do share are the
    distinctive ones — 'brent', 'repo', a company name. Three rare terms in
    common on the same day is a duplicate far more often than it is a coincidence.

    Rarity has to be measured by document frequency, not by an idf threshold: any
    term two items share already has df >= 2, so an idf cutoff drawn from the whole
    vocabulary sits above every shared term and never fires."""
    return sum(1 for t in a["_tok"] & b["_tok"] if df.get(t, 99) <= cutoff) >= 3


def cluster(items: list[dict], threshold: float = 0.34) -> list[dict]:
    """Group items reporting the same event. The highest-weight source becomes
    the canonical one; the rest become 'also reported by'."""
    for it in items:
        it["_tok"] = tokens(it["title"])
    idf, df = build_idf(items)
    cutoff = max(2, round(len(items) * 0.04))  # "rare" = in <=4% of today's items
    groups: list[list[dict]] = []
    by_url: dict[str, int] = {}

    for it in sorted(items, key=lambda x: (-x["weight"], x["title"])):
        idx = by_url.get(it["canonical"])
        if idx is None:
            best, best_score = None, threshold
            for gi, g in enumerate(groups):
                score = max(similarity(it, o, idf) for o in g[:3])
                if score >= best_score:
                    best, best_score = gi, score
                elif best is None and any(rare_overlap(it, o, df, cutoff) for o in g[:3]):
                    best = gi
            idx = best
        if idx is None:
            by_url[it["canonical"]] = len(groups)
            groups.append([it])
        else:
            by_url.setdefault(it["canonical"], idx)
            groups[idx].append(it)

    clusters = []
    for g in groups:
        g.sort(key=lambda x: (-x["weight"], x["published"]))
        head = dict(g[0])
        head.pop("_tok", None)
        head["also_reported_by"] = sorted({o["source"] for o in g[1:]})[:5]
        head["corroboration"] = len(g)
        head["variant_titles"] = [o["title"] for o in g[1:4]]
        clusters.append(head)
    return clusters


def in_window(items: list[dict], hours: int) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    keep = []
    for it in items:
        try:
            when = datetime.fromisoformat(it["published"].replace("Z", "+00:00"))
        except Exception:  # noqa: BLE001
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        if when >= cutoff:
            keep.append(it)
    return keep


# ───────────────────────────────── scoring ─────────────────────────────────

CA_TERMS = re.compile(
    r"\b(gst|income[- ]tax|cbdt|cbic|tds|tcs|itr|audit|auditor|icai|ind as|ifrs|"
    r"companies act|mca|roc|sebi|listing obligation|lodr|insolvency|ibc|ibbi|"
    r"transfer pricing|fema|tax|levy|cess|assessment|appellate|itat|gstr|"
    r"accounting standard|disclosure|compliance|filing|due date|penalt)",
    re.I,
)
MACRO_TERMS = re.compile(
    r"\b(repo rate|inflation|cpi|wpi|gdp|fiscal deficit|monetary policy|liquidity|"
    r"rupee|inr|crude|brent|bond yield|g-sec|fii|fpi|credit growth|imf|fed|fomc|"
    r"tariff|trade deficit|current account|pmi|iip)",
    re.I,
)


def heuristic_score(it: dict) -> dict:
    """Deterministic fallback so the digest still works with no API key."""
    text = f"{it['title']} {it.get('summary', '')}"
    ca = min(10, 4 + it.get("ca_bias", 0) * 2 + (3 if CA_TERMS.search(text) else 0))
    macro = min(10, 3 + (3 if MACRO_TERMS.search(text) else 0))
    corrob = min(1.0, 0.34 * (it["corroboration"] - 1))
    base = 0.5 * max(ca, macro) + 0.5 * it["weight"] + corrob
    priority = int(max(1, min(10, round(base * 0.85))))
    if it["tier"] == "primary" and CA_TERMS.search(text):
        bucket = "ca_work"
    elif priority >= 8:
        bucket = "must_know"
    elif it["region"] == "india":
        bucket = "india_economy"
    elif MACRO_TERMS.search(text):
        bucket = "global"
    else:
        bucket = "watchlist"
    return {
        "headline": it["title"],
        "what_happened": it.get("summary") or it["title"],
        "why_it_matters": "Scored without the model — open the source to judge relevance yourself.",
        "action": None,
        "priority": priority,
        "scores": {"ca": min(10, ca), "business": min(10, macro), "market": min(10, macro), "global": 5},
        "bucket": bucket,
        "confidence": "low",
    }


PROMPT = """You are the analyst behind a chartered accountant's morning brief. He practises in India: direct tax, GST, audit, financial reporting, company law. He also follows the Indian and global economy and invests.

Below are today's candidate stories, already deduplicated. Return the {n} that genuinely deserve his attention today and discard the rest.

Rules that matter more than anything else:

1. The headline must state the IMPLICATION FOR HIM, never the event. Write "This could change the interest-rate outlook for your clients' borrowing costs", not "RBI holds repo rate". If you cannot articulate why he should care, drop the story.
2. "why_it_matters" is addressed to him in second person, one or two sentences, concrete. Name the mechanism — who is affected and through what channel.
3. "action" is non-null only when there is something he should actually do this week (check applicability to clients, diarise a due date, review a disclosure). Otherwise null. Do not invent work.
4. priority is 1-10. Reserve 9-10 for things that change what he does. A large global story he can only watch is a 6, not a 9.
5. Be willing to return fewer than {n} items. A short honest brief beats a padded one.
6. Never assert a fact that is not in the supplied title or summary. If detail is thin, say less.
7. Candidates were deduplicated by word overlap, which misses paraphrase. If several candidates describe ONE event, return a single item, use the most authoritative candidate as "ref" (a regulator beats a newspaper), and list the other candidate numbers in "merged_refs".

Buckets: must_know, ca_work, india_economy, global, markets, watchlist.

Also write "five_minutes": up to 5 single-sentence lines, the version he reads if he only has five minutes. Each line must be self-contained and implication-first.

Return ONLY valid JSON, no markdown fence:
{{"five_minutes": ["..."], "items": [{{"ref": <candidate number>, "bucket": "...", "headline": "...", "what_happened": "...", "why_it_matters": "...", "action": null, "priority": 7, "merged_refs": [], "scores": {{"ca": 0, "business": 0, "market": 0, "global": 0}}, "confidence": "high"}}]}}

CANDIDATES:
{candidates}"""


def _call_anthropic(key: str, prompt: str) -> str:
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        timeout=180,
        headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
        json={
            "model": MODEL or "claude-sonnet-5",
            "max_tokens": 8000,
            "messages": [{"role": "user", "content": prompt}],
        },
    )
    r.raise_for_status()
    return "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text")


def _call_gemini(key: str, prompt: str) -> str:
    """Google's free tier, via the documented native endpoint. Model names churn
    every few months, so several are tried in order and a 404 on one just moves
    to the next — a rename should never take the brief down."""
    models = [m for m in [
        os.environ.get("DFI_MODEL") or None,
        "gemini-flash-latest",
        "gemini-3.5-flash",
        "gemini-2.5-flash",
    ] if m]
    last: Exception | None = None
    for model in dict.fromkeys(models):  # de-dupe, keep order
        try:
            r = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                timeout=180,
                headers={"x-goog-api-key": key, "content-type": "application/json"},
                json={
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {"maxOutputTokens": 8000, "responseMimeType": "application/json"},
                },
            )
            if r.status_code in (400, 404) and "model" in r.text.lower():
                log(f"  · gemini model '{model}' rejected ({r.status_code}), trying next")
                last = RuntimeError(r.text[:200])
                continue
            r.raise_for_status()
            parts = r.json()["candidates"][0]["content"]["parts"]
            return "".join(pt.get("text", "") for pt in parts)
        except requests.HTTPError as exc:
            body = exc.response.text[:200] if exc.response is not None else ""
            log(f"  · gemini '{model}' HTTP {exc.response.status_code if exc.response is not None else '?'}: {body}")
            last = exc
        except Exception as exc:  # noqa: BLE001
            last = exc
    raise last or RuntimeError("no gemini model accepted the request")


def ai_score(items: list[dict], n: int) -> dict | None:
    """Prefers Anthropic if that key exists, else Gemini's free tier, else None
    (heuristic fallback). Empty-string secrets count as absent — GitHub hands
    unset secrets over as empty strings, not missing variables."""
    ant = os.environ.get("ANTHROPIC_API_KEY") or None
    gem = os.environ.get("GEMINI_API_KEY") or None
    if not (ant or gem):
        log("  · no ANTHROPIC_API_KEY or GEMINI_API_KEY, using heuristic scoring")
        return None
    provider = ("anthropic", ant) if ant else ("gemini", gem)

    lines = []
    for i, it in enumerate(items):
        extra = f" | also: {', '.join(it['also_reported_by'][:3])}" if it["also_reported_by"] else ""
        lines.append(
            f"[{i}] ({it['source']}, {it['tier']}) {it['title']}\n"
            f"    {it.get('summary', '')[:320]}{extra}"
        )
    prompt = PROMPT.format(n=n, candidates="\n".join(lines))

    for attempt in range(3):
        try:
            name, key = provider
            text = _call_anthropic(key, prompt) if name == "anthropic" else _call_gemini(key, prompt)
            text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
            out = json.loads(text)
            out["_provider"] = name
            return out
        except Exception as exc:  # noqa: BLE001
            log(f"  · model attempt {attempt + 1} ({provider[0]}) failed: {type(exc).__name__}: {exc}")
            time.sleep(3 * (attempt + 1))
    return None


# ───────────────────────────────── assembly ─────────────────────────────────


def item_id(it: dict) -> str:
    return hashlib.sha1(it["canonical"].encode()).hexdigest()[:12]


def load_yesterday() -> dict:
    today = datetime.now(IST).date().isoformat()
    files = [f for f in sorted(DATA.glob("archive/*.json")) if f.stem != today]
    if not files:
        return {}
    try:
        prev = json.loads(files[-1].read_text())
        return {i["id"]: i for i in prev.get("items", [])}
    except Exception:  # noqa: BLE001
        return {}


def build(clusters: list[dict], markets: list[dict], stats: dict, use_ai: bool) -> dict:
    ranked = sorted(clusters, key=lambda x: (-x["weight"], -x["corroboration"]))[: MAX_ITEMS * 4]
    verdict = ai_score(ranked, MAX_ITEMS) if use_ai else None

    items, five = [], []
    if verdict and verdict.get("items"):
        five = [s for s in verdict.get("five_minutes", []) if isinstance(s, str)][:5]
        for row in verdict["items"]:
            try:
                src = ranked[int(row["ref"])]
            except (KeyError, ValueError, IndexError):
                continue
            for extra in row.get("merged_refs") or []:
                try:
                    other = ranked[int(extra)]
                except (ValueError, IndexError):
                    continue
                if other["source"] != src["source"]:
                    src.setdefault("also_reported_by", []).append(other["source"])
            src["also_reported_by"] = sorted(set(src.get("also_reported_by", [])))[:5]
            items.append({**row, "_src": src})
    else:
        for src in ranked[:MAX_ITEMS]:
            items.append({**heuristic_score(src), "_src": src})
        five = [i["headline"] for i in sorted(items, key=lambda x: -x["priority"])[:5]]

    prev = load_yesterday()
    out = []
    for row in items:
        src = row.pop("_src")
        iid = item_id(src)
        was = prev.get(iid)
        out.append(
            {
                "id": iid,
                "bucket": row.get("bucket", "watchlist"),
                "headline": row.get("headline", src["title"]),
                "what_happened": row.get("what_happened", src.get("summary", "")),
                "why_it_matters": row.get("why_it_matters", ""),
                "action": row.get("action") or None,
                "priority": int(row.get("priority", 5)),
                "scores": row.get("scores", {}),
                "confidence": row.get("confidence", "medium"),
                "is_update": bool(was),
                "update_note": "Carried over from yesterday with new reporting." if was else None,
                "source": {
                    "name": src["source"],
                    "tier": src["tier"],
                    "url": src["url"],
                    "published": src["published"],
                },
                "also_reported_by": src.get("also_reported_by", []),
            }
        )

    order = {b: i for i, (b, _) in enumerate(BUCKETS)}
    out.sort(key=lambda x: (order.get(x["bucket"], 9), -x["priority"]))
    now = datetime.now(IST)
    return {
        "date": now.date().isoformat(),
        "generated_at": now.isoformat(),
        "scoring": (verdict or {}).get("_provider", "model") if verdict else "heuristic",
        "stats": {
            "items": len(out),
            "actions": sum(1 for i in out if i["action"]),
            "candidates": len(clusters),
            "sources_ok": stats["ok"],
            "sources_failed": stats["failed"],
        },
        "five_minutes": five,
        "markets": markets,
        "buckets": [{"id": b, "label": l} for b, l in BUCKETS],
        "items": out,
    }


# ───────────────────────────────── email ─────────────────────────────────


def render_email(d: dict) -> str:
    def esc(s):
        return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    when = datetime.fromisoformat(d["generated_at"]).strftime("%a %d %b %Y")
    rows = []
    labels = {b["id"]: b["label"] for b in d["buckets"]}
    seen = set()
    for it in d["items"]:
        if it["bucket"] not in seen:
            seen.add(it["bucket"])
            rows.append(
                f'<tr><td style="padding:26px 0 6px;font:600 11px/1.4 -apple-system,sans-serif;'
                f'letter-spacing:.09em;text-transform:uppercase;color:#8A8F96">'
                f'{esc(labels.get(it["bucket"], it["bucket"]))}</td></tr>'
            )
        tone = "#B3261E" if it["priority"] >= 8 else "#A85B0A" if it["priority"] >= 6 else "#5C6268"
        action = (
            f'<div style="margin-top:9px;font:600 13px/1.5 -apple-system,sans-serif;color:#B3261E">'
            f'Action · {esc(it["action"])}</div>'
            if it["action"]
            else ""
        )
        rows.append(
            f'<tr><td style="padding:14px 0;border-top:1px solid #EAE8E4">'
            f'<table width="100%" cellpadding="0" cellspacing="0"><tr>'
            f'<td width="34" valign="top" style="font:600 15px/1.3 ui-monospace,Menlo,monospace;color:{tone}">'
            f'{it["priority"]}</td>'
            f'<td style="border-left:3px solid {tone};padding-left:13px">'
            f'<div style="font:600 17px/1.35 Georgia,serif;color:#16191C">{esc(it["headline"])}</div>'
            f'<div style="margin-top:7px;font:400 14px/1.6 -apple-system,sans-serif;color:#3D4248">'
            f'{esc(it["what_happened"])}</div>'
            f'<div style="margin-top:7px;font:400 14px/1.6 -apple-system,sans-serif;color:#5C6268">'
            f'<b style="color:#16191C">Why it matters to you.</b> {esc(it["why_it_matters"])}</div>'
            f"{action}"
            f'<div style="margin-top:9px;font:500 12px/1.4 ui-monospace,Menlo,monospace;color:#8A8F96">'
            f'<a href="{esc(it["source"]["url"])}" style="color:#8A8F96">{esc(it["source"]["name"])} →</a></div>'
            f"</td></tr></table></td></tr>"
        )

    five = "".join(
        f'<li style="margin:0 0 9px;font:400 15px/1.55 -apple-system,sans-serif;color:#16191C">{esc(s)}</li>'
        for s in d["five_minutes"]
    )
    mkt = " · ".join(
        f'{esc(m["label"])} {esc(m["value"])} ({m["change"]:+.2f}%)' for m in d.get("markets", [])
    )

    return f"""<!doctype html><html><body style="margin:0;background:#F2F0EC;padding:20px 0">
<table width="100%" cellpadding="0" cellspacing="0"><tr><td align="center">
<table width="600" cellpadding="0" cellspacing="0" style="max-width:600px;background:#FCFCFB">
<tr><td style="background:#F2D3C6;padding:22px 26px">
  <div style="font:600 11px/1 ui-monospace,Menlo,monospace;letter-spacing:.16em;color:#7A3B28">DAILY INTEL</div>
  <div style="margin-top:8px;font:600 25px/1.2 Georgia,serif;color:#16191C">{when}</div>
  <div style="margin-top:6px;font:400 13px/1.4 -apple-system,sans-serif;color:#7A3B28">
    {d['stats']['items']} items · {d['stats']['actions']} need action</div>
</td></tr>
<tr><td style="padding:24px 26px 4px">
  <div style="font:600 11px/1 ui-monospace,Menlo,monospace;letter-spacing:.09em;color:#8A8F96">TODAY IN 5 MINUTES</div>
  <ol style="margin:14px 0 0;padding-left:20px">{five}</ol>
</td></tr>
<tr><td style="padding:0 26px 30px"><table width="100%" cellpadding="0" cellspacing="0">{''.join(rows)}</table></td></tr>
<tr><td style="padding:16px 26px 26px;border-top:1px solid #EAE8E4;
  font:400 12px/1.6 ui-monospace,Menlo,monospace;color:#8A8F96">
  {esc(mkt)}<br><br>
  Built from {d['stats']['sources_ok']} sources · scoring: {d['scoring']} ·
  relevance is a machine judgement, verify anything you act on against the original.
</td></tr></table></td></tr></table></body></html>"""


def send_email(d: dict) -> bool:
    host = os.environ.get("SMTP_HOST") or "smtp.gmail.com"
    port = int(os.environ.get("SMTP_PORT") or "587")
    user = os.environ.get("SMTP_USER") or None
    pwd = os.environ.get("SMTP_PASS") or None
    to = os.environ.get("DIGEST_TO") or None
    if not (user and pwd and to):
        log("  · email not configured (SMTP_USER / SMTP_PASS / DIGEST_TO), skipping")
        return False
    when = datetime.fromisoformat(d["generated_at"]).strftime("%d %b")
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Daily Intel · {when} · {d['stats']['items']} items, {d['stats']['actions']} need action"
    msg["From"] = user
    msg["To"] = to
    plain = "\n\n".join(
        f"[{i['priority']}] {i['headline']}\n{i['what_happened']}\nWhy it matters: {i['why_it_matters']}\n{i['source']['name']}: {i['source']['url']}"
        for i in d["items"]
    )
    msg.attach(MIMEText(plain, "plain", "utf-8"))
    msg.attach(MIMEText(render_email(d), "html", "utf-8"))
    try:
        with smtplib.SMTP(host, port, timeout=45) as s:
            s.starttls()
            s.login(user, pwd)
            s.send_message(msg)
        log(f"  · emailed {to}")
        return True
    except Exception as exc:  # noqa: BLE001
        log(f"  · email failed: {type(exc).__name__}: {exc}")
        return False


# ───────────────────────────────── main ─────────────────────────────────


def audit(conf: dict) -> int:
    """Try every source and print an honest table. No digest, no email, no commit."""
    session = requests.Session()
    ok = 0
    print(f"{'SOURCE':<18} {'STATUS':<12} DETAIL")
    print("-" * 78)
    for src in conf["sources"]:
        fn = fetch_scrape if "scrape" in src else fetch_feed
        try:
            got = fn(src, session)
        except Exception:  # noqa: BLE001
            got = []
        where = src["scrape"]["url"] if "scrape" in src else src["urls"][0]
        if got:
            ok += 1
            print(f"{src['id']:<18} {'OK':<12} {len(got)} items · {where[:52]}")
        else:
            print(f"{src['id']:<18} {'DEAD':<12} {where[:58]}")
    m = market_strip(conf, offline=False)
    print("-" * 78)
    print(f"markets strip: {'OK, ' + str(len(m)) + ' instruments' if m else 'DEAD'}")
    print(f"{ok}/{len(conf['sources'])} sources alive from this runner")
    return 0


def log(msg: str) -> None:
    print(msg, flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--audit", action="store_true", help="test every source, print a table, change nothing")
    ap.add_argument("--no-ai", action="store_true")
    ap.add_argument("--no-email", action="store_true")
    args = ap.parse_args()

    conf = yaml.safe_load(CONF.read_text())

    if args.audit:
        return audit(conf)

    DATA.mkdir(exist_ok=True)
    (DATA / "archive").mkdir(exist_ok=True)

    raw, stats = collect(conf, args.offline)
    if not raw:
        log("no items collected — leaving the previous digest in place")
        return 1

    fresh = in_window(raw, WINDOW_HOURS)
    log(f"  {len(fresh)} within {WINDOW_HOURS}h")
    clusters = cluster(fresh)
    log(f"  {len(clusters)} distinct events after deduplication")

    digest = build(clusters, market_strip(conf, args.offline), stats, use_ai=not args.no_ai)
    if args.offline:
        digest["sample"] = True  # so the UI can say so rather than passing fixtures off as news

    (DATA / "today.json").write_text(json.dumps(digest, indent=2, ensure_ascii=False))
    (DATA / "archive" / f"{digest['date']}.json").write_text(json.dumps(digest, indent=2, ensure_ascii=False))

    for old in sorted(DATA.glob("archive/*.json"))[:-60]:
        old.unlink()

    if not args.no_email:
        send_email(digest)

    log(f"done · {digest['stats']['items']} items · scoring={digest['scoring']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
