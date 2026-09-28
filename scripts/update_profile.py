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


def count_up(uid: str, x: float, y: float, value: int, cls: str = "val", anchor: str = "start", delay: float = .4,
             size: float = 24, width: float = 110, suffix: str = "", style: str = "") -> tuple[str, str]:
    """Number that rolls up from 0 to `value` (the real value is the resting frame, so it shows
    even with motion disabled). Returns (clipPath def, markup)."""
    steps, gap = 7, size * 1.7
    texts = "".join(f'<text x="{x:.1f}" y="{y - gap * k:.1f}" class="{cls}" text-anchor="{anchor}" style="{style}">'
                    f'{fmt(round(value * (1 - k / (steps - 1))))}{suffix}</text>' for k in range(steps))
    x0 = x if anchor == "start" else x - width / 2 if anchor == "middle" else x - width
    clip = f'<clipPath id="{uid}"><rect x="{x0:.1f}" y="{y - size * .95:.1f}" width="{width}" height="{size * 1.25:.1f}"/></clipPath>'
    return clip, (f'<g clip-path="url(#{uid})"><g class="cup" style="--h:{gap * (steps - 1):.0f}px;animation-delay:{delay:.2f}s">'
                  f'{texts}</g></g>')


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
  .cup {{ animation: cup 1.8s cubic-bezier(.3,.7,.2,1) both; }}
  @keyframes cup {{ from {{ transform: translateY(var(--h)); }} }}
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
    """Six milestone gauges (arc fills, numbers count up from zero, radar sweeps, sonar pulses,
    tip pings), a heartbeat vitals trace, and a stats strip that boots up under a scan beam."""
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
    W, H = 900, 378
    R = 44
    circ = 2 * math.pi * R
    out, defs = ["<g>" + "".join(
        f'<circle cx="{(k * 109.7) % (W - 40) + 20:.1f}" cy="{58 + (k * 47.9) % (H - 76):.1f}" r="{.6 + (k % 3) * .35:.2f}" fill="#FFFFFF" class="star"'
        f' style="animation-delay:{(k * .59) % 5:.2f}s;animation-duration:{3 + (k % 4)}s"/>' for k in range(44)) + "</g>"], []
    steps = 7
    for i, (label, sub, value) in enumerate(gauges):
        cx, cy = 83 + i * 146.8, 148
        col = NEON[i]
        goal = next_milestone(value)
        frac = min(1.0, value / goal) if goal else 0
        ex, ey = polar(cx, cy, R, 360 * frac)
        delay = 0.25 + i * 0.12
        # count-up reel: the real value rests in place, smaller values stacked above roll past it
        reel = "".join(f'<text x="{cx:.1f}" y="{cy + 8 - 30 * k}" class="val" text-anchor="middle">{fmt(round(value * (1 - k / (steps - 1))))}</text>'
                       for k in range(steps))
        defs.append(f'<clipPath id="cnt{i}"><rect x="{cx - 44:.1f}" y="{cy - 16}" width="88" height="32"/></clipPath>')
        defs.append(f'<linearGradient id="rad{i}" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="{col}" stop-opacity="0"/>'
                    f'<stop offset="1" stop-color="{col}" stop-opacity=".28"/></linearGradient>')
        lx, ly = polar(cx, cy, R - 7, 60)
        # charge streak along the filled arc, orbiting marker, target-lock brackets
        end = min(359.5, 360 * frac)
        (ax0, ay0), (ax1, ay1) = polar(cx, cy, R, 0), polar(cx, cy, R, end)
        arc_d = f"M{ax0:.1f},{ay0:.1f} A{R},{R} 0 {1 if end > 180 else 0} 1 {ax1:.1f},{ay1:.1f}"
        charge = (f'<path d="{arc_d}" fill="none" stroke="#FFFFFF" stroke-width="3" stroke-linecap="round" pathLength="100"'
                  f' class="charge" style="animation-delay:{delay + 2 + i * .35:.2f}s"{GLOW}/>') if end > 8 else ""
        b, g_ = R + 20, 9
        lock = "".join(f'<path d="M{cx + sx * b:.1f},{cy + sy * (b - g_):.1f} L{cx + sx * b:.1f},{cy + sy * b:.1f} L{cx + sx * (b - g_):.1f},{cy + sy * b:.1f}"/>'
                       for sx, sy in ((-1, -1), (1, -1), (-1, 1), (1, 1)))
        out.append(f"""<g>
  {tick_ring(cx, cy, R + 12, 60, 5, 3.5, "spin" if i % 2 == 0 else "spin-r", col)}
  <circle cx="{cx:.1f}" cy="{cy}" r="{R + 8}" fill="none" stroke="{C['line']}" stroke-dasharray="2 5"/>
  <g class="orbd" style="transform-origin:{cx:.1f}px {cy}px;animation-duration:{6 + i * .8:.1f}s;animation-direction:{'normal' if i % 2 == 0 else 'reverse'}">
    <circle cx="{cx:.1f}" cy="{cy - R - 8}" r="2.6" fill="{col}"{GLOW}/></g>
  <g fill="none" stroke="{col}" stroke-width="1.6" class="lock" style="animation-delay:{2.4 + i * .7:.2f}s">{lock}</g>
  <circle cx="{cx:.1f}" cy="{cy}" r="{R}" fill="none" stroke="{C['line']}" stroke-width="5"/>
  <g class="radar" style="transform-origin:{cx:.1f}px {cy}px;animation-duration:{3.2 + i * .35:.2f}s">
    <path d="{sector(cx, cy, 0, R - 7, 0, 60)}" fill="url(#rad{i})"/>
    <line x1="{cx:.1f}" y1="{cy}" x2="{lx:.1f}" y2="{ly:.1f}" stroke="{col}" stroke-opacity=".6"/>
  </g>
  <circle cx="{cx:.1f}" cy="{cy}" r="{R - 8}" fill="none" stroke="{col}" stroke-width="1.2" class="sonar" style="animation-delay:{i * .45:.2f}s"/>
  <circle cx="{cx:.1f}" cy="{cy}" r="{R}" fill="{col}" fill-opacity=".05" stroke="{col}" stroke-width="5" stroke-linecap="round"
          stroke-dasharray="{circ:.1f}" style="stroke-dashoffset:{circ * (1 - frac):.1f}; animation-delay:{delay:.2f}s"
          transform="rotate(-90 {cx:.1f} {cy})" class="arc" filter="url(#glow)" opacity="{1 if frac else 0}"/>
  <g opacity="{1 if frac else 0}">
    <circle cx="{ex:.1f}" cy="{ey:.1f}" r="3.2" fill="#FFFFFF" class="tip" style="animation-delay:{delay + 1.3:.2f}s"/>
    <circle cx="{ex:.1f}" cy="{ey:.1f}" r="4" fill="none" stroke="{col}" stroke-width="1.5" class="ping" style="animation-delay:{delay + 1.6:.2f}s"/>
  </g>
  <circle cx="{cx:.1f}" cy="{cy}" r="21" fill="{C['bg0']}" fill-opacity=".75"/>
  <g clip-path="url(#cnt{i})"><g class="count" style="--h:{30 * (steps - 1)}px;animation-delay:{delay:.2f}s">{reel}</g></g>
  {charge}
  <text x="{cx:.1f}" y="{cy + 86}" class="lbl glitch" text-anchor="middle" style="fill:{C['text']};animation-delay:{3 + i * 1.3:.1f}s">{label}</text>
  <text x="{cx:.1f}" y="{cy + 103}" class="small" text-anchor="middle">{sub}</text>
  <text x="{cx:.1f}" y="{cy + 119}" class="small" text-anchor="middle">next <tspan style="fill:{col}">▸</tspan> {fmt(goal)}</text>
</g>""")
    # ── heartbeat vitals trace ──
    by, x0, x1 = 306, 150, W - 150
    beat, pts, x = 118, [], float(x0)
    while x < x1 - 1:
        seg = [(0, 0), (30, 0), (38, -4), (46, 0), (54, 0), (58, 5), (64, -20), (70, 11), (75, 0), (86, 0), (96, -6), (106, 0), (beat, 0)]
        pts.extend((min(x + dx, x1), by + dy) for dx, dy in seg if x + dx <= x1)
        x += beat
    ekg = "M" + " L".join(f"{px:.1f},{py:.1f}" for px, py in pts)
    elen = int(sum(math.dist(pts[k], pts[k + 1]) for k in range(len(pts) - 1))) + 20
    defs.append('<linearGradient id="vital" x1="0" y1="0" x2="1" y2="0">'
                + "".join(f'<stop offset="{k / 5:.2f}" stop-color="{NEON[k]}"/>' for k in range(6)) + "</linearGradient>")
    out.append(f'<circle cx="44" cy="{by - 4}" r="4" fill="{NEON[3]}" class="blink"/>')
    out.append(f'<text x="56" y="{by}" class="small" style="fill:{C["text"]}">VITALS</text>')
    out.append(f'<text x="{W - 28}" y="{by}" class="small" text-anchor="end">NOMINAL <tspan style="fill:{NEON[3]}">●</tspan></text>')
    out.append(f'<path d="{ekg}" fill="none" stroke="{C["line"]}" stroke-width="1.4"/>')
    out.append(f'<path d="{ekg}" fill="none" stroke="url(#vital)" stroke-width="2.2" stroke-linejoin="round" class="ekg"{GLOW}/>')
    # ── stats strip ──
    since = dt.datetime.fromisoformat(user["createdAt"].replace("Z", "+00:00")).strftime("%b %Y").upper()
    strip = [
        ("PULL REQUESTS", fmt(user["pullRequests"]["totalCount"])),
        ("ISSUES", fmt(user["issues"]["totalCount"])),
        ("STARS", fmt(sum(r["stargazerCount"] for r in repos))),
        ("FORKS", fmt(sum(r["forkCount"] for r in repos))),
        ("FOLLOWERS", fmt(user["followers"]["totalCount"])),
        ("CONTRIBUTED TO", fmt(user["repositoriesContributedTo"]["totalCount"])),
        ("SINCE", since),
    ]
    sx = 28
    seg = (W - 56) / len(strip)
    sy = H - 50
    defs.append(f'<clipPath id="strip"><rect x="28" y="{sy}" width="{W - 56}" height="30" rx="6"/></clipPath>')
    defs.append(f'<linearGradient id="sbeam" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="{NEON[0]}" stop-opacity="0"/>'
                f'<stop offset=".5" stop-color="{NEON[0]}" stop-opacity=".22"/><stop offset="1" stop-color="{NEON[0]}" stop-opacity="0"/></linearGradient>')
    out.append(f'<rect x="28" y="{sy}" width="{W - 56}" height="30" rx="6" fill="{C["panel"]}" stroke="{C["line"]}"/>')
    for i, (k, v) in enumerate(strip):
        x = sx + i * seg
        if i:
            out.append(f'<line x1="{x:.1f}" y1="{sy + 6}" x2="{x:.1f}" y2="{sy + 24}" stroke="{C["line"]}"/>')
        out.append(f'<text x="{x + seg / 2:.1f}" y="{sy + 19}" class="small boot" text-anchor="middle" style="animation-delay:{1.2 + i * .15:.2f}s">'
                   f'{k} <tspan class="strong" style="fill:{NEON[i]}">{esc(v)}</tspan></text>')
    out.append(f'<g clip-path="url(#strip)"><rect x="28" y="{sy}" width="160" height="30" fill="url(#sbeam)" class="sbeam"/></g>')
    css = (
        f".arc {{ animation: arc 1.8s cubic-bezier(.3,.7,.2,1) both; }} @keyframes arc {{ from {{ stroke-dashoffset: {circ:.1f}; }} }}"
        " .tip { animation: fade .4s ease both; }"
        " .count { animation: count 1.8s cubic-bezier(.3,.7,.2,1) both; } @keyframes count { from { transform: translateY(var(--h)); } }"
        " .radar { animation: spin 3.5s linear infinite; }"
        " .sonar { transform-box: fill-box; transform-origin: center; animation: sonar 3s ease-out infinite; opacity: 0; }"
        " @keyframes sonar { 0% { transform: scale(.25); opacity: .8; } 100% { transform: scale(1); opacity: 0; } }"
        " .ping { transform-box: fill-box; transform-origin: center; animation: ping 2s ease-out infinite; opacity: 0; }"
        " @keyframes ping { 0% { transform: scale(1); opacity: .9; } 100% { transform: scale(3); opacity: 0; } }"
        f" .ekg {{ stroke-dasharray: 150 {elen}; animation: ekg 4.2s linear infinite; }}"
        f" @keyframes ekg {{ from {{ stroke-dashoffset: 150; }} to {{ stroke-dashoffset: -{elen}; }} }}"
        " .boot { animation: bootin .5s steps(3, end) both; } @keyframes bootin { from { opacity: 0; } }"
        f" .sbeam {{ animation: sbeam 4s ease-in-out infinite; }} @keyframes sbeam {{ from {{ transform: translateX(-160px); }} to {{ transform: translateX({W - 28}px); }} }}"
        " .star { opacity: .12; animation: star 4s ease-in-out infinite; } @keyframes star { 50% { opacity: .75; } }"
        " .charge { stroke-dasharray: 10 90; opacity: 0; animation: charge 3.6s cubic-bezier(.4,0,.2,1) infinite; }"
        " @keyframes charge { 0% { stroke-dashoffset: 10; opacity: 0; } 8% { opacity: 1; } 60% { stroke-dashoffset: -90; opacity: 1; } 70%, 100% { stroke-dashoffset: -90; opacity: 0; } }"
        " .orbd { animation: spin 6s linear infinite; }"
        " .lock { transform-box: fill-box; transform-origin: center; opacity: 0; animation: lock 4.2s cubic-bezier(.2,.8,.2,1) infinite; }"
        " @keyframes lock { 0% { opacity: 0; transform: scale(1.35); } 12% { opacity: 1; transform: scale(1); } 30% { opacity: 1; } 42%, 100% { opacity: 0; transform: scale(1); } }"
        " .glitch { animation: glitch 8s steps(1, end) infinite; }"
        " @keyframes glitch { 0%, 95%, 100% { opacity: 1; transform: none; } 96% { opacity: .3; transform: translateX(3px); } 97% { opacity: 1; transform: translateX(-2px); } 98% { opacity: .6; transform: none; } }"
        " .hrun { stroke-dasharray: 8 42; animation: hrun 9s linear infinite; } @keyframes hrun { from { stroke-dashoffset: 50; } to { stroke-dashoffset: 0; } }"
    )
    out.append(f'<rect x="1" y="1" width="{W - 2}" height="{H - 2}" rx="15" fill="none" stroke="url(#vital)" stroke-width="2" pathLength="100" class="hrun"{GLOW}/>'
               f'<rect x="1" y="1" width="{W - 2}" height="{H - 2}" rx="15" fill="none" stroke="#FFFFFF" stroke-width="1.3" pathLength="100" class="hrun" style="animation-delay:-4.5s"/>')
    return frame(W, H, "01", "System telemetry", "<defs>" + "".join(defs) + "</defs>\n" + "\n".join(out), css,
                 "GitHub telemetry: " + ", ".join(f"{g[0].lower()} {g[2]}" for g in gauges), NEON[0])


