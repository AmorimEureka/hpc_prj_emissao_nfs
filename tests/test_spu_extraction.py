from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import nfs_fortaleza.spu_extraction as spu_extraction
from nfs_fortaleza.spu_extraction import (
    LoadedSpuProcess,
    SpuExtractionConfigurationError,
    SpuExtractionPayload,
    _competencia_unica_remessa,
    _process_row,
    _split_nuexo_rows,
    extract_and_load_tramitando_reports,
    load_spu_process_batches,
    select_new_spu_processes,
)
from nfs_fortaleza.spu_portal import SpuProcessSummary
from nfs_fortaleza.spu_resources import (
    COGESTAO_TABLE_NAME,
    EMPENHO_TABLE_NAME,
    HISTORY_TABLE_NAME,
    NOTA_FISCAL_TABLE_NAME,
    PROCESS_TABLE_NAME,
    TRAMITANDO_REPORT_TABLE_NAME,
    spu_resources,
)


def test_spu_postgres_table_names_use_processos_ipm_prefix() -> None:
    assert PROCESS_TABLE_NAME == "processos_ipm"
    assert HISTORY_TABLE_NAME == "processos_historico_ipm"
    assert COGESTAO_TABLE_NAME == "processos_ipm_saude_cogestao"


def test_uses_oracle_competence_when_remessa_has_single_month() -> None:
    candidates = [
        (19258, 1, 10, "PACIENTE A", "1", date(2026, 7, 1), 10),
        (19258, 2, 11, "PACIENTE B", "2", date(2026, 7, 1), 20),
    ]

    assert _competencia_unica_remessa(candidates) == date(2026, 7, 1)
    assert EMPENHO_TABLE_NAME == "processos_empenho_ipm"
    assert NOTA_FISCAL_TABLE_NAME == "processos_nota_fiscal_ipm"
    assert TRAMITANDO_REPORT_TABLE_NAME == "processos_relatorios_tramitando_ipm"

    resources = spu_resources([], [], [], [], [])
    assert [resource.name for resource in resources] == [
        "processos_ipm",
        "processos_historico_ipm",
        "processos_ipm_saude_cogestao",
        "processos_empenho_ipm",
        "processos_nota_fiscal_ipm",
    ]
    assert set(resources[3].columns) == {
        "id_registro",
        "numero_processo",
        "documento_id",
        "documento_nome",
        "banco",
        "codigo_conta",
        "codigo_agencia",
        "conta",
    }
    assert "banco_agencia" not in resources[3].columns


def test_splits_nuexo_rows_into_document_specific_schemas() -> None:
    common = {
        "numero_processo": "P193251/2026",
        "documento_id": "doc-1",
        "documento_nome": "documento.pdf",
    }
    empenhos, notas_fiscais = _split_nuexo_rows(
        [
            {
                **common,
                "id_registro": "emp-1",
                "tipo_documento": "EMPENHO",
                "banco": "Banco Santander",
                "codigo_conta": "033",
                "codigo_agencia": "1234",
                "conta": "123-4",
                "numero_nfse": None,
            },
            {
                **common,
                "id_registro": "nf-1",
                "tipo_documento": "NFS_E",
                "numero_nfse": "12345",
                "chave_acesso_nfse": "ABC123",
                "cnpj_cpf_nif_prestador": "08711085000128",
                "banco": None,
            },
        ]
    )

    assert empenhos == [
        {
            **common,
            "id_registro": "emp-1",
            "banco": "Banco Santander",
            "codigo_conta": "033",
            "codigo_agencia": "1234",
            "conta": "123-4",
        }
    ]
    assert notas_fiscais == [
        {
            **common,
            "id_registro": "nf-1",
            "numero_nfse": "12345",
            "chave_acesso_nfse": "ABC123",
            "cnpj_cpf_nif_prestador": "08711085000128",
        }
    ]


def test_selects_only_new_processes_and_required_status_transition() -> None:
    new = _process("P100001/2026", "TRAMITANDO")
    unchanged = _process("P100002/2026", "TRAMITANDO")
    transitioned = _process("P100003/2026", "FINALIZADO")
    incomplete_final = _process("P100004/2026", "FINALIZADO")
    complete_final = _process("P100005/2026", "FINALIZADO")
    loaded = {
        "P100002/2026": LoadedSpuProcess("TRAMITANDO", False),
        "P100003/2026": LoadedSpuProcess("TRAMITANDO", False),
        "P100004/2026": LoadedSpuProcess("FINALIZADO", False),
        "P100005/2026": LoadedSpuProcess("FINALIZADO", True),
    }

    selected, skipped = select_new_spu_processes(
        (new, unchanged, transitioned, incomplete_final, complete_final),
        loaded,
    )

    assert [item.numero_processo for item in selected] == [
        "P100001/2026",
        "P100003/2026",
        "P100004/2026",
    ]
    assert [item.numero_processo for item in skipped] == [
        "P100002/2026",
        "P100005/2026",
    ]


def test_payload_validates_process_numbers_and_page_limit() -> None:
    payload = SpuExtractionPayload.from_mapping(
        {
            "numero_processos": ["P193251_2026", "P193251/2026"],
            "max_pages": "3",
        }
    )

    assert payload.numero_processos == ("P193251/2026",)
    assert payload.max_pages == 3
    assert payload.varredura_completa is False

    full_scan = SpuExtractionPayload.from_mapping(
        {"varredura_completa": True}
    )
    assert full_scan.varredura_completa is True

    with pytest.raises(SpuExtractionConfigurationError):
        SpuExtractionPayload.from_mapping({"max_pages": 0})
    with pytest.raises(SpuExtractionConfigurationError):
        SpuExtractionPayload.from_mapping({"varredura_completa": "true"})


