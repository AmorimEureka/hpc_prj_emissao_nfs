from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping

import dlt
import psycopg2
from psycopg2 import sql

from nfs_fortaleza.ipm_config import IpmSettings
from nfs_fortaleza.ipm_demonstrativo import (
    TABLE_NAME,
    demonstrativo_conta_ipm_resource,
)
from nfs_fortaleza.ipm_portal import IpmPortalClient, IpmReference


# The portal keeps issuing demonstratives for months already downloaded
# (e.g. one issued on 2026-11-10 for 09/2026): the most recent references
# are downloaded again on every run.
RECENT_REFERENCES_TO_REFRESH = int(
    os.getenv("IPM_REFRESH_RECENT_REFERENCES", "3")
)
# Maida writes its rows into the same table; they never count as IPM history
# and are never removed when an IPM reference is reloaded.
MAIDA_ORIGIN_COLUMN = "origem_maida"


class IpmExtractionConfigurationError(ValueError):
    """Raised when dag_run.conf contains an invalid IPM reference."""


@dataclass(frozen=True)
class IpmExtractionPayload:
    references: tuple[IpmReference, ...] = ()

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any] | None,
    ) -> IpmExtractionPayload:
        payload = value or {}
        single = payload.get("referencia")
        multiple = payload.get("referencias")
        if single and multiple:
            raise IpmExtractionConfigurationError(
                "Use referencia ou referencias, nao ambos."
            )

        raw_values: list[Any]
        if single:
            raw_values = [single]
        elif multiple is not None:
            if not isinstance(multiple, (list, tuple)):
                raise IpmExtractionConfigurationError(
                    "referencias deve ser uma lista no formato MM/AAAA."
                )
            raw_values = list(multiple)
        else:
            return cls()

        try:
            references = tuple(
                sorted({IpmReference.parse(str(item)) for item in raw_values})
            )
        except ValueError as exc:
            raise IpmExtractionConfigurationError(str(exc)) from exc
        if not references:
            raise IpmExtractionConfigurationError(
                "Informe ao menos uma referencia em referencias."
            )
        return cls(references=references)


@dataclass(frozen=True)
class IpmExtractionSummary:
    available_references: tuple[IpmReference, ...]
    loaded_references: tuple[IpmReference, ...]
    processed_references: tuple[IpmReference, ...]
    skipped_references: tuple[IpmReference, ...]
    files: tuple[Path, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "referencias_disponiveis": [
                reference.label for reference in self.available_references
            ],
            "referencias_ja_carregadas": [
                reference.label for reference in self.loaded_references
            ],
            "referencias_processadas": [
                reference.label for reference in self.processed_references
            ],
            "referencias_ignoradas": [
                reference.label for reference in self.skipped_references
            ],
            "arquivos": [str(path) for path in self.files],
        }


def extract_and_load_ipm_demonstratives(
    settings: IpmSettings,
    payload: IpmExtractionPayload,
    *,
    downloads_dir: Path,
    timeout_seconds: float = 60,
) -> IpmExtractionSummary:
    loaded = list_loaded_ipm_references(settings)
    client = IpmPortalClient(
        settings,
        downloads_dir=downloads_dir,
        timeout_seconds=timeout_seconds,
    )
    available = client.list_references()
    candidates = _select_references(available, payload.references)
    selected = _references_to_process(
        candidates,
        loaded,
        requested=bool(payload.references),
    )
    skipped = tuple(sorted(set(candidates) - set(selected)))

    if not selected:
        return IpmExtractionSummary(
            available_references=available,
            loaded_references=loaded,
            processed_references=(),
            skipped_references=skipped,
            files=(),
        )

    downloads = tuple(
        client.download_demonstrative(reference) for reference in selected
    )

    # Replace only the IPM rows of the downloaded references; the resource
    # merges by id_registro and Maida rows of the same month stay intact.
    delete_ipm_references(settings, selected)
    os.environ["DESTINATION__POSTGRES__CREDENTIALS"] = settings.database_url
    pipeline = dlt.pipeline(
        pipeline_name="ipm_saude",
        destination="postgres",
        dataset_name=settings.postgres_schema,
    )
    pipeline.run(demonstrativo_conta_ipm_resource(downloads))
    return IpmExtractionSummary(
        available_references=available,
        loaded_references=loaded,
        processed_references=selected,
        skipped_references=skipped,
        files=tuple(download.path for download in downloads),
    )


