# -*- coding: utf-8 -*-
"""
Summarise the human coding, and compute kappa when two coders overlap.

WHAT THIS IS FOR
----------------
The /coder tool writes one JSON per coder per recording. Those files
are the study's only judgments that did not come from a model, so they
carry two different results and it is worth keeping them apart:

    ACCURACY     of the units a human could judge, what share of the
                 model's claims were correct. This is a validity
                 figure — it says whether the feedback is right.

    RELIABILITY  how well two humans agree with each other. This says
                 whether "correct" is a judgment anyone can reproduce,
                 and without it the accuracy figure rests on one
                 person's opinion.

A high accuracy with no reliability estimate is the weaker of the two
to be missing, because a single coder who is systematically generous
produces exactly the same number as a fair one.

WHY "UNCLEAR" IS NOT COUNTED WRONG
----------------------------------
At ~2 deg accuracy a real fraction of fixations sit between two
plausible objects. Forcing those into correct/wrong manufactures
agreement that does not exist, so they are excluded from the accuracy
rate and reported separately. The unclear SHARE is itself a finding —
it measures how often the instrument cannot adjudicate, which is a
property of the method rather than of the model.

Usage::

    python coding_report.py                    # every coding file
    python coding_report.py --session <id>
    python coding_report.py --paste            # compact, shareable
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from collections import Counter

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

BASE = os.path.dirname(os.path.abspath(__file__))
CODING_DIR = os.path.join(BASE, "data", "coding")

#: Landis & Koch (1977). Quoted because a kappa without a benchmark is
#: a number nobody can act on.
KAPPA_BANDS = ((0.81, "almost perfect"), (0.61, "substantial"),
               (0.41, "moderate"), (0.21, "fair"), (0.0, "slight"))


def band(k: float) -> str:
    for lo, name in KAPPA_BANDS:
        if k >= lo:
            return name
    return "poor (worse than chance)"


def cohens_kappa(a: dict, b: dict) -> "dict | None":
    """Cohen's kappa between two coders over the units BOTH coded.

    Units only one coder saw are excluded rather than treated as
    disagreements: a unit one person never judged is missing data, not
    a difference of opinion, and counting it as either inflates or
    deflates the coefficient depending on which way you guess.
    """
    shared = sorted(set(a) & set(b), key=lambda k: int(k)
                    if str(k).isdigit() else 0)
    n = len(shared)
    if n < 10:
        return {"ok": False, "n": n,
                "reason": "only %d units coded by both — too few for a "
                          "stable kappa" % n}

    cats = sorted({a[u] for u in shared} | {b[u] for u in shared})
    agree = sum(1 for u in shared if a[u] == b[u])
    po = agree / n
    # Expected agreement from the marginals: what two coders with these
    # habits would agree on by chance alone.
    pe = 0.0
    for c in cats:
        pa = sum(1 for u in shared if a[u] == c) / n
        pb = sum(1 for u in shared if b[u] == c) / n
        pe += pa * pb
    kappa = (po - pe) / (1 - pe) if pe < 1 else 1.0

    # The confusion table, because a kappa without it hides WHICH
    # category the coders disagree about — usually "unclear".
    table = Counter((a[u], b[u]) for u in shared)
    return {
        "ok": True, "n": n, "categories": cats,
        "observed_agreement_pct": round(100 * po, 1),
        "expected_agreement_pct": round(100 * pe, 1),
        "kappa": round(kappa, 3),
        "interpretation": band(kappa),
        "confusion": {"%s|%s" % k: v for k, v in sorted(table.items())},
    }


def summarise(codes: dict) -> dict:
    c = Counter(codes.values())
    judged = c["correct"] + c["wrong"]
    return {
        "n_coded": len(codes),
        "correct": c["correct"], "wrong": c["wrong"],
        "unclear": c["unclear"],
        "n_judged": judged,
        "accuracy_pct": round(100.0 * c["correct"] / judged, 1)
        if judged else None,
        "unclear_pct": round(100.0 * c["unclear"] / len(codes), 1)
        if codes else None,
    }


def load_all(session_filter: str = "") -> list:
    out = []
    for path in sorted(glob.glob(os.path.join(CODING_DIR, "*.json"))):
        try:
            with open(path, encoding="utf-8") as fh:
                rec = json.load(fh)
        except (OSError, ValueError):
            continue
        if session_filter and session_filter not in str(rec.get("session")):
            continue
        rec["_path"] = path
        rec["_key"] = "%s :: %s" % (rec.get("session"), rec.get("stimulus"))
        out.append(rec)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--session", default="")
    ap.add_argument("--paste", action="store_true",
                    help="compact output with no file paths, safe to share")
    args = ap.parse_args()

    recs = load_all(args.session)
    if not recs:
        print("No coding files in data/coding/.")
        print("Open http://localhost:5050/coder and code a session first.")
        return 1

    by_rec: dict = {}
    for r in recs:
        by_rec.setdefault(r["_key"], []).append(r)

    print("=" * 74)
    print("  HUMAN CODING SUMMARY")
    print("=" * 74)

    for key, group in sorted(by_rec.items()):
        print()
        print("  %s" % (key.split(" :: ")[1] if args.paste else key))
        print("  " + "-" * 70)
        for r in group:
            s = summarise(r.get("codes") or {})
            print("    coder %-10s %3d coded | correct %3d  wrong %3d  "
                  "unclear %3d" % (r.get("coder"), s["n_coded"],
                                   s["correct"], s["wrong"], s["unclear"]))
            if s["accuracy_pct"] is not None:
                print("    %-16s accuracy %.1f %% of the %d units judged; "
                      "%.1f %% were unclear"
                      % ("", s["accuracy_pct"], s["n_judged"],
                         s["unclear_pct"]))

        if len(group) >= 2:
            print()
            for i in range(len(group)):
                for j in range(i + 1, len(group)):
                    a, b = group[i], group[j]
                    k = cohens_kappa(a.get("codes") or {},
                                     b.get("codes") or {})
                    print("    %s vs %s:" % (a.get("coder"), b.get("coder")))
                    if not k.get("ok"):
                        print("      %s" % k["reason"])
                        continue
                    print("      Cohen's kappa %.3f (%s) over %d shared "
                          "units" % (k["kappa"], k["interpretation"], k["n"]))
                    print("      observed agreement %.1f %%, expected by "
                          "chance %.1f %%"
                          % (k["observed_agreement_pct"],
                             k["expected_agreement_pct"]))
                    dis = {p: n for p, n in k["confusion"].items()
                           if p.split("|")[0] != p.split("|")[1]}
                    if dis:
                        print("      disagreements: %s"
                              % ", ".join("%s %d" % (p, n)
                                          for p, n in sorted(
                                              dis.items(),
                                              key=lambda kv: -kv[1])[:4]))
        else:
            print()
            print("    ONE CODER ONLY — no reliability estimate. The")
            print("    accuracy above rests on one person's judgment, and a")
            print("    coder who is systematically generous produces the")
            print("    same number as a fair one. Have a second person")
            print("    code the same recording with a different coder ID.")

    print()
    print("=" * 74)
    if args.paste:
        print("  Copy everything above. It contains no file paths and no")
        print("  participant names beyond the session label.")
    else:
        print("  Share this with:  python coding_report.py --paste")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
