#!/usr/bin/env python3
"""
Refreshes every dynamic part of the profile README.

Runs in GitHub Actions (.github/workflows/update-profile.yml) and:
  1. pulls public profile data from the GitHub GraphQL + REST APIs
  2. renders profile/overview.svg, profile/activity.svg and profile/languages.svg
  3. rewrites the marked blocks in README.md (latest projects, recent activity, timestamp)

Standard library only, so the workflow needs no pip install.
Local run:  GITHUB_TOKEN=<token> python3 scripts/update_profile.py
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
FEATURED: list[str] = []          # repo names always shown first in "Latest work", e.g. ["AgriVision-Ensemble-Net"]
EXCLUDE_REPOS = {USER}            # hidden from projects + language stats (the profile repo itself)
HIDE_LANGUAGES: set[str] = set()  # e.g. {"HTML", "CSS"} to keep them out of the language card
PROJECT_COUNT = 6                 # cards in the "Latest work" grid (even number looks best)
ACTIVITY_COUNT = 8                # lines in the "Recent activity" feed
TIMEZONE = dt.timezone(dt.timedelta(hours=5, minutes=30), "IST")
# ──────────────────────────────────────────────────────────────────────────────────────

TOKEN = os.environ.get("GITHUB_TOKEN", "")
ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "profile"
README = ROOT / "README.md"
NOW = dt.datetime.now(dt.timezone.utc)

# Palette shared with assets/header.svg: segmentation-label colours on scanner navy.
C = {
    "bg0": "#060B18", "bg1": "#0C1630", "panel": "#0E1730", "line": "#1C2A48",
    "text": "#E8EEF9", "soft": "#B9C5DD", "muted": "#8A97B4", "dim": "#4A5A7E",
    "cyan": "#38BDF8", "violet": "#A78BFA", "magenta": "#E879F9", "lime": "#A3E635",
    "amber": "#FBBF24", "coral": "#FB7185", "teal": "#2DD4BF", "orange": "#FB923C",
}
FONT = '-apple-system, "Segoe UI", Ubuntu, "Helvetica Neue", Arial, sans-serif'


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
        name description url homepageUrl stargazerCount forkCount isFork isArchived pushedAt
        primaryLanguage { name color }
        languages(first: 12, orderBy: {field: SIZE, direction: DESC}) { edges { size node { name color } } }
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
    if not TOKEN:
        sys.exit("GITHUB_TOKEN is not set. The GraphQL API needs a token (the workflow provides one).")
    res = _request("https://api.github.com/graphql", {"query": QUERY, "variables": {"login": USER}})
    if res.get("errors"):
        sys.exit(f"GraphQL error: {res['errors']}")
    return res["data"]["user"]


def fetch_events() -> list[dict]:
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
    """Blend bytes and repo count (sqrt(bytes) * sqrt(repos)) so one huge notebook can't swamp everything."""
    agg: dict[str, dict] = {}
    for r in repos:
        for edge in r["languages"]["edges"]:
            name = edge["node"]["name"]
            if name in HIDE_LANGUAGES:
                continue
            a = agg.setdefault(name, {"size": 0, "count": 0, "color": edge["node"]["color"] or C["muted"]})
            a["size"] += edge["size"]
            a["count"] += 1
    scored = {k: math.sqrt(v["size"]) * math.sqrt(v["count"]) for k, v in agg.items()}
    total = sum(scored.values()) or 1
    ranked = sorted(((k, agg[k]["color"], 100 * s / total) for k, s in scored.items()), key=lambda t: -t[2])
    if len(ranked) > 8:
        other = sum(p for _, _, p in ranked[7:])
        ranked = ranked[:7] + [("Other", C["dim"], other)]
    return ranked


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


def frame(w: int, h: int, title: str, body: str, extra_css: str = "") -> str:
    stamp = NOW.astimezone(TIMEZONE).strftime("%d %b %Y")
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}" role="img" aria-label="{esc(title)}">
<defs>
  <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="{C['bg0']}"/><stop offset="1" stop-color="{C['bg1']}"/></linearGradient>
  <pattern id="grid" width="24" height="24" patternUnits="userSpaceOnUse"><path d="M24 0H0V24" fill="none" stroke="#FFFFFF" stroke-opacity=".03"/></pattern>
</defs>
<style>
  text {{ font-family: {FONT}; }}
  .h {{ font-size: 17px; font-weight: 650; fill: {C['text']}; }}
  .stamp {{ font-size: 11.5px; fill: {C['dim']}; }}
  .lbl {{ font-size: 13px; fill: {C['muted']}; }}
  .val {{ font-size: 21px; font-weight: 700; fill: {C['text']}; font-variant-numeric: tabular-nums; }}
  .small {{ font-size: 11.5px; fill: {C['dim']}; font-variant-numeric: tabular-nums; }}
  .note {{ font-size: 13px; fill: {C['soft']}; }}
  {extra_css}
  @media (prefers-reduced-motion: reduce) {{ * {{ animation: none !important; }} }}
