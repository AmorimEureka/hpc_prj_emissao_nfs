from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import openpyxl
import pytest

from nfs_fortaleza.maida_portal import (
    DownloadedMaidaDocument,
    MaidaCompetency,
    MaidaDocument,
)
from nfs_fortaleza.maida_xlsx import MaidaSpreadsheetError, parse_maida_xlsx


def _download(path: Path) -> DownloadedMaidaDocument:
    return DownloadedMaidaDocument(
        document=MaidaDocument(
            competency=MaidaCompetency(2026, 1),
            source="resumo",
            document_id="doc-1",
            file_name="demonstrativo.xlsx",
            remote_path="/arquivo/demonstrativo.xlsx",
        ),
        path=path,
    )


def test_parse_maida_xlsx_filters_and_normalizes_glosas(tmp_path: Path) -> None:
    path = tmp_path / "demonstrativo.xlsx"
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.append(["Demonstrativo ISSEC"])
    sheet.append(
        [
            "NRO PROTOCOLO",
            "NÚMERO GUIA",
            "PACIENTE",
            "COD. PROCEDIMENTO",
            "DESCRIÇÃO PROCEDIMENTO",
            "QTD EXECUTADA",
            "VALOR APRESENTADO",
            "VALOR PAGO",
            "VALOR GLOSA",
            "CÓDIGO GLOSA",
            "DATA REALIZAÇÃO",
        ]
    )
    sheet.append(
        [
            "5583152",
            "521882",
            "PACIENTE TESTE",
            "30912199",
            "PROCEDIMENTO TESTE",
            2,
            "1.200,50",
            "900,25",
            "300,25",
            "1714 - VALOR SUPERIOR",
            "05/01/2026",
        ]
    )
    sheet.append(["sem-glosa", None, None, None, None, 1, 10, 10, 0, 1714])
    sheet.append(["sem-codigo", None, None, None, None, 1, 10, 0, 10, None])
    workbook.save(path)

    records = list(parse_maida_xlsx(_download(path)))

    assert len(records) == 1
    assert records[0]["numero_protocolo"] == "5583152"
    assert records[0]["codigo_glosa"] == "1714"
    assert records[0]["codigo_servico"] == "30912199"
    assert records[0]["valor_glosa"] == Decimal("300.25")
    assert records[0]["valor_processado"] == Decimal("1200.50")
    assert records[0]["valor_liberado"] == Decimal("900.25")
    assert records[0]["data_realizacao"] == date(2026, 1, 5)
    assert records[0]["referencia"] == date(2026, 1, 1)
    assert records[0]["id_registro"]


def test_parse_maida_xlsx_rejects_unknown_layout(tmp_path: Path) -> None:
    path = tmp_path / "invalido.xlsx"
    workbook = openpyxl.Workbook()
    workbook.active.append(["COLUNA A", "COLUNA B"])
    workbook.save(path)

    with pytest.raises(MaidaSpreadsheetError, match="Valor Glosa"):
        list(parse_maida_xlsx(_download(path)))


def test_lot_demonstrativo_layout_preserves_operator_guide_and_amounts(tmp_path):
    from dataclasses import replace
    path = tmp_path / "Demonstrativo_Lote31662.xlsx"
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "DemonstrativoAnaliseDeContas"
    sheet.append(["Demonstrativo de Pagamento"])
    sheet.append(["Identificador do lote", "Competência do lote", "ID do lote",
        "Número da guia do prestador", "Número da guia da operadora",
        "Situação do lote", "Tipo do lote", "ID da guia", "CNPJ do prestador",
        "Nome fantasia do prestador", "Nome do beneficiário",
        "Número da carteira do beneficiário", "Data de execução do primeiro item da guia",
        "Data de autorização da guia", "ID do item da guia", "Data de início da execução",
        "Código", "Descrição", "Status do item da guia", "Quantidade executada",
        "Valor total apresentado", "Valor apurado", "Valor glosado", "Motivo da glosa"])
    row = [31662, "09/2026", 15329, "prestador-diferente", "566985", "FECHADO",
        "RESUMO_INTERNACAO", 100, "00000000000000", "TESTE", "PACIENTE TESTE",
        "001234", "", "13/08/2026", 200, "14/08/2026", "000123", "ITEM TESTE",
        "GLOSADO", 2, 20.21, 10, 10.21, "1705 - VALOR APRESENTADO A MAIOR"]
    sheet.append(row)
    sheet.append(row[:22] + [0, " "])
    workbook.save(path)
    download = _download(path)
    download = replace(download, document=replace(download.document,
        competency=MaidaCompetency(2026, 9), source="lote_glosa_nao_recursado",
        lot_id="15329", file_name=path.name))
    records = list(parse_maida_xlsx(download))
    assert len(records) == 1
    record = records[0]
    assert record['numero_lote'] == '31662'
    assert record['numero_guia_senha'] == '566985'
    assert record['codigo_servico'] == '000123'
    assert record['codigo_beneficiario'] == '001234'
    assert record['nome_beneficiario'] == 'PACIENTE TESTE'
    assert record['quantidade_executada'] == Decimal('2')
    assert record['data_realizacao'] == date(2026, 8, 14)
    assert record['referencia'] == date(2026, 9, 1)
    assert record['valor_processado'] == Decimal('20.21')
    assert record['valor_liberado'] == Decimal('10')
    assert record['valor_glosa'] == Decimal('10.21')
    assert record['codigo_glosa'] == '1705'
    assert record['origem_maida'] == 'lote_glosa_nao_recursado'
    assert records == list(parse_maida_xlsx(download))


def test_summary_layout_keeps_existing_merge_identity():
    from nfs_fortaleza.maida_xlsx import _find_header, _parse_row
    _, columns, rows = _find_header(iter([
        ('Guia', 'Valor apresentado', 'Valor apurado', 'Valor Glosa', 'Código Glosa'),
        ('123', 10, 9, 1, '1705'),
    ]))
    record = _parse_row(next(rows), columns)
    assert record['valor_liberado'] is None
    assert record['valor_glosa'] == Decimal('1')
