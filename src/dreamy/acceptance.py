"""Current-run acceptance manifest validation.

Product code, not test code. The CLI, the evidence gate, and the citation
self-check all need one answer to "which evidence tree may this gate read, and
is it trustworthy?". Implementing that inside a test module would make test code
the source of truth for production gating.

Fail-closed at every step. Two opposite mistakes are both false passes:

  * sweeping the whole historical acceptance tree — the gate can never go green
    while any past artifact holds a dead citation, so it gets waived;
  * pointing the gate at a bare directory — it goes green by finding nothing.

So a run is admitted only when all of the following hold:

  1. a manifest path is supplied explicitly (no "latest run" auto-discovery and
     no directory fallback — a silent default is how the wrong tree gets
     blessed),
  2. the manifest parses, declares a known `schema_version`, and its `run_id`
     matches the directory basename,
  3. the resolved root is contained under the acceptance-runs base, checked
     after symlink resolution so a link cannot escape,
  4. every declared artifact exists, stays inside the run root, and matches its
     recorded sha256,
  5. the declared requirement IDs are exactly the gate's set in
     docs/acceptance/GATE-MATRIX.md, each defined in SPEC-DREAMY-V2.md,
  6. every evidence kind the gate requires is present, and each kind that names
     a requirement contains that requirement's ID,
  7. an independent reviewer verdict admits it, and the reviewer name differs
     from the recorded producer name.

Step 7 is a DECLARED-NAME check, not proof of independence: both fields are
self-asserted, so one party can supply two names. Verifying real independence
would need signed provenance; until then the reviewer being a genuinely separate
agent is an external process guarantee, not one this module can enforce.

The gate's STATUS in the matrix is deliberately NOT an input. Reading it was
circular: the cell records the conclusion, so a manifest could neither prove a
BLOCKED gate nor be rejected under a stale PASS. The matrix is parsed for the
gate's requirement-ID set -- its contract -- and the status cell is a derived
record refreshed from admitted runs.

Steps 5 and 6 exist because weaker forms were demonstrably false-green: checking
only that IDs were DEFINED admitted an empty list; requiring kinds without
checking content admitted files holding another requirement's evidence.

Validation of a run manifest never repairs the claim it is checking.
write_produced_by is a separate producer helper (I7), not part of validation.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path


def _account_home() -> Path:
    """The invoking account's home, independent of the environment.

    NOT `Path.home()` or `expanduser()`: both consult `$HOME` first, so a
    caller could relocate the acceptance-runs base simply by exporting it —
    and the party running the validator is exactly the party whose claim is
    being checked. Demonstrated with the real CLI: one manifest, identical
    bytes, rejected under the real HOME and ADMITTED under a temporary one.

    `getpwuid` reads the account database instead, which the process cannot
    rewrite. Falls back to `Path.home()` only where `pwd` is unavailable
    (non-POSIX), where no stronger source exists anyway.
    """
    try:
        import pwd

        return Path(pwd.getpwuid(os.getuid()).pw_dir)
    except (ImportError, KeyError, AttributeError):
        return Path.home()


ACCEPTANCE_RUNS_BASE = _account_home() / ".local/share/dreamy/acceptance-runs"

# ---------------------------------------------------------------------------
# I7 — evidence-corpus binding
# ---------------------------------------------------------------------------
#
# Gates that compare ~/.local/share/dreamy/acceptance against THIS checkout
# must refuse a foreign corpus. The corpus carries `.produced-by` holding a
# workspace instance id. The id is generated on first use and stored under
# the existing product state tree (~/.local/share/dreamy), keyed by the
# absolute repo root so two checkouts on one machine stay distinct. Never
# committed — a tracked UUID is identical in every clone.
#
# The producer of the acceptance tree is external to this package today
# (operators / harnesses write under acceptance/). Until a producer calls
# write_produced_by, every unmarked tree is treated as unbound and gates
# that require binding SKIP. Absence is not a free pass to compare.

PRODUCED_BY_NAME = ".produced-by"
_WORKSPACE_INSTANCES_DIRNAME = "workspace-instances"


def _product_state_dir() -> Path:
    """Machine-local product state root — same tree as output_dir / config.

    Resolved through config.resolve_path(DEFAULT_CONFIG_DIR) so a single
    expansion path owns "~" handling; inventing a parallel home-join would
    diverge the moment either side changes.
    """
    from .config import DEFAULT_CONFIG_DIR, resolve_path

    return resolve_path(DEFAULT_CONFIG_DIR)


def workspace_instance_id_path(repo_root: Path) -> Path:
    """Path of this checkout's instance-id file under the product state dir.

    Keyed by a digest of the resolved repo root so the location is stable for
    a given checkout and distinct across checkouts sharing one HOME.
    """
    digest = hashlib.sha256(
        str(Path(repo_root).resolve()).encode("utf-8")
    ).hexdigest()
    return _product_state_dir() / _WORKSPACE_INSTANCES_DIRNAME / digest


def produced_by_path(acceptance_root: Path) -> Path:
    return Path(acceptance_root) / PRODUCED_BY_NAME


def workspace_instance_id(repo_root: Path) -> str:
    """Return this checkout's workspace instance id, creating it on first use.

    Creation is exclusive (O_EXCL) so concurrent first-readers cannot each
    invent a different id; a loser of the race re-reads the winner's file.
    """
    import uuid

    path = workspace_instance_id_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return path.read_text(encoding="utf-8").strip()
    try:
        value = str(uuid.uuid4())
        os.write(fd, (value + "\n").encode("utf-8"))
    finally:
        os.close(fd)
    return value


def write_produced_by(acceptance_root: Path, repo_root: Path) -> Path:
    """Stamp *acceptance_root* as produced by this workspace.

    Intended for whatever writes the acceptance evidence tree. Dreamy's own
    pipeline does not own that tree — operators and external harnesses do —
    so the marker is optional until a producer invokes this. Gates that
    require binding skip when the marker is absent rather than inventing a
    match.
    """
    root = Path(acceptance_root)
    root.mkdir(parents=True, exist_ok=True)
    marker = produced_by_path(root)
    marker.write_text(workspace_instance_id(repo_root) + "\n", encoding="utf-8")
    return marker


def corpus_binding_skip_reason(
    acceptance_root: Path,
    repo_root: Path,
) -> str | None:
    """None when the corpus is bound to this workspace; else a skip reason.

    The reason always names the marker path so the fix — one producing run
    against this checkout, which writes the marker — is stated, not discovered.
    """
    root = Path(acceptance_root)
    marker = produced_by_path(root)
    if not marker.is_file():
        return (
            f"acceptance corpus at {root} carries no {PRODUCED_BY_NAME} marker "
            f"({marker}); not bound to this workspace — run a producing pass "
            "against this checkout (or call dreamy.acceptance.write_produced_by) "
            "before comparing the corpus to the tracked tree"
        )
    try:
        claimed = marker.read_text(encoding="utf-8").strip()
    except OSError as exc:
        return (
            f"acceptance corpus marker {marker} is unreadable ({exc}); "
            "not bound to this workspace"
        )
    if not claimed:
        return (
            f"acceptance corpus marker {marker} is empty; "
            "not bound to this workspace"
        )
    mine = workspace_instance_id(repo_root)
    if claimed != mine:
        return (
            f"acceptance corpus at {root} was produced by workspace {claimed!r} "
            f"(marker {marker}), not this workspace {mine!r}; refusing to "
            "compare a foreign corpus against this checkout"
        )
    return None


GATE_MATRIX_RELATIVE = Path("docs/acceptance/GATE-MATRIX.md")
SPEC_RELATIVE = Path("docs/specifications/SPEC-DREAMY-V2.md")

SUPPORTED_SCHEMA_VERSIONS = frozenset({1})

# A reviewer verdict that admits the run. Anything else blocks, including an
# unrecognised value: an unknown verdict is not evidence of approval.
ADMITTING_VERDICTS = frozenset({"PASS", "PASS_WITH_MINOR"})

_STATUS_RE = re.compile(r"\*{0,2}(PASS|FAIL|BLOCKED)\*{0,2}", re.IGNORECASE)
_GATE_ROW_RE = re.compile(r"^\|\s*(G\d+)\s*\|")
_REQ_ID_RE = re.compile(r"^[A-Z]{3,6}-\d{3}$")
_RUN_ID_RE = re.compile(r"^run-\d{6}-\d{6}-[a-z0-9-]+$")
# Full SHA only. An abbreviated prefix is ambiguous in principle, and this
# validator has no object database to disambiguate it against.
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")


def _commit_exists(repo_root: Path, commit: str) -> bool:
    """True when *commit* names a commit object in *repo_root*.

    Read-only by construction, per this module's contract that nothing here
    writes: `cat-file -e` only queries the object database.

    `commit` is already matched against `_COMMIT_RE` before reaching here, so
    it cannot carry a leading `-` and be read as an option. The `^{commit}`
    suffix additionally requires the object be a commit rather than a blob or
    tree that happens to hash to the same value.

    Returns False rather than raising when git is unavailable or the directory
    is not a repository. The caller turns that into a rejection, so an
    unusable environment fails closed rather than admitting the run.
    """
    try:
        result = subprocess.run(
            ["git", "cat-file", "-e", f"{commit}^{{commit}}"],
            cwd=repo_root,
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


@lru_cache(maxsize=1)
def _load_schema() -> dict:
    """Load the packaged manifest schema, or fail closed.

    Read through `importlib.resources`, not a repo-relative path: the wheel
    ships only `src/dreamy`, so a schema resolved from the checkout is absent
    after install. A validator whose schema silently vanishes in production
    enforces nothing exactly where enforcement matters most.

    Every failure raises. An unreadable or malformed bundled schema is a
    product defect, and treating it as "skip the checks" is the fail-open
    behaviour this module exists to prevent.

    Cached: the resource is immutable at runtime, so re-reading it per artifact
    would mean repeated zip reads for no benefit.
    """
    try:
        from importlib.resources import files

        raw = (
            files("dreamy")
            .joinpath("resources/run-manifest.schema.json")
            .read_text(encoding="utf-8")
        )
    except (OSError, ModuleNotFoundError, FileNotFoundError) as exc:
        raise ManifestError(
            f"packaged manifest schema is unreadable: {exc}; "
            "the installation is incomplete and no manifest can be validated"
        ) from exc
    try:
        schema = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ManifestError(f"packaged manifest schema is malformed: {exc}") from exc
    if not isinstance(schema, dict) or not isinstance(schema.get("properties"), dict):
        raise ManifestError("packaged manifest schema has no root properties")
    return schema


def _allowed_keys(schema: dict, *path: str) -> frozenset[str]:
    """Allowed property names at a point in the schema.

    Derived from the schema rather than duplicated as a literal set, so the two
    cannot drift. Traversal understands both shapes this schema uses:
    `properties` for fixed objects, and `additionalProperties` (addressed as
    `*`) for the artifact map, whose keys are artifact kinds, not fixed names.
    """
    node: object = schema
    for step in path:
        if not isinstance(node, dict):
            raise ManifestError(f"schema path {'.'.join(path)} is not an object")
        props = node.get("properties")
        if step == "*":
            node = node.get("additionalProperties")
        elif isinstance(props, dict) and step in props:
            node = props[step]
        else:
            raise ManifestError(f"schema has no path {'.'.join(path)}")
    if not isinstance(node, dict) or not isinstance(node.get("properties"), dict):
        raise ManifestError(f"schema path {'.'.join(path)} declares no properties")
    return frozenset(node["properties"])


def _reject_unknown_keys(obj: dict, allowed: frozenset[str], where: str) -> None:
    """Enforce the schema's additionalProperties:false.

    A schema nothing enforces is documentation. An unexpected key means the
    producer and the validator disagree about the contract -- most often a typo
    in a required key, which would otherwise fall through to a default and pass.
    """
    unknown = sorted(set(obj) - allowed)
    if unknown:
        raise ManifestError(
            f"unknown key(s) in {where}: {unknown}; allowed: {sorted(allowed)}"
        )


# Artifact kinds that can substantiate each gate. A gate absent from this map
# imposes no kind requirement yet -- stated rather than silently defaulted, so
# an unmapped gate is visibly unconstrained instead of appearing enforced.
GATE_EVIDENCE_KINDS: dict[str, frozenset[str]] = {
    # One kind per requirement, chosen from the requirement TEXT rather than
    # from whatever evidence happened to be produced:
    #   PKG-001 (src layout)        -> pkg001_layout
    #   PKG-002 (no checkout/$HOME) -> pkg002_independence
    #   PKG-003 (pyproject metadata)-> pkg003_metadata
    # plus wheel_build, install_checks, and suite_result for the gate's own
    # command list.
    #
    # An earlier revision claimed install_checks showed PKG-001. Two independent
    # reviews rejected that: a flat-layout project installs just as cleanly, so
    # install success proves installability, not layout.
    "G2": frozenset(
        {
            "wheel_build",
            "install_checks",
            "pkg001_layout",
            "pkg002_independence",
            "pkg003_metadata",
            "suite_result",
        }
    ),
}


# A kind name is a label. Without a content check it binds to nothing: swapping
# the pkg001_layout and pkg003_metadata files was ADMITTED -- every hash valid,
# every required kind present, each artifact holding the OTHER requirement's
# evidence. Each pattern is a token the artifact must contain to be that kind.
GATE_EVIDENCE_MARKERS: dict[str, str] = {
    "pkg001_layout": "PKG-001",
    "pkg002_independence": "PKG-002",
    "pkg003_metadata": "PKG-003",
}


_MARKER_SCAN_BYTES = 64 * 1024

# Placeholders that name no distinct party.
_SELF_REVIEW_ALIASES = frozenset({"self", "me", "same", "producer", "author"})


class ManifestError(Exception):
    """Manifest is absent, malformed, or fails an integrity check."""


@dataclass(frozen=True)
class GateRow:
    gate: str
    status: str
    requirement_ids: tuple[str, ...] = ()

    @property
    def blocked(self) -> bool:
        return self.status == "BLOCKED"


@dataclass
class ValidatedRun:
    run_id: str
    root: Path
    manifest_path: Path
    gate: str
    artifacts: dict[str, Path] = field(default_factory=dict)
    requirement_ids: tuple[str, ...] = ()
    verified_hashes: int = 0
    reviewer: str = ""


def parse_gate_matrix(repo_root: Path) -> dict[str, GateRow]:
    """Parse gate rows from GATE-MATRIX.md.

    Raises when the matrix is missing: with no matrix there is no definition of
    what a gate requires, so admitting a run would mean inventing the contract
    at validation time.
    """
    path = repo_root / GATE_MATRIX_RELATIVE
    if not path.is_file():
        raise ManifestError(
            f"gate matrix not found at {GATE_MATRIX_RELATIVE}; "
            "cannot validate a run against an undefined contract"
        )
    rows: dict[str, GateRow] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = _GATE_ROW_RE.match(line)
        if not m:
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        status = "BLOCKED"
        for cell in cells:
            sm = _STATUS_RE.fullmatch(cell)
            if sm:
                status = sm.group(1).upper()
                break
        # Column 2 is the gate's requirement set. Parsed rather than dropped:
        # without it, admission could only check that declared IDs exist
        # somewhere in the spec, which admits an empty declaration.
        req: tuple[str, ...] = ()
        if len(cells) > 1:
            req = tuple(
                tok
                for tok in (c.strip() for c in cells[1].split(","))
                if _REQ_ID_RE.fullmatch(tok)
            )
        rows[m.group(1)] = GateRow(
            gate=m.group(1), status=status, requirement_ids=req
        )
    if not rows:
        raise ManifestError(f"{GATE_MATRIX_RELATIVE} declares no gate rows")
    return rows


def spec_requirement_ids(repo_root: Path) -> set[str]:
    """Requirement IDs defined in a specification table row.

    A prose mention is not a definition, so only the first cell of a table row
    counts.
    """
    path = repo_root / SPEC_RELATIVE
    if not path.is_file():
        raise ManifestError(f"specification not found at {SPEC_RELATIVE}")
    ids: set[str] = set()
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) >= 2 and _REQ_ID_RE.fullmatch(cells[0]):
            ids.add(cells[0])
    return ids


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def validate_run_manifest(
    manifest_path: Path,
    repo_root: Path,
    runs_base: Path = ACCEPTANCE_RUNS_BASE,
) -> ValidatedRun:
    """Validate a current-run manifest, or raise ManifestError.

    Returning normally means the run may be swept. Every failure path raises;
    there is no degraded "probably fine" result, because the only consumer of a
    soft result would be a gate deciding whether to pass.
    """
    manifest_path = Path(manifest_path)
    if not manifest_path.is_file():
        raise ManifestError(f"no run manifest at {manifest_path}")

    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestError(f"run manifest unreadable: {exc}") from exc
    if not isinstance(data, dict):
        raise ManifestError("run manifest must be a JSON object")

    schema = _load_schema()
    _reject_unknown_keys(data, _allowed_keys(schema), "manifest root")

    version = data.get("schema_version")
    if version not in SUPPORTED_SCHEMA_VERSIONS:
        raise ManifestError(
            f"unsupported manifest schema_version {version!r}; "
            f"supported: {sorted(SUPPORTED_SCHEMA_VERSIONS)}"
        )

    run_id = data.get("run_id")
    if not isinstance(run_id, str) or not _RUN_ID_RE.fullmatch(run_id):
        raise ManifestError(f"invalid or missing run_id: {run_id!r}")

    gate = data.get("gate")
    if not isinstance(gate, str) or not re.fullmatch(r"G\d{1,2}", gate):
        raise ManifestError(f"invalid or missing gate: {gate!r}")

    # `commit` is the ONLY binding between an evidence run and the source state
    # it tested. Without it a manifest is unfalsifiable: the artifacts prove
    # something ran, but nothing says against what. The schema has always
    # required it and constrained it to a full 40-hex SHA; the validator did not
    # read it at all, so manifests omitting it entirely — or carrying
    # `commit: "xyz"` — were ADMITTED with every hash verified.
    commit = data.get("commit")
    if not isinstance(commit, str) or not _COMMIT_RE.fullmatch(commit):
        raise ManifestError(
            f"invalid or missing commit: {commit!r}; a full 40-character hex "
            "SHA is required — it is the only link between this evidence and "
            "the source state it was produced against"
        )

    # Well-formed is not the same as real: `"b" * 40` satisfies the pattern.
    # The SHA must exist as a commit object in this repository.
    #
    # Existence, deliberately NOT equality with HEAD. Evidence is produced at
    # one commit and validated later — often much later — so requiring
    # `commit == HEAD` would reject every historical run the moment anything
    # else was committed, which is the opposite of what recording the SHA is
    # for.
    #
    # This does NOT establish that the recorded commit is what actually
    # produced the artifacts, or that it matches the current checkout. Nothing
    # short of signed provenance could. It establishes only that the claimed
    # source state is one this repository can produce — enough to reject an
    # absent, malformed, or fabricated SHA, and to make the claim auditable by
    # someone who can check out that commit.
    if not _commit_exists(repo_root, commit):
        raise ManifestError(
            f"commit {commit} does not exist in {repo_root}; the manifest "
            "names a source state this repository cannot produce"
        )

    generated_utc = data.get("generated_utc")
    if not isinstance(generated_utc, str) or not generated_utc.strip():
        raise ManifestError("run manifest declares no generated_utc")
    try:
        parsed_utc = datetime.fromisoformat(generated_utc.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ManifestError(
            f"generated_utc is not an ISO-8601 timestamp: {generated_utc!r} ({exc})"
        ) from exc
    # The field names UTC. A naive timestamp is ambiguous across machines, and
    # a non-zero offset is a different instant than the one it appears to name
    # when compared against other manifests.
    if parsed_utc.utcoffset() != timedelta(0):
        raise ManifestError(
            f"generated_utc {generated_utc!r} is not UTC; it is "
            f"{'naive (no timezone)' if parsed_utc.tzinfo is None else f'offset {parsed_utc.utcoffset()}'}"
        )

    # Containment is checked on the RESOLVED path, so a symlinked run directory
    # cannot point at a tree outside the acceptance base.
    root_raw = data.get("root")
    if not isinstance(root_raw, str) or not root_raw:
        raise ManifestError("run manifest declares no root")
    # `expanduser()` is deliberately NOT applied: it consults `$HOME`, which
    # the party being validated controls, so a manifest declaring
    # `root: "~/.local/share/dreamy/acceptance-runs/<id>"` would re-enter
    # through the same door the account-home lookup just closed. The schema
    # says absolute; anything else is refused rather than interpreted.
    if not Path(root_raw).is_absolute():
        raise ManifestError(
            f"declared run root must be an absolute path, got {root_raw!r}"
        )
    root = Path(root_raw).resolve()
    if not runs_base.is_absolute():
        raise ManifestError(
            f"acceptance-runs base must be an absolute path, got {str(runs_base)!r}"
        )
    base = runs_base.resolve()
    if not root.is_dir():
        raise ManifestError(f"declared run root does not exist: {root}")
    if base not in root.parents:
        raise ManifestError(
            f"declared run root escapes the acceptance-runs base ({base})"
        )
    if root.name != run_id:
        raise ManifestError(
            f"run_id {run_id!r} does not match run directory {root.name!r}; "
            "a manifest must not describe a different directory"
        )

    declared = data.get("artifacts")
    if not isinstance(declared, dict) or not declared:
        raise ManifestError("run manifest declares no artifacts")

    row = parse_gate_matrix(repo_root).get(gate)
    if row is None:
        raise ManifestError(f"gate {gate} is not defined in {GATE_MATRIX_RELATIVE}")

    # The matrix status is a DERIVED RECORD of evidence, never an input to
    # admission. Gating on `row.blocked` was circular: the cell records the
    # conclusion, so a manifest could neither prove a blocked gate nor be
    # rejected for a stale PASS. Evidence is judged on its own terms here;
    # the cell is refreshed from the outcome.
    req_ids = data.get("requirement_ids") or []
    if not isinstance(req_ids, list):
        raise ManifestError("requirement_ids must be a list")
    defined = spec_requirement_ids(repo_root)
    undefined = sorted(set(req_ids) - defined)
    if undefined:
        raise ManifestError(
            f"requirement IDs not defined in {SPEC_RELATIVE}: {undefined}"
        )

    # Exact set, not subset. Checking only that declared IDs are DEFINED let a
    # manifest declare `[]` and be admitted -- demonstrated: empty ids plus one
    # junk artifact and a self-review was ADMITTED. A gate is satisfied by
    # covering every requirement it claims, no more and no less.
    required = frozenset(row.requirement_ids)
    if required and frozenset(req_ids) != required:
        missing = sorted(required - frozenset(req_ids))
        extra = sorted(frozenset(req_ids) - required)
        detail = []
        if missing:
            detail.append(f"missing {missing}")
        if extra:
            detail.append(f"not claimed by {gate}: {extra}")
        raise ManifestError(
            f"gate {gate} requires exactly {sorted(required)}; " + "; ".join(detail)
        )

    # A matching requirement-ID set does not make evidence relevant. Without
    # this, a G2 (packaging) claim was ADMITTED while backed solely by launchd
    # review artifacts -- right IDs, verified hashes, wrong subsystem. Each
    # gate names the artifact kinds that can substantiate it.
    needed = GATE_EVIDENCE_KINDS.get(gate, frozenset())
    absent = sorted(needed - set(declared))
    if absent:
        raise ManifestError(
            f"gate {gate} requires evidence artifact(s) {absent}; "
            f"declared: {sorted(declared)}"
        )

    artifacts: dict[str, Path] = {}
    for kind, spec in sorted(declared.items()):
        if not isinstance(spec, dict):
            raise ManifestError(f"artifact {kind!r} must be an object")
        _reject_unknown_keys(
            spec, _allowed_keys(schema, "artifacts", "*"), f"artifact {kind!r}"
        )
        rel, want = spec.get("path"), spec.get("sha256")
        if not isinstance(rel, str) or not rel:
            raise ManifestError(f"artifact {kind!r} declares no path")
        if not isinstance(want, str) or len(want) != 64:
            raise ManifestError(f"artifact {kind!r} declares no sha256")
        if Path(rel).is_absolute():
            raise ManifestError(f"artifact {kind!r} path must be run-root-relative")
        unresolved = root / rel
        # Symlink rejection MUST happen here, before .resolve(). Resolution
        # follows the link, so downstream consumers receive a plain path and
        # can never tell it was one. A symlinked artifact would also let the
        # hash be computed over a file outside the run.
        #
        # Walk the components from the run root rather than filtering
        # `.parents`: a parents-based predicate misses a symlinked directory
        # that is itself a direct child of root, because that path's own parent
        # IS the root and fails the containment test.
        walked = root
        for part in Path(rel).parts:
            walked = walked / part
            if walked.is_symlink():
                raise ManifestError(
                    f"artifact {kind!r} path traverses a symlink at "
                    f"{walked.relative_to(root)}: {rel}"
                )
        target = unresolved.resolve()
        if root not in target.parents:
            raise ManifestError(f"artifact {kind!r} escapes the run root")
        if not target.is_file():
            raise ManifestError(f"artifact {kind!r} missing at {rel}")
        got = _sha256(target)
        if got != want:
            raise ManifestError(
                f"artifact {kind!r} hash mismatch: manifest {want[:12]}…, "
                f"file {got[:12]}…"
            )
        # A kind that names a requirement must contain that requirement's ID.
        # Read bounded: evidence files are small, and an artifact too large to
        # read is not one a reviewer could have checked either.
        marker = GATE_EVIDENCE_MARKERS.get(kind)
        if marker is not None:
            head = target.read_bytes()[:_MARKER_SCAN_BYTES]
            if marker.encode() not in head:
                raise ManifestError(
                    f"artifact {kind!r} does not mention {marker}; "
                    f"the kind claims evidence the file does not carry"
                )

        # Two artifact kinds resolving to one file is malformed: the manifest
        # would claim broader coverage than it has.
        for prior_kind, prior in artifacts.items():
            if prior == target:
                raise ManifestError(
                    f"artifacts {prior_kind!r} and {kind!r} both resolve to "
                    f"{target.name}"
                )
        artifacts[kind] = target

    review = data.get("independent_review")
    if not isinstance(review, dict):
        raise ManifestError("run manifest declares no independent_review")
    _reject_unknown_keys(
        review, _allowed_keys(schema, "independent_review"), "independent_review"
    )
    verdict = review.get("verdict")
    if verdict not in ADMITTING_VERDICTS:
        raise ManifestError(
            f"independent review verdict {verdict!r} does not admit this run; "
            f"admitting verdicts: {sorted(ADMITTING_VERDICTS)}"
        )
    review_artifact = review.get("artifact")
    if review_artifact not in artifacts:
        raise ManifestError(
            f"independent_review.artifact {review_artifact!r} is not a declared artifact"
        )
    # The declared verdict is the producer's claim ABOUT the review. Checking
    # only that was a false green: a manifest declaring PASS while citing a
    # review whose own verdict is FAIL was ADMITTED -- demonstrated with a real
    # FAIL review document. Read the artifact and let it speak for itself.
    cited = artifacts[review_artifact]
    # Machine-readable, unconditionally. Gating the content check on a .json
    # suffix was a fall-through: citing the .md twin of the SAME review skipped
    # validation entirely, so a FAIL review admitted the run. A prose format
    # nothing can parse is not a check.
    if cited.suffix != ".json":
        raise ManifestError(
            f"cited review artifact {review_artifact!r} is {cited.suffix or 'extensionless'}; "
            "the review must be machine-readable JSON so its verdict can be "
            "read rather than taken on the manifest's word"
        )
    try:
        body = json.loads(cited.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestError(
            f"cited review artifact {review_artifact!r} is unreadable "
            f"or not JSON: {exc}"
        ) from exc
    own = body.get("verdict") if isinstance(body, dict) else None
    if own is None:
        raise ManifestError(
            f"cited review artifact {review_artifact!r} declares no verdict; "
            "a review that states no verdict cannot admit a run"
        )
    if not isinstance(own, str) or own not in ADMITTING_VERDICTS:
        raise ManifestError(
            f"cited review artifact {review_artifact!r} carries verdict "
            f"{own!r}, which does not admit this run; the manifest declared "
            f"{verdict!r}"
        )
    if own != verdict:
        raise ManifestError(
            f"declared verdict {verdict!r} disagrees with the cited "
            f"artifact's own verdict {own!r}"
        )

    reviewer = review.get("reviewer")
    if not isinstance(reviewer, str) or not reviewer.strip():
        raise ManifestError("independent_review declares no reviewer")

    # Declared-name inequality ONLY. Both fields are self-asserted strings, so
    # this cannot establish that two parties actually did the work: one hand can
    # write producer "Alice" and reviewer "Bob". Real independence is an
    # external process gate -- a distinct agent or person performing the review
    # -- and would need signed provenance to be verifiable here.
    #
    # It is still worth enforcing: before this, `reviewer: "self"` was ADMITTED,
    # which is the accidental case rather than the adversarial one.
    producer = data.get("producer")
    if not isinstance(producer, str) or not producer.strip():
        raise ManifestError("run manifest declares no producer")
    if reviewer.strip().casefold() == producer.strip().casefold():
        raise ManifestError(
            f"independent_review.reviewer {reviewer!r} equals the declared "
            "producer; a distinct reviewer name is required (declared-name "
            "check only -- it cannot prove real independence)"
        )
    if reviewer.strip().casefold() in _SELF_REVIEW_ALIASES:
        raise ManifestError(
            f"independent_review.reviewer {reviewer!r} names no distinct "
            "party; a reviewer name is required (declared-name check only)"
        )

    return ValidatedRun(
        run_id=run_id,
        root=root,
        manifest_path=manifest_path,
        gate=gate,
        artifacts=artifacts,
        requirement_ids=tuple(req_ids),
        verified_hashes=len(artifacts),
        reviewer=reviewer,
    )
