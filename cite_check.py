# -*- coding: utf-8 -*-
"""
Verify every entry in a .bib file against independent bibliographic
databases, and write the result in CITATION_LEDGER.md's vocabulary.

WHY THIS EXISTS
---------------
The failure this project actually suffered was NOT a made-up reference.
It was `scoping2025`, the most-cited entry in the v1 literature review,
which turned out to be **three different works fused into one entry**:

    title   -> Patterson, Nicklin & Vitta (2025), Res. Methods Appl. Ling.
    content -> Lau & Kasneci (2026), PACM HCI (ETRA), 10.1145/3806017
    DOI     -> a real but different CHBR article

Every field was individually plausible. A checker that only asks "does
this reference exist?" passes it, because each piece exists. The check
that catches it is CROSS-FIELD CONSISTENCY: resolve the DOI, then ask
whether the work it returns is the work the entry claims to be.

So this tool reports two independent things per entry:

  EXISTENCE   how many databases return a matching work
  COHERENCE   whether the entry's own fields agree with each other and
              with the resolved record

An entry can exist in all three databases and still be incoherent. That
combination is the dangerous one and it is reported first.

Second thing it checks: RETRACTION. The ledger already records that
Holmqvist et al. (2023) — the standard eye-tracking data-quality
reference — is retracted. A retracted work cited as live support is
worse than a missing one, and it is invisible to a plain existence
check.

VERDICTS (deliberately the same three words CITATION_LEDGER.md uses)
--------------------------------------------------------------------
  VERIFIED   >= 2 independent databases return a work matching this
             entry on title AND author, with no coherence flag.
  LISTED     exactly 1 database matched, no coherence flag. The work is
             probably real; nobody has opened it.
  TO VERIFY  0 databases matched, OR any coherence flag fired. Needs a
             human. The flag says what to look at.

Two sources rather than one is the whole point: databases mirror each
other's errors, but a fused entry rarely survives two independent
lookups keyed on different fields.

WHAT IT DOES NOT DO
-------------------
It does not open the paper and it cannot tell you whether the SENTENCE
you wrote is supported by the source. That is what CITATION_LEDGER.md's
VERIFIED tier means and this tool cannot promote an entry to it on its
own — it can only tell you the reference identifies a real, coherent,
un-retracted work. Reading remains yours.

It never modifies the .bib file. Output is a report.

USAGE
-----
    python cite_check.py --bib ../thesis-draft/references.bib \
                         --mailto you@tum.de
    python cite_check.py --bib ../thesis-draft/references.bib --json out.json
    python cite_check.py --self-test        # offline, uses fixtures

`--mailto` goes into the Crossref "polite pool" (faster, and they will
tell you before they rate-limit you). Use it.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

# Windows consoles are cp1252; piping through a subprocess makes Python
# use it even on 3.12, so one non-ASCII character would kill the run
# mid-report. Same rule as every other CLI tool in this project.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

USER_AGENT = "cite_check/1.0 (master thesis; mailto:%s)"
TIMEOUT_S = 20
SLEEP_S = 0.4            # be polite; arXiv asks 3 s, Crossref far less

# Title similarity above which two titles are "the same work". Chosen
# high: the point is to catch a DIFFERENT work sitting behind a DOI, and
# a low threshold would wave that through.
TITLE_MATCH = 0.82
# Below this, a DOI is pointing somewhere else entirely.
TITLE_MISMATCH = 0.55


# ── text normalisation ────────────────────────────────────────────────

def _fold(text: str) -> str:
    """Lowercase, strip accents/LaTeX/punctuation. Comparison only."""
    text = re.sub(r"\\[a-zA-Z]+\s*", " ", text or "")
    # LaTeX accent escapes are backslash + a NON-letter: \"o \'e \^a \~n
    # \=u \.z. Drop the escape, keep the letter — otherwise Nystr{\"o}m
    # splits into "nystr" + "om" and stops matching "Nystrom".
    text = re.sub(r"\\([^a-zA-Z])", "", text)
    text = text.replace("{", "").replace("}", "").replace("$", "")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower()
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def title_similarity(a: str, b: str) -> float:
    """Token-level Jaccard-with-order-bonus. Deliberately simple: no
    dependency, and a fused title shares few tokens with the work its
    DOI actually points to, so a crude measure separates them cleanly."""
    ta, tb = set(_fold(a).split()), set(_fold(b).split())
    if not ta or not tb:
        return 0.0
    stop = {"a", "an", "the", "of", "and", "in", "on", "for", "to",
            "with", "from", "by", "at", "is", "are"}
    ta, tb = (ta - stop) or ta, (tb - stop) or tb
    return len(ta & tb) / len(ta | tb)


def surnames(author_field: str) -> "list[str]":
    """Surnames from a BibTeX author field, both name orders."""
    out = []
    for chunk in re.split(r"\s+and\s+", author_field or ""):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "," in chunk:                      # "Surname, Given"
            out.append(_fold(chunk.split(",")[0]))
        else:                                  # "Given Surname"
            parts = _fold(chunk).split()
            if parts:
                out.append(parts[-1])
    return [s for s in out if s]


def author_overlap(bib_authors: str, record_surnames: "list[str]") -> float:
    """Fraction of the entry's surnames that appear in the record."""
    mine = set(surnames(bib_authors))
    theirs = {_fold(s) for s in record_surnames if s}
    if not mine or not theirs:
        return -1.0                            # unknown, not zero
    return len(mine & theirs) / len(mine)


