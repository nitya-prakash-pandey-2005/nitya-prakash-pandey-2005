#!/usr/bin/env python3
"""
Refreshes every dynamic part of the profile README.

Runs in GitHub Actions (.github/workflows/update-profile.yml) and:
  1. pulls public profile data from the GitHub GraphQL + REST APIs
  2. renders the sci-fi HUD cards in profile/ (hud, achievements, skyline, activity,
     clock, languages, timeline)
  3. rewrites the marked blocks in README.md (latest projects, recent activity, timestamp)

Standard library only, so the workflow needs no pip install.
Local run:      GITHUB_TOKEN=<token> python3 scripts/update_profile.py
Offline preview: PROFILE_FIXTURE=fixture.json python3 scripts/update_profile.py
                 (fixture = {"user": <GraphQL user object>, "events": [...]})
"""
from __future__ import annotations

import datetime as dt
import html
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# ─────────────────────────── settings you may want to edit ───────────────────────────
USER = os.environ.get("GH_USER") or os.environ.get("GITHUB_REPOSITORY_OWNER") or "nitya-prakash-pandey-2005"
FEATURED: list[str] = ["fieldpilot-ai", "TyreMind", "AgriVision-Ensemble-Net", "AgroSkin-AI"]          # repo names always shown first in "Latest work", e.g. ["AgriVision-Ensemble-Net"]
EXCLUDE_REPOS = {USER}            # hidden from projects + language stats (the profile repo itself)
HIDE_LANGUAGES: set[str] = set()  # e.g. {"HTML", "CSS"} to keep them out of the language card
PROJECT_COUNT = 6                 # cards in the "Latest work" grid (even number looks best)
ACTIVITY_COUNT = 8                # lines in the "Recent activity" feed
TIMEZONE = dt.timezone(dt.timedelta(hours=5, minutes=30), "IST")
# Hackathon / competition results for the achievements card: (prefix, rank, event line 1, event line 2, project)
ACHIEVEMENTS = [
    ("RANK", "5", "Kaya AI IIT", "Hackathon 2026", "FieldPilot AI"),
    ("RANK", "9", "Kaggle Agri Image", "Super-Resolution", "30-block RCAN"),
    ("TOP", "10", "TrackShift", "Innovation Challenge", "TyreMind"),
    ("TOP", "40", "IIT Delhi", "Innov8 Challenge", ""),
    ("TOP", "1.5K", "Amazon", "ML Challenge", ""),
]
# Toolkit card: (lane, [(tool, simple-icons slug or "")]). Icon paths live in scripts/toolkit_icons.json.
TOOLKIT = [
    ("Languages", [("Python", "python"), ("TypeScript", "typescript"), ("JavaScript", "javascript"), ("Bash", "gnubash")]),
    ("ML & Vision", [("PyTorch", "pytorch"), ("TensorFlow", "tensorflow"), ("scikit-learn", "scikitlearn"), ("OpenCV", "opencv"),
                     ("YOLO", "ultralytics"), ("NumPy", "numpy"), ("Pandas", "pandas")]),
    ("GenAI & Agents", [("LangGraph", "langchain"), ("Hugging Face", "huggingface"), ("Qdrant", "qdrant"), ("Qwen2.5-VL", ""),
                        ("Whisper", "openai"), ("Cohere", "")]),
    ("Medical imaging", [("SynthSeg", ""), ("BiomedParse", ""), ("ANTsPy", ""), ("OpenSlide", "")]),
    ("Apps & APIs", [("FastAPI", "fastapi"), ("React", "react"), ("Next.js", "nextdotjs"), ("Node.js", "nodedotjs"),
                     ("Express", "express"), ("MongoDB", "mongodb")]),
    ("Tooling", [("Git", "git"), ("GitHub", "github"), ("Actions", "githubactions"), ("Docker", "docker"),
                 ("Linux", "linux"), ("Jupyter", "jupyter"), ("Kaggle", "kaggle")]),
]
# ──────────────────────────────────────────────────────────────────────────────────────

TOKEN = os.environ.get("GITHUB_TOKEN", "")
FIXTURE = os.environ.get("PROFILE_FIXTURE", "")
ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "profile"
README = ROOT / "README.md"
NOW = dt.datetime.now(dt.timezone.utc)

# Palette shared with assets/header.svg: segmentation-label colours on scanner navy.
C = {
    "bg0": "#060B18", "bg1": "#0C1630", "panel": "#0E1730", "line": "#1C2A48", "tile": "#101C38",
    "text": "#E8EEF9", "soft": "#B9C5DD", "muted": "#8A97B4", "dim": "#4A5A7E",
    "cyan": "#38BDF8", "violet": "#A78BFA", "magenta": "#E879F9", "lime": "#A3E635",
    "amber": "#FBBF24", "coral": "#FB7185", "teal": "#2DD4BF", "orange": "#FB923C",
}
# Categorical slots for identity (languages), in fixed order. Validated on the #0E1730
# surface with the dataviz validator: lightness band, chroma, CVD (worst adjacent ΔE 8.6),
# normal-vision and contrast all pass. "Other" folds everything past slot 5 into gray.
CAT = ["#0284C7", "#D946EF", "#65A30D", "#8B5CF6", "#F43F5E"]
OTHER = "#4A5A7E"
# Heat ramp for magnitude (skyline levels 1-4): violet → magenta → coral → gold, lightness rising
# monotonically (validated on dark; multi-hue is the "semantic heat" exception, always with a legend).
SEQ = ["#6D28D9", "#C026D3", "#FB7185", "#FDE68A"]
# Neon accents: one per card, and for gauges / badges / weekdays that are labelled individually.
NEON = ["#38BDF8", "#A78BFA", "#E879F9", "#A3E635", "#FBBF24", "#FB7185", "#2DD4BF", "#FB923C"]
# Time-of-day bands for the commit clock (colour = band, shared by dial and bars).
BAND = {"NIGHT": "#A78BFA", "MORNING": "#FBBF24", "AFTERNOON": "#A3E635", "EVENING": "#E879F9"}
FONT = '-apple-system, "Segoe UI", Ubuntu, "Helvetica Neue", Arial, sans-serif'
MONO = 'ui-monospace, SFMono-Regular, "JetBrains Mono", "Cascadia Code", Consolas, "Liberation Mono", monospace'
GLOW = ' filter="url(#glow)"'


# ═════════════════════════════════════ API ═════════════════════════════════════
def _request(url: str, body: dict | None = None) -> dict | list:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": f"{USER}-profile-readme",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    data = json.dumps(body).encode() if body is not None else None
    last_err: Exception | None = None
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode())
        except (urllib.error.URLError, TimeoutError) as err:  # retry transient failures
            last_err = err
            time.sleep(2 ** attempt)
    raise RuntimeError(f"GitHub API request failed: {url}: {last_err}")


QUERY = """
query($login: String!) {
  user(login: $login) {
    login name createdAt
    followers { totalCount }
    pullRequests { totalCount }
    issues { totalCount }
    repositoriesContributedTo(contributionTypes: [COMMIT, PULL_REQUEST, ISSUE, REPOSITORY]) { totalCount }
    repositories(first: 100, ownerAffiliations: OWNER, privacy: PUBLIC,
                 orderBy: {field: PUSHED_AT, direction: DESC}) {
      totalCount
      nodes {
        name description url homepageUrl stargazerCount forkCount isFork isArchived pushedAt createdAt
        primaryLanguage { name color }
        languages(first: 12, orderBy: {field: SIZE, direction: DESC}) { edges { size node { name color } } }
        defaultBranchRef { target { ... on Commit {
          history(first: 100) { nodes { oid messageHeadline authoredDate author { user { login } } } }
        } } }
      }
    }
    contributionsCollection {
      totalCommitContributions
      totalPullRequestContributions
      totalIssueContributions
      restrictedContributionsCount
      contributionCalendar {
        totalContributions
        weeks { contributionDays { date contributionCount } }
      }
    }
  }
}
"""


def fetch_user() -> dict:
    if FIXTURE:
        return json.loads(Path(FIXTURE).read_text(encoding="utf-8"))["user"]
    if not TOKEN:
        sys.exit("GITHUB_TOKEN is not set. The GraphQL API needs a token (the workflow provides one).")
    res = _request("https://api.github.com/graphql", {"query": QUERY, "variables": {"login": USER}})
    if res.get("errors"):
        sys.exit(f"GraphQL error: {res['errors']}")
    return res["data"]["user"]


def fetch_events() -> list[dict]:
    if FIXTURE:
        return json.loads(Path(FIXTURE).read_text(encoding="utf-8")).get("events", [])
    try:
        return _request(f"https://api.github.com/users/{USER}/events/public?per_page=60")  # type: ignore[return-value]
    except RuntimeError as err:
        print(f"warning: events feed unavailable ({err}); keeping the previous one", file=sys.stderr)
        return []


# ═══════════════════════════════════ metrics ═══════════════════════════════════
def contribution_days(user: dict) -> list[tuple[dt.date, int]]:
    days = []
    for week in user["contributionsCollection"]["contributionCalendar"]["weeks"]:
        for d in week["contributionDays"]:
            days.append((dt.date.fromisoformat(d["date"]), d["contributionCount"]))
    return sorted(days)


def streaks(days: list[tuple[dt.date, int]]) -> tuple[int, int]:
    longest = run = 0
    for _, n in days:
        run = run + 1 if n > 0 else 0
        longest = max(longest, run)
    current = 0
    seq = list(days)
    if seq and seq[-1][1] == 0:  # today not counted yet → streak can still be alive from yesterday
        seq = seq[:-1]
    for _, n in reversed(seq):
        if n == 0:
            break
        current += 1
    return current, longest


def language_mix(repos: list[dict]) -> list[tuple[str, str, float]]:
    """Blend bytes and repo count (sqrt(bytes) * sqrt(repos)) so one huge notebook can't swamp everything.
    Top five languages take the categorical slots in rank order; the rest fold into Other."""
    agg: dict[str, dict] = {}
    for r in repos:
        for edge in r["languages"]["edges"]:
            name = edge["node"]["name"]
            if name in HIDE_LANGUAGES:
                continue
            a = agg.setdefault(name, {"size": 0, "count": 0})
            a["size"] += edge["size"]
            a["count"] += 1
    scored = {k: math.sqrt(v["size"]) * math.sqrt(v["count"]) for k, v in agg.items()}
    total = sum(scored.values()) or 1
    ranked = sorted(((k, 100 * s / total) for k, s in scored.items()), key=lambda t: -t[1])
    mix = [(name, CAT[i], pct) for i, (name, pct) in enumerate(ranked[:len(CAT)])]
    if len(ranked) > len(CAT):
        mix.append(("Other", OTHER, sum(p for _, p in ranked[len(CAT):])))
    return mix


