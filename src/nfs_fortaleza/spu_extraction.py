from __future__ import annotations

import hashlib
import logging
import os
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

import dlt
import psycopg2
from psycopg2 import sql

from nfs_fortaleza.spu_config import SpuSettings
from nfs_fortaleza.spu_pdf import (
    parse_nuexo_documents,
    parse_saude_cogestao_documents,
    parse_tramitando_report_documents,
)
from nfs_fortaleza.spu_portal import (
    PROCESS_NUMBER_PATTERN,
    SpuDocument,
    SpuMaterializedTreeUnavailable,
    SpuPortalClient,
    SpuPortalError,
    SpuProcessSummary,
    SpuSessionExpiredError,
    _clean_reason,
    canonical_spu_sector,
)
from nfs_fortaleza.spu_resources import (
    PROCESS_TABLE_NAME,
    TRAMITANDO_REPORT_TABLE_NAME,
    spu_resources,
    tramitando_report_resource,
)


LOGGER = logging.getLogger(__name__)
INCREMENTAL_KNOWN_PAGE_STREAK = 2


class SpuExtractionConfigurationError(ValueError):
    """Raised when dag_run.conf contains invalid SPU extraction options."""


class SpuBatchExtractionError(RuntimeError):
    """Raised after successful processes are loaded and some processes fail."""


@dataclass(frozen=True)
class SpuExtractionPayload:
    numero_processos: tuple[str, ...] = ()
    max_pages: int | None = None
    varredura_completa: bool = False

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any] | None,
    ) -> SpuExtractionPayload:
        payload = value or {}
        raw_numbers = payload.get("numero_processos")
        if raw_numbers is None:
            numbers: tuple[str, ...] = ()
        else:
            if not isinstance(raw_numbers, (list, tuple)):
                raise SpuExtractionConfigurationError(
                    "numero_processos deve ser uma lista."
                )
            numbers = tuple(
                sorted(
                    {
                        _normalize_process_number(str(item))
                        for item in raw_numbers
                    }
                )
            )
            if not numbers:
                raise SpuExtractionConfigurationError(
                    "numero_processos deve conter ao menos um processo."
                )

        raw_max_pages = payload.get("max_pages")
        max_pages: int | None = None
        if raw_max_pages is not None:
            if isinstance(raw_max_pages, bool):
                raise SpuExtractionConfigurationError(
                    "max_pages deve ser um inteiro maior que zero."
                )
            try:
                max_pages = int(raw_max_pages)
            except (TypeError, ValueError) as exc:
                raise SpuExtractionConfigurationError(
                    "max_pages deve ser um inteiro maior que zero."
                ) from exc
            if max_pages <= 0:
                raise SpuExtractionConfigurationError(
                    "max_pages deve ser um inteiro maior que zero."
                )
        full_scan = payload.get("varredura_completa", False)
        if not isinstance(full_scan, bool):
            raise SpuExtractionConfigurationError(
                "varredura_completa deve ser true ou false."
            )
        return cls(
            numero_processos=numbers,
            max_pages=max_pages,
            varredura_completa=full_scan,
        )


@dataclass(frozen=True)
class LoadedSpuProcess:
    status_processo: str
    detalhes_finalizados_extraidos: bool


@dataclass(frozen=True)
class SpuExtractionSummary:
    processos_encontrados: tuple[str, ...]
    processos_ja_carregados: tuple[str, ...]
    processos_processados: tuple[str, ...]
    processos_com_erro: tuple[str, ...]
    arquivos: tuple[Path, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "processos_encontrados": list(self.processos_encontrados),
            "processos_ja_carregados": list(self.processos_ja_carregados),
            "processos_processados": list(self.processos_processados),
            "processos_com_erro": list(self.processos_com_erro),
            "arquivos": [str(path) for path in self.arquivos],
        }


