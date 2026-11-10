from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from nfs_fortaleza.spu_portal import (
    SpuMaterializedTreeUnavailable,
    SpuPortalClient,
    SpuProfileInUseError,
    SpuProcessSummary,
    SpuSessionExpiredError,
    _is_target_document,
    _is_report_process_eligible,
    _is_tramitando_report_document,
    canonical_spu_sector,
    document_download_url,
    parse_process_card,
    spu_profile_lock,
)


def test_parse_process_card_extracts_requested_listing_fields() -> None:
    process = parse_process_card(
        {
            "numero": "P193251/2026",
            "cabecalho": "P193251/2026 FINALIZADO",
            "tipo": "TIPO DE PROCESSO/ASSUNTO: Pagamento de prestador",
            "data": "DATA DE ABERTURA: 04/08/2026",
            "texto": (
                "P193251/2026 FINALIZADO TIPO DE PROCESSO/ASSUNTO: "
                "Pagamento de prestador DATA DE ABERTURA: 04/08/2026 "
                "MOTIVO DA FINALIZAÇÃO: Processo pago VISUALIZAR PROCESSO"
            ),
            "motivo": "Processo pago",
            "href": (
                "https://spumaterializar.sepog.fortaleza.ce.gov.br/"
                "processos/P193251_2026/visualizar_folder"
            ),
        }
    )

    assert process.numero_processo == "P193251/2026"
    assert process.status_processo == "FINALIZADO"
    assert process.tipo_processo_assunto == "Pagamento de prestador"
    assert process.data_abertura == date(2026, 8, 4)
    assert process.motivo_finalizacao == "Processo pago"
    assert process.finalizado is True


def test_parse_process_card_accepts_underscore_number() -> None:
    process = parse_process_card(
        {
            "numero": "P193252_2026",
            "cabecalho": "P193252_2026 TRAMITANDO",
            "tipo": "Cobrança administrativa",
            "data": "05/08/2026",
            "texto": "P193252_2026 TRAMITANDO 05/08/2026",
            "href": None,
        }
    )

    assert process.numero_processo == "P193252/2026"
    assert process.tipo_processo_assunto == "Cobrança administrativa"
    assert process.finalizado is False


def test_finalized_process_without_view_has_no_inferred_folder_url() -> None:
    process = parse_process_card(
        {
            "numero": "P483030/2023",
            "cabecalho": "P483030/2023 FINALIZADO",
            "tipo": "Pagamento",
            "data": "01/08/2023",
            "texto": "P483030/2023 FINALIZADO",
            "href": None,
        }
    )
    client = SpuPortalClient(object(), downloads_dir=Path("."))  # type: ignore[arg-type]

    with pytest.raises(SpuMaterializedTreeUnavailable, match="sem o botao"):
        client._open_folder_tree(None, process)  # type: ignore[arg-type]


def test_parse_process_card_keeps_process_with_unknown_status() -> None:
    process = parse_process_card(
        {
            "numero": "P236162/2022",
            "cabecalho": "P236162/2022",
            "tipo": "Processo legado",
            "data": "01/02/2022",
            "texto": "P236162/2022 Processo legado 01/02/2022",
            "href": None,
        }
    )

    assert process.numero_processo == "P236162/2022"
    assert process.status_processo == "DESCONHECIDO"
    assert process.finalizado is False


def test_parse_process_card_recognizes_unarchived_status() -> None:
    process = parse_process_card(
        {
            "numero": "P260758/2025",
            "cabecalho": "NÚMERO P260758/2025 DESARQUIVADO",
            "tipo": "Revisão de pagamento de contas médicas",
            "data": "26/06/2025",
            "texto": (
                "P260758/2025 DESARQUIVADO Revisão de pagamento "
                "DESARQUIVADO PELO SEGUINTE MOTIVO: Equívoco"
            ),
            "motivo": "“EQUIVOCO “ VISUALIZAR MOVIMENTAÇÕES",
            "href": None,
        }
    )

    assert process.status_processo == "DESARQUIVADO"
    assert process.motivo_finalizacao == "EQUIVOCO"
    assert process.finalizado is False