def commit_hours(user: dict) -> list[int]:
    """Commits per local hour (IST) from the last 100 commits on each repo's default branch."""
    hours = [0] * 24
    for r in user["repositories"]["nodes"]:
        if r["isFork"]:
            continue
        target = ((r.get("defaultBranchRef") or {}).get("target") or {})
        for c in (target.get("history") or {}).get("nodes", []):
            login = ((c.get("author") or {}).get("user") or {}).get("login") or ""
            if login.lower() != USER.lower() or not c.get("authoredDate"):
                continue
            t = dt.datetime.fromisoformat(c["authoredDate"].replace("Z", "+00:00")).astimezone(TIMEZONE)
            hours[t.hour] += 1
    return hours


def next_milestone(v: int) -> int:
    for m in (5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000, 25000, 50000, 100000):
        if v < m:
            return m
    return int(10 ** math.ceil(math.log10(v + 1)))


# ═══════════════════════════════════ SVG kit ═══════════════════════════════════
def esc(s: str) -> str:
    return html.escape(s, quote=True)


def fmt(n: int) -> str:
    return f"{n:,}"


def mix_hex(a: str, b: str, t: float) -> str:
    """Blend colour a toward b by t (0 = a, 1 = b)."""
    pa = [int(a[i:i + 2], 16) for i in (1, 3, 5)]
    pb = [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * t):02X}" for x, y in zip(pa, pb))


def polar(cx: float, cy: float, r: float, deg: float) -> tuple[float, float]:
    """Point at `deg` degrees clockwise from 12 o'clock."""
    a = math.radians(deg)
    return cx + r * math.sin(a), cy - r * math.cos(a)


def sector(cx: float, cy: float, r0: float, r1: float, a0: float, a1: float) -> str:
    """Annular sector path from angle a0 to a1 (clockwise from 12 o'clock)."""
    large = 1 if a1 - a0 > 180 else 0
    (x0, y0), (x1, y1) = polar(cx, cy, r1, a0), polar(cx, cy, r1, a1)
    (x2, y2), (x3, y3) = polar(cx, cy, r0, a1), polar(cx, cy, r0, a0)
    return (f"M{x0:.1f},{y0:.1f} A{r1},{r1} 0 {large} 1 {x1:.1f},{y1:.1f} "
            f"L{x2:.1f},{y2:.1f} A{r0},{r0} 0 {large} 0 {x3:.1f},{y3:.1f} Z")


def tick_ring(cx: float, cy: float, r: float, n: int, major: int, length: float = 5, cls: str = "", color: str = "") -> str:
    ticks = []
    for i in range(n):
        ln = length * (1.8 if i % major == 0 else 1)
        (x0, y0), (x1, y1) = polar(cx, cy, r, 360 * i / n), polar(cx, cy, r + ln, 360 * i / n)
        ticks.append(f"M{x0:.1f},{y0:.1f}L{x1:.1f},{y1:.1f}")
    return f'<path d="{"".join(ticks)}" stroke="{color or C["dim"]}" stroke-opacity="{.55 if color else 1}" stroke-width="1" class="{cls}"/>'


def frame(w: int, h: int, code: str, title: str, body: str, extra_css: str = "", label: str = "", accent: str = "") -> str:
    """Shared HUD panel: navy glass, faint grid, corner brackets, a slow scan line and a header rule."""
    stamp = NOW.astimezone(TIMEZONE).strftime("%d %b %Y").upper()
    ac = accent or C["cyan"]
    b = 16  # corner bracket arm
    corners = "".join(
        f'<path d="M{x},{y + sy * b} L{x},{y} L{x + sx * b},{y}"/>'
        for x, y, sx, sy in ((10, 10, 1, 1), (w - 10, 10, -1, 1), (10, h - 10, 1, -1), (w - 10, h - 10, -1, -1))
    )
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}" role="img" aria-label="{esc(label or title)}">
<defs>
  <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="{C['bg0']}"/><stop offset="1" stop-color="{C['bg1']}"/></linearGradient>
  <pattern id="grid" width="24" height="24" patternUnits="userSpaceOnUse"><path d="M24 0H0V24" fill="none" stroke="#FFFFFF" stroke-opacity=".03"/></pattern>
  <linearGradient id="rule" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="{ac}" stop-opacity=".8"/><stop offset=".45" stop-color="{ac}" stop-opacity=".15"/><stop offset="1" stop-color="{ac}" stop-opacity="0"/></linearGradient>
  <linearGradient id="scanline" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="{ac}" stop-opacity="0"/><stop offset="1" stop-color="{ac}" stop-opacity=".09"/></linearGradient>
  <filter id="glow" x="-50%" y="-50%" width="200%" height="200%"><feGaussianBlur stdDeviation="3" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter>
  <clipPath id="card"><rect width="{w}" height="{h}" rx="16"/></clipPath>
</defs>
<style>
  text {{ font-family: {FONT}; }}
  .t {{ font: 600 13px {MONO}; letter-spacing: 2.6px; fill: {C['text']}; }}
  .code {{ fill: {ac}; }}
  .stamp {{ font: 10.5px {MONO}; letter-spacing: 1.6px; fill: {C['muted']}; }}
  .lbl {{ font: 11px {MONO}; letter-spacing: 1.4px; fill: {C['muted']}; }}
  .val {{ font-size: 24px; font-weight: 700; fill: {C['text']}; font-variant-numeric: tabular-nums; }}
  .small {{ font: 10.5px {MONO}; fill: {C['muted']}; }}
  .note {{ font-size: 13px; fill: {C['soft']}; }}
  .strong {{ fill: {C['text']}; font-weight: 650; }}
  .scan {{ animation: scan 7s linear infinite; }}
  @keyframes scan {{ from {{ transform: translateY(-80px); }} to {{ transform: translateY({h}px); }} }}
  .blink {{ animation: blink 1.6s steps(2, start) infinite; }}
  @keyframes blink {{ to {{ opacity: .15; }} }}
  .spin {{ transform-box: fill-box; transform-origin: center; animation: spin 60s linear infinite; }}
  .spin-r {{ transform-box: fill-box; transform-origin: center; animation: spin 90s linear infinite reverse; }}
  @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
  @keyframes fade {{ from {{ opacity: 0; }} }}
  {extra_css}
  @media (prefers-reduced-motion: reduce) {{ * {{ animation: none !important; }} .scan {{ display: none; }} }}
</style>
<g clip-path="url(#card)">
  <rect width="{w}" height="{h}" fill="url(#bg)"/>
  <rect width="{w}" height="{h}" fill="url(#grid)"/>
  <rect class="scan" x="0" y="0" width="{w}" height="80" fill="url(#scanline)"/>
</g>
<rect x=".5" y=".5" width="{w-1}" height="{h-1}" rx="15.5" fill="none" stroke="{C['line']}"/>
<g fill="none" stroke="{ac}" stroke-opacity=".8" stroke-width="1.5">{corners}</g>
<circle cx="31" cy="34" r="3.5" fill="{ac}" class="blink"/>
<text x="44" y="38.5" class="t"><tspan class="code">{esc(code)} //</tspan> {esc(title.upper())}</text>
<text x="{w-28}" y="38.5" class="stamp" text-anchor="end">SYNC {stamp} · IST</text>
<rect x="28" y="52" width="{w-56}" height="1" fill="url(#rule)"/>
{body}
</svg>
"""


# ═══════════════════════════════════ cards ═══════════════════════════════════
def render_hud(user: dict, repos: list[dict], days: list[tuple[dt.date, int]], cur: int, longest: int) -> str:
    cc = user["contributionsCollection"]
    active = sum(1 for _, n in days if n)
    gauges = [
        ("CONTRIBUTIONS", "last 12 months", cc["contributionCalendar"]["totalContributions"]),
        ("COMMITS", "last 12 months", cc["totalCommitContributions"]),
        ("ACTIVE DAYS", "last 12 months", active),
        ("REPOSITORIES", "public", len(repos)),
        ("STREAK", "days, current", cur),
        ("BEST STREAK", "days, longest", longest),
    ]
    W, H = 900, 342
    R = 44
    circ = 2 * math.pi * R
    out = []
    for i, (label, sub, value) in enumerate(gauges):
        cx, cy = 83 + i * 146.8, 148
        col = NEON[i]
        goal = next_milestone(value)
        frac = min(1.0, value / goal) if goal else 0
        ex, ey = polar(cx, cy, R, 360 * frac)
        delay = 0.25 + i * 0.12
        out.append(f"""<g>
  {tick_ring(cx, cy, R + 12, 60, 5, 3.5, "spin" if i % 2 == 0 else "spin-r", col)}
  <circle cx="{cx:.1f}" cy="{cy}" r="{R + 8}" fill="none" stroke="{C['line']}" stroke-dasharray="2 5"/>
  <circle cx="{cx:.1f}" cy="{cy}" r="{R}" fill="none" stroke="{C['line']}" stroke-width="5"/>
  <circle cx="{cx:.1f}" cy="{cy}" r="{R}" fill="{col}" fill-opacity=".05" stroke="{col}" stroke-width="5" stroke-linecap="round"
          stroke-dasharray="{circ:.1f}" style="stroke-dashoffset:{circ * (1 - frac):.1f}; animation-delay:{delay:.2f}s"
          transform="rotate(-90 {cx:.1f} {cy})" class="arc" filter="url(#glow)" opacity="{1 if frac else 0}"/>
  <circle cx="{ex:.1f}" cy="{ey:.1f}" r="3.2" fill="#FFFFFF" class="tip" style="animation-delay:{delay + 1.3:.2f}s" opacity="{1 if frac else 0}"/>
  <text x="{cx:.1f}" y="{cy + 8}" class="val" text-anchor="middle">{fmt(value)}</text>
  <text x="{cx:.1f}" y="{cy + 86}" class="lbl" text-anchor="middle" style="fill:{C['text']}">{label}</text>
  <text x="{cx:.1f}" y="{cy + 103}" class="small" text-anchor="middle">{sub}</text>
  <text x="{cx:.1f}" y="{cy + 119}" class="small" text-anchor="middle">next <tspan style="fill:{col}">▸</tspan> {fmt(goal)}</text>
