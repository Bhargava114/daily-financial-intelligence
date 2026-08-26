#!/usr/bin/env python3
"""
Learn from likes.

Triggered by the 'Learn from likes' workflow when the repo owner opens an issue
titled 'profile: likes'. Reads the liked headlines from the issue body, asks the
model to distil them into short interest phrases, and appends those to
collector/learned.yaml (never touching the hand-written profile.yaml).

The issue body is treated strictly as data: only '- ' bullet lines are read,
capped in count and length, and nothing from it is ever executed or followed
as an instruction.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import requests
import yaml

LEARNED = Path(__file__).resolve().parent / "learned.yaml"
MAX_KEPT = 15  # rolling window of learned interests


def extract_headlines(body: str) -> list[str]:
    out = []
    for ln in body.splitlines():
        ln = ln.strip()
        if ln.startswith("- ") and 8 <= len(ln) <= 240:
            # strip any markdown/link noise; keep plain words
            clean = re.sub(r"[\[\]()<>`*_]", " ", ln[2:])
            clean = re.sub(r"https?://\S+", "", clean)
            clean = re.sub(r"\s+", " ", clean).strip()[:200]
            if clean:
                out.append(clean)
    return out[:20]


def distil(headlines: list[str], existing: list[str]) -> list[str]:
    prompt = (
        "A reader marked these financial-news headlines as interesting. Extract up to 3 short "
        "investment-interest phrases (3-8 words each, e.g. 'US fiscal deficit and bond yields') "
        "that capture WHY these interested him. Do not repeat anything in EXISTING. "
        "Return ONLY a JSON array of strings, nothing else.\n"
        f"EXISTING: {json.dumps(existing)}\n"
        "HEADLINES:\n" + "\n".join(f"- {h}" for h in headlines)
    )
    ant = os.environ.get("ANTHROPIC_API_KEY") or None
    gem = os.environ.get("GEMINI_API_KEY") or None
    try:
        if ant:
            r = requests.post(
                "https://api.anthropic.com/v1/messages",
                timeout=90,
                headers={"x-api-key": ant, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                json={"model": os.environ.get("DFI_MODEL") or "claude-sonnet-5", "max_tokens": 400,
                      "messages": [{"role": "user", "content": prompt}]},
            )
            r.raise_for_status()
            text = "".join(b.get("text", "") for b in r.json()["content"] if b.get("type") == "text")
        elif gem:
            r = requests.post(
                "https://generativelanguage.googleapis.com/v1beta/models/"
                f"{os.environ.get('DFI_MODEL') or 'gemini-flash-latest'}:generateContent",
                timeout=90,
                headers={"x-goog-api-key": gem, "content-type": "application/json"},
                json={"contents": [{"parts": [{"text": prompt}]}],
                      "generationConfig": {"maxOutputTokens": 400, "responseMimeType": "application/json"}},
            )
            if r.status_code in (400, 404):  # model renamed — fall through to raw fallback
                return []
            r.raise_for_status()
            text = "".join(p.get("text", "") for p in r.json()["candidates"][0]["content"]["parts"])
        else:
            return []
        text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
        arr = json.loads(text)
        return [str(s).strip()[:80] for s in arr if isinstance(s, str) and s.strip()][:3]
    except Exception as exc:  # noqa: BLE001
        print(f"distil failed ({type(exc).__name__}), falling back to raw headlines")
        return []


def main() -> int:
    body = os.environ.get("ISSUE_BODY", "")
    headlines = extract_headlines(body)
    if not headlines:
        print("no liked headlines found in the issue body — nothing to learn")
        return 0

    data = {}
    if LEARNED.exists():
        try:
            data = yaml.safe_load(LEARNED.read_text()) or {}
        except Exception:  # noqa: BLE001
            data = {}
    existing = [str(x) for x in (data.get("learned_interests") or [])]

    new = distil(headlines, existing)
    if not new:  # no model available or it failed: keep the two clearest raw signals
        new = [h[:80] for h in headlines[:2]]
    new = [n for n in new if n not in existing]
    if not new:
        print("nothing new to add — profile already covers these")
        return 0

    merged = (existing + new)[-MAX_KEPT:]
    LEARNED.write_text(
        "# Written automatically by the 'Learn from likes' workflow.\n"
        "# Edit or delete lines freely — this is yours. profile.yaml is never touched.\n"
        + yaml.safe_dump({"learned_interests": merged}, sort_keys=False, allow_unicode=True)
    )
    print("learned:", "; ".join(new))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
