"""Emit the .jus brand assets (S1) — self-contained, no exec() chains."""
import math, pathlib

REPO = pathlib.Path("/Users/jisu/data/workspace/ref/claude-code-leak/adk-cc")
C = (32.0, 32.0)
INK, MID, PALE, FIELD = "#1c1a17", "#7d776b", "#c3bcae", "#e8e3d8"
T3 = [INK, MID, PALE]
CURL = (-4.5, -0.5, 6.0, 10.5)
# index == adjacent branch: 30deg -> b0, 150 -> b1, 270 -> b2
CORNERS = [5, 1, 3]


def verts(R, rot=0.0):
    return [(C[0] + R*math.cos(math.radians(90+rot+k*60)),
             C[1] - R*math.sin(math.radians(90+rot+k*60))) for k in range(6)]


def lerp(p, q, t):
    return (p[0]+(q[0]-p[0])*t, p[1]+(q[1]-p[1])*t)


def in_j(dx, dy, sc=1.0, tittle=True):
    R = [(3.5*sc, 11.5*sc, 1.0*sc, 17.0*sc),
         (-4.5*sc, 11.5*sc, 1.0*sc, 6.0*sc),
         tuple(v*sc for v in CURL)]
    if tittle:
        R.append((3.5*sc, 11.5*sc, 20.0*sc, 26.5*sc))
    return any(x0 <= dx <= x1 and y0 <= dy <= y1 for x0, x1, y0, y1 in R)


def which_j(cx, cy, tittle=True):
    for k in range(3):
        a = math.radians(-k*120)
        dx0, dy0 = cx-C[0], C[1]-cy
        if in_j(dx0*math.cos(a)-dy0*math.sin(a), dx0*math.sin(a)+dy0*math.cos(a),
                tittle=tittle):
            return k
    return None


def pads(R, n, rot=0.0, inset=0.86):
    V = verts(R, rot); out = []
    for s in range(6):
        A, B = V[s], V[(s+1) % 6]

        def Q(r, i, A=A, B=B):
            if r == 0:
                return C
            return lerp(lerp(C, A, r/n), lerp(C, B, r/n), i/r)

        tr = []
        for r in range(1, n+1):
            for i in range(r):
                tr.append((r, i, True, (Q(r, i), Q(r, i+1), Q(r-1, i))))
            for i in range(r-1):
                tr.append((r, i, False, (Q(r, i+1), Q(r-1, i), Q(r-1, i+1))))
        for r, i, up, pts in tr:
            cx = sum(p[0] for p in pts)/3; cy = sum(p[1] for p in pts)/3
            ip = [(cx+(x-cx)*inset, cy+(y-cy)*inset) for x, y in pts]
            out.append((s, r, i, up, cx, cy,
                        "M" + "L".join(f"{x:.2f} {y:.2f}" for x, y in ip) + "Z"))
    return out


def mark(n=3, drop=1, R=27, rot=0.0, inset=0.86, base=FIELD, tones=None,
         tittle=True):
    """S1: two rings, three J branches, corner pairs matching their branch."""
    tones = tones or T3
    top = n - drop
    body = []
    for s, r, i, up, cx, cy, d in pads(R, n, rot, inset):
        if r > top:
            continue
        f = base
        k = which_j(cx, cy, tittle)
        if k is not None:
            f = tones[k]
        elif r == top and up:
            for ci, cs in enumerate(CORNERS):
                if (s == cs and i == 0) or ((s+1) % 6 == cs and i == r-1):
                    f = tones[ci]
                    break
        if f:
            body.append(f'<path d="{d}" fill="{f}"/>')
    return "".join(body)


def simple(inset=0.93, tones=None):
    """One subdivision: six pads, three tones, two pads per branch.

    The favicon serves the 16px browser tab AND the 24px rail, where the
    two-ring lattice is illegible. Same geometry resolved for its size —
    which is what a lattice mark is for.
    """
    tones = tones or T3
    V = verts(27.5); out = []
    for s in range(6):
        pts = [C, V[s], V[(s+1) % 6]]
        cx = sum(p[0] for p in pts)/3; cy = sum(p[1] for p in pts)/3
        ip = [(cx+(x-cx)*inset, cy+(y-cy)*inset) for x, y in pts]
        d = "M" + "L".join(f"{x:.2f} {y:.2f}" for x, y in ip) + "Z"
        out.append(f'<path d="{d}" fill="{tones[[0,1,1,2,2,0][s]]}"/>')
    return "".join(out)


def wrap(inner, size=64):
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" '
            f'width="{size}" height="{size}" role="img" aria-label=".jus">'
            f'{inner}</svg>')


if __name__ == "__main__":
    (REPO/"assets"/"brand").mkdir(parents=True, exist_ok=True)
    (REPO/"assets"/"brand"/"jus-mark.svg").write_text(wrap(mark(), 512))
    (REPO/"assets"/"brand"/"jus-mark-simple.svg").write_text(wrap(simple(), 512))
    (REPO/"web"/"public"/"favicon.svg").write_text(wrap(simple()))
    pathlib.Path("/Users/jisu/.claude/jobs/18dbfcc1/tmp/icon_src.html").write_text(
        "<style>html,body{margin:0;background:transparent}</style>" + wrap(mark(), 1024))
    print("written: jus-mark.svg, jus-mark-simple.svg, favicon.svg, icon_src.html")