</g>""")
    since = dt.datetime.fromisoformat(user["createdAt"].replace("Z", "+00:00")).strftime("%b %Y").upper()
    strip = [
        ("PULL REQUESTS", fmt(user["pullRequests"]["totalCount"])),
        ("ISSUES", fmt(user["issues"]["totalCount"])),
        ("STARS", fmt(sum(r["stargazerCount"] for r in repos))),
        ("FOLLOWERS", fmt(user["followers"]["totalCount"])),
        ("CONTRIBUTED TO", fmt(user["repositoriesContributedTo"]["totalCount"])),
        ("ONLINE SINCE", since),
    ]
    sx = 28
    seg = (W - 56) / len(strip)
    out.append(f'<rect x="28" y="{H - 52}" width="{W - 56}" height="30" rx="6" fill="{C["panel"]}" stroke="{C["line"]}"/>')
    for i, (k, v) in enumerate(strip):
        x = sx + i * seg
        if i:
            out.append(f'<line x1="{x:.1f}" y1="{H - 46}" x2="{x:.1f}" y2="{H - 28}" stroke="{C["line"]}"/>')
        out.append(f'<text x="{x + seg / 2:.1f}" y="{H - 33}" class="small" text-anchor="middle">{k} <tspan class="strong">{esc(v)}</tspan></text>')
    css = (
        f".arc {{ animation: arc 1.8s cubic-bezier(.3,.7,.2,1) both; }} @keyframes arc {{ from {{ stroke-dashoffset: {circ:.1f}; }} }}"
        " .tip { animation: fade .4s ease both; }"
    )
    return frame(W, H, "01", "System telemetry", "\n".join(out), css,
                 "GitHub telemetry: " + ", ".join(f"{g[0].lower()} {g[2]}" for g in gauges), NEON[0])


def render_achievements() -> str:
    W, H = 900, 270
    out = []
    n = len(ACHIEVEMENTS)
    step = (W - 56) / n
    for i, (pre, rank, l1, l2, proj) in enumerate(ACHIEVEMENTS):
        cx, cy, r = 28 + step * (i + .5), 124, 50
        col = [NEON[4], NEON[0], NEON[2], NEON[3], NEON[1]][i % 5]
        hexp = " ".join(f"{polar(cx, cy, r, a)[0]:.1f},{polar(cx, cy, r, a)[1]:.1f}" for a in range(0, 360, 60))
        hexo = " ".join(f"{polar(cx, cy, r + 9, a)[0]:.1f},{polar(cx, cy, r + 9, a)[1]:.1f}" for a in range(0, 360, 60))
        d = 0.2 + i * 0.15
        big = 30 if len(rank) <= 2 else 24
        out.append(f"""<g class="pop" style="animation-delay:{d:.2f}s">
  <circle cx="{cx:.1f}" cy="{cy}" r="{r + 20}" fill="none" stroke="{col}" stroke-opacity=".45" stroke-dasharray="1 7" class="{'spin' if i % 2 else 'spin-r'}"/>
  <polygon points="{hexo}" fill="none" stroke="{col}" stroke-opacity=".25"/>
  <polygon points="{hexp}" fill="{col}" fill-opacity=".1" stroke="{col}" stroke-width="1.8" filter="url(#glow)"/>
  <text x="{cx:.1f}" y="{cy - 14}" class="small" text-anchor="middle" style="letter-spacing:2px">{pre}</text>
  <text x="{cx:.1f}" y="{cy + 18}" text-anchor="middle" style="font-size:{big}px;font-weight:750;fill:{C['text']}">{'#' if pre == 'RANK' else ''}{esc(rank)}</text>
  <text x="{cx:.1f}" y="{cy + 94}" class="note strong" text-anchor="middle">{esc(l1)}</text>
  <text x="{cx:.1f}" y="{cy + 111}" class="note" text-anchor="middle">{esc(l2)}</text>
  <text x="{cx:.1f}" y="{cy + 129}" class="small" text-anchor="middle" style="fill:{col}">{esc(proj.upper())}</text>
</g>""")
    css = (".pop { animation: pop .8s cubic-bezier(.2,.8,.2,1.2) both; transform-box: fill-box; transform-origin: center; }"
           " @keyframes pop { from { opacity: 0; transform: scale(.85); } }")
    label = "Results: " + "; ".join(f"{p.lower()} {r}, {a} {b}" for p, r, a, b, _ in ACHIEVEMENTS)
    return frame(W, H, "02", "Mission record", "\n".join(out), css, label, NEON[4])


def render_toolkit() -> str:
    """Tech arsenal: one colour-coded lane per domain, a data pulse running along each circuit trace,
    tools as glowing chips with their logos, and a scan beam sweeping every lane."""
    icons = json.loads((ROOT / "scripts" / "toolkit_icons.json").read_text(encoding="utf-8"))
    lane_cols = [NEON[4], NEON[1], NEON[2], NEON[0], NEON[3], NEON[5]]
    W, lane_h, top = 900, 56, 76
    H = top + lane_h * len(TOOLKIT) + 22
    tx0, tx1 = 204, W - 28
    n_tools = sum(len(t) for _, t in TOOLKIT)
    out = [f'<line x1="44" y1="{top + 22}" x2="44" y2="{top + 22 + lane_h * (len(TOOLKIT) - 1)}" stroke="{C["line"]}" stroke-width="2"/>',
           f'<line x1="44" y1="{top + 22}" x2="44" y2="{top + 22 + lane_h * (len(TOOLKIT) - 1)}" stroke="url(#bus)" stroke-width="2" class="bus"/>']
    defs = [f'<linearGradient id="bus" x1="0" y1="0" x2="0" y2="1">'
            + "".join(f'<stop offset="{i / (len(TOOLKIT) - 1):.2f}" stop-color="{c}"/>' for i, c in enumerate(lane_cols)) + "</linearGradient>"]
    for i, (lane, tools) in enumerate(TOOLKIT):
        col = lane_cols[i % len(lane_cols)]
        cy = top + 22 + i * lane_h
        defs.append(f'<linearGradient id="sh{i}" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="{col}" stop-opacity="0"/>'
                    f'<stop offset=".5" stop-color="{col}" stop-opacity=".22"/><stop offset="1" stop-color="{col}" stop-opacity="0"/></linearGradient>')
        hexp = " ".join(f"{polar(44, cy, 13, a)[0]:.1f},{polar(44, cy, 13, a)[1]:.1f}" for a in range(30, 390, 60))
        out.append(f"""<g class="chip" style="animation-delay:{.15 + i * .1:.2f}s">
  <circle cx="44" cy="{cy}" r="20" fill="none" stroke="{col}" stroke-opacity=".5" stroke-dasharray="2 5" class="{'spin' if i % 2 else 'spin-r'}"/>
  <polygon points="{hexp}" fill="{C['bg0']}" stroke="{col}" stroke-width="1.6"{GLOW}/>
  <text x="44" y="{cy + 4}" text-anchor="middle" class="hexn" style="fill:{col}">{i + 1:02d}</text>
  <text x="74" y="{cy - 2}" class="lbl" style="fill:{C['text']}">{esc(lane.upper())}</text>
  <text x="74" y="{cy + 14}" class="small">{len(tools)} tools</text>
</g>""")
        out.append(f'<line x1="{tx0 - 12}" y1="{cy}" x2="{tx1}" y2="{cy}" stroke="{col}" stroke-opacity=".22"/>')
        out.append(f'<line x1="{tx0 - 12}" y1="{cy}" x2="{tx1}" y2="{cy}" stroke="{col}" stroke-width="2.4" stroke-linecap="round"'
                   f' class="pulse" style="animation-delay:{-i * .9:.1f}s"{GLOW}/>')
        widths = [36 + 7.5 * len(name) for name, _ in tools]
        gap = min(10.0, (tx1 - tx0 - sum(widths)) / max(1, len(tools) - 1))
        x = float(tx0)
        for j, ((name, slug), w) in enumerate(zip(tools, widths)):
            if slug and slug in icons:
                glyph = (f'<g transform="translate({x + 10:.1f},{cy - 8}) scale(.6667)" class="ico" style="animation-delay:{(i * 7 + j) * .37 % 4:.2f}s">'
                         f'<path d="{icons[slug]}" fill="{col}"/></g>')
            else:  # no public logo: a small scan-target glyph
                glyph = (f'<g class="ico" style="animation-delay:{(i * 7 + j) * .37 % 4:.2f}s"><circle cx="{x + 18:.1f}" cy="{cy}" r="6" fill="none" stroke="{col}" stroke-width="1.6"/>'
                         f'<circle cx="{x + 18:.1f}" cy="{cy}" r="2" fill="{col}"/></g>')
            out.append(f'<g class="chip" style="animation-delay:{.35 + i * .1 + j * .06:.2f}s">'
                       f'<rect x="{x:.1f}" y="{cy - 15}" width="{w:.1f}" height="30" rx="8" fill="{C["panel"]}" stroke="{col}" stroke-opacity=".55"/>'
                       f'{glyph}<text x="{x + 32:.1f}" y="{cy + 4.5}" style="font-size:12.5px;fill:{C["text"]}">{esc(name)}</text></g>')
            x += w + gap
        out.append(f'<rect x="{tx0 - 12}" y="{cy - 17}" width="130" height="34" fill="url(#sh{i})" class="beam" style="animation-delay:{i * .8:.1f}s"/>')
    css = (f".hexn {{ font: 700 10.5px {MONO}; }}"
           " .chip { animation: chipin .7s cubic-bezier(.2,.8,.2,1) both; }"
           " @keyframes chipin { from { opacity: 0; transform: translateY(8px); } }"
           " .pulse { stroke-dasharray: 46 1400; animation: flow 4.5s linear infinite; }"
           " @keyframes flow { from { stroke-dashoffset: 46; } to { stroke-dashoffset: -760; } }"
           " .beam { animation: beam 6s cubic-bezier(.5,0,.5,1) infinite; opacity: 0; }"
           f" @keyframes beam {{ 0% {{ transform: translateX(0); opacity: 0; }} 15% {{ opacity: 1; }} 85% {{ opacity: 1; }} 100% {{ transform: translateX({tx1 - tx0 - 100}px); opacity: 0; }} }}"
           " .ico { animation: ico 4s ease-in-out infinite; } @keyframes ico { 50% { opacity: .55; } }"
           " .bus { stroke-dasharray: 30 300; animation: busf 3s linear infinite; } @keyframes busf { from { stroke-dashoffset: 30; } to { stroke-dashoffset: -300; } }")
    body = "<defs>" + "".join(defs) + "</defs>\n" + "\n".join(out)
    label = "Toolkit: " + "; ".join(f"{lane}: " + ", ".join(n for n, _ in tools) for lane, tools in TOOLKIT)
    return frame(W, H, "03", f"Arsenal · {n_tools} tools · {len(TOOLKIT)} domains", body, css, label, NEON[7])


def render_skyline(days: list[tuple[dt.date, int]]) -> str:
    """Isometric city of the contribution calendar: one tower per day, height and colour by count."""
    W, H = 900, 390
    if not days:
        return frame(W, H, "04", "Contribution skyline", '<text x="44" y="120" class="note">No contribution data yet.</text>')
    first = days[0][0]
    sunday0 = first - dt.timedelta(days=(first.weekday() + 1) % 7)
    ux, uy, vx, vy = 11.2, 1.5, 6.2, 9.6       # week axis u, weekday axis v
    ox, oy = 34, 188
    mx = max(n for _, n in days) or 1
    hmax = 100

    def pt(c: float, r: float, h: float = 0) -> tuple[float, float]:
        return ox + c * ux + r * vx, oy + c * uy + r * vy - h

    def poly(ps: list[tuple[float, float]]) -> str:
        return " ".join(f"{x:.1f},{y:.1f}" for x, y in ps)

    cells = []
    for d, n in days:
        col, row = (d - sunday0).days // 7, (d.weekday() + 1) % 7
        cells.append((col, row, n, d))
    ncols = max(c for c, *_ in cells) + 1
    floor, towers = [], []
    for col, row, n, d in sorted(cells, key=lambda t: (t[1] - t[0], t[1])):  # back to front
        p0, p1, p2, p3 = pt(col, row), pt(col + 1, row), pt(col + 1, row + 1), pt(col, row + 1)
        if not n:
            floor.append(f'<polygon points="{poly([p0, p1, p2, p3])}"/>')
            continue
        level = min(3, int(4 * n / (mx + 1e-9) - 1e-9)) if mx else 0
        top = SEQ[level]
        h = 5 + hmax * math.sqrt(n / mx)
        q0, q1, q2, q3 = (pt(col, row, h), pt(col + 1, row, h), pt(col + 1, row + 1, h), pt(col, row + 1, h))
        towers.append(f"""<g class="rise" style="animation-delay:{0.3 + col * 0.035:.2f}s"><title>{d:%d %b %Y}: {n}</title>
  <polygon points="{poly([p0, p3, q3, q0])}" fill="{mix_hex(top, C['bg0'], .55)}"/>
  <polygon points="{poly([p3, p2, q2, q3])}" fill="{mix_hex(top, C['bg0'], .3)}"/>
  <polygon points="{poly([q0, q1, q2, q3])}" fill="{top}"/>
