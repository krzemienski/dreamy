"""Self-contained project bundle (R14). One HTML file: report, all four
prompts, git state, evidence index, recurring goals, cost rollup. CSS+JS
inlined. No external assets. Canary must be absent in final bytes.
"""
from __future__ import annotations

import html
import os
import re
import tempfile
from pathlib import Path

from .read import ReadStore


def _iso(ms: int | None) -> str:
    if not ms:
        return "(unknown)"
    from datetime import UTC, datetime

    return datetime.fromtimestamp(ms / 1000, tz=UTC).isoformat(timespec="seconds")


def _portable_path(p: str) -> str:
    """Rewrite an absolute path under $HOME to a portable `~/`-relative form.

    A bundle is designed to be handed to another person, so rendering
    `/Users/<operator>/project` discloses both the account name and the local
    filesystem layout. `~/project` carries the same meaning to a reader
    without naming the machine. Paths outside $HOME are returned unchanged —
    they are not the operator's private layout and silently rewriting them
    would misrepresent where the work actually lives.
    """
    home = str(Path.home())
    if p == home:
        return "~"
    if p.startswith(home + os.sep):
        return "~" + p[len(home):]
    return p

def _home_path_forms() -> dict[str, str]:
    """Every encoding of `$HOME` that must not survive into a bundle.

    ONE definition, consumed by both the sanitizer and the fail-closed guard.
    They previously built their own lists, and a guard that knows fewer forms
    than the sanitizer is not fail-closed — it reports clean precisely when the
    sanitizer has a gap, which is the only case that matters. That asymmetry is
    how a separator-substituted home path reached a shipped bundle while the
    guard passed.

    Derived from `Path.home()` at call time, never a stored constant: the
    account name must not be embedded in this module's source, and deriving it
    keeps the check correct on any machine.
    """
    home = Path.home()
    parts = [seg for seg in home.parts if seg not in ("/", "\\")]
    forms = {"literal": str(home)}
    if parts:
        forms["dash-encoded"] = "-" + "-".join(parts) + "-"
        forms["underscore-encoded"] = "_" + "_".join(parts) + "_"
        forms["url-escaped"] = "%2F" + "%2F".join(parts)
        forms["json-escaped"] = "\\/" + "\\/".join(parts)
    return forms


def _portable_text(text: str) -> str:
    """Rewrite EVERY `$HOME`-absolute path occurrence inside a block of text.

    `_portable_path` takes one path and is applied to fields the bundle knows
    are paths (`project.path`). Prompt bodies, findings, and report excerpts
    embed paths mid-sentence, so a field-level rewrite cannot reach them.

    This is load-bearing rather than cosmetic. R6g REQUIRES compiled prompts to
    carry ABSOLUTE evidence paths — a relative `evidence/…` citation is
    meaningless to an agent resumed from a different cwd — while R14.b forbids
    a home path in a bundle handed to another person. Both are correct; the
    conflict is only in the shared bytes. Rewriting at the bundle boundary
    satisfies both: the on-disk artifact keeps its absolute citation, and the
    exported copy carries the portable `~/` form.

    Observed live: a real export raised `absolute home path leaked into
    bundle` on evidence-path headers emitted by the R6g rewriter — not a
    defect in either requirement, just two correct rules meeting in one file.
    """
    home = str(Path.home())
    out = text.replace(home + os.sep, "~" + os.sep).replace(home, "~")

    # ENCODED forms of the same path. A substring check for the literal
    # `/Users/<user>` is necessary but NOT sufficient: harnesses name their own
    # directories in transformed encodings that mean the same thing to a reader
    # while evading a slash-based match entirely.
    #
    # Found by independent audit, not by reasoning. Claude Code names each
    # project directory by substituting the path separator with `-`, so a real
    # citation embeds the whole home path in dash-encoded form. The guard below
    # saw no separator-delimited home path, raised nothing, and the operator's
    # account name shipped inside a prompt body — in a bundle whose own
    # docstring promised that could not happen.
    #
    # Only the ACCOUNT-NAME prefix is rewritten, never a whole directory name:
    # the project segment after it is already rendered elsewhere in the
    # narrative on purpose, and clobbering it would corrupt a legitimate
    # citation. The match is anchored so it cannot fire mid-word — `-Users-`
    # must follow a path separator or start the token, which is exactly how
    # the harness emits it.
    for label, token in _home_path_forms().items():
        if label == "literal":
            continue  # already handled above, separator-aware
        if label in ("dash-encoded", "underscore-encoded"):
            sep = token[0]
            # Anchored so it cannot fire mid-word: ordinary prose containing a
            # similar-looking hyphenated token is left alone, while a real
            # separator-substituted path is caught.
            out = re.sub(rf"(?<![A-Za-z0-9]){re.escape(token)}", f"{sep}~{sep}", out)
        else:
            out = out.replace(token, "~")
    return out