@dataclass(frozen=True)
class SpuProcessLoadSummary:
    processos_encontrados: tuple[str, ...]
    processos_ja_carregados: tuple[str, ...]
    processos_processados: tuple[str, ...]
    finalizados_para_pdf: tuple[str, ...]
    paginas_processadas: int
    lotes_carregados: int

    def as_dict(self) -> dict[str, object]:
        return {
            "processos_encontrados": list(self.processos_encontrados),
            "processos_ja_carregados": list(self.processos_ja_carregados),
            "processos_processados": list(self.processos_processados),
            "finalizados_para_pdf": list(self.finalizados_para_pdf),
            "paginas_processadas": self.paginas_processadas,
            "lotes_carregados": self.lotes_carregados,
        }


def extract_and_load_spu_processes(
    settings: SpuSettings,
    payload: SpuExtractionPayload,
    *,
    downloads_dir: Path,
) -> SpuExtractionSummary:
    """Compatibility wrapper that executes the two extraction stages."""
    process_summary = load_spu_process_batches(
        settings,
        payload,
        downloads_dir=downloads_dir,
    )
    pdf_summary = extract_and_load_spu_pdfs(
        settings,
        process_summary.finalizados_para_pdf,
        downloads_dir=downloads_dir,
    )
    return SpuExtractionSummary(
        processos_encontrados=process_summary.processos_encontrados,
        processos_ja_carregados=process_summary.processos_ja_carregados,
        processos_processados=process_summary.processos_processados,
        processos_com_erro=pdf_summary.processos_com_erro,
        arquivos=pdf_summary.arquivos,
    )


def load_spu_process_batches(
    settings: SpuSettings,
    payload: SpuExtractionPayload,
    *,
    downloads_dir: Path,
) -> SpuProcessLoadSummary:
    """Persist process metadata in dlt batches while walking SPU pages."""
    loaded = list_loaded_spu_processes(settings)
    pipeline = _spu_pipeline(settings)
    process_buffer: list[dict[str, Any]] = []
    status_buffer: list[dict[str, Any]] = []
    cogestao_buffer: list[dict[str, Any]] = []
    found: set[str] = set()
    seen: set[str] = set()
    skipped: list[str] = []
    processed: list[str] = []
    finalizados: list[str] = []
    pages_processed = 0
    batches_loaded = 0
    load_failed = False
    requested = set(payload.numero_processos)
    consecutive_known_pages = 0

    def flush() -> None:
        nonlocal batches_loaded, load_failed
        if not process_buffer:
            return
        batch_numbers = [str(row["numero_processo"]) for row in process_buffer]
        LOGGER.info(
            "SPU carregando lote %s com %s processo(s): %s ... %s",
            batches_loaded + 1,
            len(batch_numbers),
            batch_numbers[0],
            batch_numbers[-1],
        )
        try:
            _run_spu_pipeline(
                pipeline,
                processes=process_buffer,
                statuses=status_buffer,
                cogestao_rows=cogestao_buffer,
            )
        except Exception:
            load_failed = True
            raise
        processed.extend(batch_numbers)
        process_buffer.clear()
        status_buffer.clear()
        cogestao_buffer.clear()
        batches_loaded += 1
        LOGGER.info(
            "SPU lote %s carregado; %s processo(s) persistidos no total.",
            batches_loaded,
            len(processed),
        )

    try:
        with SpuPortalClient(settings, downloads_dir=downloads_dir) as client:
            for page_number, page_processes in client.iter_process_pages(
                max_pages=payload.max_pages
            ):
                pages_processed = page_number
                page_has_candidate = False
                for process in page_processes:
                    number = process.numero_processo
                    found.add(number)
                    if number in seen or (requested and number not in requested):
                        continue
                    seen.add(number)

                    candidates, already_loaded = select_new_spu_processes(
                        (process,), loaded
                    )
                    if already_loaded:
                        skipped.append(number)
                        continue

                    candidate = candidates[0]
                    page_has_candidate = True
                    observed_at = datetime.now(timezone.utc)
                    process_buffer.append(
                        _process_row(
                            candidate,
                            observed_at,
                            detalhes_finalizados_extraidos=False,
                        )
                    )
                    status_buffer.append(_status_row(candidate, observed_at))
                    loaded[number] = LoadedSpuProcess(
                        candidate.status_processo,
                        False,
                    )
                    if candidate.finalizado:
                        finalizados.append(number)

                    if len(process_buffer) >= settings.process_batch_size:
                        flush()

                LOGGER.info(
                    "SPU progresso: pagina=%s, encontrados=%s, novos=%s, "
                    "ja_carregados=%s, pendentes_no_lote=%s.",
                    page_number,
                    len(found),
                    len(processed) + len(process_buffer),
                    len(skipped),
                    len(process_buffer),
                )
                if requested and requested.issubset(found):
                    LOGGER.info(
                        "Todos os %s processo(s) solicitados foram encontrados; "
                        "encerrando a paginacao.",
                        len(requested),
                    )
                    break
                if requested or payload.varredura_completa:
                    continue

                consecutive_known_pages = (
                    0
                    if page_has_candidate
                    else consecutive_known_pages + 1
                )
                if consecutive_known_pages >= INCREMENTAL_KNOWN_PAGE_STREAK:
                    LOGGER.info(
                        "SPU incremental: %s paginas consecutivas contem "
                        "somente processos ja carregados; encerrando a "
                        "paginacao na pagina %s.",
                        consecutive_known_pages,
                        page_number,
                    )
                    break
    finally:
        if not load_failed:
            flush()

    if requested:
        missing = sorted(requested - found)
        if missing:
            raise SpuExtractionConfigurationError(
                "Processos nao encontrados nas paginas consultadas: "
                + ", ".join(missing)
                + "."
            )

    summary = SpuProcessLoadSummary(
        processos_encontrados=tuple(
            sorted(found if not requested else found & requested)
        ),
        processos_ja_carregados=tuple(skipped),
        processos_processados=tuple(processed),
        finalizados_para_pdf=tuple(finalizados),
        paginas_processadas=pages_processed,
        lotes_carregados=batches_loaded,
    )
    LOGGER.info("SPU etapa de processos concluida: %s", summary.as_dict())
    return summary