</g>""")
    # month labels along the front edge
    months, seen = [], set()
    for col, row, n, d in cells:
        if d.day <= 7 and row == 0 and (d.year, d.month) not in seen:
            seen.add((d.year, d.month))
            x, y = pt(col + .5, 7)
            months.append(f'<text x="{x:.1f}" y="{y + 16:.1f}" class="small" text-anchor="middle">{d:%b}</text>')
    for r, name in ((1, "Mon"), (3, "Wed"), (5, "Fri")):
        x, y = pt(0, r + .5)
        months.append(f'<text x="{x - 8:.1f}" y="{y + 4:.1f}" class="small" text-anchor="end">{name}</text>')
    # scan beam sweeping along the weeks
    beam = poly([pt(0, 0), pt(1.2, 0), pt(1.2, 7), pt(0, 7)])
    ex, ey = ncols * ux, ncols * uy

    total = sum(n for _, n in days)
    active = sum(1 for _, n in days if n)
    best_d, best_n = max(days, key=lambda t: t[1])
    this_month = sum(n for d, n in days if (d.year, d.month) == (days[-1][0].year, days[-1][0].month))
    stats = [("TOTAL", fmt(total)), ("ACTIVE DAYS", fmt(active)),
             ("BEST DAY", f"{best_n}" if best_n else "–"), ("THIS MONTH", fmt(this_month))]
    side = []
    for i, (k, v) in enumerate(stats):
        y = 96 + i * 52
        side.append(f'<text x="742" y="{y}" class="lbl">{k}</text>')
        side.append(f'<text x="742" y="{y + 26}" class="val">{esc(v)}</text>')
        if k == "BEST DAY" and best_n:
            side.append(f'<text x="{742 + 16 + 15 * len(v)}" y="{y + 25}" class="small">{best_d:%d %b}</text>')
    lx, ly = 742, 330
    side.append(f'<text x="{lx}" y="{ly}" class="small">less</text>')
    for i, col in enumerate(SEQ):
        side.append(f'<rect x="{lx + 34 + i * 18}" y="{ly - 10}" width="14" height="14" rx="3" fill="{col}"/>')
    side.append(f'<text x="{lx + 34 + 4 * 18 + 4}" y="{ly}" class="small">more</text>')
    side.append(f'<text x="{lx}" y="{ly + 22}" class="small" style="fill:{C["dim"]}">height = √ contributions</text>')

    body = f"""<defs><linearGradient id="beam" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="{NEON[2]}" stop-opacity="0"/><stop offset="1" stop-color="{NEON[2]}" stop-opacity=".4"/></linearGradient></defs>
<g fill="{C['tile']}" stroke="{C['line']}" stroke-width=".6">{''.join(floor)}</g>
<polygon points="{beam}" fill="url(#beam)" class="beam"/>
{''.join(towers)}
{''.join(months)}
<line x1="720" y1="74" x2="720" y2="{H - 30}" stroke="{C['line']}"/>
{''.join(side)}"""
    css = (".rise { animation: rise .9s cubic-bezier(.2,.8,.2,1) both; }"
           " @keyframes rise { from { opacity: 0; transform: translateY(18px); } }"
           f" .beam {{ animation: beam 6s linear infinite; }} @keyframes beam {{ from {{ transform: translate(0,0); }} to {{ transform: translate({ex:.1f}px,{ey:.1f}px); }} }}")
    return frame(W, H, "04", "Contribution skyline · 12 months", body, css,
                 f"Isometric contribution calendar: {total} contributions over {active} active days", NEON[2])


def _smooth(pts: list[tuple[float, float]], floor: float | None = None) -> str:
    """Catmull-Rom → cubic Bézier path through pts (control points clamped so the curve never dips below zero)."""
    if len(pts) < 2:
        return ""
    d = [f"M{pts[0][0]:.1f},{pts[0][1]:.1f}"]
    for i in range(len(pts) - 1):
        p0 = pts[i - 1] if i > 0 else pts[i]
        p1, p2 = pts[i], pts[i + 1]
        p3 = pts[i + 2] if i + 2 < len(pts) else p2
        c1 = (p1[0] + (p2[0] - p0[0]) / 6, p1[1] + (p2[1] - p0[1]) / 6)
        c2 = (p2[0] - (p3[0] - p1[0]) / 6, p2[1] - (p3[1] - p1[1]) / 6)
        if floor is not None:
            c1, c2 = (c1[0], min(c1[1], floor)), (c2[0], min(c2[1], floor))
        d.append(f"C{c1[0]:.1f},{c1[1]:.1f} {c2[0]:.1f},{c2[1]:.1f} {p2[0]:.1f},{p2[1]:.1f}")
    return " ".join(d)


def render_activity(days: list[tuple[dt.date, int]]) -> str:
    W, H = 900, 310
    # ── weekly trend (left) ──
    weeks: list[tuple[dt.date, int]] = []
    for i in range(0, len(days), 7):
        chunk = days[i:i + 7]
        weeks.append((chunk[0][0], sum(n for _, n in chunk)))
    gx0, gx1, gy0, gy1 = 52, 575, 84, 240
    peak = max((n for _, n in weeks), default=0)
    top = 3 * math.ceil(max(3, peak * 1.22) / 3)   # headroom for the peak label; multiple of 3 keeps gridlines whole
    step = (gx1 - gx0) / max(1, len(weeks) - 1)
    pts = [(gx0 + i * step, gy1 - (n / top) * (gy1 - gy0)) for i, (_, n) in enumerate(weeks)]
    line = _smooth(pts, floor=gy1)
    area = f"{line} L{gx1:.1f},{gy1} L{gx0},{gy1} Z" if line else ""
    length = int(sum(math.dist(pts[i], pts[i + 1]) for i in range(len(pts) - 1)) * 1.08) + 10

    g = []
    for k in range(4):
        y = gy0 + k * (gy1 - gy0) / 3
        g.append(f'<line x1="{gx0}" y1="{y:.1f}" x2="{gx1}" y2="{y:.1f}" stroke="{C["line"]}"/>')
        g.append(f'<text x="{gx0-10}" y="{y+4:.1f}" class="small" text-anchor="end">{round(top*(1-k/3))}</text>')
    seen = set()
    for i, (start, _) in enumerate(weeks):
        mid = start + dt.timedelta(days=3)
        if mid.day <= 7 and (mid.year, mid.month) not in seen:
            seen.add((mid.year, mid.month))
            g.append(f'<text x="{gx0 + i*step:.1f}" y="{gy1+22}" class="small" text-anchor="middle">{mid.strftime("%b")}</text>')
    if peak > 0:
        pi = max(range(len(weeks)), key=lambda i: weeks[i][1])
        px, py = pts[pi]
        anchor = "end" if px > gx1 - 170 else "start"
        dx = -12 if anchor == "end" else 12
        g.append(f'<line x1="{px:.1f}" y1="{py:.1f}" x2="{px:.1f}" y2="{gy1}" stroke="{NEON[2]}" stroke-opacity=".45" class="pop"/>')
        g.append(f'<circle cx="{px:.1f}" cy="{py:.1f}" r="9" fill="none" stroke="{NEON[2]}" stroke-opacity=".6" class="ping"/>')
        g.append(f'<circle cx="{px:.1f}" cy="{py:.1f}" r="4.5" fill="{C["bg0"]}" stroke="{C["text"]}" stroke-width="2.5" class="pop"/>')
        g.append(f'<text x="{px+dx:.1f}" y="{py-6:.1f}" class="note pop" text-anchor="{anchor}"><tspan class="strong">{peak}</tspan> in week of {weeks[pi][0]:%d %b}</text>')

    # ── weekday rhythm (right): one series, the busiest day highlighted ──
    by_wd = [0] * 7
    for d, n in days:
        by_wd[d.weekday()] += n
    names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    full = ["Mondays", "Tuesdays", "Wednesdays", "Thursdays", "Fridays", "Saturdays", "Sundays"]
    bx0, bw, gap, by1, bh = 634, 24, 12, 240, 140
    mx = max(by_wd) or 1
    bars = []
    for i, n in enumerate(by_wd):
        h = max(2, n / mx * bh)
        x = bx0 + i * (bw + gap)
        hot = n == mx and n
        bars.append(f'<rect x="{x}" y="{by1 - bh}" width="{bw}" height="{bh}" rx="4" fill="{C["panel"]}"/>')
        wd_col = [NEON[0], NEON[6], NEON[3], NEON[4], NEON[7], NEON[5], NEON[2]][i]
        bars.append(f'<rect x="{x}" y="{by1 - h:.1f}" width="{bw}" height="{h:.1f}" rx="4" fill="{wd_col}" fill-opacity="{1 if hot else .72}" class="grow"{GLOW if hot else ""}/>')
        if hot:
            bars.append(f'<text x="{x + bw/2}" y="{by1 - h - 8:.1f}" class="small" text-anchor="middle" style="fill:{C["text"]}">{fmt(n)}</text>')
        bars.append(f'<text x="{x + bw/2}" y="{by1+22}" class="small" text-anchor="middle">{names[i]}</text>')
    best = full[by_wd.index(max(by_wd))] if any(by_wd) else None
    total = sum(n for _, n in days)
    avg = total / max(1, len(days))

    notes = [f'<text x="{gx0}" y="{H-24}" class="note">{fmt(total)} contributions, {avg:.1f} per day on average</text>']
    if best:
        notes.append(f'<text x="{W-28}" y="{H-24}" class="note" text-anchor="end">Most active on <tspan class="strong">{best}</tspan></text>')
    css = (
        f".line {{ stroke-dasharray: {length}; animation: draw 2.4s cubic-bezier(.5,0,.2,1) .2s both; }}"
        f" @keyframes draw {{ from {{ stroke-dashoffset: {length}; }} }}"
        " .area { animation: fade 1.2s ease 1.4s both; } .pop { animation: fade .5s ease 2.4s both; }"
        " .ping { transform-box: fill-box; transform-origin: center; animation: ping 2.2s ease-out 2.6s infinite both; }"
        " @keyframes ping { from { transform: scale(.5); opacity: .9; } to { transform: scale(2.4); opacity: 0; } }"
        " .grow { transform-box: fill-box; transform-origin: bottom; animation: grow 1s cubic-bezier(.2,.7,.2,1) .6s both; }"
        " @keyframes grow { from { transform: scaleY(0); } }"
    )
    body = f"""<defs>
  <linearGradient id="stroke" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="{NEON[0]}"/><stop offset=".5" stop-color="{NEON[1]}"/><stop offset="1" stop-color="{NEON[2]}"/></linearGradient>
  <linearGradient id="fill" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="{NEON[1]}" stop-opacity=".35"/><stop offset="1" stop-color="{NEON[0]}" stop-opacity="0"/></linearGradient>
