#!/usr/bin/env python3
"""
Builds assets/header.svg: identity on the left, a rotating 3D point-cloud brain on the right
with every major project orbiting it as a satellite.

GitHub renders README SVGs as images, so there is no JavaScript. The 3D motion is precomputed:
rotating a point about the vertical axis moves it on screen as x = A·sin(ωt + φ), which is a
cosine keyframe with ease-in-out-sine timing, a per-point amplitude (CSS variable) and a
per-point phase (negative animation-delay). Depth fades points at the back; satellites are drawn
twice (behind and in front of the brain) and switch copies as they cross the far side.

update_profile.py rebuilds it on every refresh with the live repository pick for the outer ring.
Run by hand after editing WORK, REPO_LABELS or the identity text:  python3 scripts/build_header.py
"""
from __future__ import annotations

import html
import math
import random
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "assets" / "header.svg"
W, H = 1200, 380

# Domain colours, shared with the chips on the left.
MED, AGRI, GENAI, SPACE, RESEARCH = "#38BDF8", "#A3E635", "#E879F9", "#FBBF24", "#A78BFA"
# Outer ring: repositories. update_profile.py passes the live pick (pinned + most recently pushed),
# so new repos join the orbit automatically. Known repos get a short label and a domain colour.
REPO_LABELS = {
    "fieldpilot-ai": ("FieldPilot AI", GENAI), "TyreMind": ("TyreMind", RESEARCH), "AgriVision-Ensemble-Net": ("AgriVision", AGRI),
    "SAGE": ("SAGE", GENAI), "AgroSkin-AI": ("AgroSkin AI", MED), "ai-chest-disease-detection-cnn": ("Chest X-ray CNN", MED),
    "OpenCV-Basics": ("OpenCV Basics", SPACE), "ai-personalized-learning": ("AI Tutor", RESEARCH),
}
DEFAULT_REPOS = ["fieldpilot-ai", "TyreMind", "AgriVision-Ensemble-Net", "AgroSkin-AI", "SAGE", "ai-chest-disease-detection-cnn"]
# Inner ring: work that is not a public repository.
WORK = [
    ("Brain Seg · SGBC", MED), ("Lunar Reg · SIH", SPACE), ("Travel RAG", GENAI),
    ("Reddit · DoMS", RESEARCH), ("Pathology WSI", MED),
]


def satellite(repo: str) -> tuple[str, str]:
    if repo in REPO_LABELS:
        return REPO_LABELS[repo]
    words = repo.replace("_", " ").replace("-", " ").split()
    label = " ".join(w if any(c.isupper() for c in w) else w.capitalize() for w in words) or repo
    label = label if len(label) <= 16 else label[:15].rstrip() + "…"
    palette = [MED, AGRI, GENAI, SPACE, RESEARCH]
    return label, palette[sum(map(ord, repo)) % len(palette)]


CHIPS = [("Medical imaging", MED, 162), ("Agri vision", AGRI, 128), ("Agentic GenAI", GENAI, 144), ("Remote sensing", SPACE, 153)]
PHRASES = [
    "segmenting brain MRI :: SynthSeg · BiomedParse · ANTsPy",
    "registering Chandrayaan-2 lunar imagery :: SIH 2026",
    "mapping Reddit credibility networks :: DoMS research",
    "shipping agentic systems :: LangGraph · Qdrant · YOLO",
]

CX, CY = 950, 190          # brain centre on screen
SCALE = 138                # px per brain unit
TILT = math.radians(16)    # camera looks slightly down
START = math.radians(55)   # initial yaw, so the still frame is a three-quarter view
T_BRAIN, T_OUTER, T_INNER = 24, 40, 30  # rotation periods, seconds
LIGHT = (-0.45, 0.62, 0.64)  # key light from upper left, in camera space
MONO = 'ui-monospace, SFMono-Regular, "JetBrains Mono", "Cascadia Code", Consolas, "Liberation Mono", monospace'
SANS = '-apple-system, "Segoe UI Variable Display", "Segoe UI", Ubuntu, "Helvetica Neue", Arial, sans-serif'
PINK, ORANGE = "#F472B6", "#FB923C"


def esc(s: str) -> str:
    return html.escape(s, quote=True)


def delay(psi: float, period: float) -> str:
    """Negative delay that starts a cosine keyframe at phase psi."""
    return f"{-((psi % (2 * math.pi)) / (2 * math.pi)) * period:.2f}s"