def extract_and_load_spu_pdfs(
    settings: SpuSettings,
    process_numbers: tuple[str, ...],
    *,
    downloads_dir: Path,
) -> SpuExtractionSummary:
    """Process incomplete finalized records and persist PDF results in batches."""
    processes = list_spu_processes_for_pdf(settings, process_numbers)
    if not processes:
        LOGGER.info("SPU PDFs: nenhum processo finalizado pendente.")
        return SpuExtractionSummary((), (), (), (), ())

    pipeline = _spu_pipeline(settings)
    process_buffer: list[dict[str, Any]] = []
    cogestao_buffer: list[dict[str, Any]] = []
    empenho_buffer: list[dict[str, Any]] = []
    nota_fiscal_buffer: list[dict[str, Any]] = []
    downloaded_files: list[Path] = []
    processed: list[str] = []
    failures: list[tuple[str, str]] = []
    successful_in_buffer = 0
    batches_loaded = 0
    load_failed = False

    def flush() -> None:
        nonlocal successful_in_buffer, batches_loaded, load_failed
        if not process_buffer:
            return
        batch_numbers = [str(row["numero_processo"]) for row in process_buffer]
        LOGGER.info(
            "SPU PDFs: carregando lote %s com %s processo(s), %s linha(s) "
            "SAUDECOGESTAO, %s EMPENHO(s) e %s NOTA(s) FISCAL(is).",
            batches_loaded + 1,
            len(batch_numbers),
            len(cogestao_buffer),
            len(empenho_buffer),
            len(nota_fiscal_buffer),
        )
        try:
            _run_spu_pipeline(
                pipeline,
                processes=process_buffer,
                cogestao_rows=cogestao_buffer,
                empenho_rows=empenho_buffer,
                nota_fiscal_rows=nota_fiscal_buffer,
            )
        except Exception:
            load_failed = True
            raise
        processed.extend(batch_numbers)
        process_buffer.clear()
        cogestao_buffer.clear()
        empenho_buffer.clear()
        nota_fiscal_buffer.clear()
        successful_in_buffer = 0
        batches_loaded += 1
        LOGGER.info(
            "SPU PDFs: lote %s carregado; %s processo(s) concluidos no total.",
            batches_loaded,
            len(processed),
        )

    def mark_without_materialized_view(
        process: SpuProcessSummary,
        message: str,
    ) -> None:
        nonlocal successful_in_buffer
        process_buffer.append(
            _process_row(
                process,
                datetime.now(timezone.utc),
                detalhes_finalizados_extraidos=True,
            )
        )
        successful_in_buffer += 1
        LOGGER.info(
            "SPU PDFs: %s concluido sem documentos; %s",
            process.numero_processo,
            message,
        )
        if successful_in_buffer >= settings.pdf_batch_size:
            flush()

    try:
        with SpuPortalClient(settings, downloads_dir=downloads_dir) as client:
            for index, process in enumerate(processes, start=1):
                LOGGER.info(
                    "SPU PDFs: processando %s (%s/%s).",
                    process.numero_processo,
                    index,
                    len(processes),
                )
                if not process.url_visualizacao:
                    mark_without_materialized_view(
                        process,
                        "o processo finalizado nao possui o botao "
                        "VISUALIZAR PROCESSO.",
                    )
                    continue
                try:
                    documents = client.download_process_documents(process)
                    cogestao_documents = tuple(
                        document
                        for document in documents
                        if canonical_spu_sector(document.setor)
                        == "ipm/saudecogestao"
                    )
                    nuexo_documents = tuple(
                        document
                        for document in documents
                        if canonical_spu_sector(document.setor) == "ipm/nuexo"
                    )
                    process_cogestao = (
                        parse_saude_cogestao_documents(cogestao_documents)
                        if cogestao_documents
                        else []
                    )
                    process_nuexo = (
                        parse_nuexo_documents(nuexo_documents)
                        if nuexo_documents
                        else []
                    )
                    process_empenhos, process_notas_fiscais = _split_nuexo_rows(
                        process_nuexo
                    )
                    process_buffer.append(
                        _process_row(
                            process,
                            datetime.now(timezone.utc),
                            detalhes_finalizados_extraidos=True,
                        )
                    )
                    cogestao_buffer.extend(process_cogestao)
                    empenho_buffer.extend(process_empenhos)
                    nota_fiscal_buffer.extend(process_notas_fiscais)
                    downloaded_files.extend(document.path for document in documents)
                    successful_in_buffer += 1
                    LOGGER.info(
                        "SPU PDFs: %s extraido (%s documento(s), %s linha(s) "
                        "SAUDECOGESTAO, %s EMPENHO(s), %s NOTA(s) FISCAL(is)).",
                        process.numero_processo,
                        len(documents),
                        len(process_cogestao),
                        len(process_empenhos),
                        len(process_notas_fiscais),
                    )
                    if successful_in_buffer >= settings.pdf_batch_size:
                        flush()
                except SpuMaterializedTreeUnavailable as exc:
                    mark_without_materialized_view(process, str(exc))
                except SpuSessionExpiredError:
                    raise
                except Exception as exc:
                    LOGGER.exception(
                        "SPU PDFs: falha no processo %s.",
                        process.numero_processo,
                    )
                    failures.append((process.numero_processo, str(exc)))
    finally:
        if not load_failed:
            flush()

    summary = SpuExtractionSummary(
        processos_encontrados=tuple(item.numero_processo for item in processes),
        processos_ja_carregados=(),
        processos_processados=tuple(processed),
        processos_com_erro=tuple(number for number, _ in failures),
        arquivos=tuple(downloaded_files),
    )
    if failures:
        details = "; ".join(f"{number}: {message}" for number, message in failures)
        if not processed:
            raise SpuBatchExtractionError(
                "Todos os processos da etapa de PDFs falharam: " + details
            )
        LOGGER.warning(
            "SPU PDFs: %s processo(s) ficaram pendentes e serao tentados "
            "novamente na proxima execucao: %s",
            len(failures),
            details,
        )
    LOGGER.info("SPU etapa de PDFs concluida: %s", summary.as_dict())
    return summary