</defs>
<text x="{gx0}" y="72" class="lbl">WEEKLY CONTRIBUTIONS</text>
<text x="{bx0}" y="72" class="lbl">BY WEEKDAY</text>
{''.join(g)}
<path d="{area}" fill="url(#fill)" class="area"/>
<path d="{line}" fill="none" stroke="url(#stroke)" stroke-width="2" stroke-linecap="round" class="line" filter="url(#glow)"/>
<line x1="608" y1="64" x2="608" y2="{by1+26}" stroke="{C['line']}"/>
{''.join(bars)}
{''.join(notes)}"""
    return frame(W, H, "05", "Signal activity", body, css,
                 f"Weekly contributions over 12 months, peak {peak}; most active on {best or 'no day yet'}", NEON[1])


def render_clock(hours: list[int]) -> str:
    """24-hour polar histogram of commit times (IST) with a radar sweep, plus time-of-day split."""
    W, H = 900, 350
    cx, cy, r0, r1 = 200, 206, 40, 104
    total = sum(hours)
    mx = max(hours) or 1
    out = [f'<circle cx="{cx}" cy="{cy}" r="{r1 + 14}" fill="{C["panel"]}" stroke="{C["line"]}"/>']
    for rr in (r0, r0 + (r1 - r0) / 2, r1):
        out.append(f'<circle cx="{cx}" cy="{cy}" r="{rr:.1f}" fill="none" stroke="{C["line"]}"/>')
    out.append(tick_ring(cx, cy, r1 + 14, 96, 4, 3, "spin"))
    for h in range(0, 24, 6):
        x0, y0 = polar(cx, cy, r0, h * 15)
        x1, y1 = polar(cx, cy, r1, h * 15)
        out.append(f'<line x1="{x0:.1f}" y1="{y0:.1f}" x2="{x1:.1f}" y2="{y1:.1f}" stroke="{C["line"]}"/>')
        lx, ly = polar(cx, cy, r1 + 30, h * 15)
        out.append(f'<text x="{lx:.1f}" y="{ly + 4:.1f}" class="small" text-anchor="middle">{h:02d}h</text>')
    # radar sweep
    wedge = sector(cx, cy, 0, r1, 0, 40)
    out.append(f'<g class="radar"><path d="{wedge}" fill="url(#sweep)"/>'
               f'<line x1="{cx}" y1="{cy}" x2="{polar(cx, cy, r1, 40)[0]:.1f}" y2="{polar(cx, cy, r1, 40)[1]:.1f}" stroke="{C["cyan"]}" stroke-opacity=".7"/></g>')
    peak_h = hours.index(mx) if total else -1
    band_of = lambda h: BAND[("NIGHT", "MORNING", "AFTERNOON", "EVENING")[h // 6]]
    for h, n in enumerate(hours):
        if not n:
            continue
        rr = r0 + 4 + (r1 - r0 - 4) * math.sqrt(n / mx)
        hot = h == peak_h
        out.append(f'<path d="{sector(cx, cy, r0 + 2, rr, h * 15 + 1.6, h * 15 + 13.4)}" fill="{band_of(h)}" fill-opacity="{1 if hot else .8}"'
                   f' class="bloom" style="animation-delay:{0.2 + h * 0.04:.2f}s"{GLOW if hot else ""}><title>{h:02d}:00–{h:02d}:59 · {n} commits</title></path>')
    out.append(f'<circle cx="{cx}" cy="{cy}" r="{r0 - 4}" fill="{C["bg0"]}" stroke="{C["line"]}"/>')
    out.append(f'<text x="{cx}" y="{cy + 2}" class="val" text-anchor="middle" style="font-size:20px">{fmt(total)}</text>')
    out.append(f'<text x="{cx}" y="{cy + 18}" class="small" text-anchor="middle">commits</text>')

    # time-of-day split
    bands = [("NIGHT", "00–06", range(0, 6)), ("MORNING", "06–12", range(6, 12)),
             ("AFTERNOON", "12–18", range(12, 18)), ("EVENING", "18–24", range(18, 24))]
    counts = [sum(hours[h] for h in rng) for _, _, rng in bands]
    bmx = max(counts) or 1
    x0, x1 = 470, 790
    for i, ((name, span, _), n) in enumerate(zip(bands, counts)):
        y = 104 + i * 50
        pct = 100 * n / total if total else 0
        hot = n == bmx and n
        out.append(f'<rect x="{x0}" y="{y - 9}" width="9" height="9" rx="2" fill="{BAND[name]}"/>')
        out.append(f'<text x="{x0 + 16}" y="{y}" class="lbl" style="fill:{C["text"] if hot else C["muted"]}">{name} <tspan style="fill:{C["dim"]}">{span}</tspan></text>')
        out.append(f'<text x="{W - 40}" y="{y}" class="small" text-anchor="end" style="fill:{C["text"]}">{pct:.0f}%  <tspan style="fill:{C["muted"]}">{n}</tspan></text>')
        out.append(f'<rect x="{x0}" y="{y + 10}" width="{W - 40 - x0}" height="8" rx="4" fill="{C["panel"]}" stroke="{C["line"]}" stroke-width=".6"/>')
        out.append(f'<rect x="{x0}" y="{y + 10}" width="{max(4, (W - 40 - x0) * n / bmx):.1f}" height="8" rx="4" fill="{BAND[name]}" fill-opacity="{1 if hot else .8}" class="grow" style="animation-delay:{.5 + i * .12:.2f}s"{GLOW if hot else ""}/>')
    if total:
        kind = {"NIGHT": "a night owl", "MORNING": "an early bird", "AFTERNOON": "an afternoon builder", "EVENING": "an evening coder"}[bands[counts.index(bmx)][0]]
        msg = f'Busiest hour <tspan class="strong">{peak_h:02d}:00 IST</tspan> · verdict: <tspan class="strong">{kind}</tspan>'
    else:
        msg = "Commit times appear once public repositories have commits."
    out.append(f'<text x="{x0}" y="{H - 34}" class="note">{msg}</text>')
    out.append(f'<text x="{x0}" y="{H - 16}" class="small" style="fill:{C["dim"]}">last 100 commits per repository, default branches</text>')
    body = (f'<defs><linearGradient id="sweep" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="{C["cyan"]}" stop-opacity="0"/>'
            f'<stop offset="1" stop-color="{C["cyan"]}" stop-opacity=".22"/></linearGradient></defs>'
            f'<text x="470" y="74" class="lbl">TIME OF DAY</text>' + "\n".join(out))
    css = (f".radar {{ transform-origin: {cx}px {cy}px; animation: radar 5s linear infinite; }}"
           " @keyframes radar { to { transform: rotate(360deg); } }"
           f" .bloom {{ transform-origin: {cx}px {cy}px; animation: bloom .9s cubic-bezier(.2,.8,.2,1) both; }}"
           " @keyframes bloom { from { opacity: 0; transform: scale(.4); } }"
           " .grow { transform-box: fill-box; transform-origin: left; animation: growx 1.1s cubic-bezier(.2,.7,.2,1) both; }"
           " @keyframes growx { from { transform: scaleX(0); } }")
    return frame(W, H, "06", "Commit clock", body, css,
                 f"Commits by hour of day (IST): {total} commits, busiest hour {peak_h:02d}:00" if total else "Commit clock", NEON[3])


def render_languages(mix: list[tuple[str, str, float]]) -> str:
    W, H = 900, 320
    cx, cy, ro, ri = 196, 192, 94, 68
    out = [
        f'<circle cx="{cx}" cy="{cy}" r="{ro + 24}" fill="none" stroke="{NEON[5]}" stroke-opacity=".45" stroke-dasharray="1 6" class="spin"/>',
        tick_ring(cx, cy, ro + 8, 72, 6, 3, "spin-r"),
        f'<circle cx="{cx}" cy="{cy}" r="{(ro + ri) / 2}" fill="none" stroke="{C["panel"]}" stroke-width="{ro - ri}"/>',
        f'<g class="orbit"><circle cx="{cx}" cy="{cy - ro - 24}" r="3.5" fill="{NEON[5]}" filter="url(#glow)"/></g>',
    ]
    a = 0.0
    gap = 1.6  # degrees ≈ 2-3px surface gap between segments
    segs = []
    for name, col, pct in mix:
        sweep = 360 * pct / 100
        if sweep > gap + .4:
            segs.append(f'<path d="{sector(cx, cy, ri, ro, a + gap / 2, a + sweep - gap / 2)}" fill="{col}"><title>{esc(name)} {pct:.1f}%</title></path>')
        a += sweep
    out.append(f'<g mask="url(#reveal)">{"".join(segs)}</g>')
    if mix:
        name, _, pct = mix[0]
        out.append(f'<text x="{cx}" y="{cy - 4}" class="val" text-anchor="middle">{pct:.0f}%</text>')
        out.append(f'<text x="{cx}" y="{cy + 16}" class="small" text-anchor="middle">{esc(name.upper())}</text>')
    # ranked list: legend + direct values
    x0, x1 = 400, W - 40
    for i, (name, col, pct) in enumerate(mix):
        y = 106 + i * 34
        wbar = (x1 - x0) * pct / (mix[0][2] or 1)
        out.append(f'<rect x="{x0}" y="{y - 10}" width="10" height="10" rx="2.5" fill="{col}"/>')
        out.append(f'<text x="{x0 + 20}" y="{y}" class="note">{esc(name)}</text>')
        out.append(f'<text x="{x1}" y="{y}" class="small" text-anchor="end" style="fill:{C["text"]}">{pct:.1f}%</text>')
        out.append(f'<rect x="{x0 + 20}" y="{y + 8}" width="{x1 - x0 - 20}" height="3" rx="1.5" fill="{C["panel"]}"/>')
        out.append(f'<rect x="{x0 + 20}" y="{y + 8}" width="{max(2, wbar - 20):.1f}" height="3" rx="1.5" fill="{col}" class="grow" style="animation-delay:{.4 + i * .1:.2f}s"/>')
    if not mix:
        out.append(f'<text x="{x0}" y="130" class="note">Language data appears after your first repository with code.</text>')
    circ = 2 * math.pi * (ro + ri) / 2
    body = (f'<defs><mask id="reveal"><circle cx="{cx}" cy="{cy}" r="{(ro + ri) / 2}" fill="none" stroke="#fff" stroke-width="{ro - ri + 6}"'
            f' stroke-dasharray="{circ:.1f}" transform="rotate(-90 {cx} {cy})" class="wipe"/></mask></defs>'
            f'<text x="{x0}" y="72" class="lbl">SHARE OF CODE · BYTES × REPOS</text>' + "\n".join(out))
    css = (f".wipe {{ animation: wipe 1.6s cubic-bezier(.5,0,.2,1) .2s both; }} @keyframes wipe {{ from {{ stroke-dashoffset: {circ:.1f}; }} }}"
           f" .orbit {{ transform-origin: {cx}px {cy}px; animation: spin 14s linear infinite; }}"
           " .grow { transform-box: fill-box; transform-origin: left; animation: growx 1.1s cubic-bezier(.2,.7,.2,1) both; }"
           " @keyframes growx { from { transform: scaleX(0); } }")
    return frame(W, H, "07", "Language matrix", body, css,
                 "Languages: " + ", ".join(f"{n} {p:.1f}%" for n, _, p in mix), NEON[5])


def render_timeline(repos: list[dict], colors: dict[str, str]) -> str:
    """Mission log: every public repo placed on a time axis by creation date, labels stacked in lanes."""
    W, H = 900, 360
    x0, x1, ay = 44, W - 70, 184
    items = sorted((r for r in repos if r.get("createdAt")), key=lambda r: r["createdAt"])
    if not items:
        return frame(W, H, "08", "Mission log", '<text x="44" y="120" class="note">Repositories appear here once public.</text>')
    ts = [dt.datetime.fromisoformat(r["createdAt"].replace("Z", "+00:00")) for r in items]
    t0 = min(ts) - dt.timedelta(days=12)
    t1 = NOW
    span = (t1 - t0).total_seconds() or 1

    def X(t: dt.datetime) -> float:
        return x0 + (x1 - x0) * (t - t0).total_seconds() / span

    out = [f'<line x1="{x0}" y1="{ay}" x2="{x1}" y2="{ay}" stroke="url(#axis)" stroke-width="2"/>',
           f'<circle cx="{x1}" cy="{ay}" r="5" fill="{C["text"]}"/>',
           f'<circle cx="{x1}" cy="{ay}" r="10" fill="none" stroke="{C["text"]}" class="ping"/>',
           f'<text x="{x1 + 16}" y="{ay + 4}" class="small" style="fill:{C["text"]}">NOW</text>']
    # month ticks
    m = dt.datetime(t0.year, t0.month, 1, tzinfo=dt.timezone.utc)
    months = []
    while m <= t1:
        months.append(m)
        m = dt.datetime(m.year + (m.month == 12), m.month % 12 + 1, 1, tzinfo=dt.timezone.utc)
    every = max(1, math.ceil(len(months) / 12))
    for i, m in enumerate(months):
        if m < t0:
            continue
        x = X(m)
        out.append(f'<line x1="{x:.1f}" y1="{ay - 4}" x2="{x:.1f}" y2="{ay + 4}" stroke="{C["dim"]}"/>')
        if i % every == 0:
            lab = m.strftime("%b %y") if m.month == 1 or i == 0 else m.strftime("%b")
            out.append(f'<text x="{x:.1f}" y="{ay + 20}" class="small" text-anchor="middle" style="fill:{C["dim"]}">{lab}</text>')
    # lanes: above 1..4, below 1..4 (below starts past the month labels)
    lanes = [ay - 30 - 28 * k for k in range(4)] + [ay + 48 + 28 * k for k in range(4)]
    order = [0, 4, 1, 5, 2, 6, 3, 7]
    taken: dict[int, list[tuple[float, float]]] = {k: [] for k in range(8)}
    for i, (r, t) in enumerate(zip(items, ts)):
        x = X(t)
        wlab = 7.0 * len(r["name"]) + 18
        lx0 = min(max(x - 10, 16), W - 16 - wlab)
        best, best_cost = order[0], 1e9
        for k in order:
            cost = sum(max(0, min(lx0 + wlab, b) - max(lx0, a)) for a, b in taken[k])
            if cost == 0:
                best = k
                break
            if cost < best_cost:
                best, best_cost = k, cost
        taken[best].append((lx0 - 10, lx0 + wlab + 10))
        ly = lanes[best]
        lang = (r.get("primaryLanguage") or {}).get("name")
        col = colors.get(lang or "", OTHER)
        d = 0.3 + i * 0.12
        up = ly < ay
        out.append(f"""<g class="pop" style="animation-delay:{d:.2f}s"><title>{esc(r['name'])} · created {t:%d %b %Y} · {esc(lang or 'no language')}</title>
  <line x1="{x:.1f}" y1="{ay + (-7 if up else 7)}" x2="{x:.1f}" y2="{ly + (6 if up else -14)}" stroke="{col}" stroke-opacity=".55"/>
  <circle cx="{x:.1f}" cy="{ay}" r="5.5" fill="{C['bg0']}" stroke="{col}" stroke-width="2.4"/>
  <rect x="{lx0:.1f}" y="{ly - 15}" width="{wlab:.1f}" height="21" rx="4" fill="{C['panel']}" stroke="{col}" stroke-opacity=".6"/>
  <circle cx="{lx0 + 9:.1f}" cy="{ly - 4.5}" r="3" fill="{col}"/>
  <text x="{lx0 + 17:.1f}" y="{ly}" class="repo">{esc(r['name'])}</text>
