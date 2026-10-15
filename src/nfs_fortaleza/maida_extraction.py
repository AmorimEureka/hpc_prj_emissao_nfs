from __future__ import annotations

import logging
import os
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

import dlt
import psycopg2

from nfs_fortaleza.ipm_demonstrativo import _column_hints
from nfs_fortaleza.maida_config import MaidaSettings
from nfs_fortaleza.maida_portal import (
    DownloadedMaidaDocument,
    MaidaCompetency,
    MaidaCompetencyResult,
    MaidaPortalClient,
)
from nfs_fortaleza.maida_xlsx import MaidaSpreadsheetError, parse_maida_xlsx


START_COMPETENCY = MaidaCompetency(2026, 1)
CONTROL_TABLE = "maida_competencias_glosa"
DOCUMENT_TABLE = "maida_documentos_glosa"
LOT_TABLE = "maida_lotes_glosa"
TARGET_TABLE = "demonstrativo_conta_ipm"
logger = logging.getLogger(__name__)


class MaidaExtractionConfigurationError(ValueError):
    """Raised when dag_run.conf contains invalid competencies."""


@dataclass(frozen=True)
class MaidaExtractionPayload:
    competencies: tuple[MaidaCompetency, ...] = ()

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any] | None,
    ) -> MaidaExtractionPayload:
        payload = value or {}
        raw = payload.get("competencias")
        if raw is None and payload.get("competencia"):
            raw = [payload["competencia"]]
        if raw is None:
            return cls()
        if not isinstance(raw, (list, tuple)):
            raise MaidaExtractionConfigurationError(
                "competencias deve ser uma lista de MM/AAAA."
            )
        try:
            competencies = tuple(
                sorted({MaidaCompetency.parse(str(item)) for item in raw})
            )
        except ValueError as exc:
            raise MaidaExtractionConfigurationError(str(exc)) from exc
        return cls(competencies=competencies)


@dataclass(frozen=True)
class MaidaExtractionSummary:
    competencies: tuple[MaidaCompetency, ...]
    documents: int
    lots: int
    glosa_records: int

    def as_dict(self) -> dict[str, object]:
        return {
            "competencias": [item.label for item in self.competencies],
            "documentos": self.documents,
            "lotes": self.lots,
            "registros_glosa": self.glosa_records,
        }


def extract_and_load_maida_glosas(
    settings: MaidaSettings,
    payload: MaidaExtractionPayload,
    *,
    downloads_dir: Path,
    timeout_seconds: float = 60,
    today: date | None = None,
) -> MaidaExtractionSummary:
    current_date = today or date.today()
    competencies = payload.competencies or select_scheduled_competencies(
        settings,
        current_date,
    )
    if not competencies:
        return MaidaExtractionSummary((), 0, 0, 0)

    client = MaidaPortalClient(
        settings,
        downloads_dir=downloads_dir,
        timeout_seconds=timeout_seconds,
    )
    results: list[MaidaCompetencyResult] = []
    downloads: list[DownloadedMaidaDocument] = []
    records: list[dict[str, Any]] = []
    for competency in competencies:
        result = client.collect_competency(competency)
        results.append(result)
        for document in result.selected_documents:
            downloaded = client.download(document)
            downloads.append(downloaded)
            parsed = _parse_glosa_records(downloaded)
            records.extend(_enrich_glosa_records(parsed, downloaded, result))

    os.environ["DESTINATION__POSTGRES__CREDENTIALS"] = settings.database_url
    pipeline = dlt.pipeline(
        pipeline_name="maida_glosas",
        destination="postgres",
        dataset_name=settings.postgres_schema,
    )
    pipeline.run(
        [
            _glosa_resource(records),
            _document_resource(downloads),
            _lot_resource(results),
            _control_resource(results, records),
        ]
    )
    return MaidaExtractionSummary(
        competencies=competencies,
        documents=len(downloads),
        lots=sum(len(result.lots) for result in results),
        glosa_records=len(records),
    )


def _parse_glosa_records(
    downloaded: DownloadedMaidaDocument,
) -> list[dict[str, Any]]:
    try:
        return list(parse_maida_xlsx(downloaded))
    except MaidaSpreadsheetError:
        logger.warning(
            "Demonstrativo Maida sem colunas de glosa; arquivo ignorado: %s",
            downloaded.document.file_name,
        )
        return []