</style>
<rect width="{w}" height="{h}" rx="16" fill="url(#bg)"/>
<rect width="{w}" height="{h}" rx="16" fill="url(#grid)"/>
<rect x=".5" y=".5" width="{w-1}" height="{h-1}" rx="15.5" fill="none" stroke="{C['line']}"/>
<text x="28" y="40" class="h">{esc(title)}</text>
<text x="{w-28}" y="40" class="stamp" text-anchor="end">refreshed {stamp}</text>
{body}
</svg>
"""


def render_overview(user: dict, repos: list[dict], cur: int, longest: int) -> str:
    cc = user["contributionsCollection"]
    rows = [
        ("Contributions, last 12 months", cc["contributionCalendar"]["totalContributions"], C["cyan"]),
        ("Commits, last 12 months", cc["totalCommitContributions"], C["violet"]),
        ("Pull requests, all time", user["pullRequests"]["totalCount"], C["magenta"]),
        ("Issues, all time", user["issues"]["totalCount"], C["coral"]),
        ("Public repositories", len(repos), C["lime"]),
        ("Stars earned", sum(r["stargazerCount"] for r in repos), C["amber"]),
        ("Current streak, days", cur, C["teal"]),
        ("Longest streak, days", longest, C["orange"]),
    ]
    W, H = 900, 330
    col_w, x0s, y0, row_h = 400, (28, 472), 78, 60
    out = []
    for i, (label, value, col) in enumerate(rows):
        x0 = x0s[i // 4]
        y = y0 + (i % 4) * row_h
        goal = next_milestone(value)
        frac = max(0.015, min(1.0, value / goal))
        bar_end = col_w - 86                     # room on the right for the "next …" label
        out.append(f"""<g transform="translate({x0},{y})">
  <rect x="0" y="4" width="10" height="10" rx="2.5" fill="{col}"/>
  <text x="20" y="14" class="lbl">{esc(label)}</text>
  <text x="{col_w}" y="16" class="val" text-anchor="end">{fmt(value)}</text>
  <rect x="20" y="31" width="{bar_end - 20}" height="5" rx="2.5" fill="{C['line']}"/>
  <rect x="20" y="31" width="{(bar_end - 20) * frac:.1f}" height="5" rx="2.5" fill="{col}" class="grow"/>
  <text x="{col_w}" y="37.5" class="small" text-anchor="end">next {fmt(goal)}</text>
</g>""")
    out.append(f'<line x1="450" y1="74" x2="450" y2="{H-24}" stroke="{C["line"]}"/>')
    css = ".grow { transform-box: fill-box; transform-origin: left; animation: grow 1.4s cubic-bezier(.2,.7,.2,1) .2s both; } @keyframes grow { from { transform: scaleX(0); } }"
    return frame(W, H, "GitHub overview", "\n".join(out), css)


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
    W, H = 900, 300
    # ── weekly trend (left) ──
    weeks: list[tuple[dt.date, int]] = []
    for i in range(0, len(days), 7):
        chunk = days[i:i + 7]
        weeks.append((chunk[0][0], sum(n for _, n in chunk)))
    gx0, gx1, gy0, gy1 = 44, 575, 76, 236
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
        g.append(f'<line x1="{gx0}" y1="{y:.1f}" x2="{gx1}" y2="{y:.1f}" stroke="{C["line"]}" stroke-dasharray="2 4"/>')
        g.append(f'<text x="{gx0-8}" y="{y+4:.1f}" class="small" text-anchor="end">{round(top*(1-k/3))}</text>')
    seen = set()
    for i, (start, _) in enumerate(weeks):
        mid = start + dt.timedelta(days=3)
        if mid.day <= 7 and (mid.year, mid.month) not in seen:
            seen.add((mid.year, mid.month))
            g.append(f'<text x="{gx0 + i*step:.1f}" y="{gy1+22}" class="small" text-anchor="middle">{mid.strftime("%b")}</text>')
    if peak > 0:
        pi = max(range(len(weeks)), key=lambda i: weeks[i][1])
        px, py = pts[pi]
        anchor = "end" if px > gx1 - 150 else "start"
        dx = -10 if anchor == "end" else 10
        g.append(f'<circle cx="{px:.1f}" cy="{py:.1f}" r="4.5" fill="{C["bg0"]}" stroke="{C["magenta"]}" stroke-width="2.5" class="pop"/>')
        g.append(f'<text x="{px+dx:.1f}" y="{py-8:.1f}" class="note" text-anchor="{anchor}">peak week: {peak}</text>')

    # ── weekday rhythm (right) ──
    by_wd = [0] * 7
    for d, n in days:
        by_wd[d.weekday()] += n
    names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    cols = [C["cyan"], C["teal"], C["lime"], C["amber"], C["orange"], C["coral"], C["magenta"]]
    bx0, bw, gap, by1, bh = 628, 26, 11, 236, 134
    mx = max(by_wd) or 1
    bars = []
    for i, n in enumerate(by_wd):
        h = max(3, n / mx * bh)
        x = bx0 + i * (bw + gap)
        bars.append(f'<rect x="{x}" y="{by1 - h:.1f}" width="{bw}" height="{h:.1f}" rx="5" fill="{cols[i]}" fill-opacity="{1 if n == mx and n else .82}" class="rise"/>')
        if n == mx and n:
            bars.append(f'<text x="{x + bw/2}" y="{by1 - h - 8:.1f}" class="small" text-anchor="middle" style="fill:{C["text"]}">{fmt(n)}</text>')
        bars.append(f'<text x="{x + bw/2}" y="{by1+22}" class="small" text-anchor="middle">{names[i]}</text>')
    best = names[by_wd.index(max(by_wd))] if any(by_wd) else None
    total = sum(n for _, n in days)
    avg = total / max(1, len(days))
    active = sum(1 for _, n in days if n)

    notes = [f'<text x="44" y="{H-22}" class="note">{fmt(total)} contributions across {active} active days, {avg:.1f} per day on average.</text>']
    if best:
        notes.append(f'<text x="{W-28}" y="{H-22}" class="note" text-anchor="end">Most active on {best}days</text>')
    css = (
        f".line {{ stroke-dasharray: {length}; animation: draw 2.4s cubic-bezier(.5,0,.2,1) .2s both; }}"
        f" @keyframes draw {{ from {{ stroke-dashoffset: {length}; }} }}"
        " .area { animation: fade 1.2s ease 1.4s both; } .pop { animation: fade .5s ease 2.4s both; }"
        " .rise { transform-box: fill-box; transform-origin: bottom; animation: rise 1s cubic-bezier(.2,.7,.2,1) .6s both; }"
        " @keyframes fade { from { opacity: 0; } } @keyframes rise { from { transform: scaleY(0); } }"
    )
    body = f"""<defs>
  <linearGradient id="stroke" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="{C['cyan']}"/><stop offset=".55" stop-color="{C['violet']}"/><stop offset="1" stop-color="{C['magenta']}"/></linearGradient>
  <linearGradient id="fill" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="{C['violet']}" stop-opacity=".35"/><stop offset="1" stop-color="{C['cyan']}" stop-opacity="0"/></linearGradient>