# ── .bib parsing ──────────────────────────────────────────────────────

_ENTRY_RE = re.compile(r"@(\w+)\s*\{\s*([^,\s]+)\s*,", re.M)


def parse_bib(text: str) -> "list[dict]":
    """Parse entries. Brace-counting, so nested braces in titles survive.

    Not a general BibTeX parser — it does not expand @string macros or
    crossrefs. It is enough for a hand-maintained thesis bibliography,
    and it fails loudly (missing fields) rather than silently.
    """
    entries = []
    for m in _ENTRY_RE.finditer(text):
        start = m.end()
        depth, i = 1, start
        while i < len(text) and depth:
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
            i += 1
        body = text[start:i - 1]
        fields, j = {}, 0
        while j < len(body):
            fm = re.compile(r"(\w+)\s*=\s*").match(body, j)
            if not fm:
                j += 1
                continue
            k, j = fm.group(1).lower(), fm.end()
            if j < len(body) and body[j] == "{":
                d, s = 1, j + 1
                j += 1
                while j < len(body) and d:
                    if body[j] == "{":
                        d += 1
                    elif body[j] == "}":
                        d -= 1
                    j += 1
                val = body[s:j - 1]
            elif j < len(body) and body[j] == '"':
                s = j + 1
                j = body.find('"', s)
                val = body[s:j if j > 0 else len(body)]
                j = (j + 1) if j > 0 else len(body)
            else:
                s = j
                while j < len(body) and body[j] not in ",\n":
                    j += 1
                val = body[s:j]
            fields[k] = re.sub(r"\s+", " ", val).strip()
        entries.append({"type": m.group(1).lower(), "key": m.group(2),
                        "fields": fields})
    return entries


# ── database lookups ──────────────────────────────────────────────────

def _get_json(url: str, mailto: str) -> "dict | None":
    req = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT % (mailto or "anonymous"),
                      "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        return {"__error__": "HTTP %d" % exc.code}
    except Exception as exc:  # noqa: BLE001
        return {"__error__": str(exc)[:120]}


def _norm_record(title, authors, year, venue, doi, retracted=False,
                 source="") -> dict:
    return {"title": title or "", "authors": authors or [],
            "year": year, "venue": venue or "", "doi": (doi or "").lower(),
            "retracted": bool(retracted), "source": source}


def crossref_by_doi(doi: str, mailto: str) -> "dict | None":
    data = _get_json("https://api.crossref.org/works/%s?mailto=%s"
                     % (urllib.parse.quote(doi), urllib.parse.quote(
                         mailto or "anonymous@example.org")), mailto)
    if not data or "__error__" in data or "message" not in data:
        return data if data and "__error__" in data else None
    return _crossref_item(data["message"])


def _crossref_item(it: dict) -> dict:
    # Crossref marks a retraction with an `update-to` on the NOTICE and,
    # for the retracted work, `updated-by` carrying type "retraction".
    upd = it.get("updated-by") or []
    retracted = any((u.get("type") or "").lower() in
                    ("retraction", "withdrawal", "removal") for u in upd)
    if (it.get("type") or "") in ("retraction", "withdrawal"):
        retracted = True
    auth = [a.get("family", "") for a in (it.get("author") or [])]
    dparts = ((it.get("issued") or {}).get("date-parts") or [[None]])[0]
    return _norm_record(
        (it.get("title") or [""])[0], auth, dparts[0] if dparts else None,
        (it.get("container-title") or [""])[0] if it.get("container-title")
        else "", it.get("DOI"), retracted, "crossref")


