from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Iterator

import openpyxl

from nfs_fortaleza.maida_portal import DownloadedMaidaDocument


HEADER_SEARCH_LIMIT = 100
FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "numero_lote": ("lote", "numero lote", "n lote", "fatura"),
    "data_envio_lote": ("data envio lote", "data envio"),
    "numero_protocolo": (
        "numero protocolo",
        "nro protocolo",
        "protocolo",
        "protocolo recebimento",
    ),
    "valor_protocolo": ("valor protocolo", "valor apresentado protocolo"),
    "valor_glosa_protocolo": ("valor glosa protocolo",),
    "numero_guia_senha": (
        "numero guia senha",
        "nro guia senha",
        "numero guia",
        "guia",
    ),
    "data_realizacao": (
        "data realizacao",
        "data atendimento",
        "data execucao",
    ),
    "descricao_servico": (
        "descricao servico",
        "descricao procedimento",
        "item glosado",
        "descricao",
    ),
    "codigo_tabela": ("codigo tabela", "cod tabela"),
    "codigo_servico": (
        "codigo servico",
        "cod servico",
        "codigo procedimento",
        "cod procedimento",
        "procedimento",
    ),
    "grau_participacao": ("grau participacao", "grau part"),
    "quantidade_executada": (
        "quantidade executada",
        "qtd executada",
        "qtde apresentada",
        "qtde apre",
        "quantidade apresentada",
    ),
    "valor_processado": (
        "valor processado",
        "valor apresentado",
        "valor apres",
    ),
    "valor_liberado": ("valor liberado", "valor pago"),
    "valor_glosa": ("valor glosa", "valor glosado"),
    "codigo_glosa": (
        "codigo glosa",
        "cod glosa",
        "codigo da glosa",
    ),
    "nome_beneficiario": (
        "beneficiario",
        "nome beneficiario",
        "paciente",
    ),
    "codigo_beneficiario": (
        "codigo beneficiario",
        "numero carteira",
        "carteira",
    ),
}


# Applied only to the detailed lot layout. Keep legacy summary parsing and
# existing merge identities unchanged (notably its "Valor apurado" column).
LOT_FIELD_ALIASES = {
    "identificador do lote": "numero_lote",
    "numero da guia da operadora": "numero_guia_senha",
    "data de inicio da execucao": "data_realizacao",
    "codigo": "codigo_servico",
    "valor total apresentado": "valor_processado",
    "valor apurado": "valor_liberado",
    "motivo da glosa": "codigo_glosa",
    "nome do beneficiario": "nome_beneficiario",
    "numero da carteira do beneficiario": "codigo_beneficiario",
}


class MaidaSpreadsheetError(ValueError):
    """Raised when a Maida workbook does not have a supported layout."""


def parse_maida_xlsx(
    download: DownloadedMaidaDocument,
) -> Iterator[dict[str, Any]]:
    workbook = openpyxl.load_workbook(
        download.path,
        read_only=True,
        data_only=True,
    )
    found_layout = False
    try:
        for worksheet in workbook.worksheets:
            rows = worksheet.iter_rows(values_only=True)
            header_row, columns, remaining_rows = _find_header(rows)
            if header_row is None:
                continue
            found_layout = True
            for excel_row, values in enumerate(
                remaining_rows,
                start=header_row + 1,
            ):
                record = _parse_row(values, columns)
                if record is None:
                    continue
                record.update(_source_metadata(download, worksheet.title, excel_row))
                if record["data_envio_lote"] is None:
                    record["data_envio_lote"] = _date(
                        download.document.sent_at
                    )
                record["id_registro"] = _record_id(record)
                yield record
    finally:
        workbook.close()

    if not found_layout:
        raise MaidaSpreadsheetError(
            f"Nenhuma aba de {download.document.file_name!r} contem "
            "as colunas Valor Glosa e Codigo Glosa."
        )


