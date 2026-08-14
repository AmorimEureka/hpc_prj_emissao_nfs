from __future__ import annotations

import hashlib
import json
import logging
import re
import unicodedata
from collections import Counter
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable

import pdfplumber

from nfs_fortaleza.spu_portal import SpuDocument, SpuPortalError


LOGGER = logging.getLogger(__name__)


TRAMITANDO_REPORT_FIELDS = {
    "cd_remessa": ("remessa",),
    "nome_paciente": ("nome do paciente", "nome paciente", "paciente"),
    "numero_guia": ("numero da guia", "numero guia", "guia"),
    "numero_conta": ("numero da conta", "numero conta", "conta"),
    "cd_atendimento": ("codigo atendimento", "cod atendimento", "atendimento"),
    "competencia": ("competencia",),
    "valor": ("valor",),
}


def parse_tramitando_report_documents(
    documents: Iterable[SpuDocument],
) -> list[dict[str, Any]]:
    """Parse the compact account reports attached to TRAMITANDO processes."""
    records: list[dict[str, Any]] = []
    for document in documents:
        records_from_layout = _parse_tramitando_report_layout(document)
        if records_from_layout:
            records.extend(records_from_layout)
            continue
        pages = _extract_pdf_pages(document.path)
        document_rows = parse_tramitando_report_pages(
            pages,
            numero_processo=document.numero_processo,
            documento_id=document.document_id,
            documento_nome=document.nome,
        )
        if not document_rows:
            raise SpuPortalError(
                "O relatório de processo em tramitação não contém os campos "
                f"esperados: {document.nome}."
            )
        records.extend(document_rows)
    return records


def _parse_tramitando_report_layout(
    document: SpuDocument,
) -> list[dict[str, Any]]:
    """Read MV's fixed-width report whose embedded font has a broken cmap."""
    records: list[dict[str, Any]] = []
    filename_remessa = _report_remessa_from_filename(document.nome)
    try:
        with pdfplumber.open(document.path) as pdf:
            for page_number, page in enumerate(pdf.pages, start=1):
                words = page.extract_words(x_tolerance=1, y_tolerance=2) or []
                if not words:
                    continue
                text = page.extract_text(x_tolerance=2, y_tolerance=3) or ""
                competencia = _parse_report_competencia(text)
                header_top = min(
                    (
                        float(word["top"])
                        for word in words
                        if 250 <= float(word["x0"]) < 360
                        and _is_report_atendimento_header(str(word["text"]))
                    ),
                    default=None,
                )
                if header_top is None:
                    continue
                total_top = min(
                    (
                        float(word["top"])
                        for word in words
                        if float(word["top"]) > header_top
                        and _fold(str(word["text"])).startswith("total")
                    ),
                    default=page.height,
                )
                data_words = [
                    word
                    for word in words
                    if header_top + 5 < float(word["top"]) < total_top
                ]
                for row in _cluster_words_by_top(data_words):
                    paciente = _words_between(row, 295, 442)
                    valor = _words_between(row, 530, float("inf"))
                    if not paciente or _parse_decimal(valor) is None:
                        continue
                    values = {
                        "cd_remessa": str(filename_remessa or ""),
                        "nome_paciente": paciente,
                        "numero_guia": _words_between(row, 105, 162),
                        "numero_conta": _words_between(row, 162, 263),
                        "cd_atendimento": _words_between(row, 263, 299),
                        "competencia": (
                            competencia.strftime("%m/%Y") if competencia else ""
                        ),
                        "valor": valor,
                    }
                    record = _tramitando_report_record(
                        values,
                        numero_processo=document.numero_processo,
                        documento_id=document.document_id,
                        documento_nome=document.nome,
                        pagina_pdf=page_number,
                        filename_remessa=filename_remessa,
                    )
                    if record is not None:
                        records.append(record)
    except Exception as exc:
        raise SpuPortalError(f"Falha ao ler o PDF {document.path.name!r}.") from exc
    return records


def _parse_report_competencia(text: str) -> date | None:
    match = re.search(
        r"comp\S*?\s*:?[r]?\s*(0[1-9]|1[0-2]).(\d{4})\b",
        _fold(text),
    )
    return date(int(match.group(2)), int(match.group(1)), 1) if match else None


def _is_report_atendimento_header(text: str) -> bool:
    folded = _fold(text)
    return folded.startswith(("at", "al")) and folded.endswith("nto")


