# -*- coding: utf-8 -*-
"""
LLM–human agreement kit (the thesis's main validation instrument).

The central claim of a method-validation study using LLM feedback is
that the LLM's judgments agree with human experts. This kit provides
both halves of that workflow:

1. ``export`` — builds Excel RATING SHEETS from the logged structured
   LLM output (``data/llm_logs/*.json``). Each sheet lists the LLM's
   phases (time range, attended object, criteria_met) next to EMPTY
   columns for a human rater who watches the same recording in the
   replay tool (`/review`) and codes it against the same rubric.

2. ``kappa`` — reads filled-in sheets and computes Cohen's kappa and
   raw percentage agreement between the LLM's ``criteria_met`` and the
   human's, per sheet and pooled. κ > 0.6 is the conventional bar for
   substantial agreement (Landis & Koch, 1977).

Usage:
    python agreement_kit.py export                 # all logged recordings
    python agreement_kit.py export --session S     # filter by session
    python agreement_kit.py kappa data/agreement/*.xlsx
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys

import pandas as pd

from config import DATA_DIR, LLM_LOG_DIR
from excel_style import style_workbook

AGREEMENT_DIR = os.path.join(DATA_DIR, "agreement")

LLM_COLUMNS = ["phase", "t_start_s", "t_end_s", "llm_attended",
               "llm_criteria_met", "llm_confidence"]
HUMAN_COLUMNS = ["human_attended", "human_criteria_met", "human_notes"]


# ──────────────────────────────────────────────────────────────
# Export: LLM logs → rating sheets
# ──────────────────────────────────────────────────────────────

def _extract_structured(text: str) -> "list | None":
    m = re.search(r"```json\s*(.*?)```", text, re.DOTALL)
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
        return data if isinstance(data, list) else None
    except ValueError:
        return None


def load_evaluations(session_filter: str = "",
                     stimulus_filter: str = "") -> dict:
    """Latest structured evaluation per (session, stimulus) from the logs."""
    latest: dict[tuple, dict] = {}
    for path in sorted(glob.glob(os.path.join(LLM_LOG_DIR, "*.json"))):
        try:
            with open(path, encoding="utf-8") as fh:
                log = json.load(fh)
        except (OSError, ValueError):
            continue
        if not str(log.get("step", "")).startswith("evaluation_run"):
            continue
        ctx = log.get("context", {})
        session = str(ctx.get("session", ""))
        stimulus = str(ctx.get("stimulus", ""))
        if session_filter and session_filter not in session:
            continue
        if stimulus_filter and stimulus_filter not in stimulus:
            continue
        structured = _extract_structured(log.get("response_text", ""))
        if not structured:
            continue
        # sorted() over filenames = chronological (ns-timestamp prefix),
        # so later files overwrite earlier ones → latest run wins.
        latest[(session, stimulus)] = {
            "context": ctx,
            "model": log.get("model"),
            "structured": structured,
            "log_file": os.path.basename(path),
        }
    return latest


def export(session_filter: str = "", stimulus_filter: str = "") -> None:
    evaluations = load_evaluations(session_filter, stimulus_filter)
    if not evaluations:
        print("No structured LLM evaluations found in", LLM_LOG_DIR)
        print("Generate feedback in the review tool first "
              "(http://localhost:5050/review).")
        return
    os.makedirs(AGREEMENT_DIR, exist_ok=True)

    for (session, stimulus), ev in evaluations.items():
        rows = []
        for i, phase in enumerate(ev["structured"], start=1):
            if not isinstance(phase, dict):
                continue
            rows.append({
                "phase": i,
                "t_start_s": phase.get("t_start"),
                "t_end_s": phase.get("t_end"),
                "llm_attended": phase.get("attended"),
                "llm_criteria_met": phase.get("criteria_met"),
                "llm_confidence": phase.get("confidence"),
                "human_attended": "",
                "human_criteria_met": "",
                "human_notes": "",
            })
        if not rows:
            continue
        df = pd.DataFrame(rows, columns=LLM_COLUMNS + HUMAN_COLUMNS)
        safe = "".join(c if c.isalnum() or c in "-_." else "_"
                       for c in "%s__%s" % (session, stimulus))
        out = os.path.join(AGREEMENT_DIR, "rating_%s.xlsx" % safe)

        with pd.ExcelWriter(out, engine="openpyxl") as writer:
            df.to_excel(writer, index=False, sheet_name="ratings")
            info = pd.DataFrame({"Instructions": [
                "1. Open the replay tool (http://localhost:5050/review) "
                "and select this recording:",
                "   participant: %s" % ev["context"].get("participant", "?"),
                "   session:     %s" % session,
                "   stimulus:    %s" % stimulus,
                "2. Watch the replay with the gaze overlay, using the SAME "
                "rubric the LLM received:",
                "   %s" % (ev["context"].get("rubric") or "(none provided)"),
                "3. For each phase (time range), fill in what the "
                "participant attended (human_attended)",
                "   and whether the criteria were met "
                "(human_criteria_met: TRUE / FALSE).",
                "4. Do NOT look at the llm_* columns until you are done "
                "(blind coding).",
                "",
                "LLM model: %s   ·   source log: %s"
                % (ev["model"], ev["log_file"]),
                "Afterwards run:  python agreement_kit.py kappa %s" % out,
            ]})
            info.to_excel(writer, index=False, sheet_name="instructions")
        style_workbook(out)
        print("Wrote %s  (%d phases)" % (out, len(rows)))


# ──────────────────────────────────────────────────────────────
# Kappa: filled sheets → agreement statistics
# ──────────────────────────────────────────────────────────────

def _to_bool(value) -> "bool | None":
    s = str(value).strip().lower()
    if s in ("true", "wahr", "yes", "ja", "1", "1.0"):
        return True
    if s in ("false", "falsch", "no", "nein", "0", "0.0"):
        return False
    return None


def cohens_kappa(a: "list[bool]", b: "list[bool]") -> float:
    """Cohen's kappa for two binary raters."""
    n = len(a)
    if n == 0:
        return float("nan")
    po = sum(1 for x, y in zip(a, b) if x == y) / n
    pa_true = sum(a) / n
    pb_true = sum(b) / n
    pe = pa_true * pb_true + (1 - pa_true) * (1 - pb_true)
    if pe == 1.0:
        return 1.0 if po == 1.0 else 0.0
    return (po - pe) / (1 - pe)


