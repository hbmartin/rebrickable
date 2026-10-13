"""Explicit transactional refresh of all downloadable catalog datasets."""

from __future__ import annotations

import asyncio
import errno
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import tempfile
import time
import traceback
import uuid
from contextlib import ExitStack, suppress
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx2
from filelock import FileLock, Timeout

from rebrickable._atomic import durable_replace
from rebrickable._workers import acquire_lock, run_in_thread
from rebrickable.bridge.overrides import materialize_overrides
from rebrickable.catalog.database import (
    _MAX_POINTER_BYTES,
    CatalogPaths,
    _CacheEligibility,
    _catalog_metadata,
    _copy_source_failure,
    _database_contained,
    _file_signature,
    _inspect_catalog,
    _open_regular_file,
    _read_active_pointer,
    _read_bounded_json,
    _read_only_uri,
    _read_pointer,
    _resolve_catalog_path,
    _safe_snapshot_id,
    _snapshot_contained,
    _snapshot_directory,
    _structural_path_error,
    _verification_reusable,
    _VerificationStatus,
    _verify_database,
)
from rebrickable.catalog.importers import DATASETS, import_catalog, inspect_header
from rebrickable.catalog.schema import SCHEMA_VERSION
from rebrickable.config import Config
from rebrickable.errors import (
    CatalogImportError,
    DatasetIntegrityError,
    DownloadError,
)
from rebrickable.progress import (
    ProgressCallback,
    ProgressEvent,
    ProgressStage,
    ProgressUnit,
)
from rebrickable.types import (
    CatalogStatus,
    FileFingerprint,
    RefreshOutcome,
    RefreshReport,
)

if TYPE_CHECKING:
    from rebrickable.catalog.database import _ActivePointer, _DatabaseVerification


def _emit(callback: ProgressCallback | None, event: ProgressEvent) -> None:
    if callback is not None:
        callback(event)


def _encode_json(payload: dict[str, Any]) -> bytes:
    return f"{json.dumps(payload, sort_keys=True, separators=(',', ':'))}\n".encode()


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    encoded = _encode_json(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}."
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        durable_replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _check_pointer_destination(paths: CatalogPaths) -> None:
    """Reject obstructions before building a replacement that cannot activate."""
    try:
        parent = paths.active_pointer.parent
        while True:
            try:
                metadata = parent.stat()
            except FileNotFoundError:
                if parent.is_symlink():
                    raise NotADirectoryError(
                        f"pointer parent is a dangling link: {parent}"
                    ) from None
                if parent.parent == parent:
                    raise
                parent = parent.parent
                continue
            if not stat.S_ISDIR(metadata.st_mode):
                raise NotADirectoryError(f"pointer parent is not a directory: {parent}")
            break
        try:
            metadata = paths.active_pointer.lstat()
        except FileNotFoundError:
            return
        if not (stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode)):
            raise OSError(
                f"active pointer cannot replace non-file: {paths.active_pointer}"
            )
    except OSError as exc:
        raise CatalogImportError(f"unable to activate catalog: {exc}") from exc


async def _download_one(
    client: httpx2.AsyncClient,
    *,
    url: str,
    destination: Path,
    previous: FileFingerprint | None,
    force: bool,
    callback: ProgressCallback | None,
) -> tuple[bool, dict[str, Any]]:
    headers: dict[str, str] = {"Accept-Encoding": "identity"}
    if not force and previous is not None:
        if previous.etag:
            headers["If-None-Match"] = previous.etag
        if previous.last_modified:
            headers["If-Modified-Since"] = previous.last_modified
    try:
        async with client.stream("GET", url, headers=headers) as response:
            if response.status_code == 304:
                if previous is None:
                    raise DownloadError(
                        f"{url} returned 304 without a prior fingerprint"
                    )
                return False, asdict(previous)
            response.raise_for_status()
            digest = hashlib.sha256()
            length = 0
            with destination.open("wb") as handle:
                # Catalog objects are gzip archives, including when mislabeled
                # with Content-Encoding. Preserve the archive bytes verbatim.
                async for block in response.aiter_raw():
                    handle.write(block)
                    digest.update(block)
                    length += len(block)
                    _emit(
                        callback,
                        ProgressEvent(
                            stage=ProgressStage.DOWNLOAD,
                            dataset=destination.name,
                            current=length,
                            unit=ProgressUnit.BYTES,
                            path=destination,
                        ),
                    )
            return True, {
                "url": url,
                "etag": response.headers.get("etag"),
                "last_modified": response.headers.get("last-modified"),
                "byte_length": length,
                "sha256": digest.hexdigest(),
            }
    except (httpx2.HTTPError, OSError) as exc:
        raise DownloadError(f"unable to download {url}: {exc}") from exc


