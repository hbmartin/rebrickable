"""Explicit transactional refresh of all downloadable catalog datasets."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import time
import uuid
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx2
from filelock import FileLock

from rebrickable.bridge.overrides import materialize_overrides
from rebrickable.catalog.database import CatalogPaths, catalog_state
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
from rebrickable.types import FileFingerprint, RefreshOutcome, RefreshReport


def _emit(callback: ProgressCallback | None, event: ProgressEvent) -> None:
    if callback is not None:
        callback(event)


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}."
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


async def _download_one(
    client: httpx2.AsyncClient,
    *,
    url: str,
    destination: Path,
    previous: FileFingerprint | None,
    force: bool,
    callback: ProgressCallback | None,
) -> tuple[bool, dict[str, Any]]:
    headers: dict[str, str] = {}
    if not force and previous is not None:
        if previous.etag:
            headers["If-None-Match"] = previous.etag
        if previous.last_modified:
            headers["If-Modified-Since"] = previous.last_modified
    try:
        async with client.stream("GET", url, headers=headers) as response:
            if response.status_code == 304:
                return False, asdict(previous) if previous is not None else {}
            response.raise_for_status()
            digest = hashlib.sha256()
            length = 0
            with destination.open("wb") as handle:
                async for block in response.aiter_bytes():
                    handle.write(block)
                    digest.update(block)
                    length += len(block)
                    _emit(
                        callback,
                        ProgressEvent(
                            ProgressStage.DOWNLOAD,
                            destination.name,
                            length,
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


def _carry_crosswalk(previous: Path | None, candidate: Path) -> None:
    connection = sqlite3.connect(candidate)
    try:
        if previous is not None and previous.is_file():
            connection.execute("ATTACH DATABASE ? AS previous", (str(previous),))
            connection.execute(
                "INSERT OR IGNORE INTO api_crosswalk_cache SELECT * FROM previous.api_crosswalk_cache",
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
        result = connection.execute("PRAGMA integrity_check").fetchone()
        if result is None or result[0] != "ok":
            raise CatalogImportError("mapping cache integrity check failed")
        connection.commit()
    except sqlite3.Error as exc:
        raise CatalogImportError(f"unable to carry mapping cache: {exc}") from exc
    finally:
        connection.close()


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
    prior_state = await catalog_state(config)
    prior_by_name = {item.dataset: item for item in prior_state.files}
    config.cache_path.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(_prune_abandoned_staging, config.cache_path)
    staging = Path(tempfile.mkdtemp(prefix="refresh-", dir=config.cache_path))
    now = datetime.now(UTC)
    snapshot_id = now.strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:12]
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
            changed[name] = was_changed
            metadata[name] = item
            if not was_changed:
                if prior_state.snapshot_id is None:
                    raise DownloadError(
                        f"{filename} returned 304 without an active snapshot"
                    )
                source = paths.snapshots_dir / prior_state.snapshot_id / filename
                await asyncio.to_thread(shutil.copy2, source, file_paths[name])

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
        if changed and not any(changed.values()):
            if prior_state.snapshot_id is None or prior_state.retrieved_at is None:
                raise CatalogImportError("unchanged refresh has no active snapshot")
            return RefreshReport(
                RefreshOutcome.UNCHANGED,
                prior_state.snapshot_id,
                prior_state.retrieved_at,
                prior_state.files,
            )
        for definition in DATASETS:
            _emit(callback, ProgressEvent(ProgressStage.VALIDATE, definition.name))
            await asyncio.to_thread(
                inspect_header, file_paths[definition.name], definition
            )
        database = staging / config.database_path.name
        loop = asyncio.get_running_loop()

        def reporter(dataset: str, count: int) -> None:
            loop.call_soon_threadsafe(
                _emit,
                callback,
                ProgressEvent(
                    ProgressStage.IMPORT,
                    dataset,
                    count,
                    unit=ProgressUnit.ROWS,
                ),
            )

        row_counts, unknown = await asyncio.to_thread(
            import_catalog,
            file_paths,
            database,
            snapshot_id=snapshot_id,
            retrieved_at=now.isoformat(),
            reporter=reporter,
        )
        await asyncio.to_thread(
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
            callback, ProgressEvent(ProgressStage.PROMOTE, message="promoting snapshot")
        )
        lock = FileLock(paths.lock_file)
        await asyncio.to_thread(lock.acquire)
        try:
            previous_database = (
                paths.database_for(prior_state.snapshot_id)
                if prior_state.snapshot_id
                else None
            )
            await asyncio.to_thread(_carry_crosswalk, previous_database, database)
            await asyncio.to_thread(shutil.move, staging, destination)
            _atomic_json(paths.active_pointer, {"snapshot_id": snapshot_id})
        finally:
            lock.release()
        report = RefreshReport(RefreshOutcome.UPDATED, snapshot_id, now, fingerprints)
        _emit(callback, ProgressEvent(ProgressStage.DONE, message="catalog ready"))
        return report
    finally:
        if staging.exists():
            await asyncio.to_thread(shutil.rmtree, staging, True)


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
    except DatasetIntegrityError:
        await asyncio.sleep(2)
        return await _refresh_attempt(config, force=True, callback=callback)