def crossref_by_title(title: str, mailto: str) -> "dict | None":
    data = _get_json(
        "https://api.crossref.org/works?rows=5&select=DOI,title,author,"
        "issued,container-title,type,updated-by&query.bibliographic=%s"
        "&mailto=%s" % (urllib.parse.quote(title[:300]),
                        urllib.parse.quote(mailto or "anonymous@example.org")),
        mailto)
    if not data or "__error__" in data:
        return data
    best, best_s = None, 0.0
    for it in ((data.get("message") or {}).get("items") or []):
        rec = _crossref_item(it)
        s = title_similarity(title, rec["title"])
        if s > best_s:
            best, best_s = rec, s
    return best


def openalex_by_title(title: str, mailto: str) -> "dict | None":
    data = _get_json(
        "https://api.openalex.org/works?per-page=5&search=%s&mailto=%s"
        % (urllib.parse.quote(title[:300]),
           urllib.parse.quote(mailto or "anonymous@example.org")), mailto)
    if not data or "__error__" in data:
        return data
    best, best_s = None, 0.0
    for it in (data.get("results") or []):
        auth = [(a.get("author") or {}).get("display_name", "").split()[-1]
                for a in (it.get("authorships") or [])
                if (a.get("author") or {}).get("display_name")]
        rec = _norm_record(
            it.get("display_name"), auth, it.get("publication_year"),
            ((it.get("primary_location") or {}).get("source") or {})
            .get("display_name", ""),
            (it.get("doi") or "").replace("https://doi.org/", ""),
            bool(it.get("is_retracted")), "openalex")
        s = title_similarity(title, rec["title"])
        if s > best_s:
            best, best_s = rec, s
    return best


def semanticscholar_by_title(title: str, mailto: str) -> "dict | None":
    data = _get_json(
        "https://api.semanticscholar.org/graph/v1/paper/search?limit=5"
        "&fields=title,authors,year,venue,externalIds&query=%s"
        % urllib.parse.quote(title[:300]), mailto)
    if not data or "__error__" in data:
        return data
    best, best_s = None, 0.0
    for it in (data.get("data") or []):
        auth = [(a.get("name") or "").split()[-1]
                for a in (it.get("authors") or []) if a.get("name")]
        rec = _norm_record(
            it.get("title"), auth, it.get("year"), it.get("venue"),
            (it.get("externalIds") or {}).get("DOI"), False,
            "semanticscholar")
        s = title_similarity(title, rec["title"])
        if s > best_s:
            best, best_s = rec, s
    return best


# ── the actual judgement ──────────────────────────────────────────────