def _reuse_archive(
    paths: CatalogPaths,
    snapshot_id: str,
    filename: str,
    destination: Path,
    previous: FileFingerprint,
) -> bool:
    """Copy verified archive bytes from one stable, contained regular file."""
    source = paths.snapshots_dir / snapshot_id / filename

    def identity() -> tuple[Path, os.stat_result] | None:
        snapshot = _snapshot_directory(paths, snapshot_id)
        resolved = _resolve_catalog_path(source)
        if snapshot is None or not resolved.is_relative_to(snapshot):
            return None
        metadata = resolved.stat()
        return (resolved, metadata) if stat.S_ISREG(metadata.st_mode) else None

    with ExitStack() as resources:
        try:
            approved = identity()
            if approved is None:
                return False
            resolved, metadata = approved
            if metadata.st_size != previous.byte_length:
                return False
            opened = resources.enter_context(
                _open_regular_file(resolved, nofollow=True)
            )
        except OSError as exc:
            if _structural_path_error(exc):
                return False
            if exc.errno in {errno.EACCES, errno.EPERM}:
                try:
                    if identity() is None:
                        return False
                except OSError as probe_error:
                    if _structural_path_error(probe_error):
                        return False
                    exc.add_note(f"archive identity probe also failed: {probe_error}")
            raise
        if opened is None:
            return False
        handle, descriptor_metadata = opened

        def same_file(candidate: os.stat_result) -> bool:
            return stat.S_ISREG(candidate.st_mode) and (
                _file_signature(candidate) == _file_signature(descriptor_metadata)
            )

        try:
            checked = identity()
        except OSError as exc:
            if _structural_path_error(exc):
                return False
            raise
        if (
            not same_file(metadata)
            or checked is None
            or not same_file(checked[1])
            or checked[0] != resolved
        ):
            return False
        digest = hashlib.sha256()
        length = 0
        with destination.open("wb") as output:
            while block := handle.read(65_536):
                output.write(block)
                digest.update(block)
                length += len(block)
        return length == previous.byte_length and digest.hexdigest() == previous.sha256


def _copy_crosswalk(
    connection: sqlite3.Connection,
    previous: Path,
    *,
    eligibility: _CacheEligibility | None = None,
) -> str | None:
    """Copy cached mappings, recovering only from a confirmed unreadable source."""
    eligibility = eligibility or _CacheEligibility()
    retried = False
    while True:
        attached = False
        try:
            connection.execute(
                "ATTACH DATABASE ? AS previous", (_read_only_uri(previous),)
            )
            attached = True
            connection.execute("BEGIN")
            metadata = _catalog_metadata(connection, attached=True)
            if reason := eligibility.reason(metadata):
                connection.rollback()
                connection.execute("DETACH DATABASE previous")
                return reason
            connection.execute(
                "INSERT OR IGNORE INTO api_crosswalk_cache SELECT * FROM previous.api_crosswalk_cache",
            )
        except sqlite3.Error as exc:
            # Restore the candidate before probing or repairing the source.
            cleanup_failure = None
            cleanup_phase = "rollback"
            try:
                connection.rollback()
                if attached:
                    cleanup_phase = "DETACH"
                    connection.execute("DETACH DATABASE previous")
            except Exception as cleanup_error:  # noqa: BLE001 - ordinary cleanup failures leave the SQLite exception primary
                cleanup_failure = cleanup_error
            if cleanup_failure is not None:
                detail = "".join(
                    traceback.format_exception(cleanup_failure, chain=False)
                )
                exc.add_note(
                    f"mapping cache {cleanup_phase} cleanup also failed:\n{detail}"
                )
                raise
            code = getattr(exc, "sqlite_errorcode", 0)
            if not _copy_source_failure(exc):
                raise
            verification = _verify_database(previous)
            if not retried and (
                code == sqlite3.SQLITE_READONLY_ROLLBACK
                or verification.status is _VerificationStatus.RECOVERY_REQUIRED
            ):
                retried = True
                if verification.status is _VerificationStatus.RECOVERY_REQUIRED:
                    verification = _verify_database(previous, recover_hot_journal=True)
                if verification.status is _VerificationStatus.HEALTHY:
                    if reason := eligibility.reason(verification):
                        return reason
                    continue
            if verification.status in {
                _VerificationStatus.CORRUPT,
                _VerificationStatus.MISSING,
                _VerificationStatus.INCOMPATIBLE,
            }:
                return eligibility.reason(verification)
            if (
                verification.error is not None
                and verification.status is _VerificationStatus.OPERATIONAL_FAILURE
            ):
                raise verification.error from exc
            raise
        return None


