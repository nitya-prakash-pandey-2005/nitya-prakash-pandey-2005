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
# Sequential cyan ramp for magnitude (skyline levels 1-4), validated as an ordinal ramp on dark.
SEQ = ["#075985", "#0284C7", "#38BDF8", "#BAE6FD"]
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
          history(first: 100) { nodes { authoredDate author { user { login } } } }
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


def tick_ring(cx: float, cy: float, r: float, n: int, major: int, length: float = 5, cls: str = "") -> str:
    ticks = []
    for i in range(n):
        ln = length * (1.8 if i % major == 0 else 1)
        (x0, y0), (x1, y1) = polar(cx, cy, r, 360 * i / n), polar(cx, cy, r + ln, 360 * i / n)
        ticks.append(f"M{x0:.1f},{y0:.1f}L{x1:.1f},{y1:.1f}")
    return f'<path d="{"".join(ticks)}" stroke="{C["dim"]}" stroke-width="1" class="{cls}"/>'


def frame(w: int, h: int, code: str, title: str, body: str, extra_css: str = "", label: str = "") -> str:
    """Shared HUD panel: navy glass, faint grid, corner brackets, a slow scan line and a header rule."""
    stamp = NOW.astimezone(TIMEZONE).strftime("%d %b %Y").upper()
    b = 16  # corner bracket arm
    corners = "".join(
        f'<path d="M{x},{y + sy * b} L{x},{y} L{x + sx * b},{y}"/>'
        for x, y, sx, sy in ((10, 10, 1, 1), (w - 10, 10, -1, 1), (10, h - 10, 1, -1), (w - 10, h - 10, -1, -1))
    )
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}" role="img" aria-label="{esc(label or title)}">
<defs>
  <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="{C['bg0']}"/><stop offset="1" stop-color="{C['bg1']}"/></linearGradient>
  <pattern id="grid" width="24" height="24" patternUnits="userSpaceOnUse"><path d="M24 0H0V24" fill="none" stroke="#FFFFFF" stroke-opacity=".03"/></pattern>
  <linearGradient id="rule" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="{C['cyan']}" stop-opacity=".7"/><stop offset=".45" stop-color="{C['cyan']}" stop-opacity=".12"/><stop offset="1" stop-color="{C['cyan']}" stop-opacity="0"/></linearGradient>
  <linearGradient id="scanline" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="{C['cyan']}" stop-opacity="0"/><stop offset="1" stop-color="{C['cyan']}" stop-opacity=".08"/></linearGradient>
  <filter id="glow" x="-50%" y="-50%" width="200%" height="200%"><feGaussianBlur stdDeviation="3" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter>
  <clipPath id="card"><rect width="{w}" height="{h}" rx="16"/></clipPath>