def extract_and_load_tramitando_reports(
    settings: SpuSettings,
    process_numbers: tuple[str, ...] = (),
    *,
    downloads_dir: Path,
) -> SpuExtractionSummary:
    """Extract only new RELATORIO_<remessa>_<id>.pdf files since 2025."""
    processes = list_tramitando_processes_for_report(settings, process_numbers)
    if not processes:
        LOGGER.info("SPU relatórios em tramitação: nenhum processo elegível.")
        return SpuExtractionSummary((), (), (), (), ())

    loaded_ids = list_loaded_tramitando_report_document_ids(settings)
    pipeline = _spu_pipeline(settings)
    processed: list[str] = []
    already_loaded: list[str] = []
    failures: list[tuple[str, str]] = []
    downloaded_files: list[Path] = []
    eligible = {process.numero_processo: process for process in processes}
    found: set[str] = set()
    with SpuPortalClient(settings, downloads_dir=downloads_dir) as client:
        for _page_number, page_processes in client.iter_process_pages():
            for listed_process in page_processes:
                process = eligible.get(listed_process.numero_processo)
                if process is None:
                    continue
                found.add(process.numero_processo)
                index = len(found)
                LOGGER.info(
                    "SPU relatórios em tramitação: processando %s (%s/%s).",
                    process.numero_processo,
                    index,
                    len(processes),
                )
                try:
                    documents = client.download_tramitando_report_documents(
                        process,
                        loaded_document_ids=loaded_ids,
                    )
                    if not documents:
                        already_loaded.append(process.numero_processo)
                        continue
                    rows: list[dict[str, Any]] = []
                    documentos_com_erro: list[str] = []
                    for document in documents:
                        try:
                            rows.extend(
                                parse_tramitando_report_documents([document])
                            )
                        except SpuPortalError:
                            LOGGER.exception(
                                "SPU relatórios em tramitação: documento %s "
                                "não pôde ser interpretado.",
                                document.nome,
                            )
                            documentos_com_erro.append(document.nome)
                    if not rows:
                        raise SpuPortalError(
                            "Nenhum documento do processo contém registros "
                            "interpretáveis."
                        )
                    _enrich_tramitando_report_rows(settings, rows)
                    linhas_sem_competencia = sum(
                        row.get("competencia") is None for row in rows
                    )
                    rows = [
                        row for row in rows if row.get("competencia") is not None
                    ]
                    if linhas_sem_competencia:
                        LOGGER.warning(
                            "SPU relatórios em tramitação: %s registro(s) "
                            "sem competência após o enriquecimento foram "
                            "mantidos pendentes.",
                            linhas_sem_competencia,
                        )
                    if not rows:
                        raise SpuPortalError(
                            "Os documentos não produziram registros com "
                            "competência válida."
                        )
                    pipeline.run(tramitando_report_resource(rows))
                    processed.append(process.numero_processo)
                    downloaded_files.extend(
                        document.path for document in documents
                    )
                    loaded_ids.update(
                        document.document_id for document in documents
                    )
                    LOGGER.info(
                        "SPU relatórios em tramitação: %s carregado com %s "
                        "documento(s) e %s registro(s).",
                        process.numero_processo,
                        len(documents),
                        len(rows),
                    )
                    if documentos_com_erro:
                        LOGGER.warning(
                            "SPU relatórios em tramitação: %s documento(s) "
                            "do processo permaneceram pendentes.",
                            len(documentos_com_erro),
                        )
                except SpuSessionExpiredError:
                    raise
                except Exception as exc:
                    LOGGER.exception(
                        "SPU relatórios em tramitação: falha no processo %s.",
                        process.numero_processo,
                    )
                    failures.append((process.numero_processo, str(exc)))
            if found == set(eligible):
                break

    missing = set(eligible) - found
    failures.extend(
        (number, "Processo nao encontrado nas paginas atuais do SPU.")
        for number in sorted(missing)
    )

    summary = SpuExtractionSummary(
        processos_encontrados=tuple(item.numero_processo for item in processes),
        processos_ja_carregados=tuple(already_loaded),
        processos_processados=tuple(processed),
        processos_com_erro=tuple(number for number, _ in failures),
        arquivos=tuple(downloaded_files),
    )
    if failures:
        details = "; ".join(f"{number}: {message}" for number, message in failures)
        if not processed:
            raise SpuBatchExtractionError(
                "Todos os relatórios em tramitação falharam: " + details
            )
        LOGGER.warning(
            "%s processo(s) com relatórios em tramitação serão tentados "
            "novamente: %s",
            len(failures),
            details,
        )
    return summary