def _carry_crosswalk(
    previous: Path | None,
    candidate: Path,
    *,
    eligibility: _CacheEligibility | None = None,
) -> str | None:
    connection = None
    failed = False
    try:
        connection = sqlite3.connect(candidate, uri=True)
        skipped_cache = (
            _copy_crosswalk(connection, previous, eligibility=eligibility)
            if previous is not None
            else None
        )
        connection.execute(
            "DELETE FROM api_crosswalk_cache WHERE entity_kind='part' "
            "AND canonical_id NOT IN (SELECT part_num FROM parts)"
        )
        connection.execute(
            "DELETE FROM api_crosswalk_cache WHERE entity_kind='color' "
            "AND canonical_id NOT IN (SELECT CAST(id AS TEXT) FROM colors)"
        )
        connection.execute(
            """
            UPDATE search_documents
            SET external_ids=trim(coalesce((
                SELECT group_concat(value, ' ') FROM (
                    SELECT external_id AS value FROM api_crosswalk_cache c
                    WHERE c.entity_kind=search_documents.kind
                      AND c.canonical_id=search_documents.canonical_id
                    UNION
                    SELECT source_id AS value FROM user_mapping_overrides u
                    WHERE u.entity_kind=search_documents.kind
                      AND u.target_system='rebrickable'
                      AND u.target_id=search_documents.canonical_id
                    ORDER BY value
                )
            ), ''))
            WHERE kind IN ('part', 'color')
            """
        )
        connection.execute("INSERT INTO search_fts(search_fts) VALUES('rebuild')")
        connection.commit()
    except (OSError, sqlite3.Error) as exc:
        failed = True
        raise CatalogImportError(f"unable to carry mapping cache: {exc}") from exc
    except BaseException:
        failed = True
        raise
    finally:
        if connection is not None:
            if failed:
                with suppress(BaseException):
                    connection.close()
            else:
                try:
                    connection.close()
                except Exception as exc:
                    raise CatalogImportError(
                        f"unable to close mapping cache: {exc}"
                    ) from exc
    return skipped_cache


_ACTIVATION_MARKER = ".rebrickable-activation.json"


def _history_present(paths: CatalogPaths, snapshot_id: str) -> bool:
    try:
        (paths.snapshots_dir / snapshot_id).lstat()
    except FileNotFoundError:
        return False
    except OSError:
        return True  # Preserve uncertain provenance for a later cleanup retry.
    return True


def _activation_history(pointer: _ActivePointer) -> tuple[str, tuple[str, ...]]:
    """Read proven activation order, or bootstrap conservatively from the pointer."""
    try:
        generation = pointer.data["_activation_generation"]
        history = pointer.data["_activation_history"]
        if (
            not isinstance(generation, str)
            or not isinstance(history, list)
            or not history
            or any(not _safe_snapshot_id(item) for item in history)
            or len(set(history)) != len(history)
            or pointer.activation_id != history[0]
        ):
            raise ValueError("invalid activation metadata")
        uuid.UUID(generation)
    except (KeyError, TypeError, ValueError):
        return uuid.uuid4().hex, (
            pointer.activation_id,
        ) if pointer.activation_id else ()
    return generation, tuple(history)


def _managed_snapshot(path: Path, generation: str | None) -> bool:
    if generation is None:
        return False
    try:
        with _open_regular_file(path / _ACTIVATION_MARKER, nofollow=True) as opened:
            if opened is None:
                return False
            marker = _read_bounded_json(*opened, limit=_MAX_POINTER_BYTES)
    except (OSError, ValueError, RecursionError):
        return False
    return marker == {"generation": generation}


