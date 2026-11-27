from decimal import Decimal

from nfs_fortaleza.issec_notas_fiscais import (
    NotasFiscaisIssec,
    colunas_das_pagas,
    parse_detalhe_nota,
    parse_notas_pagas,
    parse_notas_pendentes,
)
from nfs_fortaleza.issec_portal import IssecPortalClient


def _dialogo(caminho):
    return f"javascript&#x3A;apex.theme42.dialog&#x28;&#x27;{caminho}&#x27;,&#x7B;&#x7D;&#x29;"


DETALHE = "&#x5C;u002Fords&#x5C;u002Fr&#x5C;u002Faplicacoes&#x5C;u002Fportal-credenciado&#x5C;u002Fdetalhes-da-nota&#x3F;nu_grupo&#x3D;{nup}&#x5C;u0026session&#x3D;1"
ENVIAR = "&#x5C;u002Fords&#x5C;u002Fr&#x5C;u002Faplicacoes&#x5C;u002Fportal-credenciado&#x5C;u002Fenviar-nota-fiscal&#x3F;nu_nup&#x3D;{nup}"


def _pendente(nup, processo, valor, enviar_texto="", agrupado=True):
    # Sem grupo, o portal deixa o link do detalhe sem o texto ANALISAR.
    analisar = f'<a href="{_dialogo(DETALHE.format(nup=nup))}" >{"ANALISAR" if agrupado else ""}</a>'
    return (
        f'<tr><td class="t-Report-cell" headers="NU_NUP">{nup}</td>'
        f'<td class="t-Report-cell" headers="NU_ORDEM">{processo}</td>'
        f'<td class="t-Report-cell" headers="NU_VALOR">{valor}</td>'
        f'<td class="t-Report-cell" headers="DERIVED$01"><a href="{_dialogo(ENVIAR.format(nup=nup))}" >{enviar_texto}</a></td>'
        f'<td class="t-Report-cell" headers="DERIVED$03">{analisar}</td>'
        f'<td class="t-Report-cell" headers="DERIVED$02"><button class="ver-nota-fiscal"></button></td></tr>'
    )


CABECALHO_PAGAS = (
    '<table class="a-IRR-table"><tr>'
    '<th class="a-IRR-header u-tC" id="C1" ><a>Processo</a></th>'
    '<th class="a-IRR-header u-tC" id="C2" ><a>NUP</a></th>'
    '<th class="a-IRR-header u-tC" id="C3" ><a>Valor Pago</a></th>'
    '<th class="a-IRR-header u-tC" id="#TIPO_PROCESSO#" ><a>Tipo Processo</a></th></tr>'
)


def _paga(processo, nup, valor, tipo):
    tipo_html = f'<a href="{_dialogo(DETALHE.format(nup=nup))}" class="{tipo}">{tipo}</a>'
    return (
        f'<tr ><td class=" u-tC" headers="C1" >{processo}</td><td class=" u-tC" headers="C2" >{nup}</td>'
        f'<td class=" u-tC" headers="C3" >{valor}</td><td class=" u-tC" headers="#TIPO_PROCESSO#" >{tipo_html}</td></tr>'
    )


def _membro(processo, nup, valor):
    return (
        f'<tr><td class="t-Report-cell" headers="NU_ORDEM">{processo}</td>'
        f'<td class="t-Report-cell" headers="NU_NUP">{nup}</td><td class="t-Report-cell" headers="NU_VALOR">{valor}</td></tr>'
    )


def test_pendentes_enviar_indica_nf_a_enviar_e_analisar_indica_grupo():
    html = "<table>" + _pendente("46042045616202661", "2600045616", "R$4.957,47") + _pendente(
        "46042045700202600", "2600045700", "R$100,00", enviar_texto="ENVIAR", agrupado=False
    ) + "</table>"

    enviada, a_enviar = parse_notas_pendentes(html)

    assert enviada["nup"] == "46042045616202661" and enviada["processo"] == "2600045616"
    assert enviada["valor"] == Decimal("4957.47")
    assert enviada["nf_a_enviar"] is False and enviada["agrupado"] is True
    assert enviada["detalhe_path"] == "/ords/r/aplicacoes/portal-credenciado/detalhes-da-nota?nu_grupo=46042045616202661&session=1"
    assert a_enviar["nf_a_enviar"] is True and a_enviar["agrupado"] is False


