# -*- coding: utf-8 -*-
"""
The spatial vocabulary the tracker can actually adjudicate.

WHY THIS REPLACES FREE-FORM BOUNDING BOXES
------------------------------------------
Asking a multimodal model for a normalised bbox makes it do two jobs at
once: identify what the marker is on, and state where that thing is in
frame. The 2026-08-10 session showed those failing independently. Of 60
claims, 46 named objects SMALLER than the 127 px measurement error and
could not be scored either way; of the 14 that could, the misses shared
a direction of +160, +404 px — four times the tracker's own measured
accuracy, which the validation had just bounded at 2.13 deg. The model
was placing boxes from a prior about how classrooms look rather than
from where the marker was.

Both failures come from the same source: the answer space was
unbounded. A fixed vocabulary removes it. The model chooses a region;
the region's rectangle is known exactly, so the model cannot misplace
it, and every claim is scoreable by construction. What remains measured
is the only thing RQ3 actually asks — did the model identify WHERE the
participant was attending — with the localisation error taken out.

THE ADMISSIBILITY RULE, WHICH IS NOT A PREFERENCE
-------------------------------------------------
A gaze point can only be assigned to the right region if the spatial
error cannot carry it across a boundary. So

    min region dimension (px) >= 2 x accuracy (px)

This is the same constraint metrics_spec states for AOIs, applied to
the region grid. It means the grid is NOT a free choice: it is
determined by the session's measured accuracy. At 2.13 deg (127 px) a
3x3 grid over a 1680x945 video gives 560x315 cells and passes. At the
3 deg inclusion limit (174 px) the same grid needs 348 px and FAILS on
the vertical — so that session gets a coarser grid, and the thesis says
so rather than quietly assigning points the data cannot place.

``admissible_grid()`` returns the finest grid a given accuracy
supports. A session that cannot support even 2x2 cannot support a
spatial claim at all, and is reported as such.
"""

from __future__ import annotations

import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

#: Candidate grids, finest first. Each is (columns, rows).
#: 3x3 is the finest worth attempting: below ~2 deg the cells stop being
#: describable in words a reader can picture ("upper-centre-left"), and
#: the pedagogical claims this study makes are about areas of a room,
#: not coordinates.
GRID_LADDER = ((3, 3), (3, 2), (2, 2))

COL_NAMES = {3: ("left", "centre", "right"), 2: ("left", "right")}
ROW_NAMES = {3: ("upper", "middle", "lower"), 2: ("upper", "lower")}


def region_name(col: int, row: int, cols: int, rows: int) -> str:
    """Human-readable name, e.g. 'upper-left', 'middle-centre'."""
    return "%s-%s" % (ROW_NAMES[rows][row], COL_NAMES[cols][col])


def build_grid(cols: int, rows: int) -> list:
    """Regions as dicts with a name and a NORMALISED rect [x, y, w, h]."""
    out = []
    for r in range(rows):
        for c in range(cols):
            out.append({
                "name": region_name(c, r, cols, rows),
                "bbox": [round(c / cols, 4), round(r / rows, 4),
                         round(1.0 / cols, 4), round(1.0 / rows, 4)],
                "col": c, "row": r,
            })
    return out


def admissible_grid(accuracy_px: float, video_w: int, video_h: int) -> dict:
    """The finest grid this accuracy can adjudicate, and why.

    Returns a dict with ``cols``, ``rows``, ``regions``, ``cell_px`` and
    ``admissible``. When even the coarsest grid fails, ``admissible`` is
    False and the session cannot support a spatial claim — which is a
    result to report, not an error to work around.
    """
    need = 2.0 * float(accuracy_px)
    tried = []
    for cols, rows in GRID_LADDER:
        cw, ch = video_w / cols, video_h / rows
        tried.append({"cols": cols, "rows": rows,
                      "cell_px": [round(cw), round(ch)],
                      "passes": bool(min(cw, ch) >= need)})
        if min(cw, ch) >= need:
            return {
                "cols": cols, "rows": rows,
                "regions": build_grid(cols, rows),
                "cell_px": [round(cw), round(ch)],
                "required_px": round(need),
                "admissible": True,
                "rejected": tried[:-1],
                "rule": ("min cell dimension >= 2 x accuracy (%.0f px); "
                         "%dx%d gives %.0fx%.0f px"
                         % (need, cols, rows, cw, ch)),
            }
    return {
        "cols": None, "rows": None, "regions": [],
        "cell_px": None, "required_px": round(need),
        "admissible": False,
        "rejected": tried,
        "rule": ("no grid down to %dx%d has cells of %.0f px, so this "
                 "session's accuracy cannot place a gaze point in any "
                 "region reliably. Report it; do not assign anyway."
                 % (GRID_LADDER[-1][0], GRID_LADDER[-1][1], need)),
    }


def vocabulary_text(grid: dict) -> str:
    """The region list, phrased for the prompt.

    Spelled out in plain spatial language rather than as coordinates:
    the model is being asked to look at a picture, and 'the upper-left
    third of the frame' is a description it can act on where
    '[0, 0, 0.33, 0.33]' invites it to reason about numbers instead.
    """
    if not grid.get("admissible"):
        return ""
    cols, rows = grid["cols"], grid["rows"]
    frac = {2: "half", 3: "third"}
    lines = []
    for reg in grid["regions"]:
        row_word = ROW_NAMES[rows][reg["row"]]
        col_word = COL_NAMES[cols][reg["col"]]
        lines.append("  - \"%s\": the %s vertical %s of the frame, %s side"
                     % (reg["name"], row_word, frac[rows],
                        col_word if col_word != "centre" else "centre"))
    return "\n".join(lines)


def region_by_name(grid: dict, name: str) -> "dict | None":
    if not name:
        return None
    key = str(name).strip().lower().replace("_", "-").replace(" ", "-")
    for reg in grid.get("regions", []):
        if reg["name"] == key:
            return reg
    return None


if __name__ == "__main__":
    for acc_deg in (1.5, 2.13, 3.0, 5.0):
        acc_px = acc_deg * 58.2
        g = admissible_grid(acc_px, 1680, 945)
        print("%.2f deg (%3.0f px) -> %s  %s"
              % (acc_deg, acc_px,
                 ("%dx%d" % (g["cols"], g["rows"])) if g["admissible"]
                 else "NO GRID", g["rule"]))