def _norm(v: tuple[float, float, float]) -> tuple[float, float, float]:
    m = math.sqrt(sum(c * c for c in v)) or 1
    return v[0] / m, v[1] / m, v[2] / m


# ─────────────────────────────── anatomical brain ───────────────────────────────
# Union of ellipsoids (x left-right, y up, z front-back): two hemispheres with a fissure between
# them, temporal lobes, a two-lobed cerebellum and the brainstem. Points are sampled on each
# part's surface and kept only where they are not buried inside another part.
PARTS = [
    # centre, radii, sample count, kind
    ((+0.33, 0.06, 0.0), (0.40, 0.60, 0.97), 1000, "cortex"),
    ((-0.33, 0.06, 0.0), (0.40, 0.60, 0.97), 1000, "cortex"),
    ((+0.45, -0.28, 0.14), (0.29, 0.27, 0.54), 230, "temporal"),
    ((-0.45, -0.28, 0.14), (0.29, 0.27, 0.54), 230, "temporal"),
    ((+0.23, -0.47, -0.60), (0.29, 0.22, 0.31), 170, "cerebellum"),
    ((-0.23, -0.47, -0.60), (0.29, 0.22, 0.31), 170, "cerebellum"),
    ((0.0, -0.66, -0.20), (0.12, 0.30, 0.13), 50, "stem"),
]


def _inside(p: tuple[float, float, float], part: tuple) -> bool:
    (cx, cy, cz), (rx, ry, rz), *_ = part
    return ((p[0] - cx) / rx) ** 2 + ((p[1] - cy) / ry) ** 2 + ((p[2] - cz) / rz) ** 2 < .93


def _fold(p: tuple[float, float, float], kind: str) -> float:
    """Meandering gyri pattern in [-1, 1]; the cerebellum gets fine horizontal folia."""
    x, y, z = p
    if kind == "cerebellum":
        return math.sin(46 * y + 3 * math.sin(6 * x))
    if kind == "stem":
        return 0.0
    return (.65 * math.sin(12 * x + 5 * math.sin(4 * z) + 3 * y) * math.cos(11 * z + 4 * math.sin(5 * y))
            + .35 * math.sin(17 * y + 9 * z + 2 * math.sin(7 * x)))


def brain_points() -> list[tuple]:
    """(x, y, z, nx, ny, nz, colour, albedo, radius) for every visible surface point."""
    golden = math.pi * (3 - math.sqrt(5))
    pts = []
    for idx, part in enumerate(PARTS):
        (cx, cy, cz), (rx, ry, rz), n, kind = part
        for i in range(n):
            v = 1 - 2 * (i + .5) / n
            r = math.sqrt(1 - v * v)
            a = i * golden
            ux, uy, uz = r * math.cos(a), v, r * math.sin(a)
            p = (cx + rx * ux, cy + ry * uy, cz + rz * uz)
            if any(_inside(p, q) for j, q in enumerate(PARTS) if j != idx):
                continue
            if kind == "cortex":
                if abs(p[0]) < .05:                       # keep the longitudinal fissure open
                    continue
                if p[1] > 0:                              # taller parietal crown, lower frontal pole
                    p = (p[0], p[1] * (1 + .14 * (-p[2]) + .06), p[2])
                if p[1] < -.25:                           # flatter base
                    p = (p[0], -.25 + (p[1] + .25) * .6, p[2])
            nrm = _norm(((p[0] - cx) / rx ** 2, (p[1] - cy) / ry ** 2, (p[2] - cz) / rz ** 2))
            f = _fold(p, kind)
            e = .012                                      # bump-map the folds into the normal
            g = [(_fold(tuple(p[k] + (e if k == d else 0) for k in range(3)), kind) - f) / e for d in range(3)]
            gd = sum(g[k] * nrm[k] for k in range(3))
            g = [g[k] - gd * nrm[k] for k in range(3)]
            nrm = _norm(tuple(nrm[k] - .05 * g[k] for k in range(3)))
            p = tuple(p[k] + .022 * f * nrm[k] for k in range(3))
            if kind == "cortex":
                col = MED if p[2] > .3 else PINK if p[2] < -.42 else AGRI if p[1] < -.02 and abs(p[0]) > .5 else RESEARCH
            else:
                col = {"temporal": AGRI, "cerebellum": SPACE, "stem": ORANGE}[kind]
            pts.append((*p, *nrm, col, .6 + .4 * (f + 1) / 2, 1.7 if kind == "cerebellum" else 2.2))
    return pts