def render_achievements() -> str:
    """Mission record: hex badges whose outlines draw themselves, ranks that roll in like a slot
    machine and land on the real value, a glint sweeping each badge, rising sparks, an orbiting
    marker, and a circuit linking every result with a travelling pulse."""
    W, H = 900, 300
    n = len(ACHIEVEMENTS)
    step = (W - 56) / n
    cy, r = 128, 50
    cols = [NEON[4], NEON[0], NEON[2], NEON[3], NEON[1]]
    per = 6 * r  # hexagon perimeter
    out, defs = [], []
    xs = [28 + step * (i + .5) for i in range(n)]
    defs.append(f'<linearGradient id="link" x1="0" y1="0" x2="1" y2="0">'
                + "".join(f'<stop offset="{k / max(1, n - 1):.2f}" stop-color="{cols[k % len(cols)]}"/>' for k in range(n)) + "</linearGradient>")
    defs.append('<linearGradient id="glint" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#FFFFFF" stop-opacity="0"/>'
                '<stop offset=".5" stop-color="#FFFFFF" stop-opacity=".35"/><stop offset="1" stop-color="#FFFFFF" stop-opacity="0"/></linearGradient>')
    out.append("<g>" + "".join(
        f'<circle cx="{(k * 119.3) % (W - 40) + 20:.1f}" cy="{58 + (k * 43.7) % (H - 76):.1f}" r="{.6 + (k % 3) * .35:.2f}" fill="#FFFFFF" class="star"'
        f' style="animation-delay:{(k * .57) % 5:.2f}s;animation-duration:{3 + (k % 4)}s"/>' for k in range(40)) + "</g>")
    out.append(f'<line x1="{xs[0]:.1f}" y1="{cy}" x2="{xs[-1]:.1f}" y2="{cy}" stroke="{C["line"]}" stroke-width="2"/>')
    out.append(f'<line x1="{xs[0]:.1f}" y1="{cy}" x2="{xs[-1]:.1f}" y2="{cy}" stroke="url(#link)" stroke-width="2.4" class="link"{GLOW}/>')
    digits = "0123456789"
    for i, (pre, rank, l1, l2, proj) in enumerate(ACHIEVEMENTS):
        cx, col = xs[i], cols[i % len(cols)]
        hexp = " ".join(f"{polar(cx, cy, r, a)[0]:.1f},{polar(cx, cy, r, a)[1]:.1f}" for a in range(0, 360, 60))
        hexo = " ".join(f"{polar(cx, cy, r + 9, a)[0]:.1f},{polar(cx, cy, r + 9, a)[1]:.1f}" for a in range(0, 360, 60))
        d = 0.2 + i * 0.18
        big = 30 if len(rank) <= 2 else 24
        shown = ("#" if pre == "RANK" else "") + rank
        defs.append(f'<clipPath id="hx{i}"><polygon points="{hexp}"/></clipPath>')
        defs.append(f'<clipPath id="slot{i}"><rect x="{cx - 44:.1f}" y="{cy - 8}" width="88" height="36"/></clipPath>')
        # slot-machine reel: the real rank sits at the resting position, decoys stacked above it
        reel = [shown] + ["".join(digits[(i * 7 + k * 3 + j * 5) % 10] if ch.isdigit() else ch for j, ch in enumerate(shown)) for k in range(1, 8)]
        reel_txt = "".join(f'<text x="{cx:.1f}" y="{cy + 18 - 36 * k}" text-anchor="middle" class="rk" style="font-size:{big}px">{esc(t)}</text>'
                           for k, t in enumerate(reel))
        sparks = "".join(
            f'<circle cx="{cx + dx:.1f}" cy="{cy - r + 6}" r="{1.6 + (k % 3) * .5:.1f}" fill="{col}" class="spark" style="animation-delay:{d + 1.2 + k * .55:.2f}s"/>'
            for k, dx in enumerate((-22, -8, 6, 18, 28, -30)))
        rays = "".join(f'<path d="{sector(cx, cy, r + 8, r + 40, a0, a0 + 7)}" fill="{col}"/>' for a0 in range(0, 360, 30))
        confetti = ""
        for k in range(10):
            ang = math.radians(k * 36 + i * 11)
            dist = 70 + (k * 13 + i * 7) % 26
            ccol = NEON[(k + i) % len(NEON)]
            confetti += (f'<rect x="{cx - 2.5:.1f}" y="{cy - 1.2:.1f}" width="5" height="2.4" rx="1" fill="{ccol}" class="conf"'
                         f' style="--dx:{dist * math.sin(ang):.0f}px;--dy:{-dist * math.cos(ang):.0f}px;animation-delay:{d + 1.9 + k * .03:.2f}s"/>')
        ped_y = cy + r + 16
        defs.append(f'<linearGradient id="beam{i}" x1="0" y1="1" x2="0" y2="0"><stop offset="0" stop-color="{col}" stop-opacity=".45"/>'
                    f'<stop offset="1" stop-color="{col}" stop-opacity="0"/></linearGradient>')
        out.append(f"""<g class="rays" style="transform-origin:{cx:.1f}px {cy}px;animation-duration:{26 + i * 3}s">{rays}</g>
<g class="holo" style="animation-delay:{d + 1:.2f}s">
  <polygon points="{cx - 38:.1f},{ped_y} {cx + 38:.1f},{ped_y} {cx + 20:.1f},{cy} {cx - 20:.1f},{cy}" fill="url(#beam{i})" class="flick" style="animation-delay:{i * .7:.1f}s"/>
  <ellipse cx="{cx:.1f}" cy="{ped_y}" rx="40" ry="7" fill="{col}" fill-opacity=".12" stroke="{col}" stroke-width="1.4"{GLOW}/>
  <ellipse cx="{cx:.1f}" cy="{ped_y}" rx="40" ry="7" fill="none" stroke="{col}" class="pedping" style="animation-delay:{d + 1.4 + i * .3:.2f}s"/>
</g>
<g class="pop" style="animation-delay:{d:.2f}s">
  <circle cx="{cx:.1f}" cy="{cy}" r="{r + 22}" fill="none" stroke="{col}" stroke-opacity=".45" stroke-dasharray="1 7" class="{'spin' if i % 2 else 'spin-r'}"/>
  <g class="orb" style="transform-origin:{cx:.1f}px {cy}px;animation-duration:{8 + i * 1.5:.1f}s"><circle cx="{cx:.1f}" cy="{cy - r - 22}" r="3" fill="{col}"{GLOW}/></g>
  <polygon points="{hexo}" fill="none" stroke="{col}" stroke-opacity=".25"/>
  <polygon points="{hexp}" fill="{C['bg0']}"/>
  <polygon points="{hexp}" fill="{col}" fill-opacity=".1" stroke="{col}" stroke-width="2" class="draw" style="animation-delay:{d:.2f}s"{GLOW}/>
  <polygon points="{hexp}" fill="none" stroke="{col}" stroke-width="5" stroke-opacity=".35" class="pulse" style="animation-delay:{d + 1.6:.2f}s"/>
  <g clip-path="url(#hx{i})"><rect x="{cx - r - 60:.1f}" y="{cy - r}" width="40" height="{2 * r}" fill="url(#glint)" transform="skewX(-18)" class="glint" style="animation-delay:{d + 2 + i * .4:.2f}s"/></g>
  <text x="{cx:.1f}" y="{cy - 16}" class="small" text-anchor="middle" style="letter-spacing:2px;fill:{col}">{pre}</text>
  <g clip-path="url(#slot{i})"><g class="reel" style="--h:{36 * (len(reel) - 1)}px;animation-delay:{d + .3:.2f}s">{reel_txt}</g></g>
  {sparks}
  {confetti}
</g>
<g class="rise" style="animation-delay:{d + .5:.2f}s">
  <text x="{cx:.1f}" y="{cy + 98}" class="note strong" text-anchor="middle">{esc(l1)}</text>
  <text x="{cx:.1f}" y="{cy + 116}" class="note" text-anchor="middle">{esc(l2)}</text>
  <text x="{cx:.1f}" y="{cy + 136}" class="small" text-anchor="middle" style="fill:{col}">{esc(proj.upper())}</text>
</g>""")
    css = (f".rk {{ font-weight: 800; fill: {C['text']}; font-variant-numeric: tabular-nums; }}"
           " .pop { animation: pop .8s cubic-bezier(.2,.8,.2,1.2) both; transform-box: fill-box; transform-origin: center; }"
           " @keyframes pop { from { opacity: 0; transform: scale(.8); } }"
           f" .draw {{ stroke-dasharray: {per}; animation: draw 1.4s cubic-bezier(.6,0,.2,1) both; }}"
           f" @keyframes draw {{ from {{ stroke-dashoffset: {per}; fill-opacity: 0; }} }}"
           " .pulse { animation: pulse 2.6s ease-in-out infinite both; } @keyframes pulse { 0%, 100% { opacity: 0; } 50% { opacity: 1; } }"
           " .reel { animation: reel 1.6s cubic-bezier(.15,.6,.2,1) both; } @keyframes reel { from { transform: translateY(var(--h)); } }"
           " .glint { animation: glint 5s ease-in-out infinite; } @keyframes glint { 0% { transform: skewX(-18deg) translateX(0); } 30%, 100% { transform: skewX(-18deg) translateX(190px); } }"
           " .spark { opacity: 0; animation: spark 3.3s ease-out infinite; }"
           " @keyframes spark { 0% { opacity: 0; transform: translateY(0); } 15% { opacity: 1; } 100% { opacity: 0; transform: translateY(-46px); } }"
           " .orb { animation: spin 8s linear infinite; }"
           " .rise { animation: rise .8s cubic-bezier(.2,.8,.2,1) both; } @keyframes rise { from { opacity: 0; transform: translateY(8px); } }"
           " .link { stroke-dasharray: 60 900; animation: link 3.5s linear infinite; } @keyframes link { from { stroke-dashoffset: 60; } to { stroke-dashoffset: -900; } }"
           " .star { opacity: .12; animation: star 4s ease-in-out infinite; } @keyframes star { 50% { opacity: .75; } }"
           " .rays { opacity: .09; animation: spin 30s linear infinite; }"
           " .holo { animation: fade .8s ease both; }"
           " .flick { animation: flick 3s ease-in-out infinite; } @keyframes flick { 0%, 100% { opacity: .8; } 45% { opacity: .35; } 50% { opacity: .9; } 55% { opacity: .5; } }"
           " .pedping { transform-box: fill-box; transform-origin: center; opacity: 0; animation: pedping 2.8s ease-out infinite; }"
           " @keyframes pedping { 0% { transform: scale(1); opacity: .9; } 100% { transform: scale(1.6); opacity: 0; } }"
           " .conf { transform-box: fill-box; transform-origin: center; opacity: 0; animation: conf 6s cubic-bezier(.1,.7,.3,1) infinite; }"
           " @keyframes conf { 0% { opacity: 0; transform: translate(0, 0) rotate(0); } 2% { opacity: 1; }"
           " 28% { opacity: 0; transform: translate(var(--dx), var(--dy)) rotate(220deg); } 100% { opacity: 0; transform: translate(var(--dx), var(--dy)) rotate(220deg); } }"
           " .runa { stroke-dasharray: 8 42; animation: runa 8s linear infinite; } @keyframes runa { from { stroke-dashoffset: 50; } to { stroke-dashoffset: 0; } }")
    out.append(f'<rect x="1" y="1" width="{W - 2}" height="{H - 2}" rx="15" fill="none" stroke="url(#link)" stroke-width="2" pathLength="100" class="runa"{GLOW}/>'
               f'<rect x="1" y="1" width="{W - 2}" height="{H - 2}" rx="15" fill="none" stroke="#FFFFFF" stroke-width="1.3" pathLength="100" class="runa" style="animation-delay:-4s"/>')
    label = "Results: " + "; ".join(f"{p.lower()} {r}, {a} {b}" for p, r, a, b, _ in ACHIEVEMENTS)
    return frame(W, H, "02", "Mission record", "<defs>" + "".join(defs) + "</defs>\n" + "\n".join(out), css, label, NEON[4])


