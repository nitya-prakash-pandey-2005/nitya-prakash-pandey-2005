#!/usr/bin/env python3
"""
Builds assets/header.svg: identity on the left, a rotating 3D point-cloud brain on the right
with every major project orbiting it as a satellite.

GitHub renders README SVGs as images, so there is no JavaScript. The 3D motion is precomputed:
rotating a point about the vertical axis moves it on screen as x = A·sin(ωt + φ), which is a
cosine keyframe with ease-in-out-sine timing, a per-point amplitude (CSS variable) and a
per-point phase (negative animation-delay). Depth fades points at the back; satellites are drawn
twice (behind and in front of the brain) and switch copies as they cross the far side.

Run after editing PROJECTS or the identity text:  python3 scripts/build_header.py
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
# (label, colour) — outer ring first, then inner ring.
PROJECTS_OUTER = [
    ("FieldPilot AI", GENAI), ("TyreMind", RESEARCH), ("AgriVision", AGRI),
    ("SAGE", GENAI), ("Lunar Reg · SIH", SPACE), ("AgroSkin AI", MED),
]
PROJECTS_INNER = [
    ("Brain Seg · SGBC", MED), ("Travel RAG", GENAI), ("Chest X-ray CNN", MED),
    ("Reddit · DoMS", RESEARCH), ("Pathology WSI", MED),
]
CHIPS = [("Medical imaging", MED, 162), ("Agri vision", AGRI, 128), ("Agentic GenAI", GENAI, 144), ("Remote sensing", SPACE, 153)]
PHRASES = [
    "segmenting brain MRI :: SynthSeg · BiomedParse · ANTsPy",
    "registering Chandrayaan-2 lunar imagery :: SIH 2026",
    "mapping Reddit credibility networks :: DoMS research",
    "shipping agentic systems :: LangGraph · Qdrant · YOLO",
]

CX, CY = 952, 184          # brain centre on screen
SCALE = 118                # px per brain unit
TILT = math.radians(16)    # camera looks slightly down
START = math.radians(55)   # initial yaw, so the still frame is a three-quarter view
T_BRAIN, T_OUTER, T_INNER = 22, 40, 30  # rotation periods, seconds
MONO = 'ui-monospace, SFMono-Regular, "JetBrains Mono", "Cascadia Code", Consolas, "Liberation Mono", monospace'
SANS = '-apple-system, "Segoe UI Variable Display", "Segoe UI", Ubuntu, "Helvetica Neue", Arial, sans-serif'


def esc(s: str) -> str:
    return html.escape(s, quote=True)


def delay(psi: float, period: float) -> str:
    """Negative delay that starts a cosine keyframe at phase psi."""
    return f"{-((psi % (2 * math.pi)) / (2 * math.pi)) * period:.2f}s"


# ─────────────────────────────── brain point cloud ───────────────────────────────
def brain_points(rng: random.Random) -> list[tuple[float, float, float, str, float, float]]:
    """(x, y, z, colour, radius, brightness) in brain units; x left-right, y up, z front-back."""
    pts = []
    n = 860
    golden = math.pi * (3 - math.sqrt(5))
    for i in range(n):
        y = 1 - 2 * (i + .5) / n
        r = math.sqrt(1 - y * y)
        a = i * golden
        x, z = r * math.cos(a), r * math.sin(a)
        if abs(x) < .07 and y > .1:           # longitudinal fissure
            continue
        fold = math.sin(9 * x + 4 * z) * math.cos(10 * y - 3 * z) * .6 + math.sin(15 * z + 6 * y + 2 * x) * .4
        bump = 1 + .05 * fold
        X, Y, Z = .8 * x * bump, .7 * y * bump, 1.0 * z * bump
        if Y < -.22:                           # flatter base
            Y = -.22 + (Y + .22) * .55
        if Z > .45:                            # narrower frontal pole
            X *= 1 - .28 * (Z - .45)
        if Y < 0 and -.25 < Z < .6:            # temporal lobes
            X *= 1.07
        X += .035 if X > 0 else -.035          # open the hemispheres a little
        if Z > .35:
            col = MED                          # frontal
        elif Z < -.45:
            col = GENAI                        # occipital
        elif Y < -.02 and abs(X) > .45:
            col = AGRI                         # temporal
        else:
            col = RESEARCH                     # parietal
        pts.append((X, Y, Z, col, 1.45, .35 + .65 * (fold + 1) / 2))  # gyri bright, sulci dim
    for i in range(120):                       # cerebellum
        y = 1 - 2 * (i + .5) / 120
        r = math.sqrt(1 - y * y)
        a = i * golden
        f = 1 + .07 * math.sin(28 * y)
        pts.append((.5 * r * math.cos(a) * f, -.5 + .2 * y * f, -.6 + .3 * r * math.sin(a) * f, SPACE, 1.2, .5 + .5 * (math.sin(28 * y) + 1) / 2))
    for k in range(14):                        # brain stem
        a = k * 2.4
        pts.append((.09 * math.cos(a), -.55 - .025 * k, -.18 + .09 * math.sin(a), SPACE, 1.2, .8))
    for _ in range(26):                        # bright interior "neurons"
        u = [rng.uniform(-1, 1) for _ in range(3)]
        m = math.sqrt(sum(c * c for c in u)) or 1
        rad = rng.uniform(.2, .55)
        pts.append((.75 * u[0] / m * rad, .6 * u[1] / m * rad, .9 * u[2] / m * rad, "#FFFFFF", 1.5, 1))
    return pts


def render_brain(rng: random.Random) -> str:
    out = []
    for i, (x, y, z, col, r, lum) in enumerate(brain_points(rng)):
        rr = math.hypot(x, z)
        phi = math.atan2(x, z) + START
        ax = SCALE * rr
        ay = SCALE * rr * math.sin(TILT)
        by = CY - SCALE * y * math.cos(TILT)
        twinkle = col == "#FFFFFF" or rng.random() < .04
        halo = (f'<circle cx="{CX}" cy="{by:.1f}" r="{r + 3.5:.1f}" fill="{col}" class="tw" style="animation-delay:{rng.uniform(0, 5):.2f}s"/>'
                if twinkle else "")
        out.append(
            f'<g class="bx" style="--a:{ax:.1f}px;animation-delay:{delay(phi - math.pi / 2, T_BRAIN)}">'
            f'<g class="by" style="--a:{ay:.1f}px;animation-delay:{delay(phi, T_BRAIN)}">'
            f'<g class="bd" style="animation-delay:{delay(phi, T_BRAIN)}">{halo}'
            f'<circle cx="{CX}" cy="{by:.1f}" r="{r}" fill="{col}" fill-opacity="{lum:.2f}"/></g></g></g>')
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


def render_ring(projects: list[tuple[str, str]], a: float, b: float, alpha: float, period: float, cls: str, label_dy: float) -> tuple[str, str, str, str]:
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
    ring = lambda f, op: f'<path d="{ring_path(a, b, alpha, f)}" fill="none" stroke="{MED}" stroke-opacity="{op}" stroke-width="1" stroke-dasharray="{"3 5" if f else "2 7"}"/>'
    return ring(False, .22), "\n".join(back), "\n".join(front), ring(True, .5)


# ─────────────────────────────── left column ───────────────────────────────
def identity() -> tuple[str, str]:
    bars = [(64, 64, MED), (132, 38, RESEARCH), (174, 52, GENAI), (230, 30, AGRI), (264, 44, SPACE), (312, 22, "#FB7185")]
    chips, x = [], 64
    for name, col, w in CHIPS:
        chips.append(f'<g transform="translate({x},292)"><rect width="{w}" height="30" rx="15" fill="{col}" fill-opacity=".10" stroke="{col}" stroke-opacity=".55"/>'
                     f'<rect x="12" y="11" width="8" height="8" rx="2" fill="{col}"/><text x="27" y="20" class="chip">{esc(name)}</text></g>')
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
  <text x="62" y="152" class="name">Nitya Prakash Pandey</text>
  {''.join(f'<rect x="{x}" y="176" width="{w}" height="4" rx="2" fill="{c}"/>' for x, w, c in bars)}
  <text x="64" y="224" class="lead">I build vision models for brains, crops and lunar terrain.</text>
  <text x="64" y="256" class="sub">BS Data Science at IIT Madras. Research intern in medical image segmentation.</text>
  {''.join(chips)}
  {''.join(term)}"""
    return "\n  ".join(clips), body