def render_brain(rng: random.Random) -> str:
    """Each point: X and Y follow the yaw as sinusoids; opacity follows Lambert lighting plus a
    facing term, which is also a sinusoid in the yaw and clamps to 0 on the far side (culling)."""
    vy, vz = math.sin(TILT), math.cos(TILT)
    lx, ly, lz = _norm(LIGHT)
    kv, kl = .55, .62
    kz, kx = kv * vz + kl * lz, kl * lx
    out = []
    for x, y, z, nx, ny, nz, col, alb, r in brain_points():
        rr, phi = math.hypot(x, z), math.atan2(x, z) + START
        c0 = .12 + (kv * vy + kl * ly) * ny
        P, Q = kz * nz + kx * nx, kx * nz - kz * nx
        amp, psi = math.hypot(P, Q), START - math.atan2(Q, P)
        hi, lo = alb * (c0 + amp), alb * (c0 - amp)
        by = CY - SCALE * y * math.cos(TILT)
        halo = (f'<circle cx="{CX}" cy="{by:.1f}" r="5" fill="#FFFFFF" class="tw" style="animation-delay:{rng.uniform(0, 6):.2f}s"/>'
                if rng.random() < .025 else "")
        out.append(
            f'<g class="bx" style="--a:{SCALE * rr:.1f}px;animation-delay:{delay(phi - math.pi / 2, T_BRAIN)}">'
            f'<g class="by" style="--a:{SCALE * rr * math.sin(TILT):.1f}px;animation-delay:{delay(phi, T_BRAIN)}">'
            f'<g class="lit" style="--h:{hi:.2f};--l:{lo:.2f};animation-delay:{delay(psi, T_BRAIN)}">{halo}'
            f'<circle cx="{CX}" cy="{by:.1f}" r="{r}" fill="{col}"/></g></g></g>')
    return "\n".join(out)


# ─────────────────────────────── orbiting projects ───────────────────────────────
def ring_geometry(a: float, b: float, alpha_deg: float) -> tuple[float, float, float, float]:
    """Amplitudes and phases of X(θ), Y(θ) for an ellipse (a·sinθ, b·cosθ) rotated by alpha."""
    al = math.radians(alpha_deg)
    ax, px = math.hypot(a * math.cos(al), b * math.sin(al)), math.atan2(-b * math.sin(al), a * math.cos(al))
    ay, py = math.hypot(a * math.sin(al), b * math.cos(al)), math.atan2(b * math.cos(al), a * math.sin(al))
    return ax, px, ay, py


def ring_path(a: float, b: float, alpha_deg: float, front: bool) -> str:
    al = math.radians(alpha_deg)
    pts = []
    rng = range(-90, 91, 4) if front else range(90, 271, 4)
    for d in rng:
        t = math.radians(d)
        ex, ey = a * math.sin(t), b * math.cos(t)
        pts.append((CX + ex * math.cos(al) - ey * math.sin(al), CY + 6 + ex * math.sin(al) + ey * math.cos(al)))
    return "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in pts)


def render_ring(projects: list[tuple[str, str]], a: float, b: float, alpha: float, period: float, cls: str, label_dy: float, tint: str) -> tuple[str, str, str, str]:
    """Returns (back ring, back satellites, front satellites, front ring)."""
    ax, px, ay, py = ring_geometry(a, b, alpha)
    back, front = [], []
    for i, (label, col) in enumerate(projects):
        th = 2 * math.pi * i / len(projects)
        # X = ax·sin(ωt+th+px) → cosine phase th+px-π/2 ; Y = ay·sin(ωt+th+py) ; depth = cos(ωt+th)
        dx, dy, dz = delay(th + px - math.pi / 2, period), delay(th + py - math.pi / 2, period), delay(th, period)
        for copy, sink in (("front", front), ("back", back)):
            dim = copy == "back"
            sink.append(
                f'<g class="{cls}x" style="--a:{ax:.1f}px;animation-delay:{dx}"><g class="{cls}y" style="--a:{ay:.1f}px;animation-delay:{dy}">'
                f'<g class="{cls}{copy}" style="animation-delay:{dz}"{" opacity=" + chr(34) + ".4" + chr(34) if dim else ""}>'
                f'<circle cx="{CX}" cy="{CY + 6}" r="9" fill="{col}" fill-opacity=".16"/>'
                f'<circle cx="{CX}" cy="{CY + 6}" r="4.2" fill="{col}"{"" if dim else " filter=" + chr(34) + "url(#glow)" + chr(34)}/>'
                f'<text x="{CX}" y="{CY + 6 + label_dy}" class="sat" text-anchor="middle">{esc(label)}</text></g></g></g>')
    ring = lambda f, op: f'<path d="{ring_path(a, b, alpha, f)}" fill="none" stroke="{tint}" stroke-opacity="{op}" stroke-width="1.1" stroke-dasharray="{"3 5" if f else "2 7"}"/>'
    return ring(False, .22), "\n".join(back), "\n".join(front), ring(True, .5)