def render_about() -> str:
    """Operator dossier: holographic ID emblem with typed ID fields, bio lines fading in, active
    missions with live status bars, and a ticker of other builds."""
    W, H = 900, 452
    out, defs = ["<g>" + "".join(
        f'<circle cx="{(k * 113.9) % (W - 40) + 20:.1f}" cy="{58 + (k * 51.1) % (H - 76):.1f}" r="{.6 + (k % 3) * .35:.2f}" fill="#FFFFFF" class="star"'
        f' style="animation-delay:{(k * .61) % 5:.2f}s;animation-duration:{3 + (k % 4)}s"/>' for k in range(46)) + "</g>"], []
    # ── left: emblem ──
    ex, ey = 168, 152
    hexp = lambda r: " ".join(f"{polar(ex, ey, r, a)[0]:.1f},{polar(ex, ey, r, a)[1]:.1f}" for a in range(0, 360, 60))
    defs.append('<linearGradient id="holo" x1="0" y1="0" x2="1" y2="1">'
                + "".join(f'<stop offset="{k / 4:.2f}" stop-color="{c}"/>' for k, c in enumerate([NEON[0], NEON[1], NEON[2], NEON[4], NEON[3]]))
                + "</linearGradient>")
    defs.append(f'<clipPath id="hexclip"><polygon points="{hexp(58)}"/></clipPath>')
    defs.append(f'<linearGradient id="hscan" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="{NEON[0]}" stop-opacity="0"/>'
                f'<stop offset="1" stop-color="{NEON[0]}" stop-opacity=".45"/></linearGradient>')
    out.append(f'<circle cx="{ex}" cy="{ey}" r="96" fill="url(#holo)" opacity=".07" class="breathe"/>')
    out.append(f'<circle cx="{ex}" cy="{ey}" r="86" fill="none" stroke="url(#holo)" stroke-width="1.2" stroke-dasharray="2 8" class="spin"/>')
    out.append(f'<circle cx="{ex}" cy="{ey}" r="76" fill="none" stroke="{NEON[1]}" stroke-opacity=".5" stroke-dasharray="30 14 4 14" class="spin-r"/>')
    out.append(tick_ring(ex, ey, 66, 48, 4, 3, "spin", NEON[0]))
    for k, (rot, col) in enumerate(((-28, NEON[0]), (34, NEON[2]))):  # gyroscope rings flipping in 3D
        out.append(f'<g transform="rotate({rot} {ex} {ey})"><ellipse cx="{ex}" cy="{ey}" rx="94" ry="26" fill="none" stroke="{col}"'
                   f' stroke-opacity=".55" stroke-width="1.3" class="gyro" style="animation-delay:-{k * 2.5:.1f}s;animation-duration:{5 + k * 1.5:.1f}s"/></g>')
    out.append(f'<polygon points="{hexp(58)}" fill="{C["bg0"]}" stroke="url(#holo)" stroke-width="2.4"{GLOW}/>')
    out.append(f'<g clip-path="url(#hexclip)"><rect x="{ex - 60}" y="{ey - 100}" width="120" height="40" fill="url(#hscan)" class="hsweep"/>'
               f'<polygon points="{hexp(44)}" fill="none" stroke="{NEON[0]}" stroke-opacity=".25"/></g>')
    out.append(f'<polygon points="{hexp(58)}" fill="none" stroke="#FFFFFF" stroke-width="2" pathLength="100" class="hrun"{GLOW}/>')
    out.append(f'<text x="{ex}" y="{ey + 11}" text-anchor="middle" class="ini g1" style="fill:{NEON[0]}">NPP</text>'
               f'<text x="{ex}" y="{ey + 11}" text-anchor="middle" class="ini g2" style="fill:{NEON[2]}">NPP</text>')
    out.append(f'<text x="{ex}" y="{ey + 11}" text-anchor="middle" class="ini">NPP</text>')
    for k, (col, dur) in enumerate(((NEON[2], 7), (NEON[4], 11), (NEON[3], 15))):
        out.append(f'<g class="orb" style="animation-duration:{dur}s;animation-delay:-{k * 2.3:.1f}s">'
                   f'<circle cx="{ex}" cy="{ey - 86 + (k * 10 if k else 0)}" r="3.4" fill="{col}"{GLOW}/></g>')
    fields = [("NAME", "Nitya Prakash Pandey"), ("BASE", "IIT Madras · BS Data Science"), ("ROLE", "Vision & ML engineer"),
              ("FOCUS", "Medical imaging · Agentic AI"), ("MODE", "Hackathons · Kaggle")]
    for k, (key, val) in enumerate(fields):
        y = 272 + k * 25
        out.append(f'<g class="type" style="animation-delay:{.5 + k * .25:.2f}s"><text x="40" y="{y}" class="fk">{key}</text>'
                   f'<text x="100" y="{y}" class="fv">{esc(val)}</text></g>')
        out.append(f'<line x1="40" y1="{y + 8}" x2="318" y2="{y + 8}" stroke="{C["line"]}"/>')
        out.append(f'<text x="318" y="{y}" text-anchor="end" class="ok" style="animation-delay:{1.5 + k * .25:.2f}s">✓</text>')
    out.append(f'<line x1="344" y1="72" x2="344" y2="{H - 70}" stroke="{C["line"]}"/>')
    out.append(f'<line x1="344" y1="72" x2="344" y2="{H - 70}" stroke="url(#holo)" stroke-width="2" class="vflow"{GLOW}/>')
    # ── right: bio + missions ──
    rx = 368
    bio = ("BS Data Science student at IIT Madras who likes taking ML from a notebook to a working system. "
           "Most of my time goes into medical imaging: at SGBC Brain Centre I integrate and benchmark "
           "brain-segmentation tools and score them with Dice. The rest goes into hackathons and Kaggle, "
           "where I build vision and agentic systems against tight deadlines.")
    out.append(f'<text x="{rx}" y="80" class="lbl">BRIEF</text>')
    for k, ln in enumerate(_wrap(bio, 72, 5)):
        out.append(f'<text x="{rx}" y="{104 + k * 19}" class="bio rise" style="animation-delay:{.3 + k * .18:.2f}s">{esc(ln)}</text>')
    missions = [
        ("Brain MRI segmentation benchmarks", "SGBC Brain Centre · SynthSeg · BiomedParse · ANTsPy", NEON[0]),
        ("Reddit credibility & network research", "Research internship · DoMS", NEON[1]),
        ("Chandrayaan-2 lunar image registration", "Smart India Hackathon 2026", NEON[4]),
        ("Kaggle & hackathons", "Agriculture · health · climate", NEON[3]),
    ]
    out.append(f'<rect x="{rx - 8}" y="90" width="{W - 30 - rx + 8}" height="20" rx="4" fill="url(#readb)" class="read"/>')
    out.append(f'<rect x="{rx - 8}" y="{242 - 18}" width="{W - 30 - rx + 8}" height="38" rx="6" fill="url(#readb)" class="mscan"/>')
    defs.append(f'<linearGradient id="readb" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="{NEON[1]}" stop-opacity=".22"/>'
                f'<stop offset=".7" stop-color="{NEON[0]}" stop-opacity=".07"/><stop offset="1" stop-color="{NEON[0]}" stop-opacity="0"/></linearGradient>')
    out.append(f'<text x="{rx}" y="214" class="lbl">ACTIVE MISSIONS · {len(missions)}</text>')
    bx0, bx1 = 712, W - 30
    defs.append('<pattern id="stripes" width="14" height="8" patternUnits="userSpaceOnUse" patternTransform="skewX(-40)">'
                '<rect width="7" height="8" fill="#FFFFFF" fill-opacity=".35"/></pattern>')
    for k, (title, ctx, col) in enumerate(missions):
        y = 242 + k * 40
        d = .9 + k * .2
        defs.append(f'<clipPath id="bar{k}"><rect x="{bx0}" y="{y + 1}" width="{bx1 - bx0}" height="7" rx="3.5"/></clipPath>')
        out.append(f"""<g class="rise" style="animation-delay:{d:.2f}s">
  <circle cx="{rx + 5}" cy="{y}" r="4.5" fill="{col}" class="led" style="animation-delay:{k * .4:.1f}s"/>
  <circle cx="{rx + 5}" cy="{y}" r="4.5" fill="none" stroke="{col}" class="ping" style="animation-delay:{k * .6:.1f}s"/>
  <text x="{rx + 20}" y="{y + 4}" class="mt">{esc(title)}</text>
  <text x="{rx + 20}" y="{y + 20}" class="small">{esc(ctx)}</text>
  <text x="{bx1}" y="{y - 6}" class="small" text-anchor="end" style="fill:{col}">IN PROGRESS</text>
  <rect x="{bx0}" y="{y + 1}" width="{bx1 - bx0}" height="7" rx="3.5" fill="{col}" fill-opacity=".85"/>
  <g clip-path="url(#bar{k})"><rect x="{bx0 - 28}" y="{y + 1}" width="{bx1 - bx0 + 56}" height="7" fill="url(#stripes)" class="stripe"/></g>
</g>""")
    # ── ticker ──
    built = ["Travel RAG agent · LangGraph · Qdrant · Cohere", "Whole-slide pathology pipeline · PyTorch foundation models",
             "AgroSkin · AI skin-disease detector", "FieldPilot AI · 10-agent smart-glasses inspector",
             "TyreMind · physics-informed race ML", "SAGE · agentic commerce control plane"]
    ty = H - 40
    items, x = [], 0.0
    seq_cols = [NEON[0], NEON[2], NEON[3], NEON[4], NEON[1], NEON[5]]
    for rep in range(2):
        for k, b in enumerate(built):
            items.append(f'<text x="{x:.1f}" y="{ty}" class="tk"><tspan style="fill:{seq_cols[k]}">◆</tspan> {esc(b)}</text>')
            x += 7.6 * len(b) + 56
        if rep == 0:
            loop = x
    out.append(f'<rect x="28" y="{ty - 20}" width="{W - 56}" height="30" rx="6" fill="{C["panel"]}" stroke="{C["line"]}"/>')
    out.append(f'<text x="44" y="{ty}" class="lbl" style="fill:{NEON[4]}">ALSO BUILT ▸</text>')
    defs.append(f'<clipPath id="tickm"><rect x="160" y="{ty - 20}" width="{W - 188}" height="30"/></clipPath>')
    out.append(f'<g clip-path="url(#tickm)"><g transform="translate(160,0)"><g class="marq">{"".join(items)}</g></g></g>')
    css = (f".ini {{ font: 800 30px {FONT}; letter-spacing: 2px; fill: url(#holo); }}"
           f" .fk {{ font: 700 10.5px {MONO}; letter-spacing: 1.6px; fill: {NEON[0]}; }} .fv {{ font-size: 13px; fill: {C['text']}; }}"
           f" .bio {{ font-size: 13.5px; fill: {C['soft']}; }} .mt {{ font-size: 14px; font-weight: 650; fill: {C['text']}; }}"
           f" .tk {{ font: 12px {MONO}; fill: {C['soft']}; }}"
           " .breathe { transform-box: fill-box; transform-origin: center; animation: breathe 4s ease-in-out infinite; }"
           " @keyframes breathe { 50% { transform: scale(1.12); opacity: .02; } }"
           f" .orb {{ transform-origin: {ex}px {ey}px; animation: spin 9s linear infinite; }}"
           " .hsweep { animation: hsweep 3.2s linear infinite; } @keyframes hsweep { from { transform: translateY(0); } to { transform: translateY(170px); } }"
           " .type { animation: type 1s cubic-bezier(.3,0,.2,1) both; }"
           " @keyframes type { from { clip-path: inset(0 100% 0 0); } to { clip-path: inset(0 0 0 0); } }"
           " .rise { animation: rise .8s cubic-bezier(.2,.8,.2,1) both; } @keyframes rise { from { opacity: 0; transform: translateY(6px); } }"
           " .led { animation: blink 1.6s ease-in-out infinite; }"
           " .ping { transform-box: fill-box; transform-origin: center; animation: ping 2.4s ease-out infinite; }"
           " @keyframes ping { from { transform: scale(1); opacity: .9; } to { transform: scale(2.8); opacity: 0; } }"
           " .stripe { animation: stripe 1s linear infinite; } @keyframes stripe { to { transform: translateX(14px); } }"
           f" .marq {{ animation: marq {loop / 45:.1f}s linear infinite; }} @keyframes marq {{ to {{ transform: translateX(-{loop:.1f}px); }} }}")
    css += (" .star { opacity: .12; animation: star 4s ease-in-out infinite; } @keyframes star { 50% { opacity: .75; } }"
            " .gyro { transform-box: fill-box; transform-origin: center; animation: gyro 5s cubic-bezier(.37,0,.63,1) infinite alternate; }"
            " @keyframes gyro { from { transform: scaleY(1); } to { transform: scaleY(-1); } }"
            " .hrun { stroke-dasharray: 12 88; animation: hrun 4s linear infinite; } @keyframes hrun { from { stroke-dashoffset: 100; } to { stroke-dashoffset: 0; } }"
            " .g1, .g2 { opacity: 0; animation: gl 6s steps(1, end) 2.5s infinite; } .g2 { animation-name: gl2; }"
            " @keyframes gl { 0%, 93%, 100% { opacity: 0; transform: none; } 94% { opacity: .8; transform: translate(-3px, 1px); } 96% { opacity: .5; transform: translate(2px, -1px); } }"
            " @keyframes gl2 { 0%, 93%, 100% { opacity: 0; transform: none; } 94% { opacity: .8; transform: translate(3px, -1px); } 96% { opacity: .5; transform: translate(-2px, 1px); } }"
            f" .ok {{ font: 700 13px {MONO}; fill: {NEON[3]}; animation: okp .5s cubic-bezier(.2,.9,.3,1.4) both; transform-box: fill-box; transform-origin: center; }}"
            " @keyframes okp { from { opacity: 0; transform: scale(2.2); } }"
            " .vflow { stroke-dasharray: 36 400; animation: vflow 3.4s linear infinite; } @keyframes vflow { from { stroke-dashoffset: 36; } to { stroke-dashoffset: -400; } }"
            " .read { opacity: 0; animation: read 6s ease-in-out 2.2s infinite; }"
            " @keyframes read { 0% { opacity: 0; transform: translateY(0); } 8% { opacity: 1; } 80% { opacity: 1; transform: translateY(76px); } 100% { opacity: 0; transform: translateY(76px); } }"
            f" .mscan {{ opacity: 0; animation: mscan {len(missions) * 1.3:.1f}s steps({len(missions)}, end) 3s infinite; }}"
            f" @keyframes mscan {{ from {{ opacity: 1; transform: translateY(0); }} to {{ opacity: 1; transform: translateY({len(missions) * 40}px); }} }}"
            " .arun { stroke-dasharray: 8 42; animation: arun 9s linear infinite; } @keyframes arun { from { stroke-dashoffset: 50; } to { stroke-dashoffset: 0; } }")
    out.append(f'<rect x="1" y="1" width="{W - 2}" height="{H - 2}" rx="15" fill="none" stroke="url(#holo)" stroke-width="2" pathLength="100" class="arun"{GLOW}/>'
               f'<rect x="1" y="1" width="{W - 2}" height="{H - 2}" rx="15" fill="none" stroke="#FFFFFF" stroke-width="1.3" pathLength="100" class="arun" style="animation-delay:-4.5s"/>')
    body = "<defs>" + "".join(defs) + "</defs>\n" + "\n".join(out)
    label = ("Operator profile: Nitya Prakash Pandey, BS Data Science at IIT Madras, vision and ML engineer. "
             + bio + " Active missions: " + "; ".join(f"{t} ({c})" for t, c, _ in missions) + ". Also built: " + "; ".join(built))
    return frame(W, H, "00", "Operator profile", body, css, label, NEON[1])