def test_parse_process_card_preserves_status_from_header() -> None:
    process = parse_process_card(
        {
            "numero": "P387860/2024",
            "cabecalho": "NÚMERO P387860/2024",
            "tipo": "Revisão de pagamento",
            "data": "25/09/2024",
            "texto": (
                "P387860/2024 ABERTURA REPROVADA "
                "ABERTURA REPROVADA PELO SEGUINTE MOTIVO: Certidão vencida"
            ),
            "href": None,
        }
    )

    assert process.status_processo == "ABERTURA REPROVADA"
    assert process.finalizado is False


def test_parse_process_card_does_not_use_whole_card_as_reason() -> None:
    process = parse_process_card(
        {
            "numero": "P248911/2026",
            "cabecalho": "NÚMERO P248911/2026 TRAMITANDO",
            "tipo": "Pagamento de contas médicas",
            "data": "10/06/2026",
            "texto": (
                "NÚMERO P248911/2026 TRAMITANDO SOLICITAÇÃO DE "
                "FORNECEDORES/PRESTADORES PAGAMENTO DE CONTAS MÉDICAS"
            ),
            "href": None,
        }
    )

    assert process.status_processo == "TRAMITANDO"
    assert process.motivo_finalizacao is None


def test_canonical_spu_sector_accepts_historical_cogestao_name() -> None:
    assert canonical_spu_sector("IPM/SAUDECOGESTAO") == "ipm/saudecogestao"
    assert canonical_spu_sector("IPM/CO-GESTORA") == "ipm/saudecogestao"
    assert canonical_spu_sector("IPM/NUEXO") == "ipm/nuexo"


def test_target_document_filter_skips_dispatches_and_certificates() -> None:
    assert _is_target_document("ipm/saudecogestao", "RELATORIO.pdf")
    assert not _is_target_document(
        "ipm/saudecogestao",
        "RELATÓRIO_18259_45584518.pdf",
    )
    assert not _is_target_document(
        "ipm/saudecogestao",
        "RELATORIO_18624_22833231.pdf",
    )
    assert not _is_target_document("ipm/saudecogestao", "FOLHA DE DESPACHO.pdf")
    assert _is_target_document("ipm/nuexo", "EMP_1946.pdf")
    assert _is_target_document("ipm/nuexo", "14652_IPM.pdf")
    assert not _is_target_document("ipm/nuexo", "CNDS PRONTOCARDIO.pdf")
    assert not _is_target_document(
        "ipm/nuexo", "CONSULTA REGULARIDADE DO EMPREGADOR.pdf"
    )


def test_tramitando_report_filter_accepts_only_exact_numeric_name() -> None:
    assert _is_tramitando_report_document("RELATORIO_19218_27877736.pdf")
    assert _is_tramitando_report_document("RELATÓRIO_19218_27877736.PDF")
    assert not _is_tramitando_report_document("RELATORIO.pdf")
    assert not _is_tramitando_report_document("RELATORIO_19218.pdf")
    assert not _is_tramitando_report_document("RELATORIO_19218_X.pdf")


@pytest.mark.parametrize(
    ("numero", "status", "expected"),
    [
        ("P100001/2024", "TRAMITANDO", True),
        ("P100002/2024", "FINALIZADO", True),
        ("P100003/2023", "FINALIZADO", False),
        ("P100004/2026", "CANCELADO", False),
    ],
)
def test_report_process_filter_accepts_both_statuses_since_2024(
    numero: str,
    status: str,
    expected: bool,
) -> None:
    process = SpuProcessSummary(
        numero_processo=numero,
        status_processo=status,
        tipo_processo_assunto=None,
        data_abertura=None,
        motivo_finalizacao=None,
        url_visualizacao=None,
    )

    assert _is_report_process_eligible(process) is expected


def test_process_list_reports_expired_session_to_orchestrator() -> None:
    class LoginPage:
        url = ""

        def goto(self, *_args: object, **_kwargs: object) -> None:
            self.url = "https://spuvirtual.sepog.fortaleza.ce.gov.br/auth/login"

    settings = SimpleNamespace(
        portal_origin="https://spuvirtual.sepog.fortaleza.ce.gov.br",
        page_timeout_seconds=90,
    )
    client = SpuPortalClient(  # type: ignore[arg-type]
        settings,
        downloads_dir=Path("."),
    )

    with pytest.raises(SpuSessionExpiredError, match="renovacao humana"):
        client._open_process_list(LoginPage())  # type: ignore[arg-type]
    assert not hasattr(client, "_login")


