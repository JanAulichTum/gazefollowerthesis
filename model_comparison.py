# -*- coding: utf-8 -*-
"""
Run the SAME feedback payload through N multimodal models and compare them.

WHAT THIS DOES AND DOES NOT ESTABLISH
-------------------------------------
Running several models answers "is the result specific to one vendor?"
It does NOT establish that any of them is right: models share training
data, architectures and failure modes, so they can agree and all be
wrong. Inter-model agreement is RELIABILITY, not validity.

Validity comes from two other places, and this module reports both
alongside the agreement figure so they are never confused:

    correspondence_pct   each model's spatial claims scored against the
                         RECORDED GAZE (claim_check.py). The gaze is an
                         independent measurement, so this is a genuine
                         accuracy figure — and it is fully automatic.
    Cohen's kappa        the model's evaluative judgment against HUMAN
                         coders (agreement_kit.py). Only humans can
                         anchor a judgment; run it on a SAMPLE.

FAIRNESS
--------
Every model receives the byte-identical payload that app.py prepared for
the reference run — same frames, same marker radius, same stated
uncertainty, same task text — replayed from data/llm_replay/. If the
prompt were rebuilt per model the comparison would confound model
identity with prompt drift. Temperature is pinned to 0 where the API
exposes it.

CONFIGURATION
-------------
data/models.json, any number of entries::

    [
      {"name": "gemini-3.5-flash", "provider": "gemini",
       "model": "gemini-3.5-flash", "api_key_env": "GEMINI_API_KEY"},
      {"name": "gpt-5", "provider": "openai",
       "model": "gpt-5", "api_key_env": "OPENAI_API_KEY"},
      {"name": "claude-opus-5", "provider": "anthropic",
       "model": "claude-opus-5", "api_key_env": "ANTHROPIC_API_KEY"}
    ]

``provider: "mock"`` needs no key and is used by the test suite.

Usage::

    python model_comparison.py --list
    python model_comparison.py --demo
    python model_comparison.py <replay.json>
    python model_comparison.py <replay.json> --models gemini-3.5-flash,gpt-5
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

BASE = os.path.dirname(os.path.abspath(__file__))
REPLAY_DIR = os.path.join(BASE, "data", "llm_replay")
OUT_DIR = os.path.join(BASE, "data", "model_comparison")
MODELS_FILE = os.path.join(BASE, "data", "models.json")

TIMEOUT_S = 180


# ──────────────────────────────────────────────────────────────────────
# Providers
# ──────────────────────────────────────────────────────────────────────

def _post(url: str, body: dict, headers: dict) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _parts_to_openai(parts: list) -> list:
    """Gemini-style parts -> OpenAI chat content blocks."""
    content = []
    for p in parts:
        if "text" in p:
            content.append({"type": "text", "text": p["text"]})
        elif "inline_data" in p or "inlineData" in p:
            d = p.get("inline_data") or p.get("inlineData")
            content.append({
                "type": "image_url",
                "image_url": {"url": "data:%s;base64,%s"
                              % (d.get("mime_type") or d.get("mimeType",
                                                             "image/jpeg"),
                                 d.get("data"))},
            })
    return content


def _parts_to_anthropic(parts: list) -> list:
    content = []
    for p in parts:
        if "text" in p:
            content.append({"type": "text", "text": p["text"]})
        elif "inline_data" in p or "inlineData" in p:
            d = p.get("inline_data") or p.get("inlineData")
            content.append({
                "type": "image",
                "source": {"type": "base64",
                           "media_type": d.get("mime_type")
                           or d.get("mimeType", "image/jpeg"),
                           "data": d.get("data")},
            })
    return content


def call_gemini(cfg: dict, parts: list, key: str, max_tokens: int) -> str:
    url = ("https://generativelanguage.googleapis.com/v1beta/models/"
           "%s:generateContent?key=%s" % (cfg["model"], key))
    data = _post(url, {
        "contents": [{"parts": parts}],
        "generationConfig": {"maxOutputTokens": max_tokens, "temperature": 0},
    }, {})
    cands = data.get("candidates") or []
    if not cands:
        raise RuntimeError("no candidates: %s" % str(data)[:200])
    out = []
    for p in (cands[0].get("content") or {}).get("parts") or []:
        if "text" in p:
            out.append(p["text"])
    return "".join(out)


def call_openai(cfg: dict, parts: list, key: str, max_tokens: int) -> str:
    url = cfg.get("base_url", "https://api.openai.com/v1") + "/chat/completions"
    body = {
        "model": cfg["model"],
        "messages": [{"role": "user", "content": _parts_to_openai(parts)}],
        "max_completion_tokens": max_tokens,
    }
    if cfg.get("supports_temperature", True):
        body["temperature"] = 0
    data = _post(url, body, {"Authorization": "Bearer %s" % key})
    return data["choices"][0]["message"]["content"]


def call_anthropic(cfg: dict, parts: list, key: str, max_tokens: int) -> str:
    data = _post("https://api.anthropic.com/v1/messages", {
        "model": cfg["model"],
        "max_tokens": max_tokens,
        "temperature": 0,
        "messages": [{"role": "user", "content": _parts_to_anthropic(parts)}],
    }, {"x-api-key": key, "anthropic-version": "2023-06-01"})
    return "".join(b.get("text", "") for b in data.get("content", []))


def call_mock(cfg: dict, parts: list, key: str, max_tokens: int) -> str:
    """Deterministic stand-in, so the harness is testable without keys."""
    return cfg.get("canned_response", "```json\n[]\n```")


PROVIDERS = {"gemini": call_gemini, "openai": call_openai,
             "anthropic": call_anthropic, "mock": call_mock}


# ──────────────────────────────────────────────────────────────────────
# Parsing and agreement
# ──────────────────────────────────────────────────────────────────────

def extract_structured(text: str) -> "list | None":
    """The ```json …``` block the prompt requires."""
    if not text:
        return None
    m = re.search(r"```json\s*(.+?)```", text, re.S)
    if not m:
        m = re.search(r"(\[\s*\{.+?\}\s*\])", text, re.S)
    if not m:
        return None
    try:
        parsed = json.loads(m.group(1).strip())
        return parsed if isinstance(parsed, list) else None
    except ValueError:
        return None


def fleiss_kappa(ratings: list) -> "float | None":
    """Fleiss' kappa for N raters over items with categorical labels.

    ``ratings``: one list per ITEM, holding each rater's label (None =
    that rater did not label the item; such items are skipped, because
    Fleiss assumes a fixed number of raters per item).

    Used here for CROSS-MODEL agreement on criteria_met. Note what this
    is not: high kappa across models means they are consistent, not
    correct. Reported next to correspondence_pct for exactly that reason.
    """
    rows = [r for r in ratings if r and all(v is not None for v in r)]
    if len(rows) < 2:
        return None
    n = len(rows[0])
    if n < 2 or any(len(r) != n for r in rows):
        return None
    cats = sorted({v for r in rows for v in r}, key=str)
    if len(cats) < 2:
        return 1.0                     # everyone agreed on everything
    N = len(rows)
    p_i = []
    cat_totals = {c: 0 for c in cats}
    for r in rows:
        counts = {c: r.count(c) for c in cats}
        for c in cats:
            cat_totals[c] += counts[c]
        p_i.append((sum(v * v for v in counts.values()) - n) / (n * (n - 1)))
    p_bar = sum(p_i) / N
    p_e = sum((cat_totals[c] / (N * n)) ** 2 for c in cats)
    if p_e >= 1.0:
        return 1.0
    return round((p_bar - p_e) / (1 - p_e), 3)


def load_models(names: "str | None" = None) -> list:
    if not os.path.isfile(MODELS_FILE):
        return []
    with open(MODELS_FILE, encoding="utf-8") as fh:
        models = json.load(fh)
    if names:
        wanted = {n.strip() for n in names.split(",") if n.strip()}
        models = [m for m in models if m.get("name") in wanted]
    return models


def run_models(parts: list, models: list, max_tokens: int = 4000) -> dict:
    """Call every configured model with the identical payload."""
    out = {}
    for cfg in models:
        name = cfg.get("name") or cfg.get("model")
        fn = PROVIDERS.get(cfg.get("provider"))
        if fn is None:
            out[name] = {"ok": False, "error": "unknown provider %r"
                         % cfg.get("provider")}
            continue
        key = os.environ.get(cfg.get("api_key_env", ""), "")
        if not key and cfg.get("provider") != "mock":
            out[name] = {"ok": False,
                         "error": "no API key in $%s" % cfg.get("api_key_env")}
            continue
        try:
            text = fn(cfg, parts, key, max_tokens)
            out[name] = {"ok": True, "text": text,
                         "structured": extract_structured(text),
                         "model": cfg.get("model")}
        except (urllib.error.URLError, urllib.error.HTTPError,
                KeyError, ValueError, RuntimeError) as exc:
            out[name] = {"ok": False, "error": str(exc)[:200]}
    return out


def compare(results: dict, samples: list, accuracy_deg: float,
            px_per_deg: float, video_w: int, video_h: int,
            expected_units: "int | None" = None) -> dict:
    """Score every model's claims against the gaze, then cross-compare."""
    import claim_check

    per_model = {}
    for name, r in results.items():
        if not r.get("ok") or not r.get("structured"):
            per_model[name] = {"ok": False,
                               "error": r.get("error", "no structured output")}
            continue
        scored = claim_check.check_all(r["structured"], samples, accuracy_deg,
                                       px_per_deg, video_w, video_h)
        n = len(r["structured"])
        entry = {"ok": True, **scored, "n_claims": n}
        # TRUNCATION GUARD. With one unit per fixation the count is known
        # in advance. A model that returns fewer has been cut off, and the
        # missing units are always the LAST ones — so its correspondence
        # score would describe only the start of the video while looking
        # like a whole-video figure.
        if expected_units:
            entry["expected_units"] = expected_units
            entry["complete"] = (n == expected_units)
            if n < expected_units:
                entry["truncated"] = True
                entry["truncation_note"] = (
                    "returned %d of %d units — output was cut off; the "
                    "MISSING units are the end of the video, so this "
                    "model's score is not comparable" % (n, expected_units))
            elif n > expected_units:
                entry["truncation_note"] = (
                    "returned %d units, more than the %d requested — the "
                    "model ignored the unit definition" % (n, expected_units))
        per_model[name] = entry

    # Cross-model agreement on criteria_met, aligned by phase index.
    # Index alignment is crude but honest: the models are asked for the
    # same phase structure, and any mismatch in length is reported rather
    # than silently truncated.
    ok_names = [n for n, v in per_model.items()
                if v.get("ok") and v.get("truncated") is not True]
    lengths = {n: len(results[n]["structured"]) for n in ok_names}
    ratings = []
    if len(ok_names) >= 2 and len(set(lengths.values())) == 1:
        for i in range(next(iter(lengths.values()))):
            ratings.append([results[n]["structured"][i].get("criteria_met")
                            for n in ok_names])
    return {
        "per_model": per_model,
        "models_compared": ok_names,
        "phase_counts": lengths,
        "phase_counts_match": len(set(lengths.values())) == 1 if lengths else None,
        "cross_model_kappa_criteria_met": fleiss_kappa(ratings),
        "truncated_models": [n for n, v in per_model.items()
                             if v.get("truncated")],
        "expected_units": expected_units,
        "kappa_note": "Cross-model agreement is RELIABILITY, not validity. "
                      "Validity is correspondence_pct (vs recorded gaze) and "
                      "Cohen's kappa vs human coders.",
    }