</g>""")
    # legend (same language colours as the language matrix)
    lx = 44
    leg = []
    for name, col in list(colors.items()) + [("Other / none", OTHER)]:
        leg.append(f'<rect x="{lx}" y="{H - 34}" width="10" height="10" rx="2.5" fill="{col}"/><text x="{lx + 16}" y="{H - 25}" class="small">{esc(name)}</text>')
        lx += 16 + 7 * len(name) + 22
    body = (f'<defs><linearGradient id="axis" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="{NEON[6]}" stop-opacity=".15"/>'
            f'<stop offset=".5" stop-color="{NEON[1]}"/><stop offset="1" stop-color="{NEON[2]}"/></linearGradient></defs>' + "\n".join(out) + "".join(leg))
    css = (f".repo {{ font: 12px {MONO}; fill: {C['text']}; }} .pop {{ animation: fade .6s ease both; }}"
           " .ping { transform-box: fill-box; transform-origin: center; animation: ping 2.2s ease-out infinite; }"
           " @keyframes ping { from { transform: scale(.5); opacity: .9; } to { transform: scale(2.2); opacity: 0; } }")
    return frame(W, H, "08", "Mission log · repository launches", body, css,
                 "Timeline of repository creation: " + ", ".join(f"{r['name']} {t:%b %Y}" for r, t in zip(items, ts)), NEON[6])


# ═══════════════════════════════ README blocks ═══════════════════════════════
def ago(ts: str) -> str:
    t = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    s = (NOW - t).total_seconds()
    for unit, size in (("year", 31536000), ("month", 2592000), ("week", 604800), ("day", 86400), ("hour", 3600), ("minute", 60)):
        if s >= size:
            n = int(s // size)
            return f"{n} {unit}{'s' if n > 1 else ''} ago"
    return "just now"


def pick_projects(repos: list[dict]) -> list[dict]:
    pool = sorted((r for r in repos if not r["isArchived"]), key=lambda r: r["pushedAt"], reverse=True)
    feat = [r for name in FEATURED for r in pool if r["name"] == name]
    rest = [r for r in pool if r not in feat]
    return (feat + rest)[:PROJECT_COUNT]


def _wrap(text: str, width: int, lines: int) -> list[str]:
    words, out, cur = text.split(), [], ""
    for w in words:
        if len(cur) + len(w) + (1 if cur else 0) <= width:
            cur = f"{cur} {w}" if cur else w
        else:
            out.append(cur)
            cur = w
            if len(out) == lines:
                break
    if len(out) < lines and cur:
        out.append(cur)
    if len(out) == lines and " ".join(out) != " ".join(words):
        out[-1] = out[-1][:width - 1].rstrip() + "…"
    return out


def render_project_card(r: dict, i: int, colors: dict[str, str]) -> str:
    """Compact animated HUD card for one repository: status LED, brief, language, and a
    16-week commit sparkline that draws itself."""
    W, H = 440, 176
    ac = [NEON[0], NEON[2], NEON[3], NEON[4], NEON[1], NEON[5]][i % 6]
    featured = r["name"] in FEATURED
    pushed = dt.datetime.fromisoformat(r["pushedAt"].replace("Z", "+00:00"))
    active = (NOW - pushed).days <= 14
    led = NEON[3] if active else C["amber"]
    # weekly commits, last 16 weeks
    nodes = (((r.get("defaultBranchRef") or {}).get("target") or {}).get("history") or {}).get("nodes", [])
    weeks = [0] * 16
    for c in nodes:
        t = dt.datetime.fromisoformat(c["authoredDate"].replace("Z", "+00:00"))
        k = (NOW - t).days // 7
        if 0 <= k < 16:
            weeks[15 - k] += 1
    sx0, sx1, sy0, sy1 = 262, W - 56, 130, 154
    status = f'{"ACTIVE" if active else "IDLE"} · {ago(r["pushedAt"]).replace(" ago", "")}'
    led_x = W - 22 - 7.6 * len(status) - 10
    mx = max(weeks) or 1
    pts = [(sx0 + (sx1 - sx0) * k / 15, sy1 - (sy1 - sy0) * n / mx) for k, n in enumerate(weeks)]
    line = _smooth(pts, floor=sy1)
    length = int(sum(math.dist(pts[k], pts[k + 1]) for k in range(15)) * 1.1) + 10
    lang = (r.get("primaryLanguage") or {}).get("name")
    lcol = colors.get(lang or "", OTHER)
    name = r["name"] if len(r["name"]) <= 30 else r["name"][:29] + "…"
    desc = _wrap(r["description"] or "Mission brief pending: description coming soon.", 58, 2)
    b = 12
    corners = "".join(f'<path d="M{x},{y + sy * b} L{x},{y} L{x + sx * b},{y}"/>'
                      for x, y, sx, sy in ((8, 8, 1, 1), (W - 8, 8, -1, 1), (8, H - 8, 1, -1), (W - 8, H - 8, -1, -1)))
    meta = []
    mx0 = 22
    meta.append(f'<circle cx="{mx0 + 5}" cy="{H - 26}" r="5" fill="{lcol}"/><text x="{mx0 + 15}" y="{H - 22}" class="m">{esc(lang or "—")}</text>')
    mx0 += 24 + 7.2 * len(lang or "—")
    for sym, val in (("★", r["stargazerCount"]), ("⑂", r["forkCount"]), ("◆", f"{len(nodes)} commit{'' if len(nodes) == 1 else 's'}")):
        meta.append(f'<text x="{mx0}" y="{H - 22}" class="m"><tspan style="fill:{ac}">{sym}</tspan> {val}</text>')
        mx0 += 22 + 7.2 * len(str(val))
    tag = "★ FEATURED" if featured else "◉ LATEST"
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" role="img" aria-label="{esc(r['name'])}: {esc(r['description'] or '')}">
<defs>
  <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="{C['bg0']}"/><stop offset="1" stop-color="{mix_hex(C['bg1'], ac, .08)}"/></linearGradient>
  <pattern id="grid" width="20" height="20" patternUnits="userSpaceOnUse"><path d="M20 0H0V20" fill="none" stroke="#FFFFFF" stroke-opacity=".03"/></pattern>
  <linearGradient id="spark" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="{ac}" stop-opacity=".3"/><stop offset="1" stop-color="{ac}"/></linearGradient>
  <linearGradient id="sfill" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="{ac}" stop-opacity=".3"/><stop offset="1" stop-color="{ac}" stop-opacity="0"/></linearGradient>
  <linearGradient id="scan" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="{ac}" stop-opacity="0"/><stop offset=".5" stop-color="{ac}" stop-opacity=".16"/><stop offset="1" stop-color="{ac}" stop-opacity="0"/></linearGradient>
  <filter id="glow" x="-50%" y="-50%" width="200%" height="200%"><feGaussianBlur stdDeviation="2.5" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter>
  <clipPath id="card"><rect width="{W}" height="{H}" rx="14"/></clipPath>
</defs>
<style>
  text {{ font-family: {FONT}; }}
  .tag {{ font: 600 10.5px {MONO}; letter-spacing: 1.6px; fill: {ac}; }}
  .st {{ font: 10.5px {MONO}; letter-spacing: 1.2px; fill: {C['muted']}; }}
  .nm {{ font-size: 19px; font-weight: 700; fill: {C['text']}; letter-spacing: -.2px; }}
  .ds {{ font-size: 12.5px; fill: {C['soft']}; }}
  .m {{ font: 11.5px {MONO}; fill: {C['soft']}; }}
  .sweep {{ animation: sweep 5s cubic-bezier(.5,0,.5,1) {i * .6:.1f}s infinite; }}
  @keyframes sweep {{ from {{ transform: translateX(-160px); }} to {{ transform: translateX({W + 40}px); }} }}
  .led {{ animation: led 1.8s ease-in-out infinite; }} @keyframes led {{ 50% {{ opacity: .25; }} }}
  .ring {{ transform-box: fill-box; transform-origin: center; animation: ring 2.4s ease-out infinite; }}
  @keyframes ring {{ from {{ transform: scale(.6); opacity: .9; }} to {{ transform: scale(2.6); opacity: 0; }} }}
  .draw {{ stroke-dasharray: {length}; animation: draw 2.2s cubic-bezier(.5,0,.2,1) .3s both; }}
  @keyframes draw {{ from {{ stroke-dashoffset: {length}; }} }}
  .in {{ animation: in .8s cubic-bezier(.2,.8,.2,1) both; }} @keyframes in {{ from {{ opacity: 0; transform: translateY(6px); }} }}
  .d1 {{ animation-delay: .15s; }} .d2 {{ animation-delay: .3s; }} .d3 {{ animation-delay: .45s; }}
  .fade {{ animation: fade 1s ease 1.8s both; }} @keyframes fade {{ from {{ opacity: 0; }} }}
  .spin {{ transform-box: fill-box; transform-origin: center; animation: spin 14s linear infinite; }} @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
  @media (prefers-reduced-motion: reduce) {{ * {{ animation: none !important; }} .sweep {{ display: none; }} }}
</style>
<g clip-path="url(#card)">
  <rect width="{W}" height="{H}" fill="url(#bg)"/>
  <rect width="{W}" height="{H}" fill="url(#grid)"/>
  <rect x="0" y="0" width="140" height="{H}" fill="url(#scan)" class="sweep"/>
  <rect x="0" y="0" width="{W}" height="3" fill="{ac}" opacity=".85"/>
</g>
<rect x=".5" y=".5" width="{W - 1}" height="{H - 1}" rx="13.5" fill="none" stroke="{ac}" stroke-opacity=".35"/>
<g fill="none" stroke="{ac}" stroke-width="1.5" stroke-opacity=".85">{corners}</g>
<g class="in">
  <text x="22" y="34" class="tag">P-{i + 1:02d} · {tag}</text>
  <circle cx="{led_x:.1f}" cy="30" r="4" fill="{led}" class="led"/>
  <circle cx="{led_x:.1f}" cy="30" r="4" fill="none" stroke="{led}" class="ring"/>
  <text x="{W - 22}" y="34" class="st" text-anchor="end">{esc(status)}</text>
</g>
<text x="22" y="66" class="nm in d1">{esc(name)}</text>
<g class="in d2">{''.join(f'<text x="22" y="{88 + k * 17}" class="ds">{esc(t)}</text>' for k, t in enumerate(desc))}</g>
<g class="in d3">{''.join(meta)}</g>
<text x="{sx1}" y="{sy0 - 6}" class="st" text-anchor="end" style="font-size:9.5px">COMMITS · 16 WK</text>
<line x1="{sx0}" y1="{sy1}" x2="{sx1}" y2="{sy1}" stroke="{C['line']}"/>
<path d="{line} L{sx1},{sy1} L{sx0},{sy1} Z" fill="url(#sfill)" class="fade"/>
<path d="{line}" fill="none" stroke="url(#spark)" stroke-width="2" stroke-linecap="round" class="draw" filter="url(#glow)"/>
<circle cx="{pts[-1][0]:.1f}" cy="{pts[-1][1]:.1f}" r="3" fill="#FFFFFF" class="fade"/>
<circle cx="{W - 30}" cy="{H - 26}" r="9" fill="none" stroke="{ac}" stroke-opacity=".6" stroke-dasharray="2 3" class="spin"/>
<path d="M{W - 33},{H - 30} L{W - 27},{H - 26} L{W - 33},{H - 22}" fill="none" stroke="{ac}" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>
</svg>
"""