</defs>
<style>
  text {{ font-family: {FONT}; }}
  .t {{ font: 600 13px {MONO}; letter-spacing: 2.6px; fill: {C['text']}; }}
  .code {{ fill: {C['cyan']}; }}
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
<g fill="none" stroke="{C['cyan']}" stroke-opacity=".75" stroke-width="1.5">{corners}</g>
<circle cx="31" cy="34" r="3.5" fill="{C['cyan']}" class="blink"/>
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
        goal = next_milestone(value)
        frac = min(1.0, value / goal) if goal else 0
        ex, ey = polar(cx, cy, R, 360 * frac)
        delay = 0.25 + i * 0.12
        out.append(f"""<g>
  {tick_ring(cx, cy, R + 12, 60, 5, 3.5, "spin" if i % 2 == 0 else "spin-r")}
  <circle cx="{cx:.1f}" cy="{cy}" r="{R + 8}" fill="none" stroke="{C['line']}" stroke-dasharray="2 5"/>
  <circle cx="{cx:.1f}" cy="{cy}" r="{R}" fill="none" stroke="{C['line']}" stroke-width="5"/>
  <circle cx="{cx:.1f}" cy="{cy}" r="{R}" fill="none" stroke="{C['cyan']}" stroke-width="5" stroke-linecap="round"
          stroke-dasharray="{circ:.1f}" style="stroke-dashoffset:{circ * (1 - frac):.1f}; animation-delay:{delay:.2f}s"
          transform="rotate(-90 {cx:.1f} {cy})" class="arc" filter="url(#glow)" opacity="{1 if frac else 0}"/>
  <circle cx="{ex:.1f}" cy="{ey:.1f}" r="3.2" fill="{C['text']}" class="tip" style="animation-delay:{delay + 1.3:.2f}s" opacity="{1 if frac else 0}"/>
  <text x="{cx:.1f}" y="{cy + 8}" class="val" text-anchor="middle">{fmt(value)}</text>
  <text x="{cx:.1f}" y="{cy + 86}" class="lbl" text-anchor="middle" style="fill:{C['text']}">{label}</text>
  <text x="{cx:.1f}" y="{cy + 103}" class="small" text-anchor="middle">{sub}</text>
  <text x="{cx:.1f}" y="{cy + 119}" class="small" text-anchor="middle" style="fill:{C['dim']}">next ▸ {fmt(goal)}</text>
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
                 "GitHub telemetry: " + ", ".join(f"{g[0].lower()} {g[2]}" for g in gauges))


def render_achievements() -> str:
    W, H = 900, 270
    out = []
    n = len(ACHIEVEMENTS)
    step = (W - 56) / n
    for i, (pre, rank, l1, l2, proj) in enumerate(ACHIEVEMENTS):
        cx, cy, r = 28 + step * (i + .5), 124, 50
        hexp = " ".join(f"{polar(cx, cy, r, a)[0]:.1f},{polar(cx, cy, r, a)[1]:.1f}" for a in range(0, 360, 60))
        hexo = " ".join(f"{polar(cx, cy, r + 9, a)[0]:.1f},{polar(cx, cy, r + 9, a)[1]:.1f}" for a in range(0, 360, 60))
        d = 0.2 + i * 0.15
        big = 30 if len(rank) <= 2 else 24
        out.append(f"""<g class="pop" style="animation-delay:{d:.2f}s">
  <circle cx="{cx:.1f}" cy="{cy}" r="{r + 20}" fill="none" stroke="{C['cyan']}" stroke-opacity=".35" stroke-dasharray="1 7" class="{'spin' if i % 2 else 'spin-r'}"/>
  <polygon points="{hexo}" fill="none" stroke="{C['line']}"/>
  <polygon points="{hexp}" fill="{C['cyan']}" fill-opacity=".07" stroke="{C['cyan']}" stroke-width="1.6" filter="url(#glow)"/>
  <text x="{cx:.1f}" y="{cy - 14}" class="small" text-anchor="middle" style="letter-spacing:2px">{pre}</text>
  <text x="{cx:.1f}" y="{cy + 18}" text-anchor="middle" style="font-size:{big}px;font-weight:750;fill:{C['text']}">{'#' if pre == 'RANK' else ''}{esc(rank)}</text>
  <text x="{cx:.1f}" y="{cy + 94}" class="note strong" text-anchor="middle">{esc(l1)}</text>
  <text x="{cx:.1f}" y="{cy + 111}" class="note" text-anchor="middle">{esc(l2)}</text>
  <text x="{cx:.1f}" y="{cy + 129}" class="small" text-anchor="middle" style="fill:{C['cyan']}">{esc(proj.upper())}</text>
</g>""")
    css = (".pop { animation: pop .8s cubic-bezier(.2,.8,.2,1.2) both; transform-box: fill-box; transform-origin: center; }"
           " @keyframes pop { from { opacity: 0; transform: scale(.85); } }")
    label = "Results: " + "; ".join(f"{p.lower()} {r}, {a} {b}" for p, r, a, b, _ in ACHIEVEMENTS)
    return frame(W, H, "02", "Mission record", "\n".join(out), css, label)


def render_skyline(days: list[tuple[dt.date, int]]) -> str:
    """Isometric city of the contribution calendar: one tower per day, height and colour by count."""
    W, H = 900, 390
    if not days:
        return frame(W, H, "03", "Contribution skyline", '<text x="44" y="120" class="note">No contribution data yet.</text>')
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

    body = f"""<defs><linearGradient id="beam" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="{C['cyan']}" stop-opacity="0"/><stop offset="1" stop-color="{C['cyan']}" stop-opacity=".35"/></linearGradient></defs>
<g fill="{C['tile']}" stroke="{C['line']}" stroke-width=".6">{''.join(floor)}</g>
<polygon points="{beam}" fill="url(#beam)" class="beam"/>
{''.join(towers)}
{''.join(months)}
<line x1="720" y1="74" x2="720" y2="{H - 30}" stroke="{C['line']}"/>
{''.join(side)}"""
    css = (".rise { animation: rise .9s cubic-bezier(.2,.8,.2,1) both; }"
           " @keyframes rise { from { opacity: 0; transform: translateY(18px); } }"
           f" .beam {{ animation: beam 6s linear infinite; }} @keyframes beam {{ from {{ transform: translate(0,0); }} to {{ transform: translate({ex:.1f}px,{ey:.1f}px); }} }}")
    return frame(W, H, "03", "Contribution skyline · 12 months", body, css,
                 f"Isometric contribution calendar: {total} contributions over {active} active days")


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
        g.append(f'<line x1="{px:.1f}" y1="{py:.1f}" x2="{px:.1f}" y2="{gy1}" stroke="{C["cyan"]}" stroke-opacity=".35" class="pop"/>')
        g.append(f'<circle cx="{px:.1f}" cy="{py:.1f}" r="9" fill="none" stroke="{C["cyan"]}" stroke-opacity=".5" class="ping"/>')
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
        bars.append(f'<rect x="{x}" y="{by1 - h:.1f}" width="{bw}" height="{h:.1f}" rx="4" fill="{C["cyan"] if hot else "#33466E"}" class="grow"/>')
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
  <linearGradient id="stroke" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="{C['cyan']}" stop-opacity=".55"/><stop offset="1" stop-color="{C['cyan']}"/></linearGradient>
  <linearGradient id="fill" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="{C['cyan']}" stop-opacity=".28"/><stop offset="1" stop-color="{C['cyan']}" stop-opacity="0"/></linearGradient>
</defs>
<text x="{gx0}" y="72" class="lbl">WEEKLY CONTRIBUTIONS</text>
<text x="{bx0}" y="72" class="lbl">BY WEEKDAY</text>
{''.join(g)}
<path d="{area}" fill="url(#fill)" class="area"/>
<path d="{line}" fill="none" stroke="url(#stroke)" stroke-width="2" stroke-linecap="round" class="line" filter="url(#glow)"/>
<line x1="608" y1="64" x2="608" y2="{by1+26}" stroke="{C['line']}"/>
{''.join(bars)}
{''.join(notes)}"""
    return frame(W, H, "04", "Signal activity", body, css,
                 f"Weekly contributions over 12 months, peak {peak}; most active on {best or 'no day yet'}")


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
    for h, n in enumerate(hours):
        if not n:
            continue
        rr = r0 + 4 + (r1 - r0 - 4) * math.sqrt(n / mx)
        hot = h == peak_h
        out.append(f'<path d="{sector(cx, cy, r0 + 2, rr, h * 15 + 1.6, h * 15 + 13.4)}" fill="{C["cyan"] if hot else "#1E7FB8"}"'
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
        out.append(f'<text x="{x0}" y="{y}" class="lbl" style="fill:{C["text"] if hot else C["muted"]}">{name} <tspan style="fill:{C["dim"]}">{span}</tspan></text>')
        out.append(f'<text x="{W - 40}" y="{y}" class="small" text-anchor="end" style="fill:{C["text"]}">{pct:.0f}%  <tspan style="fill:{C["muted"]}">{n}</tspan></text>')
        out.append(f'<rect x="{x0}" y="{y + 10}" width="{W - 40 - x0}" height="8" rx="4" fill="{C["panel"]}" stroke="{C["line"]}" stroke-width=".6"/>')
        out.append(f'<rect x="{x0}" y="{y + 10}" width="{max(4, (W - 40 - x0) * n / bmx):.1f}" height="8" rx="4" fill="{C["cyan"] if hot else "#33466E"}" class="grow" style="animation-delay:{.5 + i * .12:.2f}s"/>')
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
    return frame(W, H, "05", "Commit clock", body, css,
                 f"Commits by hour of day (IST): {total} commits, busiest hour {peak_h:02d}:00" if total else "Commit clock")


def render_languages(mix: list[tuple[str, str, float]]) -> str:
    W, H = 900, 320
    cx, cy, ro, ri = 196, 192, 94, 68
    out = [
        f'<circle cx="{cx}" cy="{cy}" r="{ro + 24}" fill="none" stroke="{C["cyan"]}" stroke-opacity=".35" stroke-dasharray="1 6" class="spin"/>',
        tick_ring(cx, cy, ro + 8, 72, 6, 3, "spin-r"),
        f'<circle cx="{cx}" cy="{cy}" r="{(ro + ri) / 2}" fill="none" stroke="{C["panel"]}" stroke-width="{ro - ri}"/>',
        f'<g class="orbit"><circle cx="{cx}" cy="{cy - ro - 24}" r="3" fill="{C["cyan"]}" filter="url(#glow)"/></g>',
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
    return frame(W, H, "06", "Language matrix", body, css,
                 "Languages: " + ", ".join(f"{n} {p:.1f}%" for n, _, p in mix))


def render_timeline(repos: list[dict], colors: dict[str, str]) -> str:
    """Mission log: every public repo placed on a time axis by creation date, labels stacked in lanes."""
    W, H = 900, 360
    x0, x1, ay = 44, W - 70, 184
    items = sorted((r for r in repos if r.get("createdAt")), key=lambda r: r["createdAt"])
    if not items:
        return frame(W, H, "07", "Mission log", '<text x="44" y="120" class="note">Repositories appear here once public.</text>')
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
    body = (f'<defs><linearGradient id="axis" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="{C["cyan"]}" stop-opacity=".1"/>'
            f'<stop offset="1" stop-color="{C["cyan"]}"/></linearGradient></defs>' + "\n".join(out) + "".join(leg))
    css = (f".repo {{ font: 12px {MONO}; fill: {C['text']}; }} .pop {{ animation: fade .6s ease both; }}"
           " .ping { transform-box: fill-box; transform-origin: center; animation: ping 2.2s ease-out infinite; }"
           " @keyframes ping { from { transform: scale(.5); opacity: .9; } to { transform: scale(2.2); opacity: 0; } }")
    return frame(W, H, "07", "Mission log · repository launches", body, css,
                 "Timeline of repository creation: " + ", ".join(f"{r['name']} {t:%b %Y}" for r, t in zip(items, ts)))


# ═══════════════════════════════ README blocks ═══════════════════════════════
def ago(ts: str) -> str:
    t = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    s = (NOW - t).total_seconds()
    for unit, size in (("year", 31536000), ("month", 2592000), ("week", 604800), ("day", 86400), ("hour", 3600), ("minute", 60)):
        if s >= size:
            n = int(s // size)
            return f"{n} {unit}{'s' if n > 1 else ''} ago"
    return "just now"


def shield(text: str, color: str) -> str:
    t = text.replace("-", "--").replace("_", "__")
    return f"https://img.shields.io/badge/{urllib.parse.quote(t)}-{color.lstrip('#')}?style=flat-square"


def projects_block(repos: list[dict]) -> str:
    pool = sorted((r for r in repos if not r["isArchived"]), key=lambda r: r["pushedAt"], reverse=True)
    feat = [r for name in FEATURED for r in pool if r["name"] == name]
    rest = [r for r in pool if r not in feat]
    pick = (feat + rest)[:PROJECT_COUNT]
    if not pick:
        return "<p><i>Projects appear here automatically once repositories are public.</i></p>"
    cells = []
    for r in pick:
        lang = r["primaryLanguage"]
        badges = []
        if lang:
            badges.append(f'<img alt="{esc(lang["name"])}" src="{shield(lang["name"], lang["color"] or "8A97B4")}"/>')
        if r["stargazerCount"]:
            badges.append(f'<img alt="stars" src="{shield("★ " + str(r["stargazerCount"]), "FBBF24")}"/>')
        if r["forkCount"]:
            badges.append(f'<img alt="forks" src="{shield("⑂ " + str(r["forkCount"]), "A78BFA")}"/>')
        desc = f'<br/><sub>{esc(r["description"])}</sub>' if r["description"] else ""
        demo = f' · <a href="{esc(r["homepageUrl"])}">live demo</a>' if r.get("homepageUrl") else ""
        cells.append(
            f'<td width="50%" valign="top">\n<a href="{r["url"]}"><b>{esc(r["name"])}</b></a>{desc}\n'
            f'<br/><br/>{" ".join(badges)} <sub>updated {ago(r["pushedAt"])}{demo}</sub>\n</td>'
        )
    rows = ["<tr>\n" + "\n".join(cells[i:i + 2]) + "\n</tr>" for i in range(0, len(cells), 2)]
    return "<table>\n" + "\n".join(rows) + "\n</table>"


def activity_block(events: list[dict]) -> str | None:
    if not events:
        return None
    lines: list[tuple[str, str, str]] = []  # (key for merging, text, ts)
    push_counts: dict[str, int] = {}

    def repo_link(full: str) -> str:
        owner, _, name = full.partition("/")
        label = name if owner.lower() == USER.lower() else full
        return f"[{label}](https://github.com/{full})"

    for e in events:
        t, p, repo, ts = e.get("type"), e.get("payload", {}) or {}, e.get("repo", {}).get("name", ""), e.get("created_at", "")
        if repo.split("/")[-1] in EXCLUDE_REPOS:
            continue
        text = key = None
        if t == "PushEvent":
            key = f"push:{repo}:{ts[:10]}"
            push_counts[key] = push_counts.get(key, 0) + int(p.get("size") or p.get("distinct_size") or 0)
            text = "🔨 Pushed to {r}"
        elif t == "CreateEvent" and p.get("ref_type") == "repository":
            text = "✨ Created {r}"
        elif t == "CreateEvent" and p.get("ref_type") in ("branch", "tag"):
            text = f"🌿 Created {p['ref_type']} `{esc(p.get('ref') or '')}` in {{r}}"
        elif t == "PublicEvent":
            text = "🌍 Open-sourced {r}"
        elif t == "PullRequestEvent":
            pr = p.get("pull_request", {}) or {}
            verb = "Merged" if p.get("action") == "closed" and pr.get("merged") else (p.get("action") or "Updated").capitalize()
            title = f": {esc(pr['title'])}" if pr.get("title") else ""
            text = f"🔀 {verb} PR #{pr.get('number', p.get('number', ''))} in {{r}}{title}"
        elif t == "IssuesEvent":
            iss = p.get("issue", {}) or {}
            text = f"🐛 {(p.get('action') or 'Updated').capitalize()} issue #{iss.get('number', '')} in {{r}}"
        elif t == "ReleaseEvent":
            rel = p.get("release", {}) or {}
            text = f"🚀 Released {esc(rel.get('tag_name') or '')} of {{r}}"
        elif t == "WatchEvent":
            text = "⭐ Starred {r}"
        elif t == "ForkEvent":
            text = "🍴 Forked {r}"
        elif t == "PullRequestReviewEvent":
            text = "👀 Reviewed a pull request in {r}"
        elif t == "IssueCommentEvent":
            text = "💬 Commented in {r}"
        if not text:
            continue
        key = key or f"{t}:{repo}:{ts}"
        if any(k == key for k, _, _ in lines):
            continue
        lines.append((key, text.replace("{r}", repo_link(repo)), ts))
        if len(lines) >= ACTIVITY_COUNT:
            break
    out = []
    for key, text, ts in lines:
        n = push_counts.get(key, 0)
        if key.startswith("push:") and n:
            text += f" ({n} commit{'s' if n > 1 else ''})"
        out.append(f"- {text} <sub>{ago(ts)}</sub>")
    return "\n".join(out) if out else None


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
        "skyline.svg": render_skyline(days),
        "activity.svg": render_activity(days),
        "clock.svg": render_clock(commit_hours(user)),
        "languages.svg": render_languages(mix),
        "timeline.svg": render_timeline(repos, colors),
    }
    for name, svg in cards.items():
        (OUT / name).write_text(svg, encoding="utf-8")
    for stale in ("overview.svg",):  # cards from the previous layout
        (OUT / stale).unlink(missing_ok=True)

    if README.exists():
        doc = README.read_text(encoding="utf-8")
        doc = replace_block(doc, "PROJECTS", projects_block(repos))
        feed = activity_block(fetch_events())
        if feed:
            doc = replace_block(doc, "ACTIVITY", feed)
        stamp = NOW.astimezone(TIMEZONE).strftime("%d %b %Y, %H:%M %Z")
        doc = replace_block(doc, "UPDATED", f"<sub>Auto-refreshed by GitHub Actions on {stamp}</sub>")
        README.write_text(doc, encoding="utf-8")
    print(f"ok: {len(repos)} repos, {len(days)} days, streak {cur}/{longest}")


if __name__ == "__main__":
    main()