LANG_SLUG = {
    "Python": "python", "TypeScript": "typescript", "JavaScript": "javascript", "HTML": "html5", "CSS": "css",
    "C++": "cplusplus", "C": "c", "Java": "java", "Go": "go", "Rust": "rust", "R": "r", "Kotlin": "kotlin",
    "Swift": "swift", "PHP": "php", "Ruby": "ruby", "Dart": "dart", "Scala": "scala", "Julia": "julia",
    "Cuda": "nvidia", "Solidity": "solidity", "Lua": "lua", "Haskell": "haskell",
}
LANG_ALIAS = {"shell": "bash", "jupyter notebook": "jupyter", "dockerfile": "docker"}


def toolkit_lanes(repo_langs: list[str]) -> list[tuple[str, list[tuple[str, str]]]]:
    """TOOLKIT plus any language from the repositories that is not listed yet (Languages lane, max 7)."""
    lanes = [(lane, list(tools)) for lane, tools in TOOLKIT]
    have = {name.lower() for _, tools in lanes for name, _ in tools}
    for lang in repo_langs:
        key = LANG_ALIAS.get(lang.lower(), lang.lower())
        if key in have or lang == "Other" or len(lanes[0][1]) >= 7:
            continue
        lanes[0][1].append((lang, LANG_SLUG.get(lang, "")))
        have.add(key)
    return lanes


def render_toolkit(repo_langs: list[str] = ()) -> str:
    """Tech arsenal: one colour-coded lane per domain on a twinkling starfield. Each lane has a data
    pulse along its circuit trace, a packet that hops chip to chip, and a scan beam; chips cascade in,
    then fire in a rippling sequence while their logos float; lane markers ping and labels glitch."""
    icons = json.loads((ROOT / "scripts" / "toolkit_icons.json").read_text(encoding="utf-8"))
    TOOLKIT = toolkit_lanes(list(repo_langs))  # noqa: N806 — shadows the setting with the live lanes
    lane_cols = [NEON[4], NEON[1], NEON[2], NEON[0], NEON[3], NEON[5]]
    W, lane_h, top = 900, 56, 76
    H = top + lane_h * len(TOOLKIT) + 22
    tx0, tx1 = 204, W - 28
    n_tools = sum(len(t) for _, t in TOOLKIT)
    stars = "".join(
        f'<circle cx="{(k * 137.508) % (W - 40) + 20:.1f}" cy="{60 + (k * 53.7) % (H - 80):.1f}" r="{.7 + (k % 3) * .35:.2f}" fill="#FFFFFF" class="star"'
        f' style="animation-delay:{(k * .61) % 5:.2f}s;animation-duration:{3 + (k % 4)}s"/>' for k in range(56))
    out = [f'<g>{stars}</g>',
           f'<line x1="44" y1="{top + 22}" x2="44" y2="{top + 22 + lane_h * (len(TOOLKIT) - 1)}" stroke="{C["line"]}" stroke-width="2"/>',
           f'<line x1="44" y1="{top + 22}" x2="44" y2="{top + 22 + lane_h * (len(TOOLKIT) - 1)}" stroke="url(#bus)" stroke-width="2" class="bus"/>']
    defs = [f'<linearGradient id="bus" x1="0" y1="0" x2="0" y2="1">'
            + "".join(f'<stop offset="{i / (len(TOOLKIT) - 1):.2f}" stop-color="{c}"/>' for i, c in enumerate(lane_cols)) + "</linearGradient>"]
    hop_css = []
    for i, (lane, tools) in enumerate(TOOLKIT):
        col = lane_cols[i % len(lane_cols)]
        cy = top + 22 + i * lane_h
        defs.append(f'<linearGradient id="sh{i}" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="{col}" stop-opacity="0"/>'
                    f'<stop offset=".5" stop-color="{col}" stop-opacity=".22"/><stop offset="1" stop-color="{col}" stop-opacity="0"/></linearGradient>')
        hexp = " ".join(f"{polar(44, cy, 13, a)[0]:.1f},{polar(44, cy, 13, a)[1]:.1f}" for a in range(30, 390, 60))
        cclip, cmark = count_up(f"tc{i}", 74, cy + 14, len(tools), cls="small", delay=.6 + i * .12, size=11, width=70, suffix=" tools")
        defs.append(cclip)
        out.append(f"""<g class="chip" style="animation-delay:{.15 + i * .1:.2f}s">
  <circle cx="44" cy="{cy}" r="20" fill="none" stroke="{col}" stroke-opacity=".5" stroke-dasharray="2 5" class="{'spin' if i % 2 else 'spin-r'}"/>
  <polygon points="{hexp}" fill="{C['bg0']}" stroke="{col}" stroke-width="1.6"{GLOW}/>
  <circle cx="44" cy="{cy}" r="13" fill="none" stroke="{col}" stroke-width="1.5" class="hping" style="animation-delay:{1 + i * .5:.1f}s"/>
  <text x="44" y="{cy + 4}" text-anchor="middle" class="hexn" style="fill:{col}">{i + 1:02d}</text>
  <text x="74" y="{cy - 2}" class="lbl glitch" style="fill:{C['text']};animation-delay:{2 + i * 1.1:.1f}s">{esc(lane.upper())}</text>
  {cmark}
</g>""")
        out.append(f'<line x1="{tx0 - 12}" y1="{cy}" x2="{tx1}" y2="{cy}" stroke="{col}" stroke-opacity=".22"/>')
        out.append(f'<line x1="{tx0 - 12}" y1="{cy}" x2="{tx1}" y2="{cy}" stroke="{col}" stroke-width="2.4" stroke-linecap="round"'
                   f' class="pulse" style="animation-delay:{-i * .9:.1f}s"{GLOW}/>')
        widths = [36 + 7.5 * len(name) for name, _ in tools]
        gap = min(10.0, (tx1 - tx0 - sum(widths)) / max(1, len(tools) - 1))
        x = float(tx0)
        stops = [tx0 - 8]
        for j, ((name, slug), w) in enumerate(zip(tools, widths)):
            if slug and slug in icons:
                glyph = (f'<g transform="translate({x + 10:.1f},{cy - 8}) scale(.6667)" class="ico" style="animation-delay:{(i * 7 + j) * .37 % 4:.2f}s">'
                         f'<path d="{icons[slug]}" fill="{col}"/></g>')
            else:  # no public logo: a small scan-target glyph
                glyph = (f'<g class="ico" style="animation-delay:{(i * 7 + j) * .37 % 4:.2f}s"><circle cx="{x + 18:.1f}" cy="{cy}" r="6" fill="none" stroke="{col}" stroke-width="1.6"/>'
                         f'<circle cx="{x + 18:.1f}" cy="{cy}" r="2" fill="{col}"/></g>')
            out.append(f'<g class="chip" style="animation-delay:{.35 + i * .1 + j * .06:.2f}s">'
                       f'<rect x="{x:.1f}" y="{cy - 15}" width="{w:.1f}" height="30" rx="8" fill="{C["panel"]}" stroke="{col}" stroke-opacity=".55"/>'
                       f'<g class="bob" style="animation-delay:{(i * 5 + j * 3) * .29 % 3:.2f}s">{glyph}</g>'
                       f'<text x="{x + 32:.1f}" y="{cy + 4.5}" style="font-size:12.5px;fill:{C["text"]}">{esc(name)}</text>'
                       f'<rect x="{x:.1f}" y="{cy - 15}" width="{w:.1f}" height="30" rx="8" fill="{col}" fill-opacity=".08" stroke="{col}" stroke-width="2"'
                       f' class="fire" style="animation-delay:{2.4 + i * .22 + j * .3:.2f}s"{GLOW}/></g>')
            x += w + gap
            stops.append(x - gap / 2)
        # packet that hops through the gaps between chips: hold at each gap, dash to the next
        seg = 100 / (len(stops) - 1)
        frames = []
        for k, sx in enumerate(stops):
            t = k * seg
            frames.append(f"{t:.1f}% {{ transform: translateX({sx - stops[0]:.1f}px); }}")
            if k < len(stops) - 1:
                frames.append(f"{t + .55 * seg:.1f}% {{ transform: translateX({sx - stops[0]:.1f}px); }}")
        hop_css.append(f" @keyframes hop{i} {{ {' '.join(frames)} }}"
                       f" .hop{i} {{ animation: hop{i} {1.1 * (len(stops) - 1):.1f}s cubic-bezier(.6,0,.3,1) {2.2 + i * .45:.2f}s infinite both; }}")
        out.append(f'<circle cx="{stops[0]:.1f}" cy="{cy}" r="3.4" fill="#FFFFFF" class="hop{i}"{GLOW}/>')
        out.append(f'<rect x="{tx0 - 12}" y="{cy - 17}" width="130" height="34" fill="url(#sh{i})" class="beam" style="animation-delay:{i * .8:.1f}s"/>')
    css = (f".hexn {{ font: 700 10.5px {MONO}; }}"
           " .chip { animation: chipin .7s cubic-bezier(.2,.8,.2,1) both; }"
           " @keyframes chipin { from { opacity: 0; transform: translateY(8px); } }"
           " .pulse { stroke-dasharray: 46 1400; animation: flow 4.5s linear infinite; }"
           " @keyframes flow { from { stroke-dashoffset: 46; } to { stroke-dashoffset: -760; } }"
           " .beam { animation: beam 6s cubic-bezier(.5,0,.5,1) infinite; opacity: 0; }"
           f" @keyframes beam {{ 0% {{ transform: translateX(0); opacity: 0; }} 15% {{ opacity: 1; }} 85% {{ opacity: 1; }} 100% {{ transform: translateX({tx1 - tx0 - 100}px); opacity: 0; }} }}"
           " .ico { animation: ico 4s ease-in-out infinite; } @keyframes ico { 50% { opacity: .55; } }"
           " .bob { animation: bob 3s ease-in-out infinite; } @keyframes bob { 50% { transform: translateY(-2px); } }"
           " .fire { opacity: 0; animation: fire 5.5s ease-out infinite; }"
           " @keyframes fire { 0%, 14%, 100% { opacity: 0; } 4% { opacity: 1; } }"
           " .hping { transform-box: fill-box; transform-origin: center; opacity: 0; animation: hping 3s ease-out infinite; }"
           " @keyframes hping { 0% { transform: scale(1); opacity: .9; } 60%, 100% { transform: scale(2.3); opacity: 0; } }"
           " .glitch { animation: glitch 7s steps(1, end) infinite; }"
           " @keyframes glitch { 0%, 95%, 100% { opacity: 1; transform: none; } 96% { opacity: .35; transform: translateX(3px); }"
           " 97% { opacity: 1; transform: translateX(-2px); } 98% { opacity: .6; transform: none; } }"
           " .star { animation: star 4s ease-in-out infinite; opacity: .15; } @keyframes star { 50% { opacity: .75; } }"
           " .bus { stroke-dasharray: 30 300; animation: busf 3s linear infinite; } @keyframes busf { from { stroke-dashoffset: 30; } to { stroke-dashoffset: -300; } }"
           + "".join(hop_css))
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
    floor, towers, tops = [], [], []
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
  <polygon points="{poly([q0, q1, q2, q3])}" fill="#FFFFFF" class="twk" style="animation-delay:{(col * 7 + row * 3) % 50 / 10 + 1.5:.1f}s"/>
