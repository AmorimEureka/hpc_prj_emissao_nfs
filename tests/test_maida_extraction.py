from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from nfs_fortaleza.maida_extraction import (
    MaidaExtractionConfigurationError,
    MaidaExtractionPayload,
    _competency_range,
    _parse_glosa_records,
    _enrich_glosa_records,
)
from nfs_fortaleza.maida_portal import (
    DownloadedMaidaDocument,
    MaidaCompetency,
    MaidaDocument,
    MaidaCompetencyResult,
)
from nfs_fortaleza.maida_xlsx import MaidaSpreadsheetError


def test_payload_accepts_single_and_multiple_competencies() -> None:
    single = MaidaExtractionPayload.from_mapping({"competencia": "01/2026"})
    multiple = MaidaExtractionPayload.from_mapping(
        {"competencias": ["03/2026", "2026-02", "03/2026"]}
    )

    assert single.competencies == (MaidaCompetency(2026, 1),)
    assert multiple.competencies == (
        MaidaCompetency(2026, 2),
        MaidaCompetency(2026, 3),
    )


def test_payload_rejects_invalid_competency() -> None:
    with pytest.raises(MaidaExtractionConfigurationError):
        MaidaExtractionPayload.from_mapping({"competencia": "13/2026"})


def test_competency_range_starts_in_january_2026() -> None:
    values = list(
        _competency_range(MaidaCompetency(2026, 1), MaidaCompetency(2026, 4))
    )
    assert [value.first_day for value in values] == [
        date(2026, 1, 1),
        date(2026, 2, 1),
        date(2026, 3, 1),
        date(2026, 4, 1),
    ]


def test_parse_glosa_records_ignores_document_without_glosa_layout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    downloaded = DownloadedMaidaDocument(
        document=MaidaDocument(
            competency=MaidaCompetency(2026, 6),
            source="lotes",
            document_id="doc-sem-glosa",
            file_name="Demonstrativo_Lote26921.xlsx",
            remote_path="/arquivo/Demonstrativo_Lote26921.xlsx",
        ),
        path=tmp_path / "Demonstrativo_Lote26921.xlsx",
    )

    def invalid_workbook(_downloaded: DownloadedMaidaDocument):
        raise MaidaSpreadsheetError("layout sem colunas de glosa")

    monkeypatch.setattr(
        "nfs_fortaleza.maida_extraction.parse_maida_xlsx",
        invalid_workbook,
    )

    assert _parse_glosa_records(downloaded) == []
    assert "Demonstrativo_Lote26921.xlsx" in caplog.text


def test_enriches_record_with_card_from_same_lot_and_guide(tmp_path: Path) -> None:
    document = MaidaDocument(
        competency=MaidaCompetency(2026, 1), source="resumo",
        document_id="doc-1", file_name="demo.xlsx", remote_path="/demo.xlsx",
    )
    downloaded = DownloadedMaidaDocument(document=document, path=tmp_path / "demo.xlsx")
    result = MaidaCompetencyResult(
        document.competency, (), (), (),
        ({
            "lote_id": "uuid-lote", "identificador_lote": "19414",
            "numero_guia": "521882", "numero_carteira": "000123456",
        },),
    )
    records = [{"numero_lote": "19414", "numero_guia_senha": "521882"}]

    enriched = _enrich_glosa_records(records, downloaded, result)

    assert enriched[0]["codigo_beneficiario"] == "000123456"


def test_missing_guide_card_preserves_card_from_lot_spreadsheet(tmp_path):
    competency = MaidaCompetency(2026, 9)
    document = MaidaDocument(competency, 'lote_glosa_nao_recursado', 'doc', 'lote.xlsx', '/lote.xlsx', lot_id='15329')
    downloaded = DownloadedMaidaDocument(document, tmp_path / 'lote.xlsx')
    result = MaidaCompetencyResult(competency, (), (document,), (),
        guides=({'lote_id': '15329', 'numero_guia': '566985', 'numero_carteira': None},))
    records = [{'numero_guia_senha': '566985', 'numero_lote': '31662', 'codigo_beneficiario': '001234'}]
    assert _enrich_glosa_records(records, downloaded, result)[0]['codigo_beneficiario'] == '001234'