def kappa(paths: "list[str]") -> None:
    pooled_llm: list[bool] = []
    pooled_human: list[bool] = []
    for path in paths:
        try:
            df = pd.read_excel(path, sheet_name="ratings")
        except Exception as exc:  # noqa: BLE001
            print("! could not read %s: %s" % (path, exc))
            continue
        pairs = []
        for _, row in df.iterrows():
            lv = _to_bool(row.get("llm_criteria_met"))
            hv = _to_bool(row.get("human_criteria_met"))
            if lv is not None and hv is not None:
                pairs.append((lv, hv))
        name = os.path.basename(path)
        if not pairs:
            print("%s: no rated phases yet (fill human_criteria_met "
                  "with TRUE/FALSE)" % name)
            continue
        llm = [p[0] for p in pairs]
        human = [p[1] for p in pairs]
        pooled_llm += llm
        pooled_human += human
        po = sum(1 for x, y in pairs if x == y) / len(pairs)
        print("%s: n=%d  agreement=%.0f %%  kappa=%.2f"
              % (name, len(pairs), 100 * po, cohens_kappa(llm, human)))

    if pooled_llm:
        po = sum(1 for x, y in zip(pooled_llm, pooled_human) if x == y) \
            / len(pooled_llm)
        k = cohens_kappa(pooled_llm, pooled_human)
        print("\nPOOLED: n=%d  agreement=%.0f %%  Cohen's kappa=%.2f"
              % (len(pooled_llm), 100 * po, k))
        verdict = ("substantial" if k > 0.6 else
                   "moderate" if k > 0.4 else "weak")
        print("Interpretation (Landis & Koch): %s agreement "
              "(κ > 0.6 is the conventional bar)" % verdict)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_exp = sub.add_parser("export", help="build rating sheets from LLM logs")
    p_exp.add_argument("--session", default="", help="filter by session id")
    p_exp.add_argument("--stimulus", default="", help="filter by stimulus")
    p_kap = sub.add_parser("kappa", help="compute agreement from filled sheets")
    p_kap.add_argument("sheets", nargs="+", help="rating sheet .xlsx files")
    args = parser.parse_args()

    if args.cmd == "export":
        export(args.session, args.stimulus)
    else:
        kappa(args.sheets)


if __name__ == "__main__":
    main()