@pytest.mark.parametrize(
    ("full_scan", "expected_pages", "expected_processed"),
    [
        (False, 3, ("P100010/2026",)),
        (True, 4, ("P100010/2026", "P100014/2026")),
    ],
)
def test_process_pagination_stops_after_known_pages_unless_full_scan(
    monkeypatch: pytest.MonkeyPatch,
    full_scan: bool,
    expected_pages: int,
    expected_processed: tuple[str, ...],
) -> None:
    pages = (
        (
            1,
            (
                _process("P100010/2026", "TRAMITANDO"),
                _process("P100011/2026", "FINALIZADO"),
            ),
        ),
        (2, (_process("P100012/2026", "FINALIZADO"),)),
        (3, (_process("P100013/2026", "FINALIZADO"),)),
        (4, (_process("P100014/2026", "TRAMITANDO"),)),
    )
    loaded = {
        "P100011/2026": LoadedSpuProcess("FINALIZADO", True),
        "P100012/2026": LoadedSpuProcess("FINALIZADO", True),
        "P100013/2026": LoadedSpuProcess("FINALIZADO", True),
    }

    class FakePortalClient:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def __enter__(self) -> FakePortalClient:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def iter_process_pages(
            self,
            *,
            max_pages: int | None = None,
        ):
            for page_number, processes in pages:
                if max_pages is not None and page_number > max_pages:
                    break
                yield page_number, processes

    monkeypatch.setattr(
        spu_extraction,
        "list_loaded_spu_processes",
        lambda _settings: dict(loaded),
    )
    monkeypatch.setattr(spu_extraction, "SpuPortalClient", FakePortalClient)
    monkeypatch.setattr(spu_extraction, "_spu_pipeline", lambda _settings: object())
    monkeypatch.setattr(
        spu_extraction,
        "_run_spu_pipeline",
        lambda *_args, **_kwargs: None,
    )
    settings = SimpleNamespace(process_batch_size=50)

    summary = load_spu_process_batches(
        settings,  # type: ignore[arg-type]
        SpuExtractionPayload(varredura_completa=full_scan),
        downloads_dir=Path("."),
    )

    assert summary.paginas_processadas == expected_pages
    assert summary.processos_processados == expected_processed


def test_process_row_can_mark_finalized_pdf_as_pending() -> None:
    process = _process("P100006/2026", "FINALIZADO")

    row = _process_row(
        process,
        datetime(2026, 8, 5, tzinfo=timezone.utc),
        detalhes_finalizados_extraidos=False,
    )

    assert row["detalhes_finalizados_extraidos"] is False


def test_tramitando_reports_load_only_new_documents(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    process = _process("P335842/2026", "TRAMITANDO")
    document = SimpleNamespace(document_id="new-document", path=tmp_path / "x.pdf")
    loaded_rows = []

    class FakePortalClient:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def iter_process_pages(self):
            yield 1, (process,)

        def download_tramitando_report_documents(
            self,
            _process: SpuProcessSummary,
            *,
            loaded_document_ids: set[str],
        ):
            assert loaded_document_ids == {"old-document"}
            return (document,)

    class FakePipeline:
        def run(self, resource) -> None:
            loaded_rows.extend(resource)

    monkeypatch.setattr(
        spu_extraction,
        "list_tramitando_processes_for_report",
        lambda *_args: (process,),
    )
    monkeypatch.setattr(
        spu_extraction,
        "list_loaded_tramitando_report_document_ids",
        lambda *_args: {"old-document"},
    )
    monkeypatch.setattr(spu_extraction, "SpuPortalClient", FakePortalClient)
    monkeypatch.setattr(spu_extraction, "_spu_pipeline", lambda *_args: FakePipeline())
    monkeypatch.setattr(
        spu_extraction,
        "parse_tramitando_report_documents",
        lambda *_args: [
            {"id_registro": "row-1", "competencia": date(2026, 7, 1)}
        ],
    )
    monkeypatch.setattr(
        spu_extraction,
        "_enrich_tramitando_report_rows",
        lambda *_args: None,
    )

    summary = extract_and_load_tramitando_reports(
        SimpleNamespace(),  # type: ignore[arg-type]
        downloads_dir=tmp_path,
    )

    assert summary.processos_processados == ("P335842/2026",)
    assert loaded_rows == [
        {"id_registro": "row-1", "competencia": date(2026, 7, 1)}
    ]


def test_tramitando_process_query_preserves_regex_quantifier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executed = []

    class FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, query, parameters) -> None:
            executed.append((query, parameters))

        def fetchall(self):
            return []

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def cursor(self):
            return FakeCursor()

    monkeypatch.setattr(
        spu_extraction.psycopg2,
        "connect",
        lambda *_args, **_kwargs: FakeConnection(),
    )

    result = spu_extraction.list_tramitando_processes_for_report(
        SimpleNamespace(database_url="postgresql://db", postgres_schema="api")
    )

    assert result == ()
    assert len(executed) == 1


def _process(number: str, status: str) -> SpuProcessSummary:
    return SpuProcessSummary(
        numero_processo=number,
        status_processo=status,
        tipo_processo_assunto="Pagamento",
        data_abertura=date(2026, 8, 1),
        motivo_finalizacao=None,
        url_visualizacao=None,
    )