def test_pagas_pelo_rotulo_das_colunas():
    html = CABECALHO_PAGAS + _paga("2600045611", "46042045611202639", "R$615.664,91", "AGRUPADOR") + _paga(
        "2600040383", "46042040383202619", "R$41.241,90", "NORMAL") + "</table>"
    colunas = colunas_das_pagas(html)

    agrupador, normal = parse_notas_pagas(html, colunas)

    assert colunas == {"C1": "processo", "C2": "nup", "C3": "valor_pago", "#TIPO_PROCESSO#": "tipo"}
    assert agrupador["valor_pago"] == Decimal("615664.91") and agrupador["tipo"] == "AGRUPADOR"
    assert agrupador["detalhe_path"].endswith("nu_grupo=46042045611202639&session=1")
    assert normal["tipo"] == "NORMAL" and normal["detalhe_path"] is None


def test_detalhe_lista_os_processos_do_grupo():
    html = "<table>" + _membro("2600041617", "46042041617202637", "R$162,00") + _membro(
        "2600038836", "46042038836202639", "R$105,17") + "</table>"
    assert parse_detalhe_nota(html) == [
        {"processo": "2600041617", "nup": "46042041617202637", "valor": Decimal("162.00")},
        {"processo": "2600038836", "nup": "46042038836202639", "valor": Decimal("105.17")},
    ]


class FakeResponse:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        pass


APEX = (
    '<input id="pFlowId" value="112"><input id="pFlowStepId" value="44"><input id="pInstance" value="1">'
    '<input id="pSalt" value="s">'
)


def test_le_todas_as_paginas_e_detalha_so_grupos_novos(monkeypatch):
    client = IssecPortalClient("usuario", "senha")
    client.session_id = "1"
    pagina = (
        APEX + "<table>" + _pendente("46042000000000001", "2600000001", "R$10,00") + "</table>"
        + '<script>interactiveReport({"regionId":"R9","maxRowCount":"1","ajaxIdentifier":"AJ"})</script>'
        + '<input id="R9_worksheet_id" value="W"><input id="R9_report_id" value="REP">'
        + CABECALHO_PAGAS + _paga("2600000002", "46042000000000002", "R$20,00", "AGRUPADOR")
        + '</table><button title="Next" data-pagination="pgR_min_row=51max_rows=50rows_fetched=50"></button>'
    )
    pagina2 = CABECALHO_PAGAS + _paga("2600000003", "46042000000000003", "R$30,00", "AGRUPADOR") + _paga(
        "2600000004", "46042000000000004", "R$40,00", "NORMAL") + "</table>"
    detalhes = {
        "46042000000000001": "<table>" + _membro("2600000011", "1", "R$5,00") + "</table>",
        "46042000000000003": "<table>" + _membro("2600000031", "3", "R$30,00") + "</table>",
    }
    pedidos = []

    def fake_get(caminho):
        pedidos.append(caminho)
        if caminho.startswith("r/aplicacoes/portal-credenciado/44-notas-fiscais"):
            return FakeResponse(pagina)
        nup = caminho.split("nu_grupo=")[1].split("&")[0]
        return FakeResponse(detalhes[nup])

    ajax = []
    monkeypatch.setattr(client, "_get", fake_get)
    leitor = NotasFiscaisIssec(client, max_workers=1)
    monkeypatch.setattr(leitor, "_ajax", lambda contexto, referer, data: ajax.append(data) or pagina2)

    resultado = leitor.ler(grupos_pagos_conhecidos={"46042000000000002"})

    assert [p["processo"] for p in resultado["pagas"]] == ["2600000002", "2600000003", "2600000004"]
    assert ajax[0]["p_widget_action_mod"] == "pgR_min_row=51max_rows=50rows_fetched=50"
    assert ajax[0]["x01"] == "W" and ajax[0]["x02"] == "REP"
    # Grupo pago já conhecido (…002) não é detalhado de novo.
    assert not any("46042000000000002" in p for p in pedidos)
    processos = {(p["nup_grupo"], p["processo"], p["situacao"]) for p in resultado["processos"]}
    assert processos == {("46042000000000001", "2600000011", "pendente"), ("46042000000000003", "2600000031", "paga")}
    assert all("detalhe_path" not in p for p in resultado["pendentes"] + resultado["pagas"])