BUNDLE_SCHEMA_VERSION = 1


def _json_for_script(obj: object) -> str:
    """Serialise JSON safe to embed inside a `<script>` element.

    Two distinct hazards, both reachable from real session prose:

    * `</script>` closes the element early and spills the remainder into the
      document as live markup. Escaping `<`, `>`, and `&` as `\\uXXXX` makes
      the sequence unrepresentable in the source while keeping the JSON valid
      and byte-identical after `JSON.parse`.
    * U+2028 LINE SEPARATOR and U+2029 PARAGRAPH SEPARATOR are legal inside a
      JSON string but are line terminators in JavaScript source, so an
      unescaped one truncates the literal. `json.dumps` emits them raw when
      `ensure_ascii=False`; it is set False nowhere here, but the escape is
      applied explicitly so a future flag flip cannot silently reintroduce it.

    `type="application/json"` already makes the element inert, but inertness
    is about EXECUTION, not PARSING — the early-close happens in the HTML
    tokenizer, before any MIME type is consulted. The MIME type is therefore
    not a substitute for escaping.
    """
    import json as _json

    raw = _json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)
    return (
        raw.replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def _machine_payload(read_store, detail, project, prompts, report_run_id) -> dict:
    """I5 — the bundle's data as a re-importable structure.

    TWO DISTINCT PRIVACY CONTRACTS apply here, and conflating them is how the
    earlier drafts of this code went wrong:

    1. **Session/turn rows — opaque ids and timestamps ONLY.** This is the
       high-volume transcript-derived data (444,415 turns on this machine).
       No prose, no paths, no URLs, no canonical ids. Enforced in
       `ReadStore.export_sessions`, which drops `content_fingerprint` (proven
       to be truncated prose, not a hash), `native_id` (an absolute `$HOME`
       path for 2 of 5 sources), `raw_path`, `error_text`, `file_paths_json`,
       `model`, and `role`, and replaces canonical ids with per-bundle HMACs
       under a key that is discarded rather than serialised.

    2. **Project narrative — the same content the HTML already renders.**
       Project name and path, git remote, intent episodes, findings, and the
       four prompt bodies. Every one of these is ALREADY displayed in the
       human-readable sections of this same file, so the payload duplicates
       what a recipient can read on screen rather than widening exposure. A
       payload without them could not reconstruct the project, which is I5's
       entire purpose.

    The operator-facing rule is therefore: a bundle is as sensitive as its
    rendered narrative, and no more so. The session-level detail behind that
    narrative never leaves this machine.

    Both are covered by the same fail-closed guard on the final bytes —
    canary, live token, and `$HOME` — because the payload is assembled before
    that guard runs.
    """
    import secrets
    import time as _time

    mac_key = secrets.token_bytes(32)
    try:
        sessions = read_store.export_sessions(project.id, mac_key)
    finally:
        # Explicitly dropped. The key exists only for the lifetime of this
        # call; nothing below may serialise it.
        del mac_key

    return {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "bundle_id": secrets.token_hex(16),
        "exported_ms": int(_time.time() * 1000),
        "run_id": report_run_id,
        "project": {
            "name": project.name,
            "path": _portable_path(project.path),
            "git_remote": project.git_remote or "",
            "session_count": getattr(project, "session_count", 0),
        },
        "sessions": sessions,
        "episodes": [
            {
                "completion_status": e.completion_status,
                "drift_type": e.drift_type or "",
                "original_intent": e.original_intent,
            }
            for e in (detail.episodes or [])
        ],
        "findings": [
            {
                "category": f.category,
                "severity": f.severity,
                "title": f.title,
                "detail": f.detail,
                "delta_state": f.delta_state,
                "provenance": f.provenance,
                "created_ms": f.created_ms,
            }
            for f in read_store.findings({"project_id": project.id})
        ],
        "cost_30d": (
            {
                "total_usd": detail.cost_30d.total_usd,
                "per_completed_intent_usd": detail.cost_30d.per_completed_intent_usd,
                "confidence_mix": detail.cost_30d.confidence_mix,
            }
            if detail.cost_30d is not None
            else None
        ),
        "prompts": {art_type: body for art_type, body in prompts},
    }


_BUNDLE_CSS = """
body { background:#0A0A0F; color:#C8C8D4; font-family: ui-monospace, SFMono-Regular, monospace; margin: 2rem; }
h1, h2 { color:#00F0FF; }
section { background:#12121A; border:1px solid #1F1F2E; padding:1rem; margin:1rem 0; border-radius:6px; }
.copy { background:#00F0FF; color:#0A0A0F; border:none; padding:4px 10px; border-radius:4px; cursor:pointer; }
pre.prompt { background:#1F1F2E; padding:1rem; overflow:auto; white-space: pre-wrap; border-radius:4px; }
pre { background:#1F1F2E; padding:0.75rem; overflow:auto; white-space: pre-wrap; border-radius:4px; }
h3 { color:#6A6A80; font-size:0.85rem; text-transform:uppercase; letter-spacing:0.05em; margin:0.75rem 0 0.25rem; }
table { width:100%; border-collapse:collapse; font-size:0.85rem; }
th { text-align:left; color:#6A6A80; border-bottom:1px solid #1F1F2E; padding:4px 8px; font-weight:normal; }
td { border-bottom:1px solid #1F1F2E; padding:4px 8px; vertical-align:top; }
.muted { color:#6A6A80; }
.numeric { font-variant-numeric: tabular-nums; text-align:right; }
"""

_BUNDLE_JS = """
function copyBlock(btn) {
  const text = btn.nextElementSibling.innerText;
  navigator.clipboard.writeText(text).then(() => {
    btn.textContent = 'copied';
    setTimeout(() => { btn.textContent = 'copy'; }, 1500);
  });
}
"""


def _artifact_body(read_store, project, artifact_type: str) -> str | None:
    """Artifact text for one type: on-disk report first, then the state DB.

    The previous lookup used a RELATIVE ``projects/<path-with-dashes>`` path
    that never existed, so every prompt silently rendered "(not generated
    yet)" — a bundle that looks complete while carrying none of the product.
    Artifacts actually live at
    ``<output_dir>/reports/latest/projects/<sha256(path)[:12]>/prompts/<type>.md``.
    """
    import hashlib

    output_dir = getattr(read_store, "_output_dir", None)
    if output_dir:
        slug = hashlib.sha256(project.path.encode("utf-8")).hexdigest()[:12]
        p = (Path(output_dir) / "reports" / "latest" / "projects" / slug
             / "prompts" / f"{artifact_type}.md")
        if p.exists():
            return p.read_text(encoding="utf-8")
    # Fall back to the persisted copy so a bundle still carries the artifact
    # after retention purges the on-disk report tree.
    try:
        rows = read_store.prompt_artifacts(project.id, artifact_type)
    except Exception:  # noqa: BLE001
        return None
    for row in rows:
        stored = read_store.artifact_content(row.id)
        if stored:
            return stored
    return None


def _report_section(read_store) -> tuple[str, str | None]:
    """The executive report for the run this bundle ships with (R14.c).

    Returns `(html, run_id)`. The run id is parsed from `report.json` beside
    the markdown so every other section can be bound to the SAME run: a
    bundle that pairs one run's report with another run's git snapshot is
    two sources of truth in one archival file.

    Read from disk rather than re-rendered so the bundle carries the same
    bytes the operator saw, not a second rendering that could disagree.
    """
    import json as _json

    output_dir = getattr(read_store, "_output_dir", None)
    if not output_dir:
        return "", None
    root = Path(output_dir) / "reports" / "latest"
    md = root / "report.md"
    if not md.exists():
        return (
            "<section><h2>Executive report</h2>"
            "<div class='muted'>No report on disk — run the pipeline to generate one.</div>"
            "</section>",
            None,
        )
    run_id = None
    meta = root / "report.json"
    if meta.exists():
        try:
            run_id = _json.loads(meta.read_text(encoding="utf-8")).get("run_id")
        except (OSError, ValueError):
            run_id = None
    return (
        "<section><h2>Executive report</h2>"
        f"<pre>{_html_escape(md.read_text(encoding='utf-8'))}</pre></section>",
        run_id,
    )


def _findings_section(read_store, project_id: str) -> str:
    """Every finding for this project, with R13 state and C.1 provenance."""
    try:
        rows = read_store.findings(filter={"project_id": project_id})
    except Exception:  # noqa: BLE001
        rows = []
    if not rows:
        return (
            "<section><h2>Findings</h2>"
            "<div class='muted'>No findings recorded for this project.</div></section>"
        )
    cells = [
        "<section><h2>Findings</h2>",
        f"<div class='muted'>{len(rows)} total</div>",
        "<table><tr><th>Sev</th><th>State</th><th>Src</th><th>Category</th>"
        "<th>Title</th><th>Detail</th></tr>",
    ]
    for f in rows:
        # (D) deterministic vs (A) agent — the C.1 provenance marker, carried
        # into the bundle so an archived finding is still attributable.
        cells.append(
            f"<tr><td>{_html_escape(f.severity)}</td>"
            f"<td>{_html_escape(f.delta_state)}</td>"
            f"<td>({_html_escape(f.provenance)})</td>"
            f"<td>{_html_escape(f.category)}</td>"
            f"<td>{_html_escape(f.title)}</td>"
            f"<td class='muted'>{_html_escape(f.detail)}</td></tr>"
        )
    cells.append("</table></section>")
    return "".join(cells)


def _git_section(read_store, project_id: str, run_id: str | None = None) -> str:
    """Git state as of the run this bundle ships with (R14.c).

    Read through `ReadStore.git_snapshot` — the same read layer every other
    section uses — NOT by shelling out to `git`. Re-reading the live repo at
    export time would put today's working tree next to a report describing a
    different moment, which is two sources of truth in one archival artifact.
    It also means a bundle still exports correctly after the project has been
    moved or deleted.

    `run_id` binds the snapshot to the SAME run the executive report above
    describes. Without it a bundle pairs the newest snapshot with an older
    report, reintroducing the inconsistency in a subtler form.
    """
    snap = read_store.git_snapshot(project_id, run_id)
    if snap is None and run_id:
        # The report's run never captured this project (not analysed in that
        # window). Say so rather than silently substituting another run's.
        return (
            "<section><h2>Git state</h2>"
            f"<div class='muted'>No git snapshot captured for this project during run "
            f"{_html_escape(run_id)}.</div></section>"
        )
    if snap is None:
        return (
            "<section><h2>Git state</h2>"
            "<div class='muted'>No git snapshot recorded — run the pipeline to capture one."
            "</div></section>"
        )
    if snap.error:
        return (
            "<section><h2>Git state</h2>"
            f"<div class='muted'>unavailable at capture time: {_html_escape(snap.error)}</div>"
            "</section>"
        )
    stamp = _iso(snap.captured_ms)
    # `gather_git_evidence` stores the commit id under "sha"
    # (see src/dreamy/changes.py, the recent_log dict literal).
    log_lines = "\n".join(
        f"{c.get('sha', '')[:8]} {c.get('date', '')} {c.get('subject', '')}"
        for c in snap.recent_log
    )
    return (
        "<section><h2>Git state</h2>"
        f"<div class='muted'>captured {_html_escape(stamp)} during run "
        f"{_html_escape(snap.run_id)}</div>"
        f"<h3>status</h3><pre>{_html_escape(snap.status_porcelain or '(clean)')}</pre>"
        f"<h3>recent commits</h3><pre>{_html_escape(log_lines or '(none)')}</pre>"
        f"<h3>diff stat</h3><pre>{_html_escape(snap.diff_stat or '(none)')}</pre>"
        "</section>"
    )


def _evidence_section(read_store, project) -> str:
    """Index of the artifact evidence directory (R14.c "evidence index").

    Names and sizes only. The bundle is meant to be handed to someone else,
    so evidence *contents* are deliberately not inlined — the index tells the
    reader what was captured and where it lived.

    Paths are rendered RELATIVE to the output root. A bundle is a shareable
    artifact, so embedding `/Users/<name>/...` would leak the operator's
    home directory to every recipient.
    """
    import hashlib

    output_dir = getattr(read_store, "_output_dir", None)
    if not output_dir:
        return ""
    slug = hashlib.sha256(project.path.encode("utf-8")).hexdigest()[:12]
    root = Path(output_dir) / "reports" / "latest" / "projects" / slug / "evidence"
    label = f"reports/latest/projects/{slug}/evidence"
    if not root.is_dir():
        return (
            "<section><h2>Evidence index</h2>"
            f"<div class='muted'>No evidence directory at {_html_escape(label)}</div>"
            "</section>"
        )
    entries = sorted(p for p in root.rglob("*") if p.is_file())
    if not entries:
        return (
            "<section><h2>Evidence index</h2>"
            f"<div class='muted'>Empty: {_html_escape(label)}</div></section>"
        )
    rows = "".join(
        f"<tr><td>{_html_escape(str(p.relative_to(root)))}</td>"
        f"<td class='numeric'>{p.stat().st_size}</td></tr>"
        for p in entries
    )
    return (
        "<section><h2>Evidence index</h2>"
        f"<div class='muted'>{_html_escape(label)} — {len(entries)} file(s)</div>"
        f"<table><tr><th>Path</th><th>Bytes</th></tr>{rows}</table></section>"
    )


def export_project_bundle(read_store: ReadStore, project_id: str, output_path: Path) -> Path:
    detail = read_store.project_detail(project_id)
    if detail is None:
        raise ValueError(f"project not found: {project_id}")
    project = detail.summary
    prompts = []
    for art_type in ("resumption", "validation", "remediation", "next_tasks"):
        body = _artifact_body(read_store, project, art_type)
        if body is None:
            body = f"# {art_type}\n\n(not generated for this project)\n"
        prompts.append((art_type, body))

    sections = []
    sections.append(f"<h1>Project bundle — {_html_escape(project.name)}</h1>")
    sections.append(
        f"<section><h2>Path</h2><pre>{_html_escape(_portable_path(project.path))}</pre></section>"
    )
    sections.append(
        f"<section><h2>Git remote</h2><pre>{_html_escape(project.git_remote or '(none)')}</pre></section>"
    )
    # One run id, derived from the report itself, binds every run-scoped
    # section below to the same moment.
    report_html, report_run_id = _report_section(read_store)
    sections.append(report_html)
    if detail.episodes:
        rows = "".join(
            f"<tr><td>{_html_escape(e.completion_status)}</td>"
            f"<td>{_html_escape(e.drift_type or '-')}</td>"
            f"<td>{_html_escape(e.original_intent)}</td></tr>"
            for e in detail.episodes
        )
        sections.append(
            "<section><h2>Intent episodes</h2>"
            f"<div class='muted'>{len(detail.episodes)} episode(s)</div>"
            f"<table><tr><th>Status</th><th>Drift</th><th>Original intent</th></tr>{rows}</table>"
            "</section>"
        )
    sections.append(_findings_section(read_store, project_id))
    sections.append(_git_section(read_store, project_id, report_run_id))
    sections.append(_evidence_section(read_store, project))
    if detail.cost_30d is not None:
        c = detail.cost_30d
        sections.append(
            f"<section><h2>30-day cost</h2>"
            f"<div>total: ${c.total_usd:.4f}</div>"
            f"<div>per_intent: ${c.per_completed_intent_usd:.4f}</div>"
            f"<div>confidence: {_html_escape(str(c.confidence_mix))}</div>"
            f"</section>"
        )
    for art_type, body in prompts:
        sections.append(
            f"<section><h2>{_html_escape(art_type)}</h2>"
            f"<button class='copy' onclick='copyBlock(this)'>copy</button>"
            f"<pre class='prompt'>{_html_escape(body)}</pre></section>"
        )

    # I5: the same data the sections above render, in machine-readable form,
    # so a bundle can be re-imported instead of only read.
    #
    # `<script type="application/json">` is INERT — browsers do not execute
    # it — so the bundle stays safe to hand to a person while becoming
    # readable by `dreamy import`. A second `.json` artifact was rejected: two
    # files describing one project drift apart, and one file cannot disagree
    # with itself.
    #
    # Emitted BEFORE the portabilize + guard step below, deliberately. The
    # guard runs on final bytes, so the payload is covered by the same
    # canary/token/home-path checks as the rendered HTML rather than being a
    # second surface that has to be remembered separately.
    payload = _machine_payload(read_store, detail, project, prompts, report_run_id)
    sections.append(
        '<script type="application/json" id="dreamy-bundle">'
        + _json_for_script(payload)
        + "</script>"
    )

    html_doc = (
        "<!doctype html><html><head><meta charset='utf-8'><title>Project bundle</title>"
        f"<style>{_BUNDLE_CSS}</style></head><body>"
        + "\n".join(sections)
        + f"<script>{_BUNDLE_JS}</script>"
        + "</body></html>"
    )

    # Portabilize LAST, over the fully-assembled document, so every rendered
    # path is covered no matter which section contributed it — project record,
    # report excerpt, finding, or an R6g-absolute citation inside a prompt
    # body. Rewriting per-section would leave whichever source is added next
    # uncovered, which is exactly how the 27-line leak reached the guard.
    #
    # The guard below then runs on the rewritten bytes and stays fail-closed:
    # it is the proof that this rewrite covered everything, not a formality.
    html_doc = _portable_text(html_doc)

    # Secret scan on the FINAL rendered bytes — canary plus the live router
    # token, read from the environment without ever printing it. Raising is
    # correct here: a bundle is meant to be handed to someone else, so a leak
    # would travel further than any other artifact this tool produces.
    from .redact import contains_canary

    if contains_canary(html_doc):
        raise RuntimeError("canary leaked into bundle")
    _token_env = os.environ.get(
        getattr(getattr(read_store, "_cfg", None), "ninerouter_api_key_env", "")
        or "NINEROUTER_API_KEY",
        "",
    )
    if _token_env and _token_env in html_doc:
        raise RuntimeError("live auth token leaked into bundle")

    # A bundle is the one artifact explicitly meant to leave this machine, so
    # an absolute home path is a disclosure, not cosmetics: it names the
    # operator's account and local filesystem layout to whoever receives the
    # file. `_portable_path` rewrites the known roots; this guard proves the
    # rewrite actually covered every rendered path, including any that arrived
    # embedded in a report, finding, or prompt body rather than through the
    # project record.
    # Checked in EVERY encoding `_portable_text` rewrites, not just the literal
    # one. A guard that knows fewer forms than the sanitizer is not fail-closed:
    # it passes precisely when the sanitizer has a gap, which is the only case
    # that matters. This asymmetry is exactly how a dash-encoded home path
    # reached a shipped bundle while the guard reported clean.
    for _label, _needle in _home_path_forms().items():
        if _needle in html_doc:
            # Count + encoding class only. An earlier version echoed the first
            # leaked LINE, which necessarily contains the very account name the
            # guard exists to keep out of shared output — and this message
            # lands in logs and CI transcripts that travel further than the
            # bundle does. The line number is enough to find it locally.
            hits = [
                i for i, line in enumerate(html_doc.splitlines(), 1)
                if _needle in line
            ]
            raise RuntimeError(
                f"home path leaked into bundle ({_label} form); "
                f"{len(hits)} line(s), first at line {hits[0]}"
            )

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(output_path.parent), prefix=".bundle-", suffix=".html.tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(html_doc)
        os.replace(tmp_name, output_path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return output_path


def _html_escape(s: str) -> str:
    return html.escape(str(s), quote=True)