</g>""")
        tops.append((h, n, (q0[0] + q2[0]) / 2, (q0[1] + q2[1]) / 2, top))
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
    stats = [("TOTAL", total), ("ACTIVE DAYS", active), ("BEST DAY", best_n), ("THIS MONTH", this_month)]
    side, sdefs = [], []
    for i, (k, v) in enumerate(stats):
        y = 96 + i * 52
        side.append(f'<text x="742" y="{y}" class="lbl">{k}</text>')
        clip, mark = count_up(f"sk{i}", 742, y + 26, v, delay=.6 + i * .15, width=130)
        sdefs.append(clip)
        side.append(mark)
        if k == "BEST DAY" and best_n:
            side.append(f'<text x="{742 + 16 + 15 * len(fmt(v))}" y="{y + 25}" class="small">{best_d:%d %b}</text>')
    fx = []  # beacon over the best day and sparks off the tallest towers
    for rank, (h, n, tx, ty, tcol) in enumerate(sorted(tops, key=lambda t: -t[0])[:6]):
        if rank == 0:
            fx.append(f'<rect x="{tx - 4:.1f}" y="{ty - 96:.1f}" width="8" height="96" fill="url(#beacon)" class="beacon"/>')
            fx.append(f'<ellipse cx="{tx:.1f}" cy="{ty:.1f}" rx="9" ry="4" fill="none" stroke="#FFFFFF" stroke-width="1.5" class="bping"/>')
        for k in range(2):
            fx.append(f'<circle cx="{tx + (k * 6 - 3):.1f}" cy="{ty - 4:.1f}" r="{1.6 + k * .5:.1f}" fill="{tcol}" class="spk" style="animation-delay:{1.8 + rank * .5 + k * 1.3:.1f}s"/>')
    lx, ly = 742, 330
    side.append(f'<text x="{lx}" y="{ly}" class="small">less</text>')
    for i, col in enumerate(SEQ):
        side.append(f'<rect x="{lx + 34 + i * 18}" y="{ly - 10}" width="14" height="14" rx="3" fill="{col}"/>')
    side.append(f'<text x="{lx + 34 + 4 * 18 + 4}" y="{ly}" class="small">more</text>')
    side.append(f'<text x="{lx}" y="{ly + 22}" class="small" style="fill:{C["dim"]}">height = √ contributions</text>')

    body = f"""<defs><linearGradient id="beam" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="{NEON[2]}" stop-opacity="0"/><stop offset="1" stop-color="{NEON[2]}" stop-opacity=".4"/></linearGradient>
<linearGradient id="beacon" x1="0" y1="1" x2="0" y2="0"><stop offset="0" stop-color="#FFFFFF" stop-opacity=".75"/><stop offset="1" stop-color="{SEQ[3]}" stop-opacity="0"/></linearGradient>{''.join(sdefs)}</defs>
<g fill="{C['tile']}" stroke="{C['line']}" stroke-width=".6">{''.join(floor)}</g>
<polygon points="{beam}" fill="url(#beam)" class="beam"/>
{''.join(towers)}
<g class="late">{''.join(fx)}</g>
{''.join(months)}
<line x1="720" y1="74" x2="720" y2="{H - 30}" stroke="{C['line']}"/>
{''.join(side)}"""
    css = (".rise { animation: rise .9s cubic-bezier(.2,.8,.2,1) both; }"
           " @keyframes rise { from { opacity: 0; transform: translateY(18px); } }"
           f" .beam {{ animation: beam 6s linear infinite; }} @keyframes beam {{ from {{ transform: translate(0,0); }} to {{ transform: translate({ex:.1f}px,{ey:.1f}px); }} }}"
           " .late { animation: fade .8s ease 2.4s both; }"
           " .twk { opacity: 0; animation: twk 5s ease-in-out infinite; } @keyframes twk { 0%, 80%, 100% { opacity: 0; } 88% { opacity: .55; } }"
           " .beacon { transform-box: fill-box; transform-origin: bottom; animation: beacon 2.4s ease-in-out 1.6s infinite both; }"
           " @keyframes beacon { 0%, 100% { opacity: .35; transform: scaleY(.8); } 50% { opacity: 1; transform: scaleY(1); } }"
           " .bping { transform-box: fill-box; transform-origin: center; animation: bping 2.4s ease-out 1.6s infinite both; }"
           " @keyframes bping { from { transform: scale(1); opacity: .9; } to { transform: scale(3.2); opacity: 0; } }"
           " .spk { opacity: 0; animation: spk 3s ease-out infinite; }"
           " @keyframes spk { 0% { opacity: 0; transform: translateY(0); } 15% { opacity: 1; } 100% { opacity: 0; transform: translateY(-60px); } }")
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
        bars.append(f'<clipPath id="wd{i}"><rect x="{x}" y="{by1 - h:.1f}" width="{bw}" height="{h:.1f}" rx="4"/></clipPath>'
                    f'<g clip-path="url(#wd{i})"><rect x="{x}" y="{by1}" width="{bw}" height="26" fill="url(#shim)" class="shim" style="animation-delay:{1.6 + i * .25:.2f}s"/></g>')
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
        f" .comet {{ stroke-dasharray: 16 {length + 40}; animation: comet 4.5s linear 2.6s infinite both; opacity: 0; }}"
        f" @keyframes comet {{ 0% {{ stroke-dashoffset: 16; opacity: 1; }} 100% {{ stroke-dashoffset: -{length}; opacity: 1; }} }}"
        f" .vscan {{ animation: vscan 7s ease-in-out 2.4s infinite both; opacity: 0; }}"
        f" @keyframes vscan {{ 0% {{ transform: translateX(0); opacity: 0; }} 10%, 90% {{ opacity: .55; }} 100% {{ transform: translateX({gx1 - gx0}px); opacity: 0; }} }}"
        f" .shim {{ animation: shim 3.2s ease-in-out infinite; }} @keyframes shim {{ 0% {{ transform: translateY(0); }} 60%, 100% {{ transform: translateY(-{bh + 30}px); }} }}"
    )
    body = f"""<defs>
  <linearGradient id="stroke" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="{NEON[0]}"/><stop offset=".5" stop-color="{NEON[1]}"/><stop offset="1" stop-color="{NEON[2]}"/></linearGradient>
  <linearGradient id="fill" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="{NEON[1]}" stop-opacity=".35"/><stop offset="1" stop-color="{NEON[0]}" stop-opacity="0"/></linearGradient>
  <linearGradient id="shim" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#FFFFFF" stop-opacity="0"/><stop offset=".5" stop-color="#FFFFFF" stop-opacity=".45"/><stop offset="1" stop-color="#FFFFFF" stop-opacity="0"/></linearGradient>
