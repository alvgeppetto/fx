from __future__ import annotations

import importlib.metadata
import json
import platform
from pathlib import Path, PurePosixPath
from typing import Annotated

import typer
from proofpack.bundle import seal_artifact_bundle, verify_bundle
from proofpack.canonical import sha256_digest
from proofpack.errors import IntegrityError, PackValidationError
from proofpack.io import confined_path, strict_json_loads
from proofpack.models import ArtifactLock

from proofpack_fx import __version__

fx_app = typer.Typer(
    help="Seal and verify fx measurement evidence through ProofPack Core.",
    no_args_is_help=True,
)
PRODUCER_DOCUMENT = "proofpack-producer.json"


def attach(root: typer.Typer) -> None:
    """Attach the fx evidence commands without adding fx behavior to ProofPack Core."""
    root.add_typer(fx_app, name="fx")


def _document_digest(root: Path, relative: str, label: str) -> str:
    path = PurePosixPath(relative)
    if (
        path.is_absolute()
        or ".." in path.parts
        or "\\" in relative
        or relative in ("", ".")
    ):
        raise PackValidationError(f"{label} path must be confined to the bundle")
    document_path = confined_path(root, relative)
    try:
        document = strict_json_loads(document_path.read_text(encoding="utf-8"), label)
    except (OSError, UnicodeDecodeError) as error:
        raise PackValidationError(f"cannot read {label}: {error}") from error
    if not isinstance(document, dict):
        raise PackValidationError(f"{label} must contain an object")
    return sha256_digest(document)


def _roots(root: Path, subject_document: str, context_document: str) -> tuple[str, str]:
    return (
        _document_digest(root, subject_document, "fx measurement subject"),
        _document_digest(root, context_document, "fx measurement context"),
    )


def _proofpack_source() -> object:
    distribution = importlib.metadata.distribution("proofpack-core")
    direct_url = distribution.read_text("direct_url.json")
    if direct_url is None:
        return None
    try:
        value = strict_json_loads(direct_url, "proofpack-core direct_url.json")
    except PackValidationError:
        return {"invalid_direct_url_metadata": True}
    if not isinstance(value, dict):
        return {"invalid_direct_url_metadata": True}
    url = value.get("url")
    vcs = value.get("vcs_info")
    if isinstance(url, str) and isinstance(vcs, dict):
        return {"url": url, "vcs_info": vcs}
    return {"installation": "local_or_index"}


def _write_producer_document(root: Path) -> None:
    path = root / PRODUCER_DOCUMENT
    if path.exists() or path.is_symlink():
        raise PackValidationError(
            f"fx evidence bundle already contains {PRODUCER_DOCUMENT}"
        )
    distributions = sorted(
        (
            {
                "name": distribution.metadata.get("Name", "unknown"),
                "version": distribution.version,
            }
            for distribution in importlib.metadata.distributions()
        ),
        key=lambda item: (str(item["name"]).lower(), str(item["version"])),
    )
    document = {
        "schema_version": "proofpack-fx-producer/v1",
        "plugin_version": __version__,
        "proofpack_core_version": importlib.metadata.version("proofpack-core"),
        "proofpack_core_source": _proofpack_source(),
        "python": {
            "implementation": platform.python_implementation(),
            "version": platform.python_version(),
        },
        "installed_distributions": distributions,
    }
    path.write_text(
        json.dumps(document, allow_nan=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _emit(lock: ArtifactLock, json_output: bool) -> None:
    if json_output:
        typer.echo(json.dumps(lock.model_dump(mode="json"), sort_keys=True))
        return
    typer.echo(f"verified: {lock.root_digest}")
    typer.echo(f"subject: {lock.subject_digest}")
    typer.echo(f"context: {lock.context_digest}")


@fx_app.command("seal")
def seal_command(
    bundle: Annotated[
        Path,
        typer.Argument(
            exists=True,
            file_okay=False,
            dir_okay=True,
            readable=True,
            resolve_path=True,
        ),
    ],
    subject_document: Annotated[
        str,
        typer.Option(
            "--subject-document", help="Bundle-relative semantic subject document."
        ),
    ] = "subject.json",
    context_document: Annotated[
        str,
        typer.Option(
            "--context-document", help="Bundle-relative semantic context document."
        ),
    ] = "context.json",
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Seal one already-populated fx measurement directory through ProofPack Core."""
    try:
        subject_digest, context_digest = _roots(
            bundle,
            subject_document,
            context_document,
        )
        _write_producer_document(bundle)
        lock = seal_artifact_bundle(
            bundle,
            subject_digest=subject_digest,
            context_digest=context_digest,
        )
        verified = verify_bundle(bundle)
        if not isinstance(verified, ArtifactLock) or verified != lock:
            raise IntegrityError("fresh fx measurement seal did not verify identically")
    except (IntegrityError, PackValidationError, OSError, ValueError) as error:
        typer.echo(f"fx evidence seal failed: {error}", err=True)
        raise typer.Exit(code=3) from error
    _emit(lock, json_output)


@fx_app.command("verify")
def verify_command(
    bundle: Annotated[
        Path,
        typer.Argument(
            exists=True,
            file_okay=False,
            dir_okay=True,
            readable=True,
            resolve_path=True,
        ),
    ],
    subject_document: Annotated[
        str,
        typer.Option(
            "--subject-document", help="Bundle-relative semantic subject document."
        ),
    ] = "subject.json",
    context_document: Annotated[
        str,
        typer.Option(
            "--context-document", help="Bundle-relative semantic context document."
        ),
    ] = "context.json",
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Verify ProofPack integrity and rederive fx's semantic subject and context roots."""
    try:
        lock = verify_bundle(bundle)
        if not isinstance(lock, ArtifactLock):
            raise IntegrityError(
                "fx measurement bundle does not use proofpack-evidence/v1"
            )
        subject_digest, context_digest = _roots(
            bundle,
            subject_document,
            context_document,
        )
        if lock.subject_digest != subject_digest:
            raise IntegrityError("fx measurement subject digest mismatch")
        if lock.context_digest != context_digest:
            raise IntegrityError("fx measurement context digest mismatch")
    except (IntegrityError, PackValidationError, OSError, ValueError) as error:
        typer.echo(f"fx evidence verification failed: {error}", err=True)
        raise typer.Exit(code=3) from error
    _emit(lock, json_output)