def assess(entry: dict, records: "list[dict]",
           doi_record: "dict | None") -> dict:
    """Combine lookups into a verdict plus coherence flags.

    `records` are title-keyed hits from independent databases.
    `doi_record` is what the entry's OWN DOI resolves to, or None.
    """
    f = entry["fields"]
    title = f.get("title", "")
    flags: "list[str]" = []
    notes: "list[str]" = []

    matched = [r for r in records
               if r and "__error__" not in r
               and title_similarity(title, r.get("title", "")) >= TITLE_MATCH]

    # --- COHERENCE 1: does the entry's DOI point at the entry's title?
    # This is the scoping2025 check and the reason this tool exists.
    if doi_record and "__error__" not in doi_record:
        sim = title_similarity(title, doi_record.get("title", ""))
        if sim <= TITLE_MISMATCH:
            flags.append("DOI_POINTS_ELSEWHERE")
            notes.append(
                'DOI %s resolves to "%s" (%s) — not this entry\'s title. '
                "Fields from different works may have been merged."
                % (f.get("doi", "?"),
                   (doi_record.get("title") or "?")[:90],
                   doi_record.get("year") or "?"))
        elif sim < TITLE_MATCH:
            notes.append("DOI title only partially matches (%.2f) — check."
                         % sim)

    # --- COHERENCE 2: right title, wrong people.
    for r in matched + ([doi_record] if doi_record
                        and "__error__" not in doi_record else []):
        ov = author_overlap(f.get("author", ""), r.get("authors") or [])
        if ov == 0.0:
            flags.append("AUTHOR_MISMATCH")
            notes.append(
                "no surname from this entry appears in the %s record "
                "(%s). A real title attributed to the wrong authors."
                % (r.get("source"), ", ".join((r.get("authors") or [])[:4])))
            break

    # --- COHERENCE 3: year drift.
    try:
        my_year = int(re.sub(r"[^0-9]", "", f.get("year", ""))[:4])
    except ValueError:
        my_year = None
    if my_year:
        for r in matched:
            if r.get("year") and abs(int(r["year"]) - my_year) > 1:
                flags.append("YEAR_MISMATCH")
                notes.append("entry says %d, %s says %s."
                             % (my_year, r.get("source"), r.get("year")))
                break

    # --- COHERENCE 4: retraction. Worse than missing.
    for r in matched + ([doi_record] if doi_record
                        and "__error__" not in doi_record else []):
        if r.get("retracted"):
            flags.append("RETRACTED")
            notes.append("%s flags this work as retracted or withdrawn. "
                         "Do not cite as live support." % r.get("source"))
            break

    errors = [r["__error__"] for r in records
              if r and "__error__" in r]
    if errors:
        notes.append("lookup errors: %s" % "; ".join(sorted(set(errors))))

    n = len({r["source"] for r in matched})
    if flags:
        verdict = "TO VERIFY"
    elif n >= 2:
        verdict = "VERIFIED"
    elif n == 1:
        verdict = "LISTED"
    else:
        verdict = "TO VERIFY"
        notes.append("no database returned a work matching this title.")

    return {"key": entry["key"], "title": title,
            "author": f.get("author", ""), "year": f.get("year", ""),
            "doi": f.get("doi", ""), "verdict": verdict,
            "sources_matched": sorted({r["source"] for r in matched}),
            "flags": sorted(set(flags)), "notes": notes}


def check_entry(entry: dict, mailto: str, sleep: float = SLEEP_S) -> dict:
    title = entry["fields"].get("title", "")
    doi = entry["fields"].get("doi", "").strip()
    doi = re.sub(r"^https?://(dx\.)?doi\.org/", "", doi, flags=re.I)
    records = []
    if title:
        for fn in (crossref_by_title, openalex_by_title,
                   semanticscholar_by_title):
            records.append(fn(title, mailto))
            time.sleep(sleep)
    doi_record = crossref_by_doi(doi, mailto) if doi else None
    if doi:
        time.sleep(sleep)
    return assess(entry, records, doi_record)


# ── reporting ─────────────────────────────────────────────────────────

_ORDER = {"TO VERIFY": 0, "LISTED": 1, "VERIFIED": 2}


def render_markdown(results: "list[dict]", bib_path: str,
                    stamp: str) -> str:
    out = ["# Citation check — `%s`" % os.path.basename(bib_path), "",
           "Automated cross-database check, %s. Produced by "
           "`cite_check.py`." % stamp, "",
           "- **VERIFIED** — two or more independent databases return "
           "this work and its fields are self-consistent. It does NOT "
           "mean the passage was read; only a human promotes an entry "
           "to VERIFIED in `CITATION_LEDGER.md`.",
           "- **LISTED** — one database matched. Probably real, "
           "unconfirmed.",
           "- **TO VERIFY** — nothing matched, or a coherence flag "
           "fired. Action stated per entry.", ""]
    counts = {}
    for r in results:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
    out += ["| verdict | entries |", "|---|---|"]
    for v in ("VERIFIED", "LISTED", "TO VERIFY"):
        out.append("| %s | %d |" % (v, counts.get(v, 0)))
    out.append("")

    flagged = [r for r in results if r["flags"]]
    if flagged:
        out += ["---", "", "## COHERENCE FLAGS — read these first", "",
                "These entries may exist in every database and still be "
                "wrong. Each field is plausible on its own; it is the "
                "combination that fails.", ""]
        for r in flagged:
            out.append("### `%s` — %s" % (r["key"], ", ".join(r["flags"])))
            out.append("")
            out.append("> %s" % (r["title"][:160] or "(no title)"))
            out.append("")
            for n in r["notes"]:
                out.append("- %s" % n)
            out.append("")

    for v in ("TO VERIFY", "LISTED", "VERIFIED"):
        group = [r for r in results if r["verdict"] == v]
        if not group:
            continue
        out += ["---", "", "## %s" % v, ""]
        for r in sorted(group, key=lambda x: x["key"]):
            src = ", ".join(r["sources_matched"]) or "none"
            out.append("- `%s` — %s (%s) — matched: %s"
                       % (r["key"], (r["title"][:90] or "(no title)"),
                          r["year"] or "?", src))
            for n in r["notes"]:
                if not r["flags"]:
                    out.append("  - %s" % n)
    out.append("")
    return "\n".join(out)


