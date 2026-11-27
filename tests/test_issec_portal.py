import json
import ast
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from nfs_fortaleza import issec_portal
from nfs_fortaleza.issec_portal import (
    contas_paths,
    parse_guias,
    IssecPortalClient,
    IssecPortalError,
    extract_and_load_processos,
    parse_brl,
    portal_issec_source,
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
    detalhe = parse_process_detail(html, "2600003585")
    campos = json.loads(detalhe.pop("campos_detalhe"))
    assert detalhe.pop("nup_grupo") is None
    assert campos["avisos"] == []
    assert detalhe == {
        "processo": "2600003585",
        "mes_producao": date(2026, 1, 1),
        "valor_cobrado": Decimal("1633.06"),
        "valor_liberado": Decimal("1580.74"),
        "valor_servico": Decimal("1580.74"),
        "valor_total_itens_glosados": Decimal("52.32"),
    }
    assert campos["pares"]["Valor Liberado(R$):"] == "R$1.580,74"
    assert campos["linhas"][0] == ["Processo:", "Mês de Produção:"]


def test_parse_process_detail_guarda_campos_sem_coluna():
    html = """
    <table>
      <tr><th>Processo:</th><th>Mês de Produção:</th></tr>
      <tr><td>2600003772</td><td>01/01/2026</td></tr>
      <tr><th>Valor Cobrado(R$):</th><th>Valor Liberado(R$):</th></tr>
      <tr><td>R$7.190,00</td><td>R$7.190,00</td></tr>
      <tr><th>IRRF(R$):</th><th>ISS(R$):</th></tr>
      <tr><td>R$107,85</td><td>R$194,13</td></tr>
      <tr><td>Observação livre</td></tr>
    </table>
    """
    campos = json.loads(parse_process_detail(html, "2600003772")["campos_detalhe"])
    assert campos["pares"]["IRRF(R$):"] == "R$107,85"
    assert campos["pares"]["ISS(R$):"] == "R$194,13"
    assert ["Observação livre"] in campos["linhas"]


def test_select_months_comeca_em_2025():
    """Os protocolos de 2025 também são conferidos pelo portal (pedido de 26/11/2026)."""
    jan_2025_a_dez_2025 = {date(2025, m, 1) for m in range(1, 13)}
    meses = select_months_to_scrape(date(2026, 2, 10), set())
    assert meses[0] == date(2025, 1, 1)
    assert len(meses) == 14
    # Já extraídos: só os ausentes e os três últimos (reprocessados).
    assert select_months_to_scrape(date(2026, 2, 10), jan_2025_a_dez_2025) == [
        date(2025, 12, 1),
        date(2026, 1, 1),
        date(2026, 2, 1),
    ]


def test_select_months_reprocessa_tres_ultimos_e_inclui_ausentes():
    extracted = {
        *{date(2025, m, 1) for m in range(1, 13)},
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
    notas = {"pendentes": [], "pagas": [{"nup": "1", "extraido_em": "agora"}], "processos": []}
    monkeypatch.setattr(
        "nfs_fortaleza.issec_notas_fiscais.NotasFiscaisIssec.ler",
        lambda self, conhecidas: calls.setdefault("notas", notas),
    )

    result = extract_and_load_processos(
        username="usuario",
        password="senha",
        database_url="postgresql://destino",
        extracted_months=set(issec_portal.month_range(date(2025, 1, 1), date(2026, 8, 1))),
        authenticate=lambda client: calls.setdefault("client", client),
        today=date(2026, 11, 5),
    )

    assert calls["pipeline"] == {
        "pipeline_name": "portal_issec_processos",
        "dataset_name": "raw_issec_portal",
        "destination": "postgres",
    }
    assert calls["source"]["client"] is calls["client"]
    assert calls["source"]["months"] == [
        date(2026, 9, 1),
        date(2026, 10, 1),
        date(2026, 11, 1),
    ]
    assert calls["source"]["notas_fiscais"] is notas
    assert notas["pagas"][0]["primeira_extracao_em"] == "agora"
    assert result["competencias"] == ["09/2026", "10/2026", "11/2026"]
    assert result["notas_fiscais"] == {"pendentes": 0, "a_enviar": 0, "pagas": 1, "processos_de_grupos": 0}
    assert issec_portal.os.environ["DESTINATION__POSTGRES__CREDENTIALS"] == (
        "postgresql://destino"
    )


def test_sem_meses_pendentes_ainda_le_as_notas_fiscais(monkeypatch):
    # As notas fiscais mudam todo dia: o login é pedido mesmo sem mês a ler.
    monkeypatch.setattr(issec_portal, "select_months_to_scrape", lambda *a: [])
    login, carregado = [], {}
    monkeypatch.setattr(
        "nfs_fortaleza.issec_notas_fiscais.NotasFiscaisIssec.ler",
        lambda self, conhecidas: {"pendentes": [], "pagas": [], "processos": []},
    )

    class FakePipeline:
        def run(self, source):
            carregado["source"] = source

    monkeypatch.setattr(issec_portal.dlt, "pipeline", lambda **kw: FakePipeline())
    monkeypatch.setattr(issec_portal, "portal_issec_source", lambda **kwargs: kwargs)

    result = extract_and_load_processos(
        username="usuario",
        password="senha",
        database_url="postgresql://destino",
        extracted_months=set(),
        authenticate=login.append,
    )

    assert len(login) == 1
    assert carregado["source"]["months"] == []
    assert result["competencias"] == []

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
        "0 8,12,16 * * 1-5",
        "nfse_portal",
        "postgres_prontocardio",
    } <= strings
    assert "postgres_receita_certa" not in strings


def test_fonte_percorre_os_meses_com_a_sessao_recebida():
    class FakeClient:
        def scrape_month(self, month):
            return [{"processo": month.strftime("%Y%m")}]

    source = portal_issec_source(
        client=FakeClient(),
        months=[date(2026, 9, 1), date(2026, 10, 1)],
    )

    assert source.name == "portal_issec"
    assert list(source.resources["processos"]) == [
        {"processo": "202609"},
        {"processo": "202610"},
    ]


def test_cliente_adota_cookies_da_sessao_humana():
    client = IssecPortalClient("usuario", "senha")

    client.adopt_session(
        [
            {
                "name": "ORA_WWV_APP_112",
                "value": "abc",
                "domain": "144.22.149.116",
                "path": "/ords/",
            }
        ],
        "123",
    )

    assert client.session_id == "123"
    assert client.session.cookies.get("ORA_WWV_APP_112") == "abc"


def test_sessao_vazia_e_recusada():
    with pytest.raises(IssecPortalError):
        IssecPortalClient("usuario", "senha").adopt_session([], "")


def test_consulta_sem_login_humano_nao_tenta_autenticar():
    with pytest.raises(IssecPortalError):
        IssecPortalClient("usuario", "senha").list_processes(date(2026, 9, 1))


def test_parse_process_detail_le_o_nup_do_grupo():
    html = """
    <h2>Relatório de Consulta de Processos: 2600036705</h2>
    <div class="aviso"><span>&#9888;</span>
      Este processo faz parte do grupo com NUP: 46042042895202610</div>
    <table>
      <tr><th>Processo:</th><th>Mês de Produção:</th></tr>
      <tr><td>2600036705</td><td>01/08/2026</td></tr>
      <tr><th>Valor Cobrado(R$):</th><th>Valor Liberado(R$):</th></tr>
      <tr><td>R$2.118,58</td><td>R$2.118,58</td></tr>
    </table>
    """
    detalhe = parse_process_detail(html, "2600036705")
    assert detalhe["nup_grupo"] == "46042042895202610"
    campos = json.loads(detalhe["campos_detalhe"])
    assert campos["avisos"] == [
        "Este processo faz parte do grupo com NUP: 46042042895202610"
    ]


EM_ANALISE_HTML = """
<table>
  <tr><th>Processo:</th><th>Cidade:</th><th>Mês de Produção:</th><th>Entrada no Protocolo:</th><th>Tipo:</th></tr>
  <tr><td>2600041925</td><td></td><td></td><td></td><td></td></tr>
  <tr><th>Valor Cobrado(R$):</th><th>Valor Liberado(R$):</th><th>Situação:</th><th>Motivo da Situação:</th></tr>
  <tr><td>R$9.828,44</td><td></td><td></td><td>Em Análise</td></tr>
</table>
"""


def test_detalhe_em_analise_sem_mes_de_producao():
    """Caso 2600041925 (remessa 20618): o portal só preenche o mês depois da análise."""
    with pytest.raises(IssecPortalError):
        parse_process_detail(EM_ANALISE_HTML, "2600041925")

    detalhe = parse_process_detail(EM_ANALISE_HTML, "2600041925", exigir_mes_producao=False)
    assert detalhe["mes_producao"] is None
    assert detalhe["valor_cobrado"] == Decimal("9828.44")
    assert detalhe["valor_liberado"] is None
    assert json.loads(detalhe["campos_detalhe"])["pares"]["Motivo da Situação:"] == "Em Análise"


def test_busca_por_numero_le_o_processo_em_analise(monkeypatch):
    client = IssecPortalClient("usuario", "senha")
    client.session_id = "1"
    consulta = issec_portal.HtmlPage()

    class Resposta:
        def __init__(self, texto="", redirect=""):
            self.text, self._redirect = texto, redirect

        def json(self):
            return {"redirectURL": self._redirect}

    enviados = []
    monkeypatch.setattr(issec_portal, "parse_html", lambda html: consulta if html == "consulta" else issec_portal.HtmlPage())
    monkeypatch.setattr(client, "_get", lambda path: Resposta("consulta" if "consulta" in path else EM_ANALISE_HTML))
    monkeypatch.setattr(client, "_submit", lambda page, request, items, referer: enviados.append((request, items)) or Resposta(
        redirect="javascript:apex.theme42.dialog('/ords/r/aplicacoes/portal-credenciado/relnumproc?nu_proc=2600041925&session=1')"))
    monkeypatch.setattr(issec_portal, "parse_process_detail", lambda html, numero, exigir_mes_producao=True: {
        "processo": numero, "mes_producao": None, "valor_cobrado": Decimal("9828.44"), "valor_liberado": None})

    row = client.lookup_process("2600041925")

    assert enviados == [("bt-PesqNum", [("P19_NU_PROC", "2600041925"), ("P19_PROCMES", ""), ("P19_PROCANO", "")])]
    assert row["processo"] == "2600041925"
    assert row["competencia"] is None


def test_fonte_le_por_numero_os_registrados_fora_da_busca_por_periodo():
    class FakeClient:
        def scrape_month(self, month):
            return [{"processo": "2600000001", "mes_producao": month}]

        def lookup_process(self, numero):
            if numero == "2600099999":
                return None
            return {"processo": numero, "mes_producao": None}

    resumo = {}
    source = portal_issec_source(
        client=FakeClient(), months=[date(2026, 10, 1)], notas_fiscais={"pendentes": [], "pagas": [], "processos": []},
        processos_por_numero=["2600000001", "2600041925", "2600099999"], resumo_por_numero=resumo,
    )
    rows = list(source.resources["processos"])

    assert [r["processo"] for r in rows] == ["2600000001", "2600041925"]
    assert resumo == {"consultados": 2, "encontrados": 1, "em_analise": 1}


DETALHE_COM_CONTAS = (
    "<button onclick=\"apex.theme42.dialog('&#x5C;u002Fords&#x5C;u002Fr&#x5C;u002Faplicacoes"
    "&#x5C;u002Fportal-credenciado&#x5C;u002Fdetalhe-de-contas-geral?p34_nu_ordem=2600002999"
    "&#x5C;u0026p34_tp_tratamento=EXAMES&#x5C;u002520RADIOLOGICOS&#x5C;u0026session=1&#x5C;u0026cs=abc',{})\">"
    "Detalhes de Contas</button>"
    "<button onclick=\"apex.theme42.dialog('&#x5C;u002Fords&#x5C;u002Fr&#x5C;u002Faplicacoes"
    "&#x5C;u002Fportal-credenciado&#x5C;u002Fdetalhamento-de-grupo?p46_nu_grupo=1&#x5C;u0026session=1',{})\">"
    "Grupo</button>"
)

# Nomes fictícios: a coluna do beneficiário nunca pode ser gravada.
CONTAS_HTML = """
<table>
  <tr><td></td><td>(Qtde)</td><td>Valor(R$)</td></tr>
  <tr><td>01/12/2025.</td><td>0057P2P18</td><td>FULANO DE TAL-00000000000</td><td>40808033-RX TORAX</td><td>1</td><td>15,59</td></tr>
  <tr><td>01/12/2025.</td><td>0052ZDQIF</td><td>BELTRANA DE TAL-00000000001</td><td>40808041-RX OUTRO</td><td>1</td><td>16,16</td></tr>
  <tr><td>Total de Procedimentos:</td><td>2</td><td>R$ 31,75</td></tr>
</table>
"""


def test_paginas_de_contas_do_detalhe():
    assert contas_paths(DETALHE_COM_CONTAS) == [
        "r/aplicacoes/portal-credenciado/detalhe-de-contas-geral?p34_nu_ordem=2600002999"
        "&p34_tp_tratamento=EXAMES%20RADIOLOGICOS&session=1&cs=abc"
    ]


def test_guias_sem_o_beneficiario():
    guias = parse_guias(CONTAS_HTML)
    assert guias == [
        {"senha": "0057P2P18", "data": "01/12/2025", "procedimento": "40808033", "qtd": "1", "valor": "15.59"},
        {"senha": "0052ZDQIF", "data": "01/12/2025", "procedimento": "40808041", "qtd": "1", "valor": "16.16"},
    ]
    assert "FULANO" not in json.dumps(guias)


def test_detalhe_grava_as_guias_em_json(monkeypatch):
    client = IssecPortalClient("usuario", "senha")

    class Resposta:
        def __init__(self, texto):
            self.text = texto

        def raise_for_status(self):
            return None

    class Sessao:
        cookies = {}

        def get(self, url, timeout):
            return Resposta(CONTAS_HTML if "contas" in url else DETALHE_COM_CONTAS)

    monkeypatch.setattr(client, "_new_session", Sessao)
    monkeypatch.setattr(issec_portal, "parse_process_detail", lambda html, numero: {"processo": numero})

    row = client._detail(("2600002999", "/detalhe"))

    assert [g["senha"] for g in json.loads(row["guias"])] == ["0057P2P18", "0052ZDQIF"]