# ─────────────────────────────── left column ───────────────────────────────
def identity() -> tuple[str, str]:
    bars = [(64, 64, MED), (132, 38, RESEARCH), (174, 52, GENAI), (230, 30, AGRI), (264, 44, SPACE), (312, 22, "#FB7185")]
    chips, x = [], 64
    for name, col, w in CHIPS:
        k = len(chips)
        chips.append(f'<g transform="translate({x},292)"><rect width="{w}" height="30" rx="15" fill="{col}" fill-opacity=".10" stroke="{col}" stroke-opacity=".55"/>'
                     f'<rect width="{w}" height="30" rx="15" fill="{col}" fill-opacity=".14" stroke="{col}" stroke-width="2" class="fire" style="animation-delay:{1.5 + k * .45:.2f}s" filter="url(#glow)"/>'
                     f'<rect x="12" y="11" width="8" height="8" rx="2" fill="{col}" class="dot" style="animation-delay:{k * .3:.1f}s"/><text x="27" y="20" class="chip">{esc(name)}</text></g>')
        x += w + 10
    n, x0, cw = len(PHRASES), 88, 8.2
    dur = 4.2 * n
    clips, term = [], ['<text x="64" y="358" class="mono term prompt">&gt;</text>']
    for i, ph in enumerate(PHRASES):
        w = len(ph) * cw + 4
        a, b = i / n, (i + 1) / n
        kt = f"0;{a:.4f};{a + .55 / n:.4f};{b - .08 / n:.4f};{b:.4f};1"
        clips.append(f'<clipPath id="ty{i}"><rect x="{x0}" y="338" height="30" width="0"><animate attributeName="width" values="0;0;{w:.0f};{w:.0f};0;0" keyTimes="{kt}" dur="{dur}s" repeatCount="indefinite"/></rect></clipPath>')
        text = esc(ph).replace("::", '<tspan fill="#4A5A7E">▸</tspan>')
        term.append(f'<text x="{x0}" y="358" class="mono term" clip-path="url(#ty{i})">{text}</text>')
        term.append(f'<rect x="{x0}" y="345" width="8" height="16" fill="{MED}" opacity="0">'
                    f'<animate attributeName="x" values="{x0};{x0};{x0 + w:.0f};{x0 + w:.0f};{x0};{x0}" keyTimes="{kt}" dur="{dur}s" repeatCount="indefinite"/>'
                    f'<animate attributeName="opacity" values="0;1;1;1;0;0" keyTimes="{kt}" calcMode="discrete" dur="{dur}s" repeatCount="indefinite"/></rect>')
    body = f"""<g class="boot">
    <circle cx="68" cy="82" r="4" fill="{AGRI}" class="blink"/>
    <text x="84" y="86.5" class="mono status">VISION · ML ENGINEER <tspan fill="#4A5A7E">//</tspan> IIT MADRAS <tspan fill="#4A5A7E">//</tspan> STATUS <tspan fill="{AGRI}">ONLINE</tspan></text>
  </g>
  <text x="62" y="152" class="name g1" style="fill:{MED}">Nitya Prakash Pandey</text>
  <text x="62" y="152" class="name g2" style="fill:{GENAI}">Nitya Prakash Pandey</text>
  <text x="62" y="152" class="name">Nitya Prakash Pandey</text>
  <g clip-path="url(#namec)"><rect x="-40" y="96" width="70" height="70" fill="url(#glint)" class="nshine" transform="skewX(-20)"/></g>
  {''.join(f'<rect x="{x}" y="176" width="{w}" height="4" rx="2" fill="{c}" class="wave" style="animation-delay:{k * .18:.2f}s"/>' for k, (x, w, c) in enumerate(bars))}
  <text x="64" y="224" class="lead">I build vision models for brains, crops and lunar terrain.</text>
  <text x="64" y="256" class="sub">BS Data Science at IIT Madras. Research intern in medical image segmentation.</text>
  {''.join(chips)}
  {''.join(term)}"""
    return "\n  ".join(clips), body