def _prune_snapshots(
    paths: CatalogPaths,
    active_id: str,
    retain: int,
    *,
    previous_id: str | None = None,
    generation: str | None = None,
    history: tuple[str, ...] = (),
) -> None:
    """Remove superseded snapshots and orphaned partial transfers."""
    try:
        children = tuple(paths.snapshots_dir.iterdir())
    except FileNotFoundError:
        return
    if _read_pointer(paths) != active_id:
        return

    def contained(snapshot_id: str) -> bool:
        try:
            return _snapshot_contained(paths, snapshot_id)
        except (OSError, RuntimeError):
            return False  # Uncertain cleanup targets are protected for a retry.

    existing = set()
    for child in children:
        try:
            if child.is_dir() and contained(child.name):
                existing.add(child.name)
        except OSError:
            continue
    keep = {active_id}
    if retain > 1 and previous_id in existing:
        keep.add(previous_id)
    for snapshot_id in history:
        if len(keep) >= retain:
            break
        if snapshot_id in existing:
            keep.add(snapshot_id)
    for child in children:
        if child.name not in existing or child.name in keep:
            continue
        if (
            not child.name.startswith(".incoming-")
            and child.name not in history
            and not _managed_snapshot(child, generation)
        ):
            # An older implementation may have activated this directory.
            continue
        try:
            shutil.rmtree(child)
        except OSError:
            continue


def _prospective_pointer(
    paths: CatalogPaths, pointer: _ActivePointer, snapshot_id: str
) -> tuple[dict[str, Any], str | None]:
    """Preserve activation provenance and check capacity before expensive work."""
    if pointer.size_error is not None:
        raise CatalogImportError(
            f"active snapshot pointer {paths.active_pointer} {pointer.size_error}; "
            "review accumulated activation history"
        )
    previous_id = pointer.activation_id
    if previous_id is not None and not _history_present(paths, previous_id):
        previous_id = None
    generation, prior_history = _activation_history(pointer)
    history = tuple(
        dict.fromkeys(
            (
                snapshot_id,
                *((previous_id,) if previous_id is not None else ()),
                # Keep uncertain paths and earlier failed deletions for a retry.
                *(item for item in prior_history if _history_present(paths, item)),
            )
        )
    )
    payload = {
        "snapshot_id": snapshot_id,
        "_activation_generation": generation,
        "_activation_history": history,
    }
    length = len(_encode_json(payload))
    if length > _MAX_POINTER_BYTES:
        raise CatalogImportError(
            f"active snapshot pointer {paths.active_pointer} exceeds the 1 MiB limit "
            f"({length} bytes > {_MAX_POINTER_BYTES} bytes; "
            f"{len(history)} activation history entries); "
            "review accumulated activation history"
        )
    return payload, previous_id


def _promote_snapshot(
    paths: CatalogPaths,
    *,
    staging: Path,
    incoming: Path,
    destination: Path,
    pointer: _ActivePointer,
    source_verification: _DatabaseVerification | None,
    retain: int,
    eligibility: _CacheEligibility,
) -> str | None:
    """Complete promotion while the caller owns and drains the promotion lock."""
    try:
        _check_pointer_destination(paths)
        pointer_payload, previous_id = _prospective_pointer(
            paths, pointer, destination.name
        )
        generation = pointer_payload["_activation_generation"]
        history = pointer_payload["_activation_history"]
        previous = None
        skipped_cache = None
        if pointer.activation_id is not None:
            try:
                safe = _database_contained(paths, pointer.activation_id)
            except OSError as exc:
                if not _structural_path_error(exc):
                    raise CatalogImportError(
                        f"unable to verify mapping source: {exc}"
                    ) from exc
                safe = False
            if safe:
                source = paths.database_for(pointer.activation_id)
                verification = (
                    source_verification
                    if source_verification is not None
                    and _verification_reusable(
                        source_verification, source, allow_stable_negative=True
                    )
                    else _verify_database(source)
                )
                if verification.status is _VerificationStatus.OPERATIONAL_FAILURE:
                    raise CatalogImportError(
                        f"unable to verify mapping source: {verification.error}"
                    ) from verification.error
                skipped_cache = eligibility.reason(verification)
                if skipped_cache is None and verification.status in {
                    _VerificationStatus.HEALTHY,
                    _VerificationStatus.RECOVERY_REQUIRED,
                }:
                    previous = source
            else:
                skipped_cache = (
                    "previous catalog source is unsafe or structurally unavailable"
                )
        skipped_cache = (
            _carry_crosswalk(
                previous,
                staging / paths.configured_database.name,
                eligibility=eligibility,
            )
            or skipped_cache
        )
        _atomic_json(staging / _ACTIVATION_MARKER, {"generation": generation})
        shutil.move(staging, incoming)
        _check_pointer_destination(paths)
        durable_replace(incoming, destination)
        _atomic_json(paths.active_pointer, pointer_payload)
        _prune_snapshots(
            paths,
            destination.name,
            retain,
            previous_id=previous_id,
            generation=generation,
            history=history,
        )
    except BaseException:
        # A durable pointer replacement can succeed before a later fsync fails.
        # Remove the candidate only if the pointer proves it is inactive.
        active_id = _read_pointer(paths)
        if active_id not in {"", destination.name}:
            with suppress(OSError):
                shutil.rmtree(destination)
        raise
    return skipped_cache