def _enrich_glosa_records(
    records: list[dict[str, Any]],
    downloaded: DownloadedMaidaDocument,
    result: MaidaCompetencyResult,
) -> list[dict[str, Any]]:
    """Completa a carteira usando a guia elegivel do mesmo lote Maida."""
    by_lot_id: dict[tuple[str, str], dict[str, Any]] = {}
    by_lot_number: dict[tuple[str, str], dict[str, Any]] = {}
    for guide in result.guides:
        guide_number = _normalize_key(guide.get("numero_guia"))
        if not guide_number:
            continue
        lot_id = _normalize_key(guide.get("lote_id"))
        lot_number = _normalize_key(guide.get("identificador_lote"))
        if lot_id:
            by_lot_id[(lot_id, guide_number)] = guide
        if lot_number:
            by_lot_number[(lot_number, guide_number)] = guide

    document_lot_id = _normalize_key(downloaded.document.lot_id)
    for record in records:
        guide_number = _normalize_key(record.get("numero_guia_senha"))
        lot_number = _normalize_key(record.get("numero_lote"))
        guide = by_lot_id.get((document_lot_id, guide_number))
        if guide is None:
            guide = by_lot_number.get((lot_number, guide_number))
        if guide is not None and guide.get("numero_carteira"):
            record["codigo_beneficiario"] = guide.get("numero_carteira")
    return records


def _normalize_key(value: object) -> str:
    return str(value or "").strip().upper()


def select_scheduled_competencies(
    settings: MaidaSettings,
    today: date,
) -> tuple[MaidaCompetency, ...]:
    current = MaidaCompetency(today.year, today.month)
    all_competencies = tuple(_competency_range(START_COMPETENCY, current))
    attempted = _attempted_competencies(settings)
    rolling = set(all_competencies[-3:])
    return tuple(
        item for item in all_competencies if item in rolling or item not in attempted
    )


def _attempted_competencies(settings: MaidaSettings) -> set[MaidaCompetency]:
    with psycopg2.connect(settings.database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.tables
                     WHERE table_schema = %s AND table_name = %s
                )
                """,
                (settings.postgres_schema, CONTROL_TABLE),
            )
            if not cursor.fetchone()[0]:
                return set()
            cursor.execute(
                f'SELECT competencia FROM "{settings.postgres_schema}".'
                f'"{CONTROL_TABLE}"'
            )
            values = cursor.fetchall()
    return {
        MaidaCompetency(value.year, value.month)
        for (value,) in values
        if isinstance(value, (date, datetime))
    }


def _competency_range(
    start: MaidaCompetency,
    end: MaidaCompetency,
) -> Iterable[MaidaCompetency]:
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        yield MaidaCompetency(year, month)
        if month == 12:
            year, month = year + 1, 1
        else:
            month += 1


def _glosa_resource(records: list[dict[str, Any]]):
    columns = _column_hints()
    columns.update(
        {
            "origem_maida": {"data_type": "text", "nullable": True},
            "documento_maida_id": {"data_type": "text", "nullable": True},
            "arquivo_maida": {"data_type": "text", "nullable": True},
            "lote_maida_id": {"data_type": "text", "nullable": True},
            "tipo_lote_maida": {"data_type": "text", "nullable": True},
            "data_processamento_maida": {"data_type": "date", "nullable": True},
            "data_envio_recurso_maida": {"data_type": "date", "nullable": True},
            "aba_maida": {"data_type": "text", "nullable": True},
            "linha_maida": {"data_type": "bigint", "nullable": True},
        }
    )
    return dlt.resource(
        records,
        name=TARGET_TABLE,
        primary_key="id_registro",
        write_disposition="merge",
        columns=columns,
    )


def _document_resource(downloads: list[DownloadedMaidaDocument]):
    records = []
    for download in downloads:
        item = asdict(download.document)
        item["competency"] = download.document.competency.first_day
        item["local_path"] = str(download.path)
        records.append(item)
    return dlt.resource(
        records,
        name=DOCUMENT_TABLE,
        primary_key="document_id",
        write_disposition="merge",
    )


def _lot_resource(results: list[MaidaCompetencyResult]):
    records = []
    for result in results:
        for lot in result.lots:
            item = dict(lot)
            item["competencia"] = result.competency.first_day
            item["chave_maida"] = (
                f"{item.get('lote_id')}:{item.get('tipo_lote')}:{item.get('origem')}"
            )
            records.append(item)
    return dlt.resource(
        records,
        name=LOT_TABLE,
        primary_key="chave_maida",
        write_disposition="merge",
    )


def _control_resource(
    results: list[MaidaCompetencyResult],
    records: list[dict[str, Any]],
):
    counts: dict[date, int] = {}
    for record in records:
        reference = record["referencia"]
        counts[reference] = counts.get(reference, 0) + 1
    now = datetime.now().astimezone()
    control = [
        {
            "competencia": result.competency.first_day,
            "tentado_em": now,
            "documentos_resumo": len(result.summary_documents),
            "documentos_lotes": len(result.lot_documents),
            "documentos_processados": len(result.selected_documents),
            "itens_glosa": counts.get(result.competency.first_day, 0),
        }
        for result in results
    ]
    return dlt.resource(
        control,
        name=CONTROL_TABLE,
        primary_key="competencia",
        write_disposition="merge",
    )