def test_process_search_opens_current_portal_panel() -> None:
    class Element:
        def __init__(self, visible=False, on_click=None):
            self.visible = visible
            self.on_click = on_click
            self.clicks = 0

        def is_visible(self):
            return self.visible

        def is_disabled(self):
            return False

        def click(self):
            self.clicks += 1
            if self.on_click:
                self.on_click()

        def wait_for(self, *, state, timeout):
            assert state == "visible"
            assert timeout == 1000
            assert self.visible

    class Locator:
        def __init__(self, elements):
            self.elements = elements

        def count(self):
            return len(self.elements)

        def nth(self, index):
            return self.elements[index]

        @property
        def first(self):
            return self.elements[0]

    process_input = Element()
    trigger = Element(
        visible=True,
        on_click=lambda: setattr(process_input, "visible", True),
    )

    class Page:
        def locator(self, selector):
            if selector.startswith("[data-react-class="):
                return Locator([process_input])
            if selector == "#step-geral-search":
                return Locator([trigger])
            return Locator([])

    client = SpuPortalClient(  # type: ignore[arg-type]
        SimpleNamespace(page_timeout_seconds=1),
        downloads_dir=Path("."),
    )

    assert client._process_search_input(Page()) is process_input  # type: ignore[arg-type]
    assert trigger.clicks == 1


def test_process_search_selects_exact_ant_option() -> None:
    class SearchInput:
        def get_attribute(self, name):
            assert name == "role"
            return "combobox"

    class Option:
        def __init__(self, text):
            self.text = text
            self.clicks = 0

        def is_visible(self):
            return True

        def inner_text(self):
            return self.text

        def click(self):
            self.clicks += 1

    class Locator:
        def __init__(self, elements):
            self.elements = elements

        def count(self):
            return len(self.elements)

        def nth(self, index):
            return self.elements[index]

    other = Option("P248910/2026")
    expected = Option("P248911/2026")

    class Page:
        def locator(self, selector):
            assert selector == ".ant-select-item-option"
            return Locator([other, expected])

        def wait_for_function(self, script, *, arg, timeout):
            assert ".ant-select-item-option" in script
            assert arg == "P248911/2026"
            assert timeout == 1000

    client = SpuPortalClient(  # type: ignore[arg-type]
        SimpleNamespace(page_timeout_seconds=1),
        downloads_dir=Path("."),
    )

    assert client._select_process_combobox_option(  # type: ignore[arg-type]
        Page(),
        SearchInput(),
        "P248911_2026",
    )
    assert other.clicks == 0
    assert expected.clicks == 1


def test_process_search_waits_for_stable_missing_result() -> None:
    class Page:
        def wait_for_function(self, script, *, arg, timeout):
            assert "__spuMissingSearchResult" in script
            assert "location.href !== state.previousUrl" in script
            assert "querySelectorAll('.card')" in script
            assert "[id=\"step2-list-num\"]" in script
            assert "navigated && matchingCard" in script
            assert "Date.now() - marker.since" in script
            assert arg == {
                "number": "P119449/2026",
                "previousUrl": (
                    "https://spuvirtual.sepog.fortaleza.ce.gov.br/"
                    "processos/usuario"
                ),
                "stabilityMs": 5000,
            }
            assert timeout == 90000

    client = SpuPortalClient(  # type: ignore[arg-type]
        SimpleNamespace(page_timeout_seconds=90),
        downloads_dir=Path("."),
    )

    client._wait_for_process_search_result(  # type: ignore[arg-type]
        Page(),
        "P119449/2026",
        previous_url=(
            "https://spuvirtual.sepog.fortaleza.ce.gov.br/processos/usuario"
        ),
    )


def test_profile_lock_rejects_simultaneous_browser(tmp_path: Path) -> None:
    profile_dir = tmp_path / "browser_profile"

    with spu_profile_lock(profile_dir):
        with pytest.raises(SpuProfileInUseError, match="ja esta em uso"):
            with spu_profile_lock(profile_dir):
                pass

    with spu_profile_lock(profile_dir):
        pass


