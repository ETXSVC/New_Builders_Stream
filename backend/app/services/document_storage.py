"""Local filesystem storage for Document uploads (Task 1.15).

Phase 1 stores Documents on local disk under a configured root
(`settings.storage_root`), not S3/object storage — design decision #4 in
docs/superpowers/plans/2026-07-08-phase-1-crm-project-management.md.
`documents.storage_path` always stores a path RELATIVE to that root, never
an absolute filesystem path: the API must never leak the server's absolute
filesystem layout (container path, host path, etc.) into a response.

`file_name` is the first user-controlled filesystem path component
anywhere in this project. It is validated BEFORE it ever touches the
filesystem, and the validation is an outright rejection of anything
resembling path traversal, an absolute path, or a nested path — not an
attempt to sanitize/strip and proceed. This mirrors the same "reject,
don't silently coerce" instinct this codebase already applies elsewhere
(an illegal Lead/Project status transition is a 409, never silently
clamped to the nearest legal state; a malformed X-Tenant-ID header is a
400, never a best-effort parse) — Phase 0's design decision #10 treated
the first user-controlled UUID with the same suspicion this module treats
the first user-controlled filesystem path with.

Versioning note: design decision #4's literal path template is
`{STORAGE_ROOT}/{company_id}/{project_id}/{file_name}}`, with no version
discriminator. Task 1.15's own requirement that a second upload of the
same file_name "insert a new row with version = previous_max + 1, don't
overwrite the file on disk (both versions must remain retrievable)" is
unsatisfiable under that literal template — two uploads sharing a
file_name would collide on the exact same path. This module resolves the
tension by nesting each version under its own `{version}/` path segment
(`{company_id}/{project_id}/{version}/{file_name}`), applied uniformly
including to version 1, rather than special-casing "first upload has no
version segment". This keeps the `{company_id}/{project_id}/` prefix the
design decision specifies while making every version's path
collision-free by construction.
"""

from __future__ import annotations

import uuid
from pathlib import Path, PureWindowsPath

from app.config import settings

# Matches documents.file_name's column width (String(255) in
# app/models/document.py, VARCHAR(255) in migration 0004). Postgres rejects
# (never truncates) an overlong insert — without this check, an over-long
# file_name passes validate_file_name(), the on-disk write succeeds (most
# filesystems tolerate far longer names than 255 chars), and only the
# subsequent INSERT fails, with an unhandled 500 instead of this module's
# own clean 422. Found during this task's code-quality review, same class
# of gap as the control-character check above it. Keep this in sync if the
# column width ever changes.
MAX_FILE_NAME_LENGTH = 255


class InvalidFileNameError(ValueError):
    """Raised by `validate_file_name()` for a `file_name` that must be
    rejected outright: path traversal (`..`), an absolute path, a nested
    path (any path separator), or an empty value. Router call sites catch
    this and translate it into a 422 (this codebase's established
    convention for a semantically-invalid user-supplied value — see
    list_projects's `status` filter check and create_task's `phase_id`
    check in app/routers/*.py, both of which raise
    HTTP_422_UNPROCESSABLE_ENTITY for values that are well-formed but
    invalid in context, as opposed to a 401/403/404 authorization/
    visibility failure)."""


