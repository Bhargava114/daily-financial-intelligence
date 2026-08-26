#!/usr/bin/env python3
"""
Weekly review — runs Saturday morning, reads the week's daily briefs from
data/archive/, and writes one synthesis: what actually changed, which threads
are building, and what next week holds. Output: data/weekly.json (the app
shows it as a card for six days) plus an email copy.

    python weekly.py             # build and email
    python weekly.py --no-email  # build only
"""

from __future__ import annotations

import argparse
import json
import os
import re
import smtplib
import sys
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

# reuse the daily collector's model callers, profile loader and paths
sys.path.insert(0, str(Path(__file__).resolve().parent))
from run import DATA, IST, call_model, load_profile, log  # noqa: E402

WEEKLY = DATA / "weekly.json"

PROMPT = """You are writing the Saturday weekly review for this reader:
{profile}

Below are the items from his daily briefs this week (newest last). Synthesise the WEEK, don't recap the days.

Return ONLY valid JSON, no markdown fence:
{{"title": "one line naming the week's defining theme",
  "changed": ["3-6 short paragraphs: what actually changed this week and why it matters to him — movement and connection across days, second person, macro first. Frame global shifts globally; bring in the India read-through only where it is genuinely material rather than routing every paragraph through the rupee"],
  "threads": [{{"name": "thread in a few words", "note": "one or two sentences on its trajectory and what confirms or breaks it"}}],
  "next_week": ["up to 5 concrete things to watch — data releases, meetings, decisions — one line each"]}}

Rules: never assert a fact or number that is not in the items below. At most 4 threads. If the week was quiet, say so honestly rather than inflating.

THIS WEEK'S ITEMS:
{items}"""


def week_items(days: int = 7) -> tuple[list[str], int]:
    cutoff = (datetime.now(IST) - timedelta(days=days)).date().isoformat()
    lines, ndays = [], 0
    for f in sorted(DATA.glob("archive/*.json"))[-days:]:
        if f.stem < cutoff:
            continue
        try:
            d = json.loads(f.read_text())
        except Exception:  # noqa: BLE001
            continue
        ndays += 1
        day = datetime.fromisoformat(d["date"]).strftime("%a %d %b")
        for it in d.get("items", []):
            if it.get("priority", 0) < 5:
                continue
            why = (it.get("why_it_matters") or "")[:140]
            lines.append(f"[{day} | p{it['priority']} | {it.get('bucket','')}] {it['headline']} — {why}")
    return lines[:90], ndays


def synthesise(lines: list[str]) -> dict | None:
    prompt = PROMPT.format(profile=load_profile() or "(no profile)", items="\n".join(lines))
    name, text = call_model(prompt)
    if not text:
        log("  · no model reachable — weekly falls back to a top-items list")
        return None
    try:
        text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
        out = json.loads(text)
        out["_provider"] = name
        return out
    except Exception as exc:  # noqa: BLE001
        log(f"  · {name} returned unparseable weekly output: {type(exc).__name__}")
        return None


def fallback(lines: list[str]) -> dict:
    top = sorted(lines, key=lambda x: -int(re.search(r"p(\d+)", x).group(1)))[:12]
    return {
        "title": "The week's highest-priority items (no model available)",
        "changed": [re.sub(r"^\[[^\]]+\]\s*", "", x) for x in top],
        "threads": [],
        "next_week": [],
        "_provider": "heuristic",
    }


def render_email(w: dict) -> str:
    def esc(s):
        return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    changed = "".join(f'<p style="font:400 15px/1.65 Georgia,serif;color:#16191C;margin:0 0 13px">{esc(x)}</p>' for x in w["changed"])
    threads = "".join(
        f'<p style="font:400 14px/1.6 -apple-system,sans-serif;color:#3D4248;margin:0 0 9px">'
        f'<b style="color:#16191C">{esc(t["name"])}.</b> {esc(t["note"])}</p>'
        for t in w.get("threads", [])
    )
    nxt = "".join(f'<li style="margin:0 0 7px;font:400 14px/1.55 -apple-system,sans-serif;color:#16191C">{esc(x)}</li>' for x in w.get("next_week", []))
    sec = lambda label: (f'<div style="margin:24px 0 10px;font:600 11px/1 ui-monospace,Menlo,monospace;'
                         f'letter-spacing:.12em;color:#8A8F96">{label}</div>')
    return f"""<!doctype html><html><body style="margin:0;background:#F2F0EC;padding:20px 0">
<table width="100%" cellpadding="0" cellspacing="0"><tr><td align="center">
<table width="600" cellpadding="0" cellspacing="0" style="max-width:600px;background:#FCFCFB">
<tr><td style="background:#F2D3C6;padding:22px 26px">
  <div style="font:600 11px/1 ui-monospace,Menlo,monospace;letter-spacing:.16em;color:#7A3B28">THE WEEK</div>
  <div style="margin-top:8px;font:600 23px/1.25 Georgia,serif;color:#16191C">{esc(w['title'])}</div>
</td></tr>
<tr><td style="padding:8px 26px 30px">
  {sec('WHAT CHANGED')}{changed}
  {sec('BUILDING THREADS') + threads if threads else ''}
  {sec('NEXT WEEK') + '<ol style="margin:0;padding-left:20px">' + nxt + '</ol>' if nxt else ''}
</td></tr></table></td></tr></table></body></html>"""


def send(w: dict) -> None:
    host = os.environ.get("SMTP_HOST") or "smtp.gmail.com"
    port = int(os.environ.get("SMTP_PORT") or "587")
    user = os.environ.get("SMTP_USER") or None
    pwd = os.environ.get("SMTP_PASS") or None
    to = os.environ.get("DIGEST_TO") or None
    if not (user and pwd and to):
        log("  · email not configured, skipping")
        return
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Daily Intel · The Week · {w['title'][:60]}"
    msg["From"], msg["To"] = user, to
    plain = w["title"] + "\n\n" + "\n\n".join(w["changed"])
    msg.attach(MIMEText(plain, "plain", "utf-8"))
    msg.attach(MIMEText(render_email(w), "html", "utf-8"))
    try:
        with smtplib.SMTP(host, port, timeout=45) as s:
            s.starttls()
            s.login(user, pwd)
            s.send_message(msg)
        log(f"  · emailed {to}")
    except Exception as exc:  # noqa: BLE001
        log(f"  · email failed: {type(exc).__name__}: {exc}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-email", action="store_true")
    args = ap.parse_args()

    lines, ndays = week_items()
    if not lines:
        log("no daily briefs found in the archive — nothing to review")
        return 0
    log(f"synthesising {len(lines)} items across {ndays} day(s)")

    w = synthesise(lines) or fallback(lines)
    now = datetime.now(IST)
    out = {
        "kind": "weekly",
        "date": now.date().isoformat(),
        "generated_at": now.isoformat(),
        "scoring": w.pop("_provider", "heuristic"),
        "days_covered": ndays,
        "title": str(w.get("title", ""))[:160],
        "changed": [str(x) for x in w.get("changed", [])][:8],
        "threads": [{"name": str(t.get("name", ""))[:60], "note": str(t.get("note", ""))[:400]}
                    for t in w.get("threads", []) if isinstance(t, dict)][:4],
        "next_week": [str(x)[:200] for x in w.get("next_week", [])][:5],
    }
    WEEKLY.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    if not args.no_email:
        send(out)
    log(f"done · weekly for {out['date']} · {ndays} days · scoring={out['scoring']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