def _enrich_tramitando_report_rows(
    settings: SpuSettings,
    rows: list[dict[str, Any]],
) -> None:
    """Correct identifiers affected by MV PDF cmap using the Oracle stage."""
    if not rows:
        return
    staging_schema = os.getenv(
        "GLOSAS_IPM_STAGING_SCHEMA", "api_prontocardio_staging"
    )
    remessas = sorted({int(row["cd_remessa"]) for row in rows})
    with psycopg2.connect(settings.database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.tables
                     WHERE table_schema = %s AND table_name = 'ipm_itens_oracle'
                )
                """,
                (staging_schema,),
            )
            if not bool(cursor.fetchone()[0]):
                LOGGER.warning(
                    "SPU relatórios em tramitação: tabela %s.ipm_itens_oracle "
                    "ausente; mantendo identificadores extraídos do PDF.",
                    staging_schema,
                )
                return
            query = sql.SQL(
                "SELECT DISTINCT cd_remessa, cd_reg, cd_atendimento, "
                "nm_paciente, nr_guia, "
                "date_trunc('month', dt_competencia)::date, vl_total_conta "
                "FROM {}.ipm_itens_oracle WHERE cd_remessa = ANY(%s)"
            ).format(sql.Identifier(staging_schema))
            cursor.execute(query, (remessas,))
            candidates = cursor.fetchall()

    for row in rows:
        same_remessa = [
            candidate
            for candidate in candidates
            if int(candidate[0]) == int(row["cd_remessa"])
        ]
        competencia_remessa = _competencia_unica_remessa(same_remessa)
        if row.get("competencia") is None and competencia_remessa is not None:
            row["competencia"] = competencia_remessa
        same_remessa_value = [
            candidate
            for candidate in same_remessa
            if candidate[6] is not None
            and Decimal(candidate[6]) == Decimal(row["valor"])
        ]
        patient_key = _match_key(str(row.get("nome_paciente") or ""))
        same_patient = [
            candidate
            for candidate in same_remessa_value
            if _match_key(str(candidate[3] or "")) == patient_key
        ]
        matches = same_patient or same_remessa_value
        unique = {
            tuple(candidate[index] for index in range(1, 7))
            for candidate in matches
        }
        if len(unique) != 1:
            continue
        cd_reg, atendimento, paciente, guia, competencia, _valor = unique.pop()
        row.update(
            numero_conta=str(cd_reg) if cd_reg is not None else None,
            cd_atendimento=int(atendimento) if atendimento is not None else None,
            nome_paciente=str(paciente) if paciente else row["nome_paciente"],
            numero_guia=str(guia) if guia is not None else None,
            competencia=competencia or row["competencia"],
        )


def _match_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    without_marks = "".join(
        char for char in normalized if not unicodedata.combining(char)
    )
    return re.sub(r"\s+", " ", without_marks).strip().upper()


def _competencia_unica_remessa(candidates: list[tuple[Any, ...]]):
    competencias = {
        candidate[5] for candidate in candidates if candidate[5] is not None
    }
    return next(iter(competencias)) if len(competencias) == 1 else None


def _spu_pipeline(settings: SpuSettings):
    os.environ["DESTINATION__POSTGRES__CREDENTIALS"] = settings.database_url
    return dlt.pipeline(
        pipeline_name="spu_virtual_fortaleza",
        destination="postgres",
        dataset_name=settings.postgres_schema,
    )


def _run_spu_pipeline(
    pipeline: Any,
    *,
    processes: list[dict[str, Any]],
    statuses: list[dict[str, Any]] | None = None,
    cogestao_rows: list[dict[str, Any]] | None = None,
    empenho_rows: list[dict[str, Any]] | None = None,
    nota_fiscal_rows: list[dict[str, Any]] | None = None,
) -> None:
    pipeline.run(
        spu_resources(
            processes,
            statuses or [],
            cogestao_rows or [],
            empenho_rows or [],
            nota_fiscal_rows or [],
        )
    )


def _split_nuexo_rows(
    rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    common_fields = (
        "id_registro",
        "numero_processo",
        "documento_id",
        "documento_nome",
    )
    empenhos: list[dict[str, Any]] = []
    notas_fiscais: list[dict[str, Any]] = []
    for row in rows:
        document_type = str(row.get("tipo_documento") or "").upper()
        common = {field: row.get(field) for field in common_fields}
        if document_type == "EMPENHO":
            empenhos.append(
                common
                | {
                    "banco": row.get("banco"),
                    "codigo_conta": row.get("codigo_conta"),
                    "codigo_agencia": row.get("codigo_agencia"),
                    "conta": row.get("conta"),
                }
            )
        elif document_type == "NFS_E":
            notas_fiscais.append(
                common
                | {
                    "numero_nfse": row.get("numero_nfse"),
                    "chave_acesso_nfse": row.get("chave_acesso_nfse"),
                    "cnpj_cpf_nif_prestador": row.get(
                        "cnpj_cpf_nif_prestador"
                    ),
                }
            )
        else:
            raise SpuExtractionConfigurationError(
                f"Tipo de documento NUEXO nao reconhecido: {document_type!r}."
            )
    return empenhos, notas_fiscais


def list_loaded_spu_processes(
    settings: SpuSettings,
) -> dict[str, LoadedSpuProcess]:
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
                (settings.postgres_schema, PROCESS_TABLE_NAME),
            )
            if not bool(cursor.fetchone()[0]):
                return {}

            query = sql.SQL(
                "SELECT numero_processo, status_processo, "
                "detalhes_finalizados_extraidos FROM {}.{}"
            ).format(
                sql.Identifier(settings.postgres_schema),
                sql.Identifier(PROCESS_TABLE_NAME),
            )
            cursor.execute(query)
            return {
                str(number): LoadedSpuProcess(
                    status_processo=str(status),
                    detalhes_finalizados_extraidos=bool(details),
                )
                for number, status, details in cursor.fetchall()
            }


def list_spu_processes_for_pdf(
    settings: SpuSettings,
    process_numbers: tuple[str, ...],
) -> tuple[SpuProcessSummary, ...]:
    if not process_numbers:
        return ()
    with psycopg2.connect(settings.database_url) as connection:
        with connection.cursor() as cursor:
            query = sql.SQL(
                "SELECT numero_processo, status_processo, "
                "tipo_processo_assunto, data_abertura, motivo_finalizacao, "
                "url_visualizacao FROM {}.{} "
                "WHERE numero_processo = ANY(%s) "
                "AND UPPER(status_processo) = 'FINALIZADO' "
                "AND NOT detalhes_finalizados_extraidos "
                "ORDER BY numero_processo"
            ).format(
                sql.Identifier(settings.postgres_schema),
                sql.Identifier(PROCESS_TABLE_NAME),
            )
            cursor.execute(query, (list(process_numbers),))
            return tuple(
                SpuProcessSummary(
                    numero_processo=str(number),
                    status_processo=str(status),
                    tipo_processo_assunto=(
                        str(process_type) if process_type else None
                    ),
                    data_abertura=opened_at,
                    motivo_finalizacao=(
                        _clean_reason(str(reason)) if reason else None
                    ),
                    url_visualizacao=str(url) if url else None,
                )
                for number, status, process_type, opened_at, reason, url
                in cursor.fetchall()
            )


def list_tramitando_processes_for_report(
    settings: SpuSettings,
    process_numbers: tuple[str, ...] = (),
) -> tuple[SpuProcessSummary, ...]:
    with psycopg2.connect(settings.database_url) as connection:
        with connection.cursor() as cursor:
            filters = [
                "UPPER(BTRIM(status_processo)) = 'TRAMITANDO'",
                "split_part(numero_processo, '/', 2) ~ '^[0-9]{{4}}$'",
                "split_part(numero_processo, '/', 2)::integer >= 2025",
            ]
            parameters: list[Any] = []
            if process_numbers:
                filters.append("numero_processo = ANY(%s)")
                parameters.append(list(process_numbers))
            query = sql.SQL(
                "SELECT numero_processo, status_processo, "
                "tipo_processo_assunto, data_abertura, motivo_finalizacao, "
                "url_visualizacao FROM {}.{} WHERE "
                + " AND ".join(filters)
                + " ORDER BY data_abertura DESC NULLS LAST, numero_processo"
            ).format(
                sql.Identifier(settings.postgres_schema),
                sql.Identifier(PROCESS_TABLE_NAME),
            )
            cursor.execute(query, parameters)
            return tuple(
                SpuProcessSummary(
                    numero_processo=str(number),
                    status_processo=str(status),
                    tipo_processo_assunto=str(process_type) if process_type else None,
                    data_abertura=opened_at,
                    motivo_finalizacao=_clean_reason(str(reason)) if reason else None,
                    url_visualizacao=str(url) if url else None,
                )
                for number, status, process_type, opened_at, reason, url
                in cursor.fetchall()
            )


def list_loaded_tramitando_report_document_ids(
    settings: SpuSettings,
) -> set[str]:
    with psycopg2.connect(settings.database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.tables
                     WHERE table_schema = %s AND table_name = %s
                )
                """,
                (settings.postgres_schema, TRAMITANDO_REPORT_TABLE_NAME),
            )
            if not bool(cursor.fetchone()[0]):
                return set()
            query = sql.SQL("SELECT DISTINCT documento_id FROM {}.{}").format(
                sql.Identifier(settings.postgres_schema),
                sql.Identifier(TRAMITANDO_REPORT_TABLE_NAME),
            )
            cursor.execute(query)
            return {str(row[0]) for row in cursor.fetchall() if row[0]}