def validate_file_name(file_name: str) -> None:
    """Rejects (never sanitizes) any `file_name` that could escape the
    intended `{STORAGE_ROOT}/{company_id}/{project_id}/{version}/`
    directory, or that isn't a plain, flat filename.

    Deliberately outright rejection of suspicious characters rather than
    "resolve the path and check containment against STORAGE_ROOT
    afterward" — the latter is also correct but strictly more complex to
    reason about (symlink edge cases, `resolve()` platform differences),
    and this codebase's established instinct (see module docstring) is to
    refuse a suspicious value outright rather than to clean it up and
    proceed.
    """
    if not file_name or not file_name.strip():
        raise InvalidFileNameError("file_name must not be empty")

    # Control characters (embedded NUL in particular) aren't a traversal
    # vector, but they're just as "attacker-controlled and unfit for a
    # filesystem path" as a `..` segment — a NUL byte isn't valid in a
    # Postgres UTF8 text value at all, so an unrejected one reaches
    # `Document.file_name == file_name` in the router and crashes with an
    # unhandled 500 from asyncpg instead of this module's own clean 422.
    # Reject outright, same "reject don't sanitize" instinct as every other
    # check here — found during this task's spec review.
    if any(ord(char) < 0x20 for char in file_name):
        raise InvalidFileNameError("file_name must not contain control characters")

    if len(file_name) > MAX_FILE_NAME_LENGTH:
        raise InvalidFileNameError(
            f"file_name must not exceed {MAX_FILE_NAME_LENGTH} characters"
        )

    if ".." in file_name:
        raise InvalidFileNameError("file_name must not contain '..'")

    # file_name must be a flat filename, never a nested path — reject BOTH
    # POSIX and Windows separators outright regardless of which platform
    # this process happens to run on, since the value is attacker-controlled
    # and a Windows-style "..\\..\\etc\\passwd" is exactly as dangerous on a
    # POSIX host's pathlib.Path("..\\..\\etc\\passwd") (which treats
    # backslashes as literal filename characters, not separators, and would
    # therefore NOT be caught by a POSIX-only separator check) as a POSIX
    # traversal string is on Windows.
    if "/" in file_name or "\\" in file_name:
        raise InvalidFileNameError("file_name must not contain path separators")

    # Catches a bare Windows drive-letter absolute path (e.g. "C:\\evil")
    # that could otherwise slip past the separator check above if written
    # without a leading separator. PureWindowsPath is used purely as a
    # parser here (not for actual filesystem access), so this check is
    # meaningful even when this process runs on a POSIX host.
    if PureWindowsPath(file_name).drive:
        raise InvalidFileNameError("file_name must not be an absolute path")


def relative_document_path(
    company_id: uuid.UUID, project_id: uuid.UUID, version: int, file_name: str
) -> str:
    """The path stored in `documents.storage_path` — always relative to
    `settings.storage_root` (see module docstring for the `{version}/`
    segment's rationale). Built with explicit forward slashes (not
    `os.sep`/`pathlib`'s platform-native separator) so the stored value is
    stable regardless of which platform the app happens to run on."""
    return f"{company_id}/{project_id}/{version}/{file_name}"


def write_document_file(
    *,
    company_id: uuid.UUID,
    project_id: uuid.UUID,
    version: int,
    file_name: str,
    content: bytes,
) -> str:
    """Validates `file_name`, writes `content` to
    `{settings.storage_root}/{company_id}/{project_id}/{version}/{file_name}`,
    and returns the RELATIVE storage_path (see `relative_document_path`) to
    persist on the new `Document` row.

    Validation runs here too, not only in the router — this function is
    safe to call directly (e.g. from a future script or another route)
    without relying on every caller to remember to validate first.

    Never overwrites an existing file: each `(project_id, file_name,
    version)` combination is unique by construction (the router computes
    `version` as `previous_max_version + 1` before calling this) — UNDER
    NORMAL SEQUENTIAL USE. Two genuinely concurrent uploads of the same
    `file_name` to the same project can legitimately both read the same
    `previous_max_version` (the router's read has no `SELECT ... FOR
    UPDATE`, advisory lock, or DB-level uniqueness constraint serializing
    it) and both compute the same next `version` — this is a real,
    reachable race under concurrent load, not just a hypothetical upstream
    bug. Raises `FileExistsError` if that happens, which `upload_document`
    (app/routers/projects.py) catches and maps to 409, rather than letting
    the loser of the race silently overwrite the winner's content or leak
    an unhandled 500.

    Uses exclusive-create ("xb" mode) rather than a separate `.exists()`
    check followed by `.write_bytes()` — that two-step form has a
    check-then-write gap between the syscalls, during which a concurrent
    writer could create the file, and `.write_bytes()` would then silently
    truncate/overwrite it (plain "wb" mode has no exclusive-create
    semantics). "xb" is atomic: the OS itself guarantees the create-and-open
    only succeeds if the file didn't already exist, closing that gap
    entirely. Found during this task's code-quality review.
    """
    validate_file_name(file_name)

    relative_path = relative_document_path(company_id, project_id, version, file_name)
    absolute_path = Path(settings.storage_root) / str(company_id) / str(project_id) / str(version) / file_name

    absolute_path.parent.mkdir(parents=True, exist_ok=True)
    with absolute_path.open("xb") as fh:
        fh.write(content)

    return relative_path


