from __future__ import annotations

import csv
import hashlib
import json
import re
from collections import Counter
from collections.abc import Iterable, Iterator
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import dlt

from nfs_fortaleza.ipm_portal import DownloadedIpmDemonstrative, IpmReference


TABLE_NAME = "demonstrativo_conta_ipm"
DETAIL_HEADER = "FATURA#LOTE#DATA ENVIO LOTE#NRO PROTOCOLO#"
OPERATOR_HEADER = "REGISTRO ANS#OPERADORA#CNPJ OPERADORA#"
DETAIL_COLUMN_COUNT = 21


def parse_ipm_demonstrative(
    content: bytes,
    reference: IpmReference,
) -> Iterator[dict[str, Any]]:
    text = content.decode("cp1252")
    lines = text.splitlines()
    cnpj_operadora = _operator_cnpj(lines)
    detail_start = _detail_start(lines)
    occurrences: Counter[str] = Counter()

    for line_number, raw_line in enumerate(
        lines[detail_start:],
        start=detail_start + 1,
    ):
        if not raw_line.strip():
            continue
        fields = next(csv.reader([raw_line], delimiter="#"))
        if len(fields) != DETAIL_COLUMN_COUNT:
            raise ValueError(
                f"Linha {line_number} da referencia {reference.label} possui "
                f"{len(fields)} campos; eram esperados {DETAIL_COLUMN_COUNT}."
            )
        fields = [field.strip() for field in fields]

        # O TXT nativo inverte os rotulos LOTE e NRO PROTOCOLO. A tela HTML
        # confirma que o identificador TISS e o lote e o numero puro e o protocolo.
        record: dict[str, Any] = {
            "referencia": date(reference.year, reference.month, 1),
            "cnpj_operadora": cnpj_operadora,
            "numero_lote": _optional_text(fields[3]),
            "data_envio_lote": _parse_date(fields[2]),
            "numero_protocolo": _optional_text(fields[1]),
            "valor_protocolo": _parse_decimal(fields[4]),
            "valor_glosa_protocolo": _parse_decimal(fields[5]),
            "numero_guia_senha": _optional_text(fields[7]),
            "data_realizacao": _parse_date(fields[10]),
            "descricao_servico": _optional_text(fields[13]),
            "codigo_tabela": _optional_text(fields[11]),
            "codigo_servico": _optional_text(fields[12]),
            "grau_participacao": _optional_text(fields[14]),
            "quantidade_executada": _parse_decimal(fields[15]),
            "valor_processado": _parse_decimal(fields[16]),
            "valor_liberado": _parse_decimal(fields[17]),
            "valor_glosa": _parse_decimal(fields[18]),
            "codigo_glosa": (
                _optional_text(fields[19]) or _optional_text(fields[20])
            ),
            "nome_beneficiario": _optional_text(fields[9]),
            "codigo_beneficiario": _optional_text(fields[8]),
        }
        fingerprint = _fingerprint(record)
        occurrences[fingerprint] += 1
        record["id_registro"] = hashlib.sha256(
            f"{fingerprint}:{occurrences[fingerprint]}".encode("utf-8")
        ).hexdigest()
        yield record


def demonstrativo_conta_ipm_resource(
    downloads: Iterable[DownloadedIpmDemonstrative],
):
    def records() -> Iterator[dict[str, Any]]:
        for download in downloads:
            yield from parse_ipm_demonstrative(
                download.path.read_bytes(),
                download.reference,
            )

    return dlt.resource(
        records(),
        name=TABLE_NAME,
        # No merge_key: Maida rows share the table and the month. The IPM
        # rows of a reloaded month are removed by delete_ipm_references.
        primary_key="id_registro",
        write_disposition="merge",
        columns=_column_hints(),
    )


def _operator_cnpj(lines: list[str]) -> str:
    for index, line in enumerate(lines):
        if line.startswith(OPERATOR_HEADER):
            if index + 1 >= len(lines):
                break
            fields = next(csv.reader([lines[index + 1]], delimiter="#"))
            if len(fields) < 3:
                break
            digits = re.sub(r"\D", "", fields[2])
            if len(digits) == 14:
                return digits
            break
    raise ValueError("CNPJ da operadora nao encontrado no demonstrativo IPM.")


def _detail_start(lines: list[str]) -> int:
    for index, line in enumerate(lines):
        if line.startswith(DETAIL_HEADER):
            return index + 1
    raise ValueError("Cabecalho de itens nao encontrado no demonstrativo IPM.")


def _optional_text(value: str) -> str | None:
    stripped = value.strip()
    return None if not stripped or stripped == "-" else stripped


def _parse_date(value: str) -> date | None:
    raw = _optional_text(value)
    if raw is None:
        return None
    try:
        return datetime.strptime(raw, "%d/%m/%Y").date()
    except ValueError as exc:
        raise ValueError(f"Data invalida no demonstrativo IPM: {value!r}.") from exc


def _parse_decimal(value: str) -> Decimal | None:
    raw = _optional_text(value)
    if raw is None:
        return None
    normalized = raw.replace(".", "").replace(",", ".")
    if normalized.startswith("."):
        normalized = "0" + normalized
    elif normalized.startswith("-."):
        normalized = normalized.replace("-.", "-0.", 1)
    try:
        return Decimal(normalized)
    except InvalidOperation as exc:
        raise ValueError(f"Numero invalido no demonstrativo IPM: {value!r}.") from exc


def _fingerprint(record: dict[str, Any]) -> str:
    raw = json.dumps(record, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _column_hints() -> dict[str, dict[str, Any]]:
    text_columns = (
        "id_registro",
        "cnpj_operadora",
        "numero_lote",
        "numero_protocolo",
        "numero_guia_senha",
        "descricao_servico",
        "codigo_tabela",
        "codigo_servico",
        "grau_participacao",
        "codigo_glosa",
        "nome_beneficiario",
        "codigo_beneficiario",
    )
    hints: dict[str, dict[str, Any]] = {
        name: {"data_type": "text", "nullable": True}
        for name in text_columns
    }
    hints["id_registro"]["nullable"] = False
    # Explicitly off: the stored dlt schema still marks referencia as the
    # merge key from earlier loads, which deleted Maida rows of the month.
    hints["referencia"] = {
        "data_type": "date",
        "nullable": False,
        "merge_key": False,
    }
    hints["data_envio_lote"] = {"data_type": "date", "nullable": True}
    hints["data_realizacao"] = {"data_type": "date", "nullable": True}
    for name in (
        "valor_protocolo",
        "valor_glosa_protocolo",
        "valor_processado",
        "valor_liberado",
        "valor_glosa",
    ):
        hints[name] = {
            "data_type": "decimal",
            "precision": 18,
            "scale": 2,
            "nullable": True,
        }
    hints["quantidade_executada"] = {
        "data_type": "decimal",
        "precision": 18,
        "scale": 4,
        "nullable": True,
    }
    return hints