# ──────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────

def _demo() -> int:
    import claim_check
    import metrics_spec

    ppd = metrics_spec.px_per_degree()
    samples = ([(t / 10.0, 0.15, 0.60, True) for t in range(0, 80)]
               + [(t / 10.0, 0.50, 0.60, True) for t in range(80, 160)])
    good = json.dumps([
        {"t_start": 0, "t_end": 8, "attended": "left student",
         "bbox": [0.02, 0.35, 0.30, 0.55], "criteria_met": True},
        {"t_start": 8, "t_end": 16, "attended": "centre group",
         "bbox": [0.35, 0.35, 0.30, 0.55], "criteria_met": True}])
    poor = json.dumps([
        {"t_start": 0, "t_end": 8, "attended": "the whiteboard",
         "bbox": [0.30, 0.00, 0.40, 0.30], "criteria_met": False},
        {"t_start": 8, "t_end": 16, "attended": "centre group",
         "bbox": [0.35, 0.35, 0.30, 0.55], "criteria_met": True}])
    models = [
        {"name": "model-A", "provider": "mock",
         "canned_response": "prose\n```json\n%s\n```" % good},
        {"name": "model-B", "provider": "mock",
         "canned_response": "prose\n```json\n%s\n```" % poor},
        {"name": "model-C", "provider": "mock",
         "canned_response": "prose\n```json\n%s\n```" % good},
    ]
    res = run_models([{"text": "demo"}], models)
    cmp_ = compare(res, samples, 2.2, ppd, 1920, 1080)
    _print_report(cmp_, "demo (mock models, synthetic gaze)")
    return 0