def write_estimate_pdf_file(*, company_id: uuid.UUID, estimate_id: uuid.UUID, content: bytes) -> str:
    """Writes an exported Estimate PDF to
    `{settings.storage_root}/{company_id}/estimates/{estimate_id}.pdf` and
    returns the RELATIVE storage_path (`{company_id}/estimates/{estimate_id}.pdf`,
    matching `relative_document_path`'s "always relative to storage_root,
    forward slashes" convention) to persist on `estimate.pdf_storage_path`
    (Task 2.15).

    Mirrors `write_document_file`'s general shape (`Path(settings.storage_root)
    / ...`, `.parent.mkdir(parents=True, exist_ok=True)`), but differs from it
    in two deliberate ways:

    1. **Always overwrites** (plain `"wb"` mode), unlike `write_document_file`'s
       exclusive-create (`"xb"`, never-overwrite) semantics. Design decision #5
       in the Phase 2 plan doc is explicit that "re-exporting after a
       line-item edit produces a new PDF from current state, previous exports
       aren't a retained artifact" — there is exactly one current PDF per
       estimate at any time, always replaced on re-export. Using `"xb"` here
       would raise `FileExistsError` on every re-export after the first,
       which is wrong for this table's semantics (there is no version
       concept for estimate PDFs the way there is for `Document` uploads).

    2. **No `validate_file_name()` call.** The filename here (`{estimate_id}.pdf`)
       is fully system-generated from a UUID that has already been validated
       as a real, RLS-visible `Estimate` by the time this function is ever
       called — never user input — so `write_document_file`'s path-traversal
       validation (built for the user-supplied `file_name` on a Document
       upload) is inapplicable here.
    """
    relative_path = f"{company_id}/estimates/{estimate_id}.pdf"
    absolute_path = Path(settings.storage_root) / str(company_id) / "estimates" / f"{estimate_id}.pdf"

    absolute_path.parent.mkdir(parents=True, exist_ok=True)
    absolute_path.write_bytes(content)

    return relative_path


def write_esignature_artifact_file(*, company_id: uuid.UUID, esignature_id: uuid.UUID, content: bytes) -> str:
    """Writes a captured e-signature artifact to
    `{settings.storage_root}/{company_id}/esignatures/{esignature_id}.png`
    and returns the RELATIVE storage_path
    (`{company_id}/esignatures/{esignature_id}.png`, matching
    `relative_document_path`'s "always relative to storage_root, forward
    slashes" convention) to persist on `Esignature.signature_artifact_path`
    (Task 2.18).

    `.png` is a deliberate, fixed extension choice for this MVP, not an
    oversight: `capture_esignature`'s own signature (Task 2.18's spec) takes
    `document_type`, not an extension parameter, and
    `docs/04-database-schema.md` Section 6's inline comment on
    `signature_artifact_path` calls it a "rendered signature image/hash" —
    PNG is the natural default for "signature image." If a future need
    arises for a different artifact format, `capture_esignature` and this
    function would both need an explicit extension parameter added to their
    signatures; that's out of scope here since neither of this service's two
    future callers (Estimate approval, Task 2.19; Change Order approval,
    Task 2.22) are built yet to demand otherwise.

    Mirrors `write_estimate_pdf_file`'s general shape (`Path(settings.storage_root)
    / ...`, `.parent.mkdir(parents=True, exist_ok=True)`), but differs from it
    in the opposite direction from how `write_estimate_pdf_file` itself differs
    from `write_document_file`:

    1. **Exclusive-create (`"xb"` mode), not always-overwrite.** Unlike an
       Estimate PDF (one current PDF per estimate, always replaced on
       re-export — see `write_estimate_pdf_file`'s own docstring), an
       `Esignature` row is immutable from the moment it's created (migration
       0006's `REVOKE UPDATE, DELETE ON esignatures FROM app_user`, design
       decision #6) and its filename is derived from the row's own newly
       generated id, not a stable parent id that could legitimately be
       re-targeted. There is therefore no legitimate scenario where this
       path is ever written to twice — a second attempt would mean either a
       UUID collision (practically impossible) or a caller bug reusing an
       id, and `"xb"` makes that fail loudly (`FileExistsError`) rather than
       silently overwriting a legally retained signature artifact.
    2. **No `validate_file_name()` call**, for the same reason
       `write_estimate_pdf_file` skips it: the filename here (`{esignature_id}.png`)
       is fully system-generated from a UUID, never user input.
    """
    relative_path = f"{company_id}/esignatures/{esignature_id}.png"
    absolute_path = Path(settings.storage_root) / str(company_id) / "esignatures" / f"{esignature_id}.png"

    absolute_path.parent.mkdir(parents=True, exist_ok=True)
    with absolute_path.open("xb") as fh:
        fh.write(content)

    return relative_path