def test_profile_lock_removes_stale_chromium_singletons(
    tmp_path: Path,
) -> None:
    profile_dir = tmp_path / "browser_profile"
    profile_dir.mkdir()
    singleton_names = ("SingletonCookie", "SingletonLock", "SingletonSocket")
    for name in singleton_names:
        (profile_dir / name).symlink_to(f"stale-{name}")

    with spu_profile_lock(profile_dir):
        assert all(
            not (profile_dir / name).is_symlink()
            for name in singleton_names
        )


MATERIALIZER = "https://spumaterializar.sepog.fortaleza.ce.gov.br"
PREVIEW = (
    "/v2/materializacoes/P298530_2026/contextos/e652/itens/__ITEM_ID__"
)


def test_document_url_uses_v2_preview_path_when_tree_has_one() -> None:
    assert document_download_url(
        MATERIALIZER,
        numero_ano="P298530_2026",
        document_id="51778574",
        preview_path=PREVIEW,
    ) == (
        f"{MATERIALIZER}/v2/materializacoes/P298530_2026/contextos/e652/"
        "itens/51778574"
    )
    # Trees without previewPath keep the previous materializer route.
    assert document_download_url(
        MATERIALIZER, numero_ano="P298530_2026", document_id="51778574"
    ) == (
        f"{MATERIALIZER}/processos/P298530_2026/documento_so/51778574/"
        "pdf?download_file=true"
    )


def _finalized_process() -> SpuProcessSummary:
    return SpuProcessSummary(
        numero_processo="P298530/2026",
        status_processo="FINALIZADO",
        tipo_processo_assunto="Pagamento",
        data_abertura=date(2026, 7, 1),
        motivo_finalizacao="Processo pago",
        url_visualizacao=(
            "https://spuvirtual.sepog.fortaleza.ce.gov.br/processos/x/"
            "materializar_v2"
        ),
    )


def test_documents_are_downloaded_through_the_v2_preview_path() -> None:
    client = SpuPortalClient.__new__(SpuPortalClient)
    client._page = object()
    tree = {
        "processo": {
            "numeroAno": "P298530_2026",
            "previewPath": PREVIEW,
            "itens": {
                "setores": [
                    {
                        "setor": "IPM/SAUDECOGESTAO",
                        "documentos": [{"id": 7, "nome": "Demonstrativo"}],
                    }
                ]
            },
        }
    }
    calls = []
    client._open_folder_tree = lambda page, process: tree
    client._download_pdf = lambda process, **kw: calls.append(kw) or Path("x")
    client.download_process_documents(_finalized_process())
    assert calls == [{
        "numero_ano": "P298530_2026",
        "sector": "IPM/SAUDECOGESTAO",
        "document_id": "7",
        "name": "Demonstrativo",
        "preview_path": PREVIEW,
    }]


def test_folder_tree_clears_only_materializer_cookies_before_opening() -> None:
    events = []

    class Element:
        first = None

        def __init__(self, present):
            self.present = present
            self.first = self

        def count(self):
            return 1 if self.present else 0

        def wait_for(self, **_kw):
            return None

        def get_attribute(self, _name):
            return '{"dataTree": {"processo": {"itens": {"setores": []}}}}'

    class Page:
        url = MATERIALIZER + "/v2/materializacoes/P298530_2026"

        def goto(self, url, **_kw):
            events.append(("goto", url))

        def locator(self, selector):
            return Element("FolderProcessoSo" in selector)

    class Context:
        def clear_cookies(self, **kw):
            events.append(("clear_cookies", kw))

    client = SpuPortalClient.__new__(SpuPortalClient)
    client.settings = SimpleNamespace(
        materializer_origin=MATERIALIZER,
        portal_origin="https://spuvirtual.sepog.fortaleza.ce.gov.br",
        page_timeout_seconds=5,
    )
    client._context = Context()
    client._is_login_page = lambda page: False
    tree = client._open_folder_tree(Page(), _finalized_process())
    assert tree == {"processo": {"itens": {"setores": []}}}
    assert events[0] == (
        "clear_cookies",
        {"domain": "spumaterializar.sepog.fortaleza.ce.gov.br"},
    )
    assert events[1][0] == "goto"