def select_new_spu_processes(
    processes: tuple[SpuProcessSummary, ...],
    loaded: Mapping[str, LoadedSpuProcess],
) -> tuple[tuple[SpuProcessSummary, ...], tuple[SpuProcessSummary, ...]]:
    candidates: list[SpuProcessSummary] = []
    skipped: list[SpuProcessSummary] = []
    for process in processes:
        previous = loaded.get(process.numero_processo)
        should_extract = (
            previous is None
            or previous.status_processo.upper() != process.status_processo.upper()
            or (
                process.finalizado
                and not previous.detalhes_finalizados_extraidos
            )
        )
        (candidates if should_extract else skipped).append(process)
    return tuple(candidates), tuple(skipped)


def _filter_requested_processes(
    available: tuple[SpuProcessSummary, ...],
    requested: tuple[str, ...],
) -> tuple[SpuProcessSummary, ...]:
    if not requested:
        return available
    by_number = {process.numero_processo: process for process in available}
    missing = sorted(set(requested) - set(by_number))
    if missing:
        raise SpuExtractionConfigurationError(
            "Processos nao encontrados nas paginas consultadas: "
            + ", ".join(missing)
            + "."
        )
    return tuple(by_number[number] for number in requested)


def _process_row(
    process: SpuProcessSummary,
    observed_at: datetime,
    *,
    detalhes_finalizados_extraidos: bool | None = None,
) -> dict[str, Any]:
    return {
        "numero_processo": process.numero_processo,
        "status_processo": process.status_processo,
        "tipo_processo_assunto": process.tipo_processo_assunto,
        "data_abertura": process.data_abertura,
        "motivo_finalizacao": process.motivo_finalizacao,
        "url_visualizacao": process.url_visualizacao,
        "detalhes_finalizados_extraidos": (
            process.finalizado
            if detalhes_finalizados_extraidos is None
            else detalhes_finalizados_extraidos
        ),
        "extraido_em": observed_at,
    }


def _status_row(
    process: SpuProcessSummary,
    observed_at: datetime,
) -> dict[str, Any]:
    identifier = hashlib.sha256(
        f"{process.numero_processo}|{process.status_processo.upper()}".encode("utf-8")
    ).hexdigest()
    return {
        "id_status": identifier,
        "numero_processo": process.numero_processo,
        "status_processo": process.status_processo,
        "observado_em": observed_at,
    }


def _normalize_process_number(value: str) -> str:
    match = PROCESS_NUMBER_PATTERN.fullmatch(value.strip())
    if not match:
        raise SpuExtractionConfigurationError(
            f"Numero de processo invalido: {value!r}. Use PNNNNNN/AAAA."
        )
    return f"{match.group(1).upper()}/{match.group(2)}"