def projects_block(pick: list[dict]) -> str:
    """Two animated cards per row, each wrapped in a link to its repository."""
    if not pick:
        return "<p><i>Projects appear here automatically once repositories are public.</i></p>"
    rows = []
    for k in range(0, len(pick), 2):
        cells = []
        for j, r in enumerate(pick[k:k + 2], start=k):
            alt = esc(f"{r['name']}: {r['description'] or 'no description yet'}")
            cells.append(f'<a href="{r["url"]}"><img src="./profile/projects/{j + 1:02d}.svg" width="49%" alt="{alt}"/></a>')
        rows.append("<p>\n" + "\n".join(cells) + "\n</p>")
    return "\n".join(rows)


FEED_STYLE = {  # kind: (tag, glyph, colour)
    "commit": ("COMMIT", "↑", NEON[0]), "create": ("CREATE", "+", NEON[3]), "branch": ("BRANCH", "⑂", NEON[6]),
    "public": ("LAUNCH", "◉", NEON[3]), "pr": ("PULL REQ", "⇄", NEON[1]), "issue": ("ISSUE", "!", NEON[5]),
    "release": ("RELEASE", "▲", NEON[4]), "star": ("STAR", "★", NEON[4]), "fork": ("FORK", "⑂", NEON[6]),
    "comment": ("COMMENT", "✎", NEON[2]), "review": ("REVIEW", "◎", NEON[1]),
}


def activity_items(events: list[dict], user: dict) -> list[dict]:
    """Recent activity: the user's own commits (grouped per repo and day, from GraphQL history)
    plus public non-push events (repos, branches, PRs, issues, releases, stars...)."""
    items: list[dict] = []
    groups: dict[tuple[str, str], dict] = {}
    for r in user["repositories"]["nodes"]:
        if r["isFork"] or r["name"] in EXCLUDE_REPOS:
            continue
        target = ((r.get("defaultBranchRef") or {}).get("target") or {})
        for c in (target.get("history") or {}).get("nodes", []):
            login = ((c.get("author") or {}).get("user") or {}).get("login") or ""
            if login.lower() != USER.lower() or not c.get("authoredDate"):
                continue
            ts = c["authoredDate"]
            day = dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(TIMEZONE).date().isoformat()
            g = groups.setdefault((r["name"], day), {"kind": "commit", "repo": f"{USER}/{r['name']}", "ts": ts, "n": 0,
                                                     "title": c.get("messageHeadline") or "", "oid": c.get("oid") or ""})
            g["n"] += 1
            if ts > g["ts"]:
                g.update(ts=ts, title=c.get("messageHeadline") or "", oid=c.get("oid") or "")
    items.extend(groups.values())
    for e in events:
        t, p, repo, ts = e.get("type"), e.get("payload", {}) or {}, e.get("repo", {}).get("name", ""), e.get("created_at", "")
        if not ts or repo.split("/")[-1] in EXCLUDE_REPOS:
            continue
        it = None
        if t == "CreateEvent" and p.get("ref_type") == "repository":
            it = {"kind": "create", "title": "New repository"}
        elif t == "CreateEvent" and p.get("ref_type") in ("branch", "tag"):
            it = {"kind": "branch", "title": f"{p['ref_type'].capitalize()} {p.get('ref') or ''}"}
        elif t == "PublicEvent":
            it = {"kind": "public", "title": "Open-sourced"}
        elif t == "PullRequestEvent":
            pr = p.get("pull_request", {}) or {}
            verb = "Merged" if p.get("action") == "closed" and pr.get("merged") else (p.get("action") or "Updated").capitalize()
            it = {"kind": "pr", "title": f"{verb} #{pr.get('number', p.get('number', ''))} {pr.get('title') or ''}".strip()}
        elif t == "IssuesEvent":
            iss = p.get("issue", {}) or {}
            it = {"kind": "issue", "title": f"{(p.get('action') or 'Updated').capitalize()} #{iss.get('number', '')} {iss.get('title') or ''}".strip()}
        elif t == "ReleaseEvent":
            it = {"kind": "release", "title": f"Released {(p.get('release') or {}).get('tag_name') or ''}"}
        elif t == "WatchEvent":
            it = {"kind": "star", "title": "Starred"}
        elif t == "ForkEvent":
            it = {"kind": "fork", "title": "Forked"}
        elif t == "PullRequestReviewEvent":
            it = {"kind": "review", "title": "Reviewed a pull request"}
        elif t == "IssueCommentEvent":
            it = {"kind": "comment", "title": "Commented"}
        if it:
            it.update(repo=repo, ts=ts, n=1)
            items.append(it)
    items.sort(key=lambda it: it["ts"], reverse=True)
    return items[:ACTIVITY_COUNT]