</defs>
<text x="44" y="66" class="lbl">Weekly contributions, last 12 months</text>
<text x="628" y="66" class="lbl">Weekly rhythm</text>
{''.join(g)}
<path d="{area}" fill="url(#fill)" class="area"/>
<path d="{line}" fill="none" stroke="url(#stroke)" stroke-width="2.6" stroke-linecap="round" class="line"/>
<line x1="604" y1="60" x2="604" y2="{by1+26}" stroke="{C['line']}"/>
{''.join(bars)}
{''.join(notes)}"""
    return frame(W, H, "Coding activity", body, css)


def render_languages(mix: list[tuple[str, str, float]]) -> str:
    W, H = 900, 200
    x0, x1, y = 28, 872, 68
    segs, x = [], float(x0)
    gap = 3
    usable = (x1 - x0) - gap * max(0, len(mix) - 1)
    for i, (name, col, pct) in enumerate(mix):
        w = max(2.0, usable * pct / 100)
        segs.append(f'<rect x="{x:.1f}" y="{y}" width="{w:.1f}" height="16" rx="4" fill="{col}"><title>{esc(name)} {pct:.1f}%</title></rect>')
        x += w + gap
    legend = []
    for i, (name, col, pct) in enumerate(mix):
        lx = x0 + (i % 4) * 214
        ly = 124 + (i // 4) * 36
        legend.append(f'<circle cx="{lx+6}" cy="{ly-4}" r="6" fill="{col}"/>'
                      f'<text x="{lx+20}" y="{ly}" class="note">{esc(name)}</text>'
                      f'<text x="{lx+196}" y="{ly}" class="small" text-anchor="end">{pct:.1f}%</text>')
    if not mix:
        legend.append(f'<text x="{x0}" y="130" class="note">Language data appears after your first repository with code.</text>')
    css = ".bar { animation: wipe 1.3s cubic-bezier(.5,0,.2,1) .2s both; } @keyframes wipe { from { clip-path: inset(0 100% 0 0); } }"
    body = f'<g class="bar">{"".join(segs)}</g>\n' + "\n".join(legend)
    return frame(W, H, "Languages across my repositories", body, css)


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

    OUT.mkdir(exist_ok=True)
    (OUT / "overview.svg").write_text(render_overview(user, repos, cur, longest), encoding="utf-8")
    (OUT / "activity.svg").write_text(render_activity(days), encoding="utf-8")
    (OUT / "languages.svg").write_text(render_languages(language_mix(repos)), encoding="utf-8")

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