def list_loaded_ipm_references(
    settings: IpmSettings,
) -> tuple[IpmReference, ...]:
    with psycopg2.connect(settings.database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM information_schema.tables
                    WHERE table_schema = %s
                      AND table_name = %s
                )
                """,
                (settings.postgres_schema, TABLE_NAME),
            )
            table_exists = bool(cursor.fetchone()[0])
            if not table_exists:
                return ()

            query = sql.SQL("SELECT DISTINCT referencia FROM {}.{}").format(
                sql.Identifier(settings.postgres_schema),
                sql.Identifier(TABLE_NAME),
            )
            if _has_maida_rows(cursor, settings):
                query = query + sql.SQL(" WHERE {} IS NULL").format(
                    sql.Identifier(MAIDA_ORIGIN_COLUMN)
                )
            cursor.execute(query)
            references = {
                _reference_from_database(row[0])
                for row in cursor.fetchall()
                if row[0] is not None
            }
    return tuple(sorted(references))


def _has_maida_rows(cursor: Any, settings: IpmSettings) -> bool:
    """Whether the shared table has the Maida origin column."""
    cursor.execute(
        """
        SELECT EXISTS (
            SELECT 1
            FROM information_schema.columns
            WHERE table_schema = %s
              AND table_name = %s
              AND column_name = %s
        )
        """,
        (settings.postgres_schema, TABLE_NAME, MAIDA_ORIGIN_COLUMN),
    )
    return bool(cursor.fetchone()[0])


def delete_ipm_references(
    settings: IpmSettings,
    references: tuple[IpmReference, ...],
) -> None:
    """Remove the IPM rows (never Maida's) of references about to reload.

    If the load fails afterwards the reference no longer counts as loaded
    and the next run downloads it again.
    """
    if not references:
        return
    with psycopg2.connect(settings.database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM information_schema.tables
                    WHERE table_schema = %s
                      AND table_name = %s
                )
                """,
                (settings.postgres_schema, TABLE_NAME),
            )
            if not cursor.fetchone()[0]:
                return
            query = sql.SQL(
                "DELETE FROM {}.{} WHERE referencia = ANY(%s)"
            ).format(
                sql.Identifier(settings.postgres_schema),
                sql.Identifier(TABLE_NAME),
            )
            if _has_maida_rows(cursor, settings):
                query = query + sql.SQL(" AND {} IS NULL").format(
                    sql.Identifier(MAIDA_ORIGIN_COLUMN)
                )
            cursor.execute(
                query,
                ([date(r.year, r.month, 1) for r in references],),
            )


def _reference_from_database(value: object) -> IpmReference:
    if isinstance(value, datetime):
        value = value.date()
    if isinstance(value, date):
        return IpmReference(year=value.year, month=value.month)

    raw = str(value).strip()
    try:
        parsed = date.fromisoformat(raw)
    except ValueError:
        try:
            return IpmReference.parse(raw)
        except ValueError as exc:
            raise RuntimeError(
                f"Referencia invalida encontrada em {TABLE_NAME}: {value!r}."
            ) from exc
    return IpmReference(year=parsed.year, month=parsed.month)


def _select_references(
    available: tuple[IpmReference, ...],
    requested: tuple[IpmReference, ...],
) -> tuple[IpmReference, ...]:
    if not available:
        raise RuntimeError("O portal IPM nao possui referencias disponiveis.")
    if not requested:
        return available

    missing = sorted(set(requested) - set(available))
    if missing:
        labels = ", ".join(reference.label for reference in missing)
        raise IpmExtractionConfigurationError(
            f"Referencias nao disponiveis no portal IPM: {labels}."
        )
    return requested


def _references_to_process(
    candidates: tuple[IpmReference, ...],
    loaded: tuple[IpmReference, ...],
    *,
    requested: bool = False,
) -> tuple[IpmReference, ...]:
    """New references plus the most recent ones, which still change.

    References named in dag_run.conf are always processed again.
    """
    if requested:
        return candidates
    recent = (
        set(candidates[-RECENT_REFERENCES_TO_REFRESH:])
        if RECENT_REFERENCES_TO_REFRESH > 0
        else set()
    )
    new = set(_only_new_references(candidates, loaded))
    return tuple(sorted(new | recent))


def _only_new_references(
    candidates: tuple[IpmReference, ...],
    loaded: tuple[IpmReference, ...],
) -> tuple[IpmReference, ...]:
    loaded_set = set(loaded)
    return tuple(
        reference for reference in candidates if reference not in loaded_set
    )