</defs>
<text x="{gx0}" y="72" class="lbl">WEEKLY CONTRIBUTIONS</text>
<text x="{bx0}" y="72" class="lbl">BY WEEKDAY</text>
{''.join(g)}
<path d="{area}" fill="url(#fill)" class="area"/>
<path d="{line}" fill="none" stroke="url(#stroke)" stroke-width="2" stroke-linecap="round" class="line" filter="url(#glow)"/>
<path d="{line}" fill="none" stroke="#FFFFFF" stroke-width="3" stroke-linecap="round" class="comet" filter="url(#glow)"/>
<line x1="{gx0}" y1="{gy0 - 6}" x2="{gx0}" y2="{gy1}" stroke="{NEON[1]}" stroke-opacity=".55" class="vscan"/>
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
        if hot:
            out.append(f'<path d="{sector(cx, cy, r0 + 2, rr + 6, h * 15 + .5, h * 15 + 14.5)}" fill="none" stroke="{band_of(h)}" stroke-width="1.5" class="hotp"/>')
    out.append(f'<circle cx="{cx}" cy="{cy}" r="{r0 - 4}" fill="{C["bg0"]}" stroke="{C["line"]}"/>')
    cclip, cmark = count_up("ck", cx, cy + 2, total, anchor="middle", delay=.5, size=20, width=70, style="font-size:20px")
    out.append(f'<defs>{cclip}</defs>{cmark}')
    out.append(f'<g class="orbit2"><circle cx="{cx}" cy="{cy - r1 - 14}" r="3.5" fill="{NEON[3]}"{GLOW}/></g>')
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
        bw_ = max(4, (W - 40 - x0) * n / bmx)
        out.append(f'<rect x="{x0}" y="{y + 10}" width="{bw_:.1f}" height="8" rx="4" fill="{BAND[name]}" fill-opacity="{1 if hot else .8}" class="grow" style="animation-delay:{.5 + i * .12:.2f}s"{GLOW if hot else ""}/>')
        out.append(f'<clipPath id="bb{i}"><rect x="{x0}" y="{y + 10}" width="{bw_:.1f}" height="8" rx="4"/></clipPath>'
                   f'<g clip-path="url(#bb{i})"><rect x="{x0 - 60}" y="{y + 10}" width="60" height="8" fill="url(#bshim)" class="bshim" style="--w:{bw_ + 60:.0f}px;animation-delay:{1.4 + i * .35:.2f}s"/></g>')
    if total:
        kind = {"NIGHT": "a night owl", "MORNING": "an early bird", "AFTERNOON": "an afternoon builder", "EVENING": "an evening coder"}[bands[counts.index(bmx)][0]]
        msg = f'Busiest hour <tspan class="strong">{peak_h:02d}:00 IST</tspan> · verdict: <tspan class="strong">{kind}</tspan>'
    else:
        msg = "Commit times appear once public repositories have commits."
    out.append(f'<text x="{x0}" y="{H - 34}" class="note">{msg}</text>')
    out.append(f'<text x="{x0}" y="{H - 16}" class="small" style="fill:{C["dim"]}">last 100 commits per repository, default branches</text>')
    body = (f'<defs><linearGradient id="sweep" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="{C["cyan"]}" stop-opacity="0"/>'
            f'<stop offset="1" stop-color="{C["cyan"]}" stop-opacity=".22"/></linearGradient>'
            '<linearGradient id="bshim" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#FFFFFF" stop-opacity="0"/>'
            '<stop offset=".5" stop-color="#FFFFFF" stop-opacity=".6"/><stop offset="1" stop-color="#FFFFFF" stop-opacity="0"/></linearGradient></defs>'
            f'<text x="470" y="74" class="lbl">TIME OF DAY</text>' + "\n".join(out))
    css = (f".radar {{ transform-origin: {cx}px {cy}px; animation: radar 5s linear infinite; }}"
           " @keyframes radar { to { transform: rotate(360deg); } }"
           f" .bloom {{ transform-origin: {cx}px {cy}px; animation: bloom .9s cubic-bezier(.2,.8,.2,1) both; }}"
           " @keyframes bloom { from { opacity: 0; transform: scale(.4); } }"
           " .grow { transform-box: fill-box; transform-origin: left; animation: growx 1.1s cubic-bezier(.2,.7,.2,1) both; }"
           " @keyframes growx { from { transform: scaleX(0); } }"
           " .hotp { animation: hotp 1.8s ease-in-out 1.4s infinite both; } @keyframes hotp { 0%, 100% { opacity: .1; } 50% { opacity: 1; } }"
           f" .orbit2 {{ transform-origin: {cx}px {cy}px; animation: spin 12s linear infinite; }}"
           " .bshim { animation: bshim 3.5s ease-in-out infinite; } @keyframes bshim { 0% { transform: translateX(0); } 60%, 100% { transform: translateX(var(--w)); } }")
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
    cycle = 1.3 * max(1, len(mix))
    for k, (name, col, pct) in enumerate(mix):
        sweep = 360 * pct / 100
        if sweep > gap + .4:
            mid = math.radians(a + sweep / 2)
            segs.append(f'<path d="{sector(cx, cy, ri, ro, a + gap / 2, a + sweep - gap / 2)}" fill="{col}" class="seg"'
                        f' style="--dx:{7 * math.sin(mid):.1f}px;--dy:{-7 * math.cos(mid):.1f}px;animation-duration:{cycle:.1f}s;animation-delay:{2 + k * 1.3:.1f}s">'
                        f'<title>{esc(name)} {pct:.1f}%</title></path>')
        a += sweep
    out.append(f'<g mask="url(#reveal)">{"".join(segs)}</g>')
    if mix:
        name, _, pct = mix[0]
        lclip, lmark = count_up("lg", cx, cy - 4, round(pct), anchor="middle", delay=.6, width=90, suffix="%")
        out.append(f'<defs>{lclip}</defs>{lmark}')
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
        out.append(f'<circle cx="{x0 + 20}" cy="{y + 9.5}" r="3" fill="#FFFFFF" class="pkt" style="--w:{max(2, wbar - 20):.0f}px;animation-delay:{1.6 + i * .45:.2f}s"{GLOW}/>')
    if not mix:
        out.append(f'<text x="{x0}" y="130" class="note">Language data appears after your first repository with code.</text>')
    circ = 2 * math.pi * (ro + ri) / 2
    body = (f'<defs><mask id="reveal"><circle cx="{cx}" cy="{cy}" r="{(ro + ri) / 2}" fill="none" stroke="#fff" stroke-width="{ro - ri + 22}"'
            f' stroke-dasharray="{circ:.1f}" transform="rotate(-90 {cx} {cy})" class="wipe"/></mask></defs>'
            f'<text x="{x0}" y="72" class="lbl">SHARE OF CODE · BYTES × REPOS</text>' + "\n".join(out))
    css = (f".wipe {{ animation: wipe 1.6s cubic-bezier(.5,0,.2,1) .2s both; }} @keyframes wipe {{ from {{ stroke-dashoffset: {circ:.1f}; }} }}"
           f" .orbit {{ transform-origin: {cx}px {cy}px; animation: spin 14s linear infinite; }}"
           " .grow { transform-box: fill-box; transform-origin: left; animation: growx 1.1s cubic-bezier(.2,.7,.2,1) both; }"
           " @keyframes growx { from { transform: scaleX(0); } }"
           " .seg { animation: seg 6s ease-in-out infinite both; }"
           " @keyframes seg { 0%, 14%, 100% { transform: translate(0, 0); } 6%, 9% { transform: translate(var(--dx), var(--dy)); } }"
           " .pkt { opacity: 0; animation: pkt 2.8s ease-in-out infinite; }"
           " @keyframes pkt { 0% { opacity: 0; transform: translateX(0); } 10% { opacity: 1; } 75% { opacity: 1; transform: translateX(var(--w)); } 100% { opacity: 0; transform: translateX(var(--w)); } }")
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
           f'<text x="{x1 + 16}" y="{ay + 4}" class="small" style="fill:{C["text"]}">NOW</text>',
           f'<g class="comet"><rect x="{x0 - 90}" y="{ay - 1.5}" width="90" height="3" rx="1.5" fill="url(#trail)"/>'
           f'<circle cx="{x0}" cy="{ay}" r="4.5" fill="#FFFFFF"{GLOW}/></g>']
    t_comet = 7.0
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
        fork = bool(r.get("isFork"))
        shown = ("⑂ " if fork else "") + r["name"]
        wlab = 7.0 * len(shown) + 18
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
        out.append(f"""<g class="pop" style="animation-delay:{d:.2f}s"><title>{esc(r['name'])}{' (fork)' if fork else ''} · created {t:%d %b %Y} · {esc(lang or 'no language')}</title>
  <line x1="{x:.1f}" y1="{ay + (-7 if up else 7)}" x2="{x:.1f}" y2="{ly + (6 if up else -14)}" stroke="{col}" stroke-opacity=".55"/>
  <circle cx="{x:.1f}" cy="{ay}" r="5.5" fill="{C['bg0']}" stroke="{col}" stroke-width="2.4"{' stroke-dasharray="2 2"' if fork else ''}/>
  <circle cx="{x:.1f}" cy="{ay}" r="5.5" fill="none" stroke="{col}" stroke-width="2" class="nping" style="animation-delay:{(x - x0) / (x1 - x0) * t_comet:.2f}s"/>
  <rect x="{lx0:.1f}" y="{ly - 15}" width="{wlab:.1f}" height="21" rx="4" fill="{C['panel']}" stroke="{col}" stroke-opacity=".6"/>
  <circle cx="{lx0 + 9:.1f}" cy="{ly - 4.5}" r="3" fill="{col}"/>
  <text x="{lx0 + 17:.1f}" y="{ly}" class="repo">{esc(shown)}</text>
</g>""")
    # legend (same language colours as the language matrix)
    lx = 44
    leg = []
    for name, col in list(colors.items()) + [("Other / none", OTHER)]:
        leg.append(f'<rect x="{lx}" y="{H - 34}" width="10" height="10" rx="2.5" fill="{col}"/><text x="{lx + 16}" y="{H - 25}" class="small">{esc(name)}</text>')
        lx += 16 + 7 * len(name) + 22
    body = (f'<defs><linearGradient id="axis" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="{NEON[6]}" stop-opacity=".15"/>'
            f'<stop offset=".5" stop-color="{NEON[1]}"/><stop offset="1" stop-color="{NEON[2]}"/></linearGradient>'
            f'<linearGradient id="trail" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="{NEON[1]}" stop-opacity="0"/>'
            f'<stop offset="1" stop-color="#FFFFFF" stop-opacity=".9"/></linearGradient></defs>' + "\n".join(out) + "".join(leg))
    css = (f".repo {{ font: 12px {MONO}; fill: {C['text']}; }} .pop {{ animation: fade .6s ease both; }}"
           f" .comet {{ animation: tcomet {t_comet}s linear infinite; }} @keyframes tcomet {{ from {{ transform: translateX(0); }} to {{ transform: translateX({x1 - x0}px); }} }}"
           f" .nping {{ transform-box: fill-box; transform-origin: center; animation: nping {t_comet}s ease-out infinite; opacity: 0; }}"
           " @keyframes nping { 0% { transform: scale(1); opacity: 1; } 12% { transform: scale(3.4); opacity: 0; } 100% { opacity: 0; } }"
           " .ping { transform-box: fill-box; transform-origin: center; animation: ping 2.2s ease-out infinite; }"
           " @keyframes ping { from { transform: scale(.5); opacity: .9; } to { transform: scale(2.2); opacity: 0; } }")
    return frame(W, H, "08", "Mission log · repository launches", body, css,
                 "Timeline of repository creation: " + ", ".join(f"{r['name']} {t:%b %Y}" for r, t in zip(items, ts)), NEON[6])


# ═══════════════════════════ comms buttons, quote, footer ═══════════════════════════
SOCIALS = [  # (label, simple-icons slug, url, subtitle)
    ("LinkedIn", "linkedin", "https://www.linkedin.com/in/nitya-prakash-pandey/", "connect · network ↗"),
    ("Kaggle", "kaggle", "https://www.kaggle.com/nityaprakashpandey", "notebooks · competitions ↗"),
    ("Email", "gmail", "mailto:nityaprakashpandey389@gmail.com", "open a channel ↗"),
    ("GitHub", "github", "https://github.com/nitya-prakash-pandey-2005", "{followers} followers · follow ↗"),
]
QUOTES = [
    ("We can only see a short distance ahead, but we can see plenty there that needs to be done.", "Alan Turing"),
    ("The best way to predict the future is to invent it.", "Alan Kay"),
    ("Any sufficiently advanced technology is indistinguishable from magic.", "Arthur C. Clarke"),
    ("Talk is cheap. Show me the code.", "Linus Torvalds"),
    ("Simplicity is prerequisite for reliability.", "Edsger W. Dijkstra"),
    ("Premature optimization is the root of all evil.", "Donald Knuth"),
    ("All models are wrong, but some are useful.", "George E. P. Box"),
    ("AI is the new electricity.", "Andrew Ng"),
    ("Programs must be written for people to read, and only incidentally for machines to execute.", "Harold Abelson"),
    ("Make it work, make it right, make it fast.", "Kent Beck"),
    ("The only way to go fast, is to go well.", "Robert C. Martin"),
    ("Imagination is more important than knowledge.", "Albert Einstein"),
    ("The science of today is the technology of tomorrow.", "Edward Teller"),
    ("Software is eating the world.", "Marc Andreessen"),
    ("The goal is to turn data into information, and information into insight.", "Carly Fiorina"),
    ("Earth is the cradle of humanity, but one cannot live in the cradle forever.", "Konstantin Tsiolkovsky"),
    ("Machine intelligence is the last invention that humanity will ever need to make.", "Nick Bostrom"),
    ("Code is like humor. When you have to explain it, it's bad.", "Cory House"),
]


def render_comms(label: str, slug: str, sub: str, col: str, k: int) -> str:
    """Holographic contact button: logo in a spinning ring, border runner, glint and glitch."""
    icons = json.loads((ROOT / "scripts" / "toolkit_icons.json").read_text(encoding="utf-8"))
    W, H = 216, 64
    path = icons.get(slug, "")
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" role="img" aria-label="{esc(label)}: {esc(sub)}">
<defs>
  <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="{C['bg0']}"/><stop offset="1" stop-color="{mix_hex(C['bg1'], col, .14)}"/></linearGradient>
  <linearGradient id="gl" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#FFFFFF" stop-opacity="0"/><stop offset=".5" stop-color="#FFFFFF" stop-opacity=".22"/><stop offset="1" stop-color="#FFFFFF" stop-opacity="0"/></linearGradient>
  <filter id="glow" x="-50%" y="-50%" width="200%" height="200%"><feGaussianBlur stdDeviation="2.2" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter>
  <clipPath id="c"><rect width="{W}" height="{H}" rx="14"/></clipPath>