def build() -> str:
    rng = random.Random(2005)
    clips, left = identity()
    ob, osb, osf, of = render_ring(PROJECTS_OUTER, 205, 50, -9, T_OUTER, "o", 19)
    ib, isb, isf, iff = render_ring(PROJECTS_INNER, 160, 74, 15, T_INNER, "i", -11)
    brain = render_brain(rng)
    n_proj = len(PROJECTS_OUTER) + len(PROJECTS_INNER)
    ease = "cubic-bezier(.37,0,.63,1)"
    osc = lambda c, t: f".{c} {{ animation: osc {t / 2}s {ease} infinite alternate both; }}"
    vis = lambda c, t, k: f".{c} {{ animation: {k} {t}s linear infinite both; }}"
    brackets = "".join(f'<path d="M{x},{y + sy * 16} L{x},{y} L{x + sx * 16},{y}"/>'
                       for x, y, sx, sy in ((712, 22, 1, 1), (1178, 22, -1, 1), (712, 358, 1, -1), (1178, 358, -1, -1)))
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" role="img" aria-labelledby="t d">
<title id="t">Nitya Prakash Pandey</title>
<desc id="d">I build vision models for brains, crops and lunar terrain. BS Data Science at IIT Madras. A rotating 3D brain with {n_proj} projects in orbit.</desc>
<defs>
  <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#060B18"/><stop offset="1" stop-color="#0C1630"/></linearGradient>
  <radialGradient id="core" cx="{CX}" cy="{CY}" r="190" gradientUnits="userSpaceOnUse"><stop offset="0" stop-color="{MED}" stop-opacity=".28"/><stop offset=".45" stop-color="{RESEARCH}" stop-opacity=".08"/><stop offset="1" stop-color="{MED}" stop-opacity="0"/></radialGradient>
  <linearGradient id="scanb" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="{MED}" stop-opacity="0"/><stop offset=".85" stop-color="{MED}" stop-opacity=".18"/><stop offset="1" stop-color="{MED}" stop-opacity=".85"/></linearGradient>
  <pattern id="grid" width="24" height="24" patternUnits="userSpaceOnUse"><path d="M24 0H0V24" fill="none" stroke="#FFFFFF" stroke-opacity=".035"/></pattern>
  <filter id="glow" x="-100%" y="-100%" width="300%" height="300%"><feGaussianBlur stdDeviation="2.5" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter>
  <clipPath id="card"><rect width="{W}" height="{H}" rx="18"/></clipPath>
  <clipPath id="scanclip"><ellipse cx="{CX}" cy="{CY - 4}" rx="118" ry="112"/></clipPath>
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
  @keyframes depth {{ from {{ opacity: 1; }} to {{ opacity: .16; }} }}
  @keyframes front {{ 0%, 24.9% {{ opacity: 1; }} 25%, 74.9% {{ opacity: 0; }} 75%, 100% {{ opacity: 1; }} }}
  @keyframes back {{ 0%, 24.9% {{ opacity: 0; }} 25%, 74.9% {{ opacity: 1; }} 75%, 100% {{ opacity: 0; }} }}
  {osc("bx", T_BRAIN)} .by {{ animation: oscy {T_BRAIN / 2}s {ease} infinite alternate both; }}
  .bd {{ animation: depth {T_BRAIN / 2}s {ease} infinite alternate both; }}
  {osc("ox", T_OUTER)} .oy {{ animation: oscy {T_OUTER / 2}s {ease} infinite alternate both; }}
  {osc("ix", T_INNER)} .iy {{ animation: oscy {T_INNER / 2}s {ease} infinite alternate both; }}
  {vis("ofront", T_OUTER, "front")} {vis("oback", T_OUTER, "back")}
  {vis("ifront", T_INNER, "front")} {vis("iback", T_INNER, "back")}
  .tw {{ opacity: 0; animation: tw 5s ease-in-out infinite; }}
  @keyframes tw {{ 0%, 70%, 100% {{ opacity: 0; }} 82% {{ opacity: .55; }} }}
  .pulse {{ animation: pulse 4s ease-in-out infinite; }}
  @keyframes pulse {{ 50% {{ opacity: .55; }} }}
  .sweep {{ animation: sweep 6s linear 1s infinite; opacity: 0; }}
  @keyframes sweep {{ 0% {{ transform: translateY(-50px); opacity: 0; }} 10% {{ opacity: 1; }} 90% {{ opacity: 1; }} 100% {{ transform: translateY(240px); opacity: 0; }} }}
  .blink {{ animation: blink 1.4s steps(2, start) infinite; }}
  @keyframes blink {{ to {{ opacity: .1; }} }}
  .spin {{ transform-origin: {CX}px {CY + 6}px; animation: spin 60s linear infinite; }}
  @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
  .boot {{ animation: boot 1s ease-out; }}
  @keyframes boot {{ from {{ opacity: 0; }} }}
  @media (prefers-reduced-motion: reduce) {{ * {{ animation-play-state: paused !important; }} }}
</style>
<g clip-path="url(#card)">
  <rect width="{W}" height="{H}" fill="url(#bg)"/>
  <rect width="{W}" height="{H}" fill="url(#grid)"/>
  <circle cx="{CX}" cy="{CY}" r="190" fill="url(#core)" class="pulse"/>

  <!-- HUD frame -->
  <g fill="none" stroke="#8A97B4" stroke-opacity=".55" stroke-width="1.2">{brackets}</g>
  <circle cx="{CX}" cy="{CY + 6}" r="168" fill="none" stroke="{MED}" stroke-opacity=".12" stroke-dasharray="1 6" class="spin"/>
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
  <g clip-path="url(#scanclip)"><rect class="sweep" x="{CX - 130}" y="{CY - 120}" width="260" height="46" fill="url(#scanb)"/></g>

  <!-- near side -->
  {of}
  {iff}
  {osf}
  {isf}

  <!-- identity -->
  {left}
</g>
</svg>
"""


if __name__ == "__main__":
    svg = build()
    OUT.write_text(svg, encoding="utf-8")
    print(f"wrote {OUT} ({len(svg) / 1024:.0f} KB)")