def _find_header(
    rows: Iterator[tuple[Any, ...]],
) -> tuple[int | None, dict[str, int], Iterator[tuple[Any, ...]]]:
    aliases = {
        alias: field
        for field, field_aliases in FIELD_ALIASES.items()
        for alias in field_aliases
    }
    for row_number, row in enumerate(rows, start=1):
        if row_number > HEADER_SEARCH_LIMIT:
            break
        normalized_row = [_normalize_header(value) for value in row]
        row_aliases = aliases
        if {"identificador do lote", "motivo da glosa"}.issubset(normalized_row):
            row_aliases = {**aliases, **LOT_FIELD_ALIASES}
        columns: dict[str, int] = {}
        for index, normalized in enumerate(normalized_row):
            field = row_aliases.get(normalized)
            if field and field not in columns:
                columns[field] = index
        if "valor_glosa" in columns and "codigo_glosa" in columns:
            return row_number, columns, rows
    return None, {}, iter(())


def _parse_row(
    values: tuple[Any, ...],
    columns: dict[str, int],
) -> dict[str, Any] | None:
    raw_value = _value(values, columns.get("valor_glosa"))
    raw_code = _value(values, columns.get("codigo_glosa"))
    value = _decimal(raw_value)
    code = _numeric_glosa_code(raw_code)
    if value is None or value <= 0 or code is None:
        return None

    record: dict[str, Any] = {
        "valor_glosa": value,
        "codigo_glosa": code,
    }
    text_fields = (
        "numero_lote",
        "numero_protocolo",
        "numero_guia_senha",
        "descricao_servico",
        "codigo_tabela",
        "codigo_servico",
        "grau_participacao",
        "nome_beneficiario",
        "codigo_beneficiario",
    )
    decimal_fields = (
        "valor_protocolo",
        "valor_glosa_protocolo",
        "quantidade_executada",
        "valor_processado",
        "valor_liberado",
    )
    for field in text_fields:
        record[field] = _text(_value(values, columns.get(field)))
    for field in decimal_fields:
        record[field] = _decimal(_value(values, columns.get(field)))
    record["data_envio_lote"] = _date(
        _value(values, columns.get("data_envio_lote"))
    )
    record["data_realizacao"] = _date(
        _value(values, columns.get("data_realizacao"))
    )
    return record


def _source_metadata(
    download: DownloadedMaidaDocument,
    sheet_name: str,
    excel_row: int,
) -> dict[str, Any]:
    document = download.document
    return {
        "referencia": document.competency.first_day,
        "origem_maida": document.source,
        "documento_maida_id": document.document_id,
        "arquivo_maida": document.file_name,
        "lote_maida_id": document.lot_id,
        "tipo_lote_maida": document.lot_type,
        "data_processamento_maida": _date(document.processed_at),
        "data_envio_recurso_maida": _date(document.appeal_sent_at),
        "aba_maida": sheet_name,
        "linha_maida": excel_row,
    }


def _record_id(record: dict[str, Any]) -> str:
    identity = {
        # Estes campos sao enriquecidos/corrigidos depois da leitura. Eles nao
        # podem alterar a chave de merge, inclusive para atualizar as linhas
        # Maida que ja foram carregadas antes do enriquecimento.
        key: None if key in {"codigo_servico", "codigo_beneficiario"} else value
        for key, value in record.items()
        if key not in {"id_registro"}
    }
    raw = json.dumps(identity, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _normalize_header(value: object) -> str:
    if value is None:
        return ""
    normalized = unicodedata.normalize("NFKD", str(value))
    normalized = "".join(char for char in normalized if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", " ", normalized.lower()).strip()


def _numeric_glosa_code(value: object) -> str | None:
    raw = _text(value)
    if raw is None:
        return None
    match = re.search(r"\d+", raw)
    return match.group(0) if match else None


def _decimal(value: object) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    raw = str(value).strip().replace("R$", "").replace(" ", "")
    if not raw or raw == "-":
        return None
    if "," in raw:
        raw = raw.replace(".", "").replace(",", ".")
    try:
        return Decimal(raw)
    except InvalidOperation as exc:
        raise MaidaSpreadsheetError(f"Valor numerico invalido: {value!r}.") from exc


def _date(value: object) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raw = str(value).strip()
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    for pattern in ("%d/%m/%Y", "%Y-%m-%d", "%d/%m/%Y %H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(raw[:19], pattern).date()
        except ValueError:
            continue
    return None


def _text(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    raw = str(value).strip()
    return raw or None


def _value(values: tuple[Any, ...], index: int | None) -> Any:
    if index is None or index >= len(values):
        return None
    return values[index]