def _print_report(cmp_: dict, title: str) -> None:
    print("=" * 76)
    print("  MODEL COMPARISON — %s" % title)
    print("=" * 76)
    print("  %-18s %10s %10s %10s %10s"
          % ("model", "claims", "testable", "supported", "corresp."))
    print("  " + "-" * 62)
    for name, v in cmp_["per_model"].items():
        if not v.get("ok"):
            print("  %-18s  failed: %s" % (name, str(v.get("error"))[:40]))
            continue
        c = v["counts"]
        print("  %-18s %10d %10d %10d %9s%%"
              % (name, v["n_claims"], v["n_testable"],
                 c["SUPPORTED"], v["correspondence_pct"]))
    print()
    for _n in cmp_.get("truncated_models") or []:
        print("  !! %s was TRUNCATED and is EXCLUDED from the agreement "
              "analysis" % _n)
        print("     %s" % cmp_["per_model"][_n].get("truncation_note", ""))
    if cmp_.get("truncated_models"):
        print()
    k = cmp_.get("cross_model_kappa_criteria_met")
    print("  cross-model Fleiss kappa on criteria_met: %s"
          % ("n/a" if k is None else k))
    if not cmp_.get("phase_counts_match"):
        print("  !! models returned different phase counts %s — "
              "index alignment is unreliable" % cmp_.get("phase_counts"))
    print()
    for line in (cmp_["kappa_note"][i:i + 70]
                 for i in range(0, len(cmp_["kappa_note"]), 70)):
        print("  " + line)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("replay", nargs="?", help="a data/llm_replay/*.json file")
    ap.add_argument("--models", help="comma-separated subset of models.json")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--demo", action="store_true")
    args = ap.parse_args()

    if args.list:
        models = load_models()
        if not models:
            print("No %s. Create it — see the module docstring."
                  % os.path.relpath(MODELS_FILE, BASE))
            return 1
        for m in models:
            key = os.environ.get(m.get("api_key_env", ""), "")
            print("  %-22s %-11s %-26s key:%s"
                  % (m.get("name"), m.get("provider"), m.get("model"),
                     "set" if key or m.get("provider") == "mock" else "MISSING"))
        return 0

    if args.demo:
        return _demo()

    path = args.replay
    if not path:
        files = sorted(glob.glob(os.path.join(REPLAY_DIR, "*.json")))
        if not files:
            print("No replay payloads in %s. Generate feedback once in the "
                  "app first — it saves the exact payload there."
                  % os.path.relpath(REPLAY_DIR, BASE))
            return 1
        path = files[-1]

    with open(path, encoding="utf-8") as fh:
        replay = json.load(fh)
    models = load_models(args.models)
    if not models:
        print("No models configured. See --list.")
        return 1

    print("Replaying %s to %d model(s)…"
          % (os.path.basename(path), len(models)))
    results = run_models(replay["parts"], models,
                         replay.get("max_tokens", 4000))
    print("NOTE: gaze samples must be supplied to score correspondence; "
          "this run reports raw outputs only.")
    os.makedirs(OUT_DIR, exist_ok=True)
    out = os.path.join(OUT_DIR, "%s_%s.json" % (
        datetime.now().strftime("%Y-%m-%d_%H%M%S"),
        os.path.splitext(os.path.basename(path))[0]))
    with open(out, "w", encoding="utf-8") as fh:
        json.dump({"replay": os.path.basename(path), "results": results},
                  fh, indent=2, ensure_ascii=False)
    for name, r in results.items():
        print("  %-22s %s" % (name, "ok, %d claims"
                              % len(r.get("structured") or [])
                              if r.get("ok") else "FAILED: %s" % r.get("error")))
    print("\nSaved: %s" % out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