def build(repos: list[str] | None = None) -> str:
    """repos: repository names for the outer ring (defaults to DEFAULT_REPOS); at most six are used."""
    rng = random.Random(2005)
    clips, left = identity()
    outer = [satellite(r) for r in (repos if repos else DEFAULT_REPOS)][:6]
    inner = [w for w in WORK if w[0] not in {o[0] for o in outer}]
    ob, osb, osf, of = render_ring(outer, 212, 56, -9, T_OUTER, "o", 19, RESEARCH)
    ib, isb, isf, iff = render_ring(inner, 182, 84, 15, T_INNER, "i", -11, MED)
    brain = render_brain(rng)
    n_proj = len(outer) + len(inner)
    ease = "cubic-bezier(.37,0,.63,1)"
    osc = lambda c, t: f".{c} {{ animation: osc {t / 2}s {ease} infinite alternate both; }}"
    vis = lambda c, t, k: f".{c} {{ animation: {k} {t}s linear infinite both; }}"
    brackets = "".join(f'<path d="M{x},{y + sy * 16} L{x},{y} L{x + sx * 16},{y}"/>'
                       for x, y, sx, sy in ((712, 22, 1, 1), (1178, 22, -1, 1), (712, 358, 1, -1), (1178, 358, -1, -1)))
    stars = "".join(
        f'<circle cx="{(k * 127.9) % (W - 20) + 10:.1f}" cy="{(k * 61.3) % (H - 20) + 10:.1f}" r="{.6 + (k % 3) * .35:.2f}" fill="#FFFFFF" class="star"'
        f' style="animation-delay:{(k * .47) % 5:.2f}s;animation-duration:{3 + (k % 4)}s"/>' for k in range(70))
    # glowing data streams from the identity column into the brain
    streams = [("M690,307 C760,300 770,240 822,222", MED, 0), ("M676,150 C730,146 760,150 812,168", GENAI, 1.3),
               ("M612,82 C700,70 760,86 850,98", AGRI, 2.6)]
    stream_svg = "".join(
        f'<path d="{d}" fill="none" stroke="{c}" stroke-opacity=".16" stroke-width="1.2"/>'
        f'<path d="{d}" fill="none" stroke="{c}" stroke-width="2.4" stroke-linecap="round" pathLength="100" class="stream" style="animation-delay:{dl}s" filter="url(#glow)"/>'
        for d, c, dl in streams)
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" role="img" aria-labelledby="t d">
<title id="t">Nitya Prakash Pandey</title>
<desc id="d">I build vision models for brains, crops and lunar terrain. BS Data Science at IIT Madras. A rotating 3D brain with {n_proj} projects in orbit.</desc>
<defs>
  <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#060B18"/><stop offset="1" stop-color="#0C1630"/></linearGradient>
  <radialGradient id="core" cx="{CX}" cy="{CY}" r="210" gradientUnits="userSpaceOnUse"><stop offset="0" stop-color="{MED}" stop-opacity=".34"/><stop offset=".35" stop-color="{RESEARCH}" stop-opacity=".16"/><stop offset=".7" stop-color="{GENAI}" stop-opacity=".05"/><stop offset="1" stop-color="{GENAI}" stop-opacity="0"/></radialGradient>
  <linearGradient id="scanb" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="{MED}" stop-opacity="0"/><stop offset=".85" stop-color="{MED}" stop-opacity=".18"/><stop offset="1" stop-color="{MED}" stop-opacity=".85"/></linearGradient>
  <pattern id="grid" width="24" height="24" patternUnits="userSpaceOnUse"><path d="M24 0H0V24" fill="none" stroke="#FFFFFF" stroke-opacity=".035"/></pattern>
  <filter id="glow" x="-100%" y="-100%" width="300%" height="300%"><feGaussianBlur stdDeviation="2.5" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter>
  <clipPath id="card"><rect width="{W}" height="{H}" rx="18"/></clipPath>
  <clipPath id="scanclip"><ellipse cx="{CX}" cy="{CY + 10}" rx="150" ry="130"/></clipPath>
  <clipPath id="namec"><text x="62" y="152" class="name">Nitya Prakash Pandey</text></clipPath>
  <linearGradient id="glint" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#FFFFFF" stop-opacity="0"/><stop offset=".5" stop-color="#FFFFFF" stop-opacity=".85"/><stop offset="1" stop-color="#FFFFFF" stop-opacity="0"/></linearGradient>
  <linearGradient id="runner" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="{MED}"/><stop offset=".5" stop-color="{RESEARCH}"/><stop offset="1" stop-color="{GENAI}"/></linearGradient>
  {clips}