def _cluster_words_by_top(
    words: list[dict[str, Any]],
    *,
    tolerance: float = 5.0,
) -> list[list[dict[str, Any]]]:
    rows: list[list[dict[str, Any]]] = []
    for word in sorted(words, key=lambda item: (float(item["top"]), item["x0"])):
        if not rows or abs(float(word["top"]) - float(rows[-1][0]["top"])) > tolerance:
            rows.append([word])
        else:
            rows[-1].append(word)
    return rows


def _words_between(
    words: list[dict[str, Any]],
    start: float,
    end: float,
) -> str:
    return " ".join(
        str(word["text"])
        for word in sorted(words, key=lambda item: float(item["x0"]))
        if start <= float(word["x0"]) < end
    ).strip()


def parse_tramitando_report_pages(
    pages: Iterable[tuple[int, list[list[list[str | None]]], str]],
    *,
    numero_processo: str,
    documento_id: str,
    documento_nome: str,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    filename_remessa = _report_remessa_from_filename(documento_nome)
    for page_number, tables, text in pages:
        page_records: list[dict[str, Any]] = []
        for table in tables:
            rows = [_normalize_table_row(row) for row in table]
            rows = [row for row in rows if any(row)]
            header = _find_tramitando_report_header(rows)
            if header is None:
                continue
            indexes, start = header
            for row in rows[start:]:
                if _tramitando_report_header_indexes(row) is not None:
                    continue
                values = {
                    field: row[index] if index < len(row) else ""
                    for field, index in indexes.items()
                }
                record = _tramitando_report_record(
                    values,
                    numero_processo=numero_processo,
                    documento_id=documento_id,
                    documento_nome=documento_nome,
                    pagina_pdf=page_number,
                    filename_remessa=filename_remessa,
                )
                if record is not None:
                    page_records.append(record)
        if not page_records:
            values = _tramitando_report_values_from_text(text)
            record = _tramitando_report_record(
                values,
                numero_processo=numero_processo,
                documento_id=documento_id,
                documento_nome=documento_nome,
                pagina_pdf=page_number,
                filename_remessa=filename_remessa,
            )
            if record is not None:
                page_records.append(record)
        records.extend(page_records)
    return records


def _find_tramitando_report_header(
    rows: list[list[str]],
) -> tuple[dict[str, int], int] | None:
    for index in range(min(5, len(rows))):
        for width in (1, 2):
            combined = [
                " ".join(
                    rows[row_index][column]
                    for row_index in range(index, min(index + width, len(rows)))
                    if column < len(rows[row_index])
                )
                for column in range(max(len(row) for row in rows[index:index + width]))
            ]
            indexes = _tramitando_report_header_indexes(combined)
            if indexes is not None:
                return indexes, index + width
    return None


def _tramitando_report_header_indexes(
    row: list[str],
) -> dict[str, int] | None:
    indexes: dict[str, int] = {}
    for index, raw_column in enumerate(row):
        column = _fold(_compact(raw_column))
        for field, aliases in TRAMITANDO_REPORT_FIELDS.items():
            if field not in indexes and any(alias == column for alias in aliases):
                indexes[field] = index
    required = set(TRAMITANDO_REPORT_FIELDS)
    return indexes if required.issubset(indexes) else None


def _tramitando_report_values_from_text(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    folded_lines = [line.strip() for line in text.splitlines() if line.strip()]
    for line in folded_lines:
        folded = _fold(line)
        for field, aliases in TRAMITANDO_REPORT_FIELDS.items():
            if field in values:
                continue
            for alias in aliases:
                match = re.match(
                    rf"^{re.escape(alias)}\s*[:\-]?\s*(.+)$",
                    folded,
                )
                if match:
                    prefix_length = len(line) - len(match.group(1))
                    values[field] = line[prefix_length:].strip(" :-–—")
                    break
    return values


def _tramitando_report_record(
    values: dict[str, str],
    *,
    numero_processo: str,
    documento_id: str,
    documento_nome: str,
    pagina_pdf: int,
    filename_remessa: int | None,
) -> dict[str, Any] | None:
    remessa_digits = _digits(values.get("cd_remessa", ""))
    cd_remessa = int(remessa_digits) if remessa_digits else filename_remessa
    paciente = _optional(values.get("nome_paciente"))
    competencia = _parse_competencia(values.get("competencia", ""))
    valor = _parse_decimal(values.get("valor", ""))
    if cd_remessa is None or paciente is None or valor is None:
        return None
    atendimento_digits = _digits(values.get("cd_atendimento", ""))
    record = {
        "numero_processo": numero_processo,
        "documento_id": documento_id,
        "documento_nome": documento_nome,
        "pagina_pdf": pagina_pdf,
        "cd_remessa": cd_remessa,
        "nome_paciente": paciente,
        "numero_guia": _optional(values.get("numero_guia")),
        "numero_conta": _optional(values.get("numero_conta")),
        "cd_atendimento": int(atendimento_digits) if atendimento_digits else None,
        "competencia": competencia,
        "valor": valor,
    }
    record["id_registro"] = hashlib.sha256(
        _fingerprint(record).encode("utf-8")
    ).hexdigest()
    record["extraido_em"] = datetime.now().astimezone()
    return record


def _report_remessa_from_filename(name: str) -> int | None:
    match = re.fullmatch(r"relatorio_(\d+)_\d+\.pdf", _fold(Path(name).name))
    return int(match.group(1)) if match else None


def _parse_competencia(value: str) -> date | None:
    match = re.search(r"\b(0?[1-9]|1[0-2])[/\-](\d{4})\b", value)
    if match:
        return date(int(match.group(2)), int(match.group(1)), 1)
    if not _optional(value):
        return None
    try:
        parsed = _parse_date(value)
    except SpuPortalError:
        return None
    return parsed.replace(day=1) if parsed else None


def parse_saude_cogestao_documents(
    documents: Iterable[SpuDocument],
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    occurrences: Counter[str] = Counter()
    files = list(documents)
    for document in files:
        pages = _extract_pdf_pages(document.path)
        document_records = parse_saude_cogestao_pages(
            pages,
            numero_processo=document.numero_processo,
            documento_id=document.document_id,
            documento_nome=document.nome,
        )
        if not document_records:
            document_records = parse_legacy_saude_cogestao_pages(
                pages,
                numero_processo=document.numero_processo,
                documento_id=document.document_id,
                documento_nome=document.nome,
            )
        for row in document_records:
            fingerprint = _fingerprint(row)
            occurrences[fingerprint] += 1
            row["id_registro"] = hashlib.sha256(
                f"{fingerprint}:{occurrences[fingerprint]}".encode("utf-8")
            ).hexdigest()
            records.append(row)
    if not files:
        raise SpuPortalError("Nenhum PDF encontrado em IPM/SAUDECOGESTAO.")
    if not records:
        LOGGER.warning(
            "Nenhuma linha reconhecida nos PDFs de IPM/SAUDECOGESTAO; "
            "o processo seguira sem linhas de cogestao."
        )
    return records


def parse_saude_cogestao_pages(
    pages: Iterable[tuple[int, list[list[list[str | None]]], str]],
    *,
    numero_processo: str,
    documento_id: str,
    documento_nome: str,
) -> list[dict[str, Any]]:
    summary: dict[str, str] | None = None
    protocol_rows: list[tuple[int, dict[str, str]]] = []
    protocol_indexes: dict[str, int] | None = None

    for page_number, tables, _text in pages:
        for table in tables:
            table_rows = [_normalize_table_row(row) for row in table]
            table_rows = [row for row in table_rows if any(row)]
            if not table_rows:
                continue
            if _is_cogestao_summary(table_rows):
                summary = _parse_cogestao_summary(table_rows)
                continue

            start = 0
            header = _find_protocol_header(table_rows)
            if header is not None:
                protocol_indexes, start = header
            if protocol_indexes is None:
                continue
            for row in table_rows[start:]:
                if _protocol_header_indexes(row) is not None:
                    continue
                values = _protocol_values(row, protocol_indexes)
                if values is None:
                    continue
                protocol_rows.append((page_number, values))

    if summary is None:
        return []
    records: list[dict[str, Any]] = []
    for page_number, protocol in protocol_rows:
        record: dict[str, Any] = {
            "cnpj": summary["cnpj"],
            "nome_prestador": summary["nome_prestador"],
            "numero_processo": summary["numero_processo"],
            "competencia_tms": summary["competencia_tms"],
            "competencia_producao": summary["competencia_producao"],
            "data_fechamento": summary["data_fechamento"],
            "valor_informado": summary["valor_informado"],
            "valor_aprovado_producao": summary["valor_aprovado_producao"],
            "valor_glosado_producao": summary["valor_glosado_producao"],
            "nr": protocol["nr"],
            "nr_origem": protocol.get("nr_origem"),
            "processo_recurso": summary.get("processo_recurso"),
            "valor_protocolo": protocol["valor_protocolo"],
            "valor_aprovado_protocolo": protocol[
                "valor_aprovado_protocolo"
            ],
            "valor_glosado_protocolo": protocol[
                "valor_glosado_protocolo"
            ],
        }
        record["cnpj"] = _digits(record["cnpj"]) or None
        record["numero_processo"] = (
            _normalize_process_number(record["numero_processo"])
            or numero_processo
        )
        record["data_fechamento"] = _parse_date(record["data_fechamento"])
        for field in (
            "valor_informado",
            "valor_aprovado_producao",
            "valor_glosado_producao",
            "valor_protocolo",
            "valor_aprovado_protocolo",
            "valor_glosado_protocolo",
        ):
            record[field] = _parse_decimal(record[field])
        for field in (
            "nome_prestador",
            "competencia_tms",
            "competencia_producao",
            "nr",
            "nr_origem",
            "processo_recurso",
        ):
            record[field] = _optional(record[field])
        record["processo_origem"] = numero_processo
        record["documento_id"] = documento_id
        record["documento_nome"] = documento_nome
        record["pagina_pdf"] = page_number
        records.append(record)
    return records


def parse_legacy_saude_cogestao_pages(
    pages: Iterable[tuple[int, list[list[list[str | None]]], str]],
    *,
    numero_processo: str,
    documento_id: str,
    documento_nome: str,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for page_number, _tables, text in pages:
        if "resumo fechamento do processo" not in _fold(text):
            continue
        provider_match = re.search(
            r"(?m)^\s*(\d{6,})\s+(\d{1,2}/\d{4})\s+"
            r"(\d{11,14})\s+(.+?)\s+\d{2}/\d{2}/\d{4}\s+"
            r"\d+\s+\d+\s+[0-9.,]+\s*$",
            text,
        )
        totals_match = re.search(
            r"(?im)^\s*TOTAL GERAL\s+\d+\s+([0-9.,]+)\s+"
            r"([0-9.,]+)\s+([0-9.,]+)\s*$",
            text,
        )
        closed_match = re.search(
            r"\bDATA\s*-\s*(\d{2}/\d{2}/\d{4})\b",
            text,
            flags=re.I,
        )
        if not provider_match or not totals_match or not closed_match:
            continue

        internal_process, production, provider_id, provider_name = (
            provider_match.groups()
        )
        informed = _parse_decimal(totals_match.group(1))
        approved = _parse_decimal(totals_match.group(3))
        glosa_match = re.search(
            r"\bGLOSA\s*:\s*R\$\s*([0-9.,]+)",
            text,
            flags=re.I,
        )
        glosado = (
            _parse_decimal(glosa_match.group(1))
            if glosa_match
            else informed - approved
            if informed is not None and approved is not None
            else None
        )
        cnpj = _digits(provider_id)
        if len(cnpj) == 13:
            cnpj = cnpj.zfill(14)
        records.append(
            {
                "cnpj": cnpj or None,
                "nome_prestador": _optional(provider_name),
                "numero_processo": numero_processo,
                "competencia_tms": _competencia_from_internal_process(
                    internal_process
                ),
                "competencia_producao": production,
                "data_fechamento": _parse_date(closed_match.group(1)),
                "valor_informado": informed,
                "valor_aprovado_producao": approved,
                "valor_glosado_producao": glosado,
                "nr": None,
                "nr_origem": None,
                "processo_recurso": None,
                "valor_protocolo": None,
                "valor_aprovado_protocolo": None,
                "valor_glosado_protocolo": None,
                "processo_origem": numero_processo,
                "documento_id": documento_id,
                "documento_nome": documento_nome,
                "pagina_pdf": page_number,
            }
        )
    return records


def parse_nuexo_documents(
    documents: Iterable[SpuDocument],
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    document_list = list(documents)
    for document in document_list:
        pages = _extract_pdf_pages(document.path)
        text = "\n".join(page_text for _, _, page_text in pages)
        records.extend(parse_nuexo_text(text, document=document))

    types = {record["tipo_documento"] for record in records}
    missing: list[str] = []
    if "EMPENHO" not in types:
        missing.append("EMPENHO")
    if "NFS_E" not in types:
        missing.append("NOTA FISCAL/NFS-E")
    if not document_list:
        raise SpuPortalError("Nenhum PDF encontrado em IPM/NUEXO.")
    if missing:
        LOGGER.warning(
            "Documentos nao reconhecidos em IPM/NUEXO: %s.",
            ", ".join(missing),
        )
    commitment_rows = [
        record for record in records if record["tipo_documento"] == "EMPENHO"
    ]
    invoice_rows = [
        record for record in records if record["tipo_documento"] == "NFS_E"
    ]
    incomplete: list[str] = []
    if not any(
        row.get("codigo_conta")
        and row.get("codigo_agencia")
        and row.get("conta")
        for row in commitment_rows
    ):
        incomplete.append("codigo/agencia/conta do EMPENHO")
    if not any(
        row.get("numero_nfse")
        and row.get("chave_acesso_nfse")
        and row.get("cnpj_cpf_nif_prestador")
        for row in invoice_rows
    ):
        incomplete.append("numero/chave/CNPJ da NFS-e")
    if incomplete:
        LOGGER.warning(
            "Campos incompletos em IPM/NUEXO: %s.",
            ", ".join(incomplete),
        )
    return records


def parse_nuexo_text(
    text: str,
    *,
    document: SpuDocument,
) -> list[dict[str, Any]]:
    folded_name = _fold(document.nome)
    folded_text = _fold(text)
    records: list[dict[str, Any]] = []

    is_commitment = "empenho" in folded_name or "banco/agencia" in folded_text
    if is_commitment:
        bank_agency = _find_value(
            text,
            (
                r"BANCO\s*/\s*AG[ÊE]NCIA",
                r"BANCO\s+E\s+AG[ÊE]NCIA",
            ),
        )
        account = _find_value(
            text,
            (r"CONTA\s+CORRENTE", r"\bCONTA\b"),
        )
        clean_bank_agency = _trim_at_label(bank_agency)
        bank = _find_bank(clean_bank_agency, text)
        codigo_conta, codigo_agencia = _split_bank_agency(
            clean_bank_agency,
        )
        records.append(
            _nuexo_record(
                document,
                tipo_documento="EMPENHO",
                banco=bank,
                banco_agencia=clean_bank_agency,
                codigo_conta=codigo_conta,
                codigo_agencia=codigo_agencia,
                conta=_trim_at_label(account),
            )
        )

    is_invoice = (
        "nfs" in folded_name
        or folded_name.startswith("nf_")
        or "nota fiscal" in folded_name
        or "nota fiscal" in folded_text
        or "nfs-e" in folded_text
        or "numero da nfs-e" in folded_text
        or "chave de acesso da nfs-e" in folded_text
    )
    if is_invoice:
        invoice_number = _find_invoice_number(text)
        access_key = _find_value(
            text,
            (
                r"CHAVE\s+DE\s+ACESSO\s+DA\s+NFS-?E",
                r"CHAVE\s+DE\s+ACESSO",
                r"C[ÓO]DIGO\s+DE\s+VERIFICA[ÇC][ÃA]O",
            ),
        )
        provider_id = _find_provider_id(text)
        records.append(
            _nuexo_record(
                document,
                tipo_documento="NFS_E",
                numero_nfse=_only_identifier(invoice_number),
                chave_acesso_nfse=_only_identifier(access_key),
                cnpj_cpf_nif_prestador=_only_identifier(provider_id),
            )
        )
    return records


def _extract_pdf_pages(
    path: Path,
) -> list[tuple[int, list[list[list[str | None]]], str]]:
    pages: list[tuple[int, list[list[list[str | None]]], str]] = []
    try:
        with pdfplumber.open(path) as pdf:
            for page_number, page in enumerate(pdf.pages, start=1):
                text = page.extract_text(x_tolerance=2, y_tolerance=3) or ""
                tables = page.extract_tables() or []
                pages.append((page_number, tables, text))
    except Exception as exc:
        raise SpuPortalError(f"Falha ao ler o PDF {path.name!r}.") from exc
    return pages


def _normalize_table_row(row: list[str | None]) -> list[str]:
    return [
        "\n".join(
            compacted
            for line in str(value or "").splitlines()
            if (compacted := _compact(line))
        )
        for value in row
    ]


def _is_cogestao_summary(rows: list[list[str]]) -> bool:
    text = _fold(" ".join(cell for row in rows for cell in row))
    return all(
        label in text
        for label in ("nome prestador", "cnpj", "numero processo", "competencia tms")
    )


def _parse_cogestao_summary(rows: list[list[str]]) -> dict[str, str]:
    summary = {
        "cnpj": _value_after_label(rows, "CNPJ", "CPF/CNPJ"),
        "nome_prestador": _value_after_label(rows, "Nome Prestador"),
        "numero_processo": _value_after_label(
            rows,
            "Numero Processo",
            "Numero Processo Origem",
        ),
        "competencia_tms": _value_after_label(rows, "Competencia TMS"),
        "competencia_producao": _value_after_label(rows, "Comp de Producao"),
        "data_fechamento": _value_after_label(rows, "Dt Fechamento"),
        "valor_informado": _value_after_label(
            rows,
            "Valor Informado",
            "Valor Informado Origem",
            "Valor Informado XML",
        ),
        "valor_aprovado_producao": _value_after_label(
            rows,
            "Valor Aprovado",
            "Valor Liberado",
        ),
        "valor_glosado_producao": _value_after_label(rows, "Valor Glosado"),
    }
    process_resource = _optional_value_after_label(rows, "Numero Processo Recurso")
    if process_resource:
        summary["processo_recurso"] = process_resource
    return summary


def _value_after_label(rows: list[list[str]], label: str, *aliases: str) -> str:
    value = _optional_value_after_label(rows, label, *aliases)
    if value is not None:
        return value
    raise SpuPortalError(f"Campo {label!r} ausente no resumo SAUDECOGESTAO.")


def _optional_value_after_label(
    rows: list[list[str]],
    label: str,
    *aliases: str,
) -> str | None:
    folded_labels = tuple(_fold(item) for item in (label, *aliases))
    for folded_label in folded_labels:
        for row in rows:
            for index, cell in enumerate(row):
                label_lines = [
                    _compact(line) for line in cell.splitlines() if _compact(line)
                ]
                matching_line = next(
                    (
                        line_index
                        for line_index, line in enumerate(label_lines)
                        if _fold(line) == folded_label
                    ),
                    None,
                )
                if matching_line is None:
                    continue
                for candidate in row[index + 1 :]:
                    candidate_lines = [
                        _compact(line)
                        for line in candidate.splitlines()
                        if _compact(line)
                    ]
                    if not candidate_lines:
                        continue
                    if matching_line < len(candidate_lines):
                        return candidate_lines[matching_line]
                    if len(label_lines) == 1:
                        return candidate_lines[0]
    return None


def _protocol_header_indexes(row: list[str]) -> dict[str, int] | None:
    folded = [_fold(cell) for cell in row]
    required = {
        "nr": ("nr",),
        "valor_protocolo": ("valor protocolo",),
        "valor_glosado_protocolo": ("valor glosado", "valor gloado"),
        "valor_aprovado_protocolo": ("valor aprovado",),
    }
    indexes: dict[str, int] = {}
    for field, labels in required.items():
        try:
            indexes[field] = next(
                index
                for index, cell in enumerate(folded)
                if (
                    cell in labels
                    if field == "nr"
                    else any(label in cell for label in labels)
                )
            )
        except StopIteration:
            return None
    for index, cell in enumerate(folded):
        if "nr origem" in cell:
            indexes["nr_origem"] = index
            break
    return indexes


def _find_protocol_header(
    rows: list[list[str]],
) -> tuple[dict[str, int], int] | None:
    for start in range(len(rows)):
        combined: list[str] = []
        for end in range(start, min(start + 4, len(rows))):
            current = rows[end]
            if len(combined) < len(current):
                combined.extend([""] * (len(current) - len(combined)))
            for index, cell in enumerate(current):
                if cell:
                    combined[index] = _compact(f"{combined[index]} {cell}")
            indexes = _protocol_header_indexes(combined)
            if indexes is not None:
                return indexes, end + 1
    return None


def _protocol_values(
    row: list[str],
    indexes: dict[str, int],
) -> dict[str, str] | None:
    populated = [cell for cell in row if cell]
    if len(populated) < 4:
        return None
    nr = populated[0]
    if not nr or not re.search(r"\d", nr):
        return None
    protocol_values = populated[-3:]
    if not any(protocol_values):
        return None
    return {
        "nr": nr,
        "nr_origem": populated[1] if "nr_origem" in indexes else "",
        "valor_protocolo": protocol_values[0],
        "valor_glosado_protocolo": protocol_values[1],
        "valor_aprovado_protocolo": protocol_values[2],
    }


def _normalize_process_number(value: str) -> str | None:
    match = re.search(r"\b(P?\d{5,})\s*[/_]\s*(\d{4})\b", value, re.I)
    if not match:
        return None
    return f"{match.group(1).upper()}/{match.group(2)}"


def _nuexo_record(
    document: SpuDocument,
    *,
    tipo_documento: str,
    banco: str | None = None,
    banco_agencia: str | None = None,
    codigo_conta: str | None = None,
    codigo_agencia: str | None = None,
    conta: str | None = None,
    numero_nfse: str | None = None,
    chave_acesso_nfse: str | None = None,
    cnpj_cpf_nif_prestador: str | None = None,
) -> dict[str, Any]:
    # Preserve the historical fingerprint input so existing dlt merge keys do
    # not change. banco_agencia is not returned or loaded as an active column.
    legacy_record = {
        "numero_processo": document.numero_processo,
        "tipo_documento": tipo_documento,
        "documento_id": document.document_id,
        "documento_nome": document.nome,
        "banco": banco,
        "banco_agencia": _optional(banco_agencia),
        "conta": _optional(conta),
        "numero_nfse": _optional(numero_nfse),
        "chave_acesso_nfse": _optional(chave_acesso_nfse),
        "cnpj_cpf_nif_prestador": _optional(cnpj_cpf_nif_prestador),
    }
    record = {
        "numero_processo": document.numero_processo,
        "tipo_documento": tipo_documento,
        "documento_id": document.document_id,
        "documento_nome": document.nome,
        "banco": banco,
        "codigo_conta": _optional(codigo_conta),
        "codigo_agencia": _optional(codigo_agencia),
        "conta": _optional(conta),
        "numero_nfse": _optional(numero_nfse),
        "chave_acesso_nfse": _optional(chave_acesso_nfse),
        "cnpj_cpf_nif_prestador": _optional(cnpj_cpf_nif_prestador),
    }
    record["id_registro"] = _fingerprint(legacy_record)
    return record


def _find_value(text: str, labels: tuple[str, ...]) -> str | None:
    for label in labels:
        match = re.search(
            rf"(?:{label})[ \t]*[:\-]?[ \t]*(?:\r?\n[ \t]*)?([^\r\n]+)",
            text,
            flags=re.I,
        )
        if match:
            return _compact(match.group(1)) or None
    return None


def _trim_at_label(value: str | None) -> str | None:
    if not value:
        return None
    trimmed = re.split(
        r"\s+(?:CONTA|BANCO|AG[ÊE]NCIA|FAVORECIDO|CPF\s*/\s*CNPJ|NIT)\s*:",
        value,
        maxsplit=1,
        flags=re.I,
    )[0]
    return _optional(trimmed)


def _find_bank(bank_agency: str | None, text: str) -> str | None:
    folded_text = _fold(text)
    known = {
        "santander": "Banco Santander",
        "bradesco": "Banco Bradesco",
        "itau": "Banco Itaú",
        "banco do brasil": "Banco do Brasil",
        "caixa economica": "Caixa Econômica Federal",
        "nubank": "Nubank",
    }
    for marker, name in known.items():
        if marker in folded_text:
            return name
    if bank_agency:
        for part in (item.strip() for item in bank_agency.split("/")):
            if re.search(r"[A-Za-zÀ-ÿ]", part):
                return part if _fold(part).startswith("banco ") else f"Banco {part}"
    return None


def _split_bank_agency(
    bank_agency: str | None,
) -> tuple[str | None, str | None]:
    raw = _optional(bank_agency)
    if raw is None:
        return None, None
    parts = [_compact(part) for part in raw.split("/") if _compact(part)]
    if len(parts) >= 3:
        return parts[0], parts[-1]
    if len(parts) == 2:
        return parts[0], parts[1]
    return (parts[0] if parts else None), None


def _find_provider_id(text: str) -> str | None:
    danfse_match = re.search(
        r"PRESTADOR\s*/\s*FORNECEDOR[^\r\n]*"
        r"CNPJ\s*/\s*CPF\s*/\s*NIF[^\r\n]*"
        r"\r?\n[ \t]*([0-9][0-9./\-]{10,24})",
        text,
        flags=re.I,
    )
    if danfse_match:
        return danfse_match.group(1)

    section_match = re.search(
        r"\bPRESTADOR\b(?P<section>.*?)(?:\bTOMADOR\b|\Z)",
        text,
        flags=re.I | re.S,
    )
    section = section_match.group("section") if section_match else text
    match = re.search(
        r"(?:CPF\s*/\s*CNPJ|CNPJ\s*/\s*CPF(?:\s*/\s*NIF)?)"
        r"[ \t]*[:\-]?[ \t]*(?:\r?\n[ \t]*)?"
        r"([0-9./\-]{11,25})",
        section,
        flags=re.I,
    )
    if match:
        return match.group(1)
    fallback = re.search(
        r"(?:CPF\s*/\s*CNPJ|CNPJ\s*/\s*CPF(?:\s*/\s*NIF)?)"
        r"(?:\s*\([^)]*\))?[ \t]*[:\-]?[ \t]*"
        r"(?:\r?\n[ \t]*)?([0-9./\-]{11,25})",
        text,
        flags=re.I,
    )
    return fallback.group(1) if fallback else None


def _find_invoice_number(text: str) -> str | None:
    for pattern in (
        r"N[ÚU]MERO\s+DA\s+NFS-?E\s*:?[ \t\r\n]*(\d{1,20})\b",
        r"\bNFS-?E\s*(?:N[º°O])?\s*:?[ \t]*(\d{1,20})\b",
    ):
        match = re.search(pattern, text, flags=re.I)
        if match:
            return match.group(1)
    return None


def _only_identifier(value: str | None) -> str | None:
    if not value:
        return None
    identifier = re.sub(r"[^A-Za-z0-9]", "", value)
    return identifier or None


def _parse_date(value: str) -> date | None:
    raw = _optional(value)
    if raw is None:
        return None
    raw = re.sub(r"/{2,}", "/", raw)
    for pattern in ("%d/%m/%Y", "%d/%m/%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, pattern).date()
        except ValueError:
            continue
    raise SpuPortalError(f"Data invalida na tabela SAUDECOGESTAO: {value!r}.")


def _parse_decimal(value: str) -> Decimal | None:
    compact_value = _compact(value)
    if re.sub(r"\s+", "", compact_value).upper() in {"-", "R$-"}:
        return Decimal("0")
    raw = _optional(value)
    if raw is None:
        return None
    negative = raw.startswith("(") and raw.endswith(")")
    normalized = re.sub(r"[^0-9,.-]", "", raw).strip(".")
    if "," in normalized and "." in normalized:
        if normalized.rfind(",") > normalized.rfind("."):
            normalized = normalized.replace(".", "").replace(",", ".")
        else:
            normalized = normalized.replace(",", "")
    elif "," in normalized:
        normalized = normalized.replace(".", "").replace(",", ".")
    elif normalized.count(".") > 1:
        parts = normalized.split(".")
        normalized = "".join(parts[:-1]) + "." + parts[-1]
    if negative:
        normalized = "-" + normalized.lstrip("-")
    try:
        return Decimal(normalized)
    except InvalidOperation as exc:
        raise SpuPortalError(
            f"Valor invalido na tabela SAUDECOGESTAO: {value!r}."
        ) from exc


def _optional(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = _compact(value).strip(" :-–—")
    return stripped or None


def _digits(value: str) -> str:
    return re.sub(r"\D", "", value)


def _competencia_from_internal_process(value: str) -> str | None:
    match = re.match(r"^(\d{4})(\d{2})", value)
    if not match:
        return None
    month = int(match.group(2))
    if not 1 <= month <= 12:
        return None
    return f"{month:02d}/{match.group(1)}"


def _compact(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _fold(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    return "".join(
        char for char in normalized if not unicodedata.combining(char)
    ).lower()


def _fingerprint(record: dict[str, Any]) -> str:
    raw = json.dumps(record, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