# ── self-test (offline) ───────────────────────────────────────────────

def _self_test() -> int:
    """Exercise the judgement on fixtures, including the real scoping2025
    failure. No network. Run this after touching the matching logic."""
    ok = True

    def t(name, cond):
        nonlocal ok
        print("  [%s] %s" % ("PASS" if cond else "FAIL", name))
        ok = ok and bool(cond)

    print("cite_check self-test")

    bib = parse_bib("""
@article{good2020, title={A theory of reading: From eye fixations to
  comprehension}, author={Just, Marcel A. and Carpenter, Patricia A.},
  year={1980}, doi={10.1037/0033-295X.87.4.329}}
@article{scoping2025, title={Methodological recommendations for
  webcam-based eye tracking: A scoping review}, author={Anonymous},
  year={2025}, doi={10.1016/j.chbr.2025.100655}}
""")
    t("parses two entries", len(bib) == 2)
    t("parses braced title with newlines",
      "scoping review" in bib[1]["fields"]["title"])
    t("parses doi", bib[1]["fields"]["doi"] == "10.1016/j.chbr.2025.100655")

    t("surnames, comma order", surnames("Just, Marcel A. and Carpenter, "
                                        "Patricia A.") == ["just", "carpenter"])
    t("surnames, natural order",
      surnames("Marcel A. Just and Patricia A. Carpenter")
      == ["just", "carpenter"])
    t("identical titles score 1.0",
      title_similarity("Eye movements in reading",
                       "Eye movements in reading") == 1.0)
    t("unrelated titles score low",
      title_similarity("Noise-robust fixation detection",
                       "A theory of reading") < TITLE_MISMATCH)
    t("LaTeX braces and accents fold away",
      title_similarity("{Eye} movements in Nystr{\\\"o}m reading",
                       "Eye movements in Nystrom reading") >= TITLE_MATCH)

    # THE REGRESSION THAT MATTERS: the DOI resolves to a real work with a
    # different title. Existence checks pass it; this must not.
    chimera = assess(
        bib[1],
        [_norm_record("Methodological recommendations for webcam-based eye "
                      "tracking: A scoping review", ["patterson"], 2025, "",
                      "", False, "crossref"),
         _norm_record("Methodological recommendations for webcam-based eye "
                      "tracking: A scoping review", ["patterson"], 2025, "",
                      "", False, "openalex")],
        _norm_record("Adoption of AI writing tools among second language "
                     "learners", ["someone"], 2025, "CHBR",
                     "10.1016/j.chbr.2025.100655", False, "crossref"))
    t("fused entry is NOT verified despite 2 title hits",
      chimera["verdict"] == "TO VERIFY")
    t("fused entry names the DOI problem",
      "DOI_POINTS_ELSEWHERE" in chimera["flags"])

    clean = assess(
        bib[0],
        [_norm_record("A theory of reading: From eye fixations to "
                      "comprehension", ["just", "carpenter"], 1980, "",
                      "", False, "crossref"),
         _norm_record("A theory of reading: From eye fixations to "
                      "comprehension", ["just", "carpenter"], 1980, "",
                      "", False, "openalex")],
        _norm_record("A theory of reading: From eye fixations to "
                     "comprehension", ["just", "carpenter"], 1980, "",
                     "10.1037/0033-295X.87.4.329", False, "crossref"))
    t("coherent entry with 2 sources is VERIFIED",
      clean["verdict"] == "VERIFIED")
    t("coherent entry has no flags", not clean["flags"])

    one = assess(bib[0],
                 [_norm_record("A theory of reading: From eye fixations to "
                               "comprehension", ["just", "carpenter"], 1980,
                               "", "", False, "crossref")], None)
    t("single source is LISTED, not VERIFIED", one["verdict"] == "LISTED")

    retr = assess(bib[0],
                  [_norm_record("A theory of reading: From eye fixations to "
                                "comprehension", ["just", "carpenter"], 1980,
                                "", "", True, "crossref"),
                   _norm_record("A theory of reading: From eye fixations to "
                                "comprehension", ["just", "carpenter"], 1980,
                                "", "", False, "openalex")], None)
    t("retracted work is never VERIFIED", retr["verdict"] == "TO VERIFY")
    t("retraction is named", "RETRACTED" in retr["flags"])

    wrong_people = assess(bib[0],
                          [_norm_record("A theory of reading: From eye "
                                        "fixations to comprehension",
                                        ["nobody", "else"], 1980, "", "",
                                        False, "crossref"),
                           _norm_record("A theory of reading: From eye "
                                        "fixations to comprehension",
                                        ["nobody", "else"], 1980, "", "",
                                        False, "openalex")], None)
    t("right title + wrong authors is flagged",
      "AUTHOR_MISMATCH" in wrong_people["flags"])

    unknown = assess(bib[0],
                     [_norm_record("A theory of reading: From eye fixations "
                                   "to comprehension", [], 1980, "", "",
                                   False, "crossref"),
                      _norm_record("A theory of reading: From eye fixations "
                                   "to comprehension", [], 1980, "", "",
                                   False, "openalex")], None)
    t("missing author metadata is not treated as a mismatch",
      "AUTHOR_MISMATCH" not in unknown["flags"])

    md = render_markdown([chimera, clean], "references.bib", "self-test")
    t("report puts coherence flags first",
      md.index("COHERENCE FLAGS") < md.index("## VERIFIED"))

    print("\n%s" % ("ALL PASS" if ok else "FAILURES ABOVE"))
    return 0 if ok else 1