def _prune_abandoned_staging(cache_path: Path) -> None:
    """Remove only uniquely named staging directories abandoned over a day ago."""
    threshold = time.time() - 86_400
    try:
        children = tuple(cache_path.iterdir())
    except FileNotFoundError:
        return
    for child in children:
        try:
            if (
                child.is_dir()
                and child.name.startswith("refresh-")
                and child.stat().st_mtime < threshold
            ):
                shutil.rmtree(child)
        except OSError:
            continue


async def _refresh_attempt(
    config: Config,
    *,
    force: bool,
    callback: ProgressCallback | None,
) -> RefreshReport:
    paths = CatalogPaths.from_config(config)
    await run_in_thread(_check_pointer_destination, paths)
    now = datetime.now(UTC)
    snapshot_id = now.strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:12]
    pointer = await run_in_thread(_read_active_pointer, paths)
    await run_in_thread(_prospective_pointer, paths, pointer, snapshot_id)
    prior_inspection = await _inspect_catalog(config, pointer, verify=True)
    if prior_inspection.error is not None:
        raise CatalogImportError(
            f"unable to verify previous catalog: {prior_inspection.error}"
        ) from prior_inspection.error
    prior_state = prior_inspection.state
    prior_by_name = {item.dataset: item for item in prior_state.files}
    config.cache_path.mkdir(parents=True, exist_ok=True)
    await run_in_thread(_prune_abandoned_staging, config.cache_path)
    staging = Path(tempfile.mkdtemp(prefix="refresh-", dir=config.cache_path))
    incoming = paths.snapshots_dir / f".incoming-{snapshot_id}"
    file_paths = {item.name: staging / item.filename for item in DATASETS}
    metadata: dict[str, dict[str, Any]] = {}
    changed: dict[str, bool] = {}
    semaphore = asyncio.Semaphore(config.refresh_concurrency)
    timeout = httpx2.Timeout(
        connect=config.connect_timeout,
        read=config.read_timeout,
        write=config.write_timeout,
        pool=config.pool_timeout,
    )

    async def fetch(name: str, filename: str) -> None:
        async with semaphore:
            url = f"{config.downloads_base_url.rstrip('/')}/{filename}"
            was_changed, item = await _download_one(
                client,
                url=url,
                destination=file_paths[name],
                previous=prior_by_name.get(name),
                force=force,
                callback=callback,
            )
            if not was_changed:
                if prior_state.snapshot_id is None:
                    raise DownloadError(
                        f"{filename} returned 304 without an active snapshot"
                    )
                try:
                    reused = await run_in_thread(
                        _reuse_archive,
                        paths,
                        prior_state.snapshot_id,
                        filename,
                        file_paths[name],
                        prior_by_name[name],
                    )
                except OSError as exc:
                    raise DownloadError(
                        f"unable to reuse cached {filename} from snapshot "
                        f"{prior_state.snapshot_id}: {exc}"
                    ) from exc
                if not reused:
                    was_changed, item = await _download_one(
                        client,
                        url=url,
                        destination=file_paths[name],
                        previous=None,
                        force=True,
                        callback=callback,
                    )
            changed[name] = was_changed
            metadata[name] = item

    try:
        config.cache_path.mkdir(parents=True, exist_ok=True)
        async with httpx2.AsyncClient(
            timeout=timeout, follow_redirects=False
        ) as client:
            try:
                async with asyncio.TaskGroup() as group:
                    for definition in DATASETS:
                        group.create_task(fetch(definition.name, definition.filename))
            except* DownloadError as errors:
                raise errors.exceptions[0] from None
        if (
            changed
            and not any(changed.values())
            and prior_state.status is CatalogStatus.READY
        ):
            if prior_state.snapshot_id is None or prior_state.retrieved_at is None:
                raise CatalogImportError("unchanged refresh has no active snapshot")
            return RefreshReport(
                outcome=RefreshOutcome.UNCHANGED,
                snapshot_id=prior_state.snapshot_id,
                retrieved_at=prior_state.retrieved_at,
                files=prior_state.files,
            )
        for definition in DATASETS:
            _emit(
                callback,
                ProgressEvent(stage=ProgressStage.VALIDATE, dataset=definition.name),
            )
            await run_in_thread(inspect_header, file_paths[definition.name], definition)
        database = staging / config.database_path.name
        loop = asyncio.get_running_loop()

        def reporter(dataset: str, count: int) -> None:
            loop.call_soon_threadsafe(
                _emit,
                callback,
                ProgressEvent(
                    stage=ProgressStage.IMPORT,
                    dataset=dataset,
                    current=count,
                    unit=ProgressUnit.ROWS,
                ),
            )

        row_counts, unknown = await run_in_thread(
            import_catalog,
            file_paths,
            database,
            snapshot_id=snapshot_id,
            retrieved_at=now.isoformat(),
            reporter=reporter,
            build_fts=False,
        )
        await run_in_thread(
            materialize_overrides, config.mapping_overrides_path, database
        )
        fingerprints = tuple(
            FileFingerprint(
                dataset=definition.name,
                url=str(metadata[definition.name]["url"]),
                etag=metadata[definition.name].get("etag"),
                last_modified=metadata[definition.name].get("last_modified"),
                byte_length=int(metadata[definition.name]["byte_length"]),
                sha256=str(metadata[definition.name]["sha256"]),
                row_count=row_counts[definition.name],
                unknown_columns=unknown[definition.name],
            )
            for definition in DATASETS
        )
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "snapshot_id": snapshot_id,
            "retrieved_at": now.isoformat(),
            "files": [asdict(item) for item in fingerprints],
        }
        _atomic_json(staging / "manifest.json", manifest)
        paths.snapshots_dir.mkdir(parents=True, exist_ok=True)
        destination = paths.snapshots_dir / snapshot_id
        _emit(
            callback,
            ProgressEvent(stage=ProgressStage.PROMOTE, message="promoting snapshot"),
        )
        lock = FileLock(
            paths.lock_file,
            timeout=config.lock_timeout,
            thread_local=False,
        )
        try:
            await acquire_lock(lock)
        except Timeout as exc:
            raise CatalogImportError(
                "another process holds the catalog promotion lock"
            ) from exc
        try:
            pointer = _read_active_pointer(paths)
            reusable = (
                prior_inspection.verification
                if pointer.activation_id == prior_state.snapshot_id
                and prior_inspection.verification is not None
                and prior_inspection.verification.status is _VerificationStatus.HEALTHY
                else None
            )
            inspection = await _inspect_catalog(config, pointer, verification=reusable)
            current_state = inspection.state
            if (
                current_state.status is CatalogStatus.READY
                and inspection.verification is None
            ):
                inspection = await _inspect_catalog(config, pointer, verify=True)
                current_state = inspection.state
            if inspection.error is not None:
                raise CatalogImportError(
                    f"unable to verify previous catalog: {inspection.error}"
                ) from inspection.error
            skipped_cache = await run_in_thread(
                _promote_snapshot,
                paths,
                staging=staging,
                incoming=incoming,
                destination=destination,
                pointer=pointer,
                source_verification=inspection.verification,
                retain=config.snapshot_retention,
                eligibility=inspection.cache_eligibility,
            )
        finally:
            lock.release()
        if skipped_cache:
            _emit(
                callback,
                ProgressEvent(
                    stage=ProgressStage.PROMOTE,
                    message=f"{skipped_cache}; skipped cached API mappings",
                ),
            )
        report = RefreshReport(
            outcome=RefreshOutcome.UPDATED,
            snapshot_id=snapshot_id,
            retrieved_at=now,
            files=fingerprints,
        )
        _emit(
            callback, ProgressEvent(stage=ProgressStage.DONE, message="catalog ready")
        )
        return report
    finally:
        for leftover in (staging, incoming):
            if leftover.exists():
                await run_in_thread(shutil.rmtree, leftover, True)


async def refresh_catalog(
    config: Config,
    *,
    force: bool = False,
    callback: ProgressCallback | None = None,
) -> RefreshReport:
    """Refresh all catalog datasets and atomically promote a complete snapshot."""
    config.cache_path.mkdir(parents=True, exist_ok=True)
    try:
        return await _refresh_attempt(config, force=force, callback=callback)
    except DatasetIntegrityError as original:
        await asyncio.sleep(2)
        try:
            return await _refresh_attempt(config, force=True, callback=callback)
        except DatasetIntegrityError as retry_failure:
            raise retry_failure from original