def activity_block(items: list[dict]) -> str:
    """Animated feed card plus the same events as a linked list, folded under it."""
    if not items:
        return '<img src="./profile/feed.svg" width="100%" alt="No public activity yet"/>'
    lines = []
    for it in items:
        owner, _, name = it["repo"].partition("/")
        label = name if owner.lower() == USER.lower() else it["repo"]
        link = f"[{label}](https://github.com/{it['repo']})"
        tag = FEED_STYLE[it["kind"]][0].capitalize()
        if it["kind"] == "commit":
            n = it["n"]
            sha = f" [`{it['oid'][:7]}`](https://github.com/{it['repo']}/commit/{it['oid']})" if it.get("oid") else ""
            text = f"**{n} commit{'s' if n > 1 else ''}** to {link}: {esc(it['title'])}{sha}"
        else:
            text = f"**{tag}** · {link} · {esc(it['title'])}"
        lines.append(f"- {text} <sub>{ago(it['ts'])}</sub>")
    alt = "Recent activity: " + "; ".join(f"{FEED_STYLE[it['kind']][0].lower()} {it['repo'].split('/')[-1]} {ago(it['ts'])}" for it in items)
    return (f'<img src="./profile/feed.svg" width="100%" alt="{esc(alt)}"/>\n\n'
            "<details>\n<summary><b>Event log with links</b></summary>\n\n" + "\n".join(lines) + "\n\n</details>")


def render_feed(items: list[dict]) -> str:
    """Event log: glowing spine with a travelling pulse, colour-coded nodes, rows that type
    themselves in one after another, a live ping on the newest event and a terminal cursor."""
    W = 900
    row, top = 50, 92
    n = len(items)
    H = top + max(1, n) * row + 44
    sx = 150
    tx = 178
    out, defs = [], []
    if n:
        y_end = top + (n - 1) * row + 26
        out.append(f'<line x1="{sx}" y1="{top - 18}" x2="{sx}" y2="{y_end}" stroke="{C["line"]}" stroke-width="2"/>')
        out.append(f'<line x1="{sx}" y1="{top - 18}" x2="{sx}" y2="{y_end}" stroke="url(#spine)" stroke-width="2.4" class="flow"{GLOW}/>')
    for i, it in enumerate(items):
        tag, glyph, col = FEED_STYLE[it["kind"]]
        if it["kind"] == "commit":  # commits take a stable per-repository colour
            col = NEON[sum(map(ord, it["repo"])) % len(NEON)]
        y = top + i * row
        d = .35 + i * .32
        name = it["repo"].split("/")[-1] if it["repo"].split("/")[0].lower() == USER.lower() else it["repo"]
        if it["kind"] == "commit":
            head = f'{it["n"]} commit{"s" if it["n"] > 1 else ""}'
            detail = it["title"] + (f"  ·  {it['oid'][:7]}" if it.get("oid") else "")
        else:
            head, detail = "", it["title"]
        detail = detail if len(detail) <= 84 else detail[:83] + "…"
        when = ago(it["ts"]).replace(" ago", "")
        out.append(f"""<g class="pop" style="animation-delay:{d:.2f}s">
  <text x="{sx - 26}" y="{y + 4}" class="small" text-anchor="end">{esc(when)}</text>
  <circle cx="{sx}" cy="{y}" r="12" fill="{C['bg0']}" stroke="{col}" stroke-width="1.8"{GLOW}/>
  <text x="{sx}" y="{y + 4.5}" text-anchor="middle" class="gl" style="fill:{col}">{glyph}</text>
</g>""")
        if i == 0:
            out.append(f'<circle cx="{sx}" cy="{y}" r="12" fill="none" stroke="{col}" class="ping"/>')
        out.append(f"""<g class="type" style="animation-delay:{d:.2f}s">
  <text x="{tx}" y="{y - 1}"><tspan class="tg" style="fill:{col}">{tag}</tspan><tspan dx="10" class="rp">{esc(name)}</tspan>{f'<tspan dx="10" class="small" style="fill:{C["soft"]}">{esc(head)}</tspan>' if head else ''}{'<tspan dx="12" class="live" style="fill:' + col + '">● LATEST</tspan>' if i == 0 else ''}</text>
  <text x="{tx}" y="{y + 17}" class="dt">{esc(detail)}</text>
</g>""")
    if not n:
        out.append(f'<text x="{tx}" y="{top + 4}" class="note">No public activity in the last 90 days.</text>')
    ty = H - 30
    out.append(f'<text x="{tx - 32}" y="{ty}" class="term"><tspan style="fill:{NEON[3]}">&gt;</tspan> listening for new signals</text>')
    out.append(f'<rect x="{tx - 32 + 7.9 * 27 + 4}" y="{ty - 12}" width="8" height="15" fill="{NEON[3]}" class="cur"/>')
    defs.append(f'<linearGradient id="spine" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="{NEON[0]}"/>'
                f'<stop offset=".5" stop-color="{NEON[1]}"/><stop offset="1" stop-color="{NEON[2]}"/></linearGradient>')
    css = (f".gl {{ font: 700 12px {MONO}; }} .tg {{ font: 700 10.5px {MONO}; letter-spacing: 1.6px; }}"
           f" .rp {{ font-size: 14.5px; font-weight: 650; fill: {C['text']}; }} .dt {{ font-size: 12.5px; fill: {C['soft']}; }}"
           f" .live {{ font: 700 9.5px {MONO}; letter-spacing: 1.4px; }} .term {{ font: 13px {MONO}; fill: {C['soft']}; }}"
           " .pop { transform-box: fill-box; transform-origin: center; animation: pop .6s cubic-bezier(.2,.9,.3,1.3) both; }"
           " @keyframes pop { from { opacity: 0; transform: scale(.5); } }"
           " .ping { transform-box: fill-box; transform-origin: center; animation: ping 2s ease-out 1s infinite both; }"
           " @keyframes ping { from { transform: scale(1); opacity: .9; } to { transform: scale(2.4); opacity: 0; } }"
           " .flow { stroke-dasharray: 40 400; animation: flow 3.2s linear infinite; }"
           " @keyframes flow { from { stroke-dashoffset: 40; } to { stroke-dashoffset: -400; } }"
           " .cur { animation: blink 1.1s steps(2, start) infinite; }"
           " .type { animation: type 1s cubic-bezier(.3,0,.2,1) both; }"
           " @keyframes type { from { clip-path: inset(0 100% 0 0); } to { clip-path: inset(0 0 0 0); } }")
    body = "<defs>" + "".join(defs) + "</defs>\n" + "\n".join(out)
    return frame(W, H, "09", "Signal log · recent activity", body, css,
                 "Recent activity: " + "; ".join(f"{it['kind']} {it['repo']}" for it in items), NEON[2])


def replace_block(doc: str, name: str, content: str) -> str:
    pat = re.compile(rf"(<!-- AUTO:{name}:START -->)(.*?)(<!-- AUTO:{name}:END -->)", re.S)
    if not pat.search(doc):
        print(f"warning: marker AUTO:{name} not found in README.md, skipping", file=sys.stderr)
        return doc
    return pat.sub(lambda m: f"{m.group(1)}\n{content}\n{m.group(3)}", doc)


# ═════════════════════════════════════ main ═════════════════════════════════════
def main() -> None:
    user = fetch_user()
    repos = [r for r in user["repositories"]["nodes"] if not r["isFork"] and r["name"] not in EXCLUDE_REPOS]
    days = contribution_days(user)
    cur, longest = streaks(days)
    mix = language_mix(repos)
    colors = {name: col for name, col, _ in mix if name != "Other"}

    OUT.mkdir(exist_ok=True)
    cards = {
        "hud.svg": render_hud(user, repos, days, cur, longest),
        "achievements.svg": render_achievements(),
        "toolkit.svg": render_toolkit(),
        "skyline.svg": render_skyline(days),
        "activity.svg": render_activity(days),
        "clock.svg": render_clock(commit_hours(user)),
        "languages.svg": render_languages(mix),
        "timeline.svg": render_timeline(repos, colors),
    }
    for name, svg in cards.items():
        (OUT / name).write_text(svg, encoding="utf-8")
    items = activity_items(fetch_events(), user)
    (OUT / "feed.svg").write_text(render_feed(items), encoding="utf-8")
    pick = pick_projects(repos)
    pdir = OUT / "projects"
    pdir.mkdir(exist_ok=True)
    for old in pdir.glob("*.svg"):
        old.unlink()
    for j, r in enumerate(pick):
        (pdir / f"{j + 1:02d}.svg").write_text(render_project_card(r, j, colors), encoding="utf-8")
    for stale in ("overview.svg",):  # cards from the previous layout
        (OUT / stale).unlink(missing_ok=True)

    if README.exists():
        doc = README.read_text(encoding="utf-8")
        doc = replace_block(doc, "PROJECTS", projects_block(pick))
        doc = replace_block(doc, "ACTIVITY", activity_block(items))
        stamp = NOW.astimezone(TIMEZONE).strftime("%d %b %Y, %H:%M %Z")
        doc = replace_block(doc, "UPDATED", f"<sub>Auto-refreshed by GitHub Actions on {stamp}</sub>")
        README.write_text(doc, encoding="utf-8")
    print(f"ok: {len(repos)} repos, {len(days)} days, streak {cur}/{longest}")


if __name__ == "__main__":
    main()