# ── entry point ───────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(
        description="Verify a .bib against Crossref, OpenAlex and "
                    "Semantic Scholar; report in CITATION_LEDGER.md terms.")
    ap.add_argument("--bib", help="path to the .bib file")
    ap.add_argument("--mailto", default=os.environ.get("CITE_CHECK_MAILTO", ""),
                    help="your email — Crossref polite pool. Use it.")
    ap.add_argument("--out", help="write the markdown report here")
    ap.add_argument("--json", help="write raw results here")
    ap.add_argument("--only", help="check one entry key (debugging)")
    ap.add_argument("--sleep", type=float, default=SLEEP_S,
                    help="seconds between API calls (default %.1f)" % SLEEP_S)
    ap.add_argument("--self-test", action="store_true",
                    help="offline logic check, no network")
    args = ap.parse_args()

    if args.self_test:
        return _self_test()
    if not args.bib:
        ap.error("--bib is required (or use --self-test)")
    if not args.mailto:
        print("NOTE: no --mailto given. Crossref will still answer, but "
              "slower and without warning you before rate-limiting.\n")

    with open(args.bib, encoding="utf-8") as fh:
        entries = parse_bib(fh.read())
    if args.only:
        entries = [e for e in entries if e["key"] == args.only]
    print("Checking %d entries from %s\n" % (len(entries), args.bib))

    results = []
    for i, e in enumerate(entries, 1):
        r = check_entry(e, args.mailto, args.sleep)
        results.append(r)
        mark = {"VERIFIED": "ok ", "LISTED": " ? ", "TO VERIFY": "!! "}
        print("  %s[%2d/%2d] %-24s %s%s"
              % (mark.get(r["verdict"], "   "), i, len(entries), r["key"],
                 r["verdict"],
                 (" — " + ", ".join(r["flags"])) if r["flags"] else ""))

    stamp = time.strftime("%Y-%m-%d %H:%M")
    md = render_markdown(results, args.bib, stamp)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(md)
        print("\nReport: %s" % args.out)
    else:
        print("\n" + md)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump({"bib": args.bib, "checked": stamp,
                       "results": results}, fh, indent=2, ensure_ascii=False)
        print("JSON:   %s" % args.json)

    flagged = sum(1 for r in results if r["flags"])
    missing = sum(1 for r in results
                  if r["verdict"] == "TO VERIFY" and not r["flags"])
    print("\nVERDICT: %d coherence-flagged, %d unmatched, %d of %d verified."
          % (flagged, missing,
             sum(1 for r in results if r["verdict"] == "VERIFIED"),
             len(results)))
    if flagged:
        print("Coherence flags are the ones that matter. An entry can "
              "exist everywhere and still describe no single real work.")
    return 1 if flagged else 0


if __name__ == "__main__":
    raise SystemExit(main())