</defs>
<style>
  .lb {{ font: 700 13px {MONO}; letter-spacing: 2.4px; fill: {C['text']}; }}
  .sb {{ font: 10.5px {MONO}; fill: {C['muted']}; }}
  .run {{ stroke-dasharray: 16 84; animation: run 4.5s linear infinite; }} @keyframes run {{ from {{ stroke-dashoffset: 100; }} to {{ stroke-dashoffset: 0; }} }}
  .sp {{ transform-box: fill-box; transform-origin: center; animation: sp 8s linear infinite; }} @keyframes sp {{ to {{ transform: rotate(360deg); }} }}
  .gl {{ animation: gls 4s ease-in-out {k * .6:.1f}s infinite; }} @keyframes gls {{ 0% {{ transform: translateX(-80px) skewX(-20deg); }} 40%, 100% {{ transform: translateX({W + 40}px) skewX(-20deg); }} }}
  .pl {{ animation: pl 2.4s ease-in-out infinite; }} @keyframes pl {{ 50% {{ opacity: .55; }} }}
  .gt {{ animation: gt 7s steps(1, end) {1.5 + k:.1f}s infinite; }} @keyframes gt {{ 0%, 95%, 100% {{ opacity: 1; transform: none; }} 96% {{ opacity: .3; transform: translateX(2px); }} 97% {{ opacity: 1; transform: translateX(-2px); }} }}
  @media (prefers-reduced-motion: reduce) {{ * {{ animation: none !important; }} .gl {{ display: none; }} }}
</style>
<g clip-path="url(#c)">
  <rect width="{W}" height="{H}" fill="url(#bg)"/>
  <rect x="0" y="0" width="50" height="{H}" fill="url(#gl)" class="gl"/>