</defs>
<style>
  text {{ font-family: {SANS}; }}
  .mono {{ font-family: {MONO}; }}
  .name {{ font-size: 58px; font-weight: 700; fill: #E8EEF9; letter-spacing: -1.2px; }}
  .lead {{ font-size: 23px; font-weight: 500; fill: #B9C5DD; }}
  .sub {{ font-size: 16px; fill: #8A97B4; }}
  .chip {{ font-size: 13.5px; font-weight: 600; fill: #E8EEF9; }}
  .status {{ font-size: 12px; letter-spacing: 3px; fill: #8A97B4; }}
  .term {{ font-size: 14px; fill: #B9C5DD; }}
  .prompt {{ fill: {MED}; }}
  .hud {{ font: 10.5px {MONO}; letter-spacing: 1.6px; fill: #8A97B4; }}
  .sat {{ font: 600 10.5px {MONO}; fill: #E8EEF9; paint-order: stroke; stroke: #060B18; stroke-width: 3.5px; stroke-linejoin: round; }}
  @keyframes osc {{ from {{ transform: translate(var(--a), 0); }} to {{ transform: translate(calc(-1 * var(--a)), 0); }} }}
  @keyframes oscy {{ from {{ transform: translate(0, var(--a)); }} to {{ transform: translate(0, calc(-1 * var(--a))); }} }}
  @keyframes lit {{ from {{ opacity: var(--h); }} to {{ opacity: var(--l); }} }}
  @keyframes front {{ 0%, 24.9% {{ opacity: 1; }} 25%, 74.9% {{ opacity: 0; }} 75%, 100% {{ opacity: 1; }} }}
  @keyframes back {{ 0%, 24.9% {{ opacity: 0; }} 25%, 74.9% {{ opacity: 1; }} 75%, 100% {{ opacity: 0; }} }}
  {osc("bx", T_BRAIN)} .by {{ animation: oscy {T_BRAIN / 2}s {ease} infinite alternate both; }}
  .lit {{ animation: lit {T_BRAIN / 2}s {ease} infinite alternate both; }}
  {osc("ox", T_OUTER)} .oy {{ animation: oscy {T_OUTER / 2}s {ease} infinite alternate both; }}
  {osc("ix", T_INNER)} .iy {{ animation: oscy {T_INNER / 2}s {ease} infinite alternate both; }}
  {vis("ofront", T_OUTER, "front")} {vis("oback", T_OUTER, "back")}
  {vis("ifront", T_INNER, "front")} {vis("iback", T_INNER, "back")}
  .tw {{ opacity: 0; animation: tw 5s ease-in-out infinite; }}
  @keyframes tw {{ 0%, 70%, 100% {{ opacity: 0; }} 82% {{ opacity: .9; }} }}
  .pulse {{ animation: pulse 4s ease-in-out infinite; }}
  @keyframes pulse {{ 50% {{ opacity: .55; }} }}
  .sweep {{ animation: sweep 6s linear 1s infinite; opacity: 0; }}
  @keyframes sweep {{ 0% {{ transform: translateY(-50px); opacity: 0; }} 10% {{ opacity: 1; }} 90% {{ opacity: 1; }} 100% {{ transform: translateY(290px); opacity: 0; }} }}
  .blink {{ animation: blink 1.4s steps(2, start) infinite; }}
  @keyframes blink {{ to {{ opacity: .1; }} }}
  .spin {{ transform-origin: {CX}px {CY + 6}px; animation: spin 60s linear infinite; }}
  @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
  .boot {{ animation: boot 1s ease-out; }}
  .star {{ opacity: .12; animation: star 4s ease-in-out infinite; }} @keyframes star {{ 50% {{ opacity: .75; }} }}
  .nshine {{ animation: nshine 6s ease-in-out 1s infinite; }}
  @keyframes nshine {{ 0% {{ transform: skewX(-20deg) translateX(0); }} 35%, 100% {{ transform: skewX(-20deg) translateX(760px); }} }}
  .g1, .g2 {{ opacity: 0; animation: gl 7s steps(1, end) 3s infinite; }} .g2 {{ animation-name: gl2; }}
  @keyframes gl {{ 0%, 94%, 100% {{ opacity: 0; transform: none; }} 95% {{ opacity: .7; transform: translate(-4px, 1px); }} 96.5% {{ opacity: .5; transform: translate(3px, -1px); }} }}
  @keyframes gl2 {{ 0%, 94%, 100% {{ opacity: 0; transform: none; }} 95% {{ opacity: .7; transform: translate(4px, -1px); }} 96.5% {{ opacity: .5; transform: translate(-3px, 1px); }} }}
  .wave {{ animation: wave 2.4s ease-in-out infinite; }} @keyframes wave {{ 0%, 100% {{ opacity: 1; }} 50% {{ opacity: .3; }} }}
  .fire {{ opacity: 0; animation: fire 4.5s ease-out infinite; }} @keyframes fire {{ 0%, 18%, 100% {{ opacity: 0; }} 6% {{ opacity: 1; }} }}
  .dot {{ animation: blink 1.6s ease-in-out infinite; }}
  .stream {{ stroke-dasharray: 14 86; animation: stream 3.9s linear infinite; }} @keyframes stream {{ from {{ stroke-dashoffset: 100; }} to {{ stroke-dashoffset: 0; }} }}
  .run {{ stroke-dasharray: 7 43; animation: run 9s linear infinite; }} @keyframes run {{ from {{ stroke-dashoffset: 50; }} to {{ stroke-dashoffset: 0; }} }}
  .hud {{ animation: hudf 8s steps(1, end) infinite; }} @keyframes hudf {{ 0%, 96%, 100% {{ opacity: 1; }} 97% {{ opacity: .2; }} 98% {{ opacity: .8; }} 99% {{ opacity: .3; }} }}
  @keyframes boot {{ from {{ opacity: 0; }} }}
  @media (prefers-reduced-motion: reduce) {{ * {{ animation-play-state: paused !important; }} }}
</style>
<g clip-path="url(#card)">
  <rect width="{W}" height="{H}" fill="url(#bg)"/>
  <rect width="{W}" height="{H}" fill="url(#grid)"/>
  <g>{stars}</g>
  <circle cx="{CX}" cy="{CY}" r="210" fill="url(#core)" class="pulse"/>
  {stream_svg}

  <!-- HUD frame -->
  <g fill="none" stroke="#8A97B4" stroke-opacity=".55" stroke-width="1.2">{brackets}</g>
  <circle cx="{CX}" cy="{CY + 6}" r="176" fill="none" stroke="{GENAI}" stroke-opacity=".2" stroke-dasharray="1 6" class="spin"/>
  <text x="726" y="44" class="hud">NEURAL CORE · 3D</text>
  <text x="1164" y="44" class="hud" text-anchor="end">ROT 360° · LIVE</text>
  <text x="726" y="346" class="hud">{n_proj} PROJECTS IN ORBIT</text>
  <text x="1164" y="346" class="hud" text-anchor="end">SEG · T1w</text>

  <!-- far side: rings and satellites behind the brain -->
  {ob}
  {ib}
  {osb}
  {isb}

  <!-- rotating brain, coloured by lobe like a segmentation map -->
  {brain}
  <g clip-path="url(#scanclip)"><rect class="sweep" x="{CX - 160}" y="{CY - 130}" width="320" height="46" fill="url(#scanb)"/></g>

  <!-- near side -->
  {of}
  {iff}
  {osf}
  {isf}

  <!-- identity -->
  {left}
</g>
<rect x="1" y="1" width="{W - 2}" height="{H - 2}" rx="17" fill="none" stroke="url(#runner)" stroke-width="2" pathLength="100" class="run" filter="url(#glow)"/>
<rect x="1" y="1" width="{W - 2}" height="{H - 2}" rx="17" fill="none" stroke="#FFFFFF" stroke-width="1.4" pathLength="100" class="run" style="animation-delay:-4.5s"/>
</svg>
"""


if __name__ == "__main__":
    svg = build()
    OUT.write_text(svg, encoding="utf-8")
    print(f"wrote {OUT} ({len(svg) / 1024:.0f} KB)")
