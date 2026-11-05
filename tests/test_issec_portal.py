import ast
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from nfs_fortaleza import issec_portal
from nfs_fortaleza.issec_portal import (
    IssecPortalClient,
    IssecPortalError,
    extract_and_load_processos,
    parse_brl,
    parse_process_detail,
    parse_process_links,
    select_months_to_scrape,
)


def test_parse_brl():
    assert parse_brl("R$1.633,06") == Decimal("1633.06")
    assert parse_brl("R$0,00") == Decimal("0.00")


def test_parse_process_links():
    html = """
    <a href="javascript:apex.theme42.dialog('\\u002Fords\\u002Frelnumproc?nu_proc=2600003585\\u0026session=1',{})"></a>
    """
    assert parse_process_links(html) == [
        ("2600003585", "/ords/relnumproc?nu_proc=2600003585&session=1")
    ]


def test_parse_process_detail():
    html = """
    <table>
      <tr><th>Processo:</th><th>Mês de Produção:</th></tr>
      <tr><td>2600003585</td><td>01/01/2026</td></tr>
      <tr><th>Valor Cobrado(R$):</th><th>Valor Liberado(R$):</th></tr>
      <tr><td>R$1.633,06</td><td>R$1.580,74</td></tr>
      <tr><th>Valor Serviço(R$):</th><th>Valor Total de Ítens Glosados:</th></tr>
      <tr><td>R$1.580,74</td><td>R$52,32</td></tr>
    </table>
    """
    assert parse_process_detail(html, "2600003585") == {
        "processo": "2600003585",
        "mes_producao": date(2026, 1, 1),
        "valor_cobrado": Decimal("1633.06"),
        "valor_liberado": Decimal("1580.74"),
        "valor_servico": Decimal("1580.74"),
        "valor_total_itens_glosados": Decimal("52.32"),
    }


def test_select_months_reprocessa_tres_ultimos_e_inclui_ausentes():
    extracted = {
        date(2026, 1, 1),
        date(2026, 2, 1),
        date(2026, 4, 1),
        date(2026, 5, 1),
        date(2026, 6, 1),
        date(2026, 7, 1),
    }
    assert select_months_to_scrape(date(2026, 10, 8), extracted) == [
        date(2026, 3, 1),
        date(2026, 8, 1),
        date(2026, 9, 1),
        date(2026, 10, 1),
    ]


def test_detalhe_de_outro_processo_e_recusado():
    html = """
    <table>
      <tr><th>Processo:</th><th>Mês de Produção:</th></tr>
      <tr><td>2600000001</td><td>01/01/2026</td></tr>
    </table>
    """
    with pytest.raises(IssecPortalError):
        parse_process_detail(html, "2600003585")


def test_listagem_percorre_todas_as_paginas(monkeypatch):
    client = IssecPortalClient("usuario", "senha")
    monkeypatch.setattr(
        client, "_report_page", lambda month: ("rel", object(), "html")
    )
    pages = {
        None: [(f"26{index:08d}", f"/p{index}") for index in range(50)],
        51: [("2600000099", "/p99")],
    }
    monkeypatch.setattr(client, "_report_ajax", lambda *a, first_row=None: first_row)
    monkeypatch.setattr(issec_portal, "parse_process_links", pages.__getitem__)

    processes = client.list_processes(date(2026, 9, 1))

    assert len(processes) == 51
    assert processes[-1] == ("2600000099", "/p99")


def test_carga_mantem_pipeline_e_tabela_lidos_pela_api(monkeypatch):
    calls = {}

    class FakePipeline:
        def run(self, source):
            calls["source"] = source

    def fake_pipeline(**kwargs):
        calls["pipeline"] = kwargs
        return FakePipeline()

    monkeypatch.setattr(issec_portal.dlt, "pipeline", fake_pipeline)
    monkeypatch.setattr(
        issec_portal,
        "portal_issec_source",
        lambda **kwargs: kwargs,
    )
    monkeypatch.delenv("DESTINATION__POSTGRES__CREDENTIALS", raising=False)

    result = extract_and_load_processos(
        username="usuario",
        password="senha",
        database_url="postgresql://destino",
        extracted_months=set(issec_portal.month_range(date(2026, 1, 1), date(2026, 8, 1))),
        today=date(2026, 11, 5),
    )

    assert calls["pipeline"] == {
        "pipeline_name": "portal_issec_processos",
        "dataset_name": "raw_issec_portal",
        "destination": "postgres",
    }
    assert calls["source"]["months"] == [
        date(2026, 9, 1),
        date(2026, 10, 1),
        date(2026, 11, 1),
    ]
    assert result == {"competencias": ["09/2026", "10/2026", "11/2026"]}
    assert issec_portal.os.environ["DESTINATION__POSTGRES__CREDENTIALS"] == (
        "postgresql://destino"
    )


def test_sem_meses_pendentes_nao_abre_pipeline(monkeypatch):
    monkeypatch.setattr(issec_portal, "select_months_to_scrape", lambda *a: [])
    monkeypatch.setattr(
        issec_portal.dlt,
        "pipeline",
        lambda **kwargs: pytest.fail("não deveria carregar"),
    )

    result = extract_and_load_processos(
        username="usuario",
        password="senha",
        database_url="postgresql://destino",
        extracted_months=set(),
    )

    assert result == {"competencias": []}


def test_dag_declara_id_agenda_e_conexao_oficial():
    source = (
        Path(__file__).parents[1] / "dags" / "extracao_processos_portal_issec.py"
    ).read_text(encoding="utf-8")
    strings = {
        node.value
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assert {
        "extracao_processos_portal_issec",
        "0 5 * * *",
        "postgres_prontocardio",
    } <= strings
    assert "postgres_receita_certa" not in strings