</g>
<rect x="1" y="1" width="{W - 2}" height="{H - 2}" rx="13" fill="none" stroke="{col}" stroke-opacity=".45"/>
<rect x="1" y="1" width="{W - 2}" height="{H - 2}" rx="13" fill="none" stroke="{col}" stroke-width="2" pathLength="100" class="run" filter="url(#glow)"/>
<circle cx="32" cy="32" r="19" fill="none" stroke="{col}" stroke-opacity=".6" stroke-dasharray="3 4" class="sp"/>
<circle cx="32" cy="32" r="14" fill="{col}" fill-opacity=".14" class="pl"/>
<g transform="translate(23,23) scale(.75)"><path d="{path}" fill="{col}" filter="url(#glow)"/></g>
<text x="62" y="29" class="lb gt">{esc(label.upper())}</text>
<text x="62" y="46" class="sb">{esc(sub)}</text>
</svg>
"""


def render_quote() -> str:
    """Quote of the day in a CRT terminal: rotates daily, types itself in, scanlines and flicker."""
    W, H = 900, 204
    day = NOW.astimezone(TIMEZONE).date()
    text, who = QUOTES[day.toordinal() % len(QUOTES)]
    lines = _wrap(f"“{text}”", 70, 2)
    out = []
    out.append(f'<rect x="28" y="66" width="{W - 56}" height="{H - 88}" rx="10" fill="{C["bg0"]}" stroke="{C["line"]}"/>')
    out.append(f'<rect x="28" y="66" width="{W - 56}" height="22" rx="10" fill="{C["panel"]}"/>'
               f'<rect x="28" y="78" width="{W - 56}" height="10" fill="{C["panel"]}"/>')
    for k, c in enumerate(("#FB7185", "#FBBF24", "#A3E635")):
        out.append(f'<circle cx="{46 + k * 16}" cy="77" r="4.5" fill="{c}"/>')
    out.append(f'<text x="{W / 2}" y="81" class="small" text-anchor="middle">transmission://quote-of-the-day · {day:%d %b %Y}</text>')
    out.append(f'<g class="type" style="animation-delay:.3s"><text x="46" y="110" class="cmd"><tspan style="fill:{NEON[3]}">$</tspan> fortune --sci-fi --daily</text></g>')
    for k, ln in enumerate(lines):
        out.append(f'<g class="type" style="animation-delay:{1 + k * .9:.1f}s"><text x="46" y="{136 + k * 24}" class="qt">{esc(ln)}</text></g>')
    ay = 136 + len(lines) * 24 - 24
    out.append(f'<g class="type" style="animation-delay:{1 + len(lines) * .9:.1f}s"><text x="{W - 46}" y="{ay}" class="au" text-anchor="end">— {esc(who)}</text></g>')
    out.append(f'<rect x="{W - 44}" y="{ay - 13}" width="8" height="16" fill="{NEON[3]}" class="cur"/>')
    out.append(f'<rect x="28" y="88" width="{W - 56}" height="{H - 110}" fill="url(#scan)" pointer-events="none"/>')
    out.append(f'<rect x="28" y="88" width="{W - 56}" height="30" fill="url(#crt)" class="crt"/>')
    defs = (f'<pattern id="scan" width="4" height="4" patternUnits="userSpaceOnUse"><rect width="4" height="1.4" fill="#FFFFFF" fill-opacity=".035"/></pattern>'
            f'<linearGradient id="crt" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="{NEON[3]}" stop-opacity="0"/>'
            f'<stop offset="1" stop-color="{NEON[3]}" stop-opacity=".1"/></linearGradient>')
    css = (f".cmd {{ font: 13px {MONO}; fill: {C['soft']}; }} .qt {{ font-size: 19px; font-weight: 600; fill: {C['text']}; font-style: italic; }}"
           f" .au {{ font: 700 13px {MONO}; fill: {NEON[3]}; letter-spacing: 1px; }}"
           " .type { animation: type 1.1s cubic-bezier(.3,0,.2,1) both; }"
           " @keyframes type { from { clip-path: inset(0 100% 0 0); } to { clip-path: inset(0 0 0 0); } }"
           " .cur { animation: blink 1s steps(2, start) infinite; }"
           f" .crt {{ animation: crt 3.5s linear infinite; }} @keyframes crt {{ from {{ transform: translateY(0); }} to {{ transform: translateY({H - 120}px); }} }}")
    return frame(W, H, "10", "Transmission of the day", "<defs>" + defs + "</defs>\n" + "\n".join(out), css,
                 f"Quote of the day: {text} — {who}", NEON[3])


def render_footer() -> str:
    """Sign-off: layered neon waves, Matrix-style digital rain, and an END OF TRANSMISSION banner."""
    W, H = 900, 210
    out, defs = [], []
    glyphs = "01ΔΣΛΞΠΦΨΩλμπ0123456789ABCDEF<>/*+=#"
    cols = 30
    for c in range(cols):
        x = 16 + c * (W - 32) / (cols - 1)
        chars = "".join(glyphs[(c * 7 + j * 13) % len(glyphs)] for j in range(12))
        tsp = "".join(f'<tspan x="{x:.1f}" dy="15">{esc(ch)}</tspan>' for ch in chars)
        col = NEON[c % len(NEON)]
        out.append(f'<text x="{x:.1f}" y="-190" class="rain" style="fill:{col};animation-duration:{4 + (c * 37) % 50 / 10:.1f}s;'
                   f'animation-delay:-{(c * 53) % 60 / 10:.1f}s">{tsp}</text>')
    rain = f'<g mask="url(#fadem)">{"".join(out)}</g>'
    rain += f'<ellipse cx="{W / 2}" cy="100" rx="380" ry="44" fill="url(#backd)"/>'
    defs.append(f'<radialGradient id="backd"><stop offset="0" stop-color="{C["bg0"]}" stop-opacity=".92"/><stop offset="1" stop-color="{C["bg0"]}" stop-opacity="0"/></radialGradient>')
    defs.append('<linearGradient id="fadeg" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#fff" stop-opacity="0"/>'
                '<stop offset=".35" stop-color="#fff" stop-opacity=".55"/><stop offset=".75" stop-color="#fff" stop-opacity=".25"/><stop offset="1" stop-color="#fff" stop-opacity="0"/></linearGradient>'
                f'<mask id="fadem"><rect y="58" width="{W}" height="{H - 58}" fill="url(#fadeg)"/></mask>')

    def wave(amp: float, wl: float, y0: float) -> str:
        pts = []
        x = -wl
        while x <= W + wl * 2:
            pts.append(f"{x:.1f},{y0 + amp * math.sin(2 * math.pi * x / wl):.1f}")
            x += wl / 16
        return f"M{pts[0]} L" + " L".join(pts[1:]) + f" L{W + wl * 2},{H} L{-wl},{H} Z"
    waves = []
    for k, (amp, wl, y0, col, op, dur) in enumerate(((10, 300, 150, NEON[0], .25, 9), (13, 380, 160, NEON[1], .3, 12), (8, 240, 172, NEON[2], .45, 7))):
        defs.append(f'<linearGradient id="wv{k}" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="{col}" stop-opacity="{op}"/>'
                    f'<stop offset="1" stop-color="{col}" stop-opacity="0"/></linearGradient>')
        waves.append(f'<path d="{wave(amp, wl, y0)}" fill="url(#wv{k})" class="wave" style="--wl:{wl}px;animation-duration:{dur}s"/>')
        waves.append(f'<path d="{wave(amp, wl, y0).split(" Z")[0].rsplit(" L", 2)[0]}" fill="none" stroke="{col}" stroke-width="1.4" stroke-opacity=".8"'
                     f' class="wave" style="--wl:{wl}px;animation-duration:{dur}s"/>')
    txt = (f'<text x="{W / 2}" y="92" text-anchor="middle" class="end g1" style="fill:{NEON[0]}">END OF TRANSMISSION</text>'
           f'<text x="{W / 2}" y="92" text-anchor="middle" class="end g2" style="fill:{NEON[2]}">END OF TRANSMISSION</text>'
           f'<text x="{W / 2}" y="92" text-anchor="middle" class="end">END OF TRANSMISSION</text>'
           f'<text x="{W / 2}" y="118" text-anchor="middle" class="bye">thanks for visiting · auto-synced every hour · see you in orbit</text>'
           f'<circle cx="{W / 2 - 172}" cy="114" r="3.5" fill="{NEON[3]}" class="blink"/>')
    css = (f".rain {{ font: 13px {MONO}; animation: rain 6s linear infinite; }} @keyframes rain {{ to {{ transform: translateY({H + 200}px); }} }}"
           " .wave { animation: wave 9s linear infinite; } @keyframes wave { to { transform: translateX(calc(-1 * var(--wl))); } }"
           f" .end {{ font: 800 28px {MONO}; letter-spacing: 6px; fill: {C['text']}; }} .bye {{ font: 12px {MONO}; fill: {C['soft']}; letter-spacing: 1px; }}"
           " .g1, .g2 { opacity: 0; animation: gl 5s steps(1, end) 1s infinite; } .g2 { animation-name: gl2; }"
           " @keyframes gl { 0%, 90%, 100% { opacity: 0; transform: none; } 91% { opacity: .8; transform: translate(-4px, 1px); } 94% { opacity: .5; transform: translate(3px, -1px); } }"
           " @keyframes gl2 { 0%, 90%, 100% { opacity: 0; transform: none; } 91% { opacity: .8; transform: translate(4px, -1px); } 94% { opacity: .5; transform: translate(-3px, 1px); } }")
    body = "<defs>" + "".join(defs) + "</defs>\n" + rain + "\n" + "\n".join(waves) + "\n" + txt
    return frame(W, H, "∞", "Signing off", body, css, "End of transmission. Thanks for visiting.", NEON[1])

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
    meta.append(f'<circle cx="{mx0 + 5}" cy="{H - 26}" r="5" fill="{lcol}"/><circle cx="{mx0 + 5}" cy="{H - 26}" r="5" fill="none" stroke="{lcol}" class="lping"/>'
                f'<text x="{mx0 + 15}" y="{H - 22}" class="m">{esc(lang or "—")}</text>')
    mx0 += 24 + 7.2 * len(lang or "—")
    cdefs = ""
    for sym, val in (("★", r["stargazerCount"]), ("⑂", r["forkCount"]), ("◆", f"{len(nodes)} commit{'' if len(nodes) == 1 else 's'}")):
        if sym == "◆":  # commit count rolls up from zero
            meta.append(f'<text x="{mx0}" y="{H - 22}" class="m"><tspan style="fill:{ac}">{sym}</tspan></text>')
            cdefs, mark = count_up("cc", mx0 + 14, H - 22, len(nodes), cls="m", delay=.8, size=11.5, width=110,
                                   suffix=f" commit{'' if len(nodes) == 1 else 's'}")
            meta.append(mark)
            continue
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
  {cdefs}
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
  .cup {{ animation: cup 1.8s cubic-bezier(.3,.7,.2,1) both; }} @keyframes cup {{ from {{ transform: translateY(var(--h)); }} }}
  .run {{ stroke-dasharray: 9 41; animation: run 7s linear infinite; }} @keyframes run {{ from {{ stroke-dashoffset: 50; }} to {{ stroke-dashoffset: 0; }} }}
  .star {{ opacity: .12; animation: star 4s ease-in-out infinite; }} @keyframes star {{ 50% {{ opacity: .7; }} }}
  .comet {{ stroke-dasharray: 10 {length + 30}; opacity: 0; animation: comet 3.2s linear 2.6s infinite both; }}
  @keyframes comet {{ 0% {{ stroke-dashoffset: 10; opacity: 1; }} 100% {{ stroke-dashoffset: -{length}; opacity: 1; }} }}
  .eping {{ transform-box: fill-box; transform-origin: center; opacity: 0; animation: eping 2.2s ease-out 2.6s infinite; }}
  @keyframes eping {{ 0% {{ transform: scale(1); opacity: .9; }} 100% {{ transform: scale(3.2); opacity: 0; }} }}
  .lping {{ transform-box: fill-box; transform-origin: center; opacity: 0; animation: eping 2.6s ease-out 1.5s infinite; }}
  .glitch {{ animation: in .8s cubic-bezier(.2,.8,.2,1) .15s both, glitch 8s steps(1, end) {2 + i * .9:.1f}s infinite; }}
  @keyframes glitch {{ 0%, 95%, 100% {{ opacity: 1; transform: none; }} 96% {{ opacity: .35; transform: translateX(3px); }} 97% {{ opacity: 1; transform: translateX(-2px); }} 98% {{ opacity: .6; transform: none; }} }}
  @media (prefers-reduced-motion: reduce) {{ * {{ animation: none !important; }} .sweep {{ display: none; }} }}
</style>
<g clip-path="url(#card)">
  <rect width="{W}" height="{H}" fill="url(#bg)"/>
  <rect width="{W}" height="{H}" fill="url(#grid)"/>
  <rect x="0" y="0" width="140" height="{H}" fill="url(#scan)" class="sweep"/>
  <rect x="0" y="0" width="{W}" height="3" fill="{ac}" opacity=".85"/>
</g>
<rect x=".5" y=".5" width="{W - 1}" height="{H - 1}" rx="13.5" fill="none" stroke="{ac}" stroke-opacity=".35"/>
<g>{''.join(f'<circle cx="{(k * 97.3 + i * 31) % (W - 30) + 15:.1f}" cy="{(k * 41.9 + i * 17) % (H - 30) + 15:.1f}" r="{.6 + (k % 3) * .3:.1f}" fill="#FFFFFF" class="star" style="animation-delay:{(k * .7 + i * .3) % 4:.1f}s"/>' for k in range(14))}</g>
<rect x="1.5" y="1.5" width="{W - 3}" height="{H - 3}" rx="13" fill="none" stroke="{ac}" stroke-width="2.2" pathLength="100" class="run"{GLOW}/>
<rect x="1.5" y="1.5" width="{W - 3}" height="{H - 3}" rx="13" fill="none" stroke="#FFFFFF" stroke-width="1.6" pathLength="100" class="run" style="animation-delay:-3.5s"/>
<g fill="none" stroke="{ac}" stroke-width="1.5" stroke-opacity=".85">{corners}</g>
<g class="in">
  <text x="22" y="34" class="tag">P-{i + 1:02d} · {tag}</text>
  <circle cx="{led_x:.1f}" cy="30" r="4" fill="{led}" class="led"/>
  <circle cx="{led_x:.1f}" cy="30" r="4" fill="none" stroke="{led}" class="ring"/>
  <text x="{W - 22}" y="34" class="st" text-anchor="end">{esc(status)}</text>
</g>
<text x="22" y="66" class="nm glitch">{esc(name)}</text>
<g class="in d2">{''.join(f'<text x="22" y="{88 + k * 17}" class="ds">{esc(t)}</text>' for k, t in enumerate(desc))}</g>
<g class="in d3">{''.join(meta)}</g>
<text x="{sx1}" y="{sy0 - 6}" class="st" text-anchor="end" style="font-size:9.5px">COMMITS · 16 WK</text>
<line x1="{sx0}" y1="{sy1}" x2="{sx1}" y2="{sy1}" stroke="{C['line']}"/>
<path d="{line} L{sx1},{sy1} L{sx0},{sy1} Z" fill="url(#sfill)" class="fade"/>
<path d="{line}" fill="none" stroke="url(#spark)" stroke-width="2" stroke-linecap="round" class="draw" filter="url(#glow)"/>
<path d="{line}" fill="none" stroke="#FFFFFF" stroke-width="2.6" stroke-linecap="round" class="comet" filter="url(#glow)"/>
<circle cx="{pts[-1][0]:.1f}" cy="{pts[-1][1]:.1f}" r="3" fill="#FFFFFF" class="fade"/>
<circle cx="{pts[-1][0]:.1f}" cy="{pts[-1][1]:.1f}" r="3.5" fill="none" stroke="{ac}" stroke-width="1.5" class="eping"/>
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
    """Event log on a starfield: glowing spine with a travelling pulse, colour-coded nodes with
    spinning rings, packets flowing into each row, rows that type themselves in, a highlight bar
    stepping through the log, commit-intensity meters, and a live terminal footer."""
    W = 900
    row, top = 50, 92
    n = len(items)
    H = top + max(1, n) * row + 44
    sx = 150
    tx = 178
    stars = "".join(
        f'<circle cx="{(k * 131.7) % (W - 40) + 20:.1f}" cy="{58 + (k * 47.3) % (H - 76):.1f}" r="{.6 + (k % 3) * .35:.2f}" fill="#FFFFFF" class="star"'
        f' style="animation-delay:{(k * .53) % 5:.2f}s;animation-duration:{3 + (k % 4)}s"/>' for k in range(44))
    out, defs = [f"<g>{stars}</g>"], []
    if n:  # highlight bar that steps down the log, one entry at a time
        out.append(f'<rect x="{tx - 14}" y="{top - 22}" width="{W - tx - 14}" height="44" rx="8" fill="url(#hl)" class="scanrow"/>')
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
  <circle cx="{sx}" cy="{y}" r="17" fill="none" stroke="{col}" stroke-opacity=".55" stroke-dasharray="3 4" class="{'spin' if i % 2 else 'spin-r'}"/>
  <circle cx="{sx}" cy="{y}" r="12" fill="{C['bg0']}" stroke="{col}" stroke-width="1.8"{GLOW}/>
  <text x="{sx}" y="{y + 4.5}" text-anchor="middle" class="gl" style="fill:{col}">{glyph}</text>
</g>""")
        out.append(f'<line x1="{sx + 18}" y1="{y}" x2="{tx - 8}" y2="{y}" stroke="{col}" stroke-opacity=".35"/>'
                   f'<circle cx="{sx + 18}" cy="{y}" r="2.2" fill="#FFFFFF" class="pkt" style="animation-delay:{d + .8 + i * .2:.2f}s"/>')
        if i == 0:
            out.append(f'<circle cx="{sx}" cy="{y}" r="12" fill="none" stroke="{col}" class="ping"/>')
        live = f'<tspan dx="12" class="live glitch" style="fill:{col}">● LATEST</tspan>' if i == 0 else ""
        out.append(f"""<g class="type" style="animation-delay:{d:.2f}s">
  <text x="{tx}" y="{y - 1}"><tspan class="tg" style="fill:{col}">{tag}</tspan><tspan dx="10" class="rp">{esc(name)}</tspan>{f'<tspan dx="10" class="small" style="fill:{C["soft"]}">{esc(head)}</tspan>' if head else ''}{live}</text>
  <text x="{tx}" y="{y + 17}" class="dt">{esc(detail)}</text>
</g>""")
        if it["kind"] == "commit":  # intensity meter: one lit bar per commit, up to ten
            lit = min(10, it["n"])
            bars = ""
            for k in range(10):
                anim = f' class="lit" style="animation-delay:{d + .9 + k * .07:.2f}s"' if k < lit else ""
                bars += (f'<rect x="{W - 118 + k * 9}" y="{y - 6 - k * 1.2:.1f}" width="6" height="{10 + k * 1.2:.1f}" rx="1.5"'
                         f' fill="{col if k < lit else C["line"]}"{anim}/>')
            out.append(f'<g class="type" style="animation-delay:{d + .5:.2f}s">{bars}</g>')
    if not n:
        out.append(f'<text x="{tx}" y="{top + 4}" class="note">No public activity in the last 90 days.</text>')
    ty = H - 30
    msg = "listening for new signals"
    out.append(f'<g class="type" style="animation-delay:{.6 + n * .32:.2f}s"><text x="{tx - 32}" y="{ty}" class="term">'
               f'<tspan style="fill:{NEON[3]}">&gt;</tspan> {msg}<tspan class="d1">.</tspan><tspan class="d2">.</tspan><tspan class="d3">.</tspan></text>'
               f'<rect x="{tx - 32 + 7.9 * (len(msg) + 5) + 4:.1f}" y="{ty - 12}" width="8" height="15" fill="{NEON[3]}" class="cur"/></g>')
    out.append(f'<text x="{W - 28}" y="{ty}" class="small" text-anchor="end">NEXT SYNC <tspan style="fill:{NEON[3]}">≤ 60 MIN</tspan></text>')
    defs.append(f'<linearGradient id="spine" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="{NEON[0]}"/>'
                f'<stop offset=".5" stop-color="{NEON[1]}"/><stop offset="1" stop-color="{NEON[2]}"/></linearGradient>')
    defs.append(f'<linearGradient id="hl" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="{NEON[2]}" stop-opacity=".16"/>'
                f'<stop offset=".6" stop-color="{NEON[1]}" stop-opacity=".06"/><stop offset="1" stop-color="{NEON[1]}" stop-opacity="0"/></linearGradient>')
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
           " @keyframes type { from { clip-path: inset(0 100% 0 0); } to { clip-path: inset(0 0 0 0); } }"
           " .star { opacity: .12; animation: star 4s ease-in-out infinite; } @keyframes star { 50% { opacity: .7; } }"
           f" .scanrow {{ animation: scanrow {max(1, n) * 1.2:.1f}s steps({max(1, n)}, end) 3s infinite both; }}"
           f" @keyframes scanrow {{ from {{ transform: translateY(0); }} to {{ transform: translateY({max(1, n) * row}px); }} }}"
           f" .pkt {{ opacity: 0; animation: pkt 2.4s ease-in infinite; }}"
           f" @keyframes pkt {{ 0% {{ opacity: 0; transform: translateX(0); }} 15% {{ opacity: 1; }} 70% {{ opacity: 1; transform: translateX({tx - sx - 26}px); }} 100% {{ opacity: 0; transform: translateX({tx - sx - 26}px); }} }}"
           " .lit { animation: lit .35s ease-out both; } @keyframes lit { from { opacity: 0; transform: translateY(4px); } }"
           " .glitch { animation: glitch 6s steps(1, end) 2s infinite; }"
           " @keyframes glitch { 0%, 94%, 100% { opacity: 1; } 95% { opacity: .2; } 96% { opacity: 1; } 97% { opacity: .4; } }"
           " .d1, .d2, .d3 { animation: dots 1.5s steps(1, end) infinite; } .d2 { animation-delay: .25s; } .d3 { animation-delay: .5s; }"
           " @keyframes dots { 0%, 100% { opacity: .15; } 30%, 70% { opacity: 1; } }")
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
        "about.svg": render_about(),
        "hud.svg": render_hud(user, repos, days, cur, longest),
        "achievements.svg": render_achievements(),
        "toolkit.svg": render_toolkit([name for name, _, _ in mix]),
        "skyline.svg": render_skyline(days),
        "activity.svg": render_activity(days),
        "clock.svg": render_clock(commit_hours(user)),
        "languages.svg": render_languages(mix),
        "timeline.svg": render_timeline([r for r in user["repositories"]["nodes"] if r["name"] not in EXCLUDE_REPOS], colors),
    }
    for name, svg in cards.items():
        (OUT / name).write_text(svg, encoding="utf-8")
    cdir = OUT / "comms"
    cdir.mkdir(exist_ok=True)
    for k, (label, slug, _, sub) in enumerate(SOCIALS):
        sub = sub.format(followers=fmt(user["followers"]["totalCount"]))
        (cdir / f"{slug}.svg").write_text(render_comms(label, slug, sub, [NEON[0], NEON[6], NEON[2], NEON[1]][k % 4], k), encoding="utf-8")
    (OUT / "quote.svg").write_text(render_quote(), encoding="utf-8")
    (OUT / "footer.svg").write_text(render_footer(), encoding="utf-8")
    items = activity_items(fetch_events(), user)
    (OUT / "feed.svg").write_text(render_feed(items), encoding="utf-8")
    pick = pick_projects(repos)
    pdir = OUT / "projects"
    pdir.mkdir(exist_ok=True)
    for old in pdir.glob("*.svg"):
        old.unlink()
    for j, r in enumerate(pick):
        (pdir / f"{j + 1:02d}.svg").write_text(render_project_card(r, j, colors), encoding="utf-8")
    try:  # the 3D header's outer orbit follows the same live pick as "Latest work"
        import build_header
        (ROOT / "assets" / "header.svg").write_text(build_header.build([r["name"] for r in pick]), encoding="utf-8")
    except Exception as err:  # never let the header break the data refresh
        print(f"warning: header not rebuilt ({err})", file=sys.stderr)
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
