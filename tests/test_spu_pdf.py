from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

import nfs_fortaleza.spu_pdf as spu_pdf
from nfs_fortaleza.spu_pdf import (
    _is_report_atendimento_header,
    _parse_report_competencia,
    parse_legacy_saude_cogestao_pages,
    parse_nuexo_text,
    parse_saude_cogestao_pages,
    parse_tramitando_report_pages,
)
from nfs_fortaleza.spu_portal import SpuDocument


@pytest.mark.parametrize(
    "label",
    (
        "Competência: 07/2026",
        "Competência: 0712026",
        "Compotênciar 0712026",
    ),
)
def test_parses_report_competence_with_mv_font_variations(label: str) -> None:
    assert _parse_report_competencia(label) == date(2026, 7, 1)


@pytest.mark.parametrize("label", ("Atendimento", "Alêndimento"))
def test_recognizes_report_attendance_header_with_mv_font_variations(
    label: str,
) -> None:
    assert _is_report_atendimento_header(label)


SUMMARY = [
    ["Nome Prestador", "Hospital Pronto"],
    ["CNPJ", "12.345.678/0001-90"],
    [
        "Número Processo",
        "P193251/2026",
        "Competência TMS",
        "07/2026",
        "Dt Fechamento",
        "31/07/2026",
    ],
    [
        "Valor Informado",
        "R$ 1.234,56",
        "Valor Glosado",
        "34,56",
        "Valor Aprovado",
        "1.200,00",
    ],
    ["Comp de Produção", "06/2026"],
]
PROTOCOL_HEADER = [
    "NR",
    "Valor Protocolo",
    "Valor Glosado",
    "Valor Aprovado",
]


def test_parse_tramitando_report_table_extracts_follow_up_fields() -> None:
    pages = [(
        1,
        [[
            [
                "Remessa",
                "Nome do Paciente",
                "Guia",
                "Conta",
                "Atendimento",
                "Competência",
                "Valor",
            ],
            [
                "19218",
                "MARIA DA SILVA",
                "778899",
                "123456",
                "314159",
                "05/2026",
                "R$ 1.234,56",
            ],
        ]],
        "",
    )]

    rows = parse_tramitando_report_pages(
        pages,
        numero_processo="P335842/2026",
        documento_id="27877736",
        documento_nome="RELATORIO_19218_27877736.pdf",
    )

    assert len(rows) == 1
    assert rows[0]["cd_remessa"] == 19218
    assert rows[0]["nome_paciente"] == "MARIA DA SILVA"
    assert rows[0]["numero_guia"] == "778899"
    assert rows[0]["numero_conta"] == "123456"
    assert rows[0]["cd_atendimento"] == 314159
    assert rows[0]["competencia"] == date(2026, 5, 1)
    assert rows[0]["valor"] == Decimal("1234.56")


def test_parse_tramitando_report_uses_remessa_from_filename() -> None:
    text = """
    NOME DO PACIENTE: JOAO DE SOUZA
    GUIA: 9001
    CONTA: 7001
    ATENDIMENTO: 6001
    COMPETENCIA: 04/2026
    VALOR: 98,70
    """

    rows = parse_tramitando_report_pages(
        [(1, [], text)],
        numero_processo="P335842/2026",
        documento_id="27877736",
        documento_nome="RELATORIO_19218_27877736.pdf",
    )

    assert len(rows) == 1
    assert rows[0]["cd_remessa"] == 19218
    assert rows[0]["valor"] == Decimal("98.70")


def test_parse_tramitando_report_allows_competence_to_be_enriched() -> None:
    text = """
    NOME DO PACIENTE: JOAO DE SOUZA
    GUIA: 9001
    CONTA: 7001
    ATENDIMENTO: 6001
    VALOR: 98,70
    """

    rows = parse_tramitando_report_pages(
        [(1, [], text)],
        numero_processo="P335842/2026",
        documento_id="27877736",
        documento_nome="RELATORIO_19218_27877736.pdf",
    )

    assert len(rows) == 1
    assert rows[0]["competencia"] is None


def test_parse_tramitando_report_treats_corrupted_competence_as_missing() -> None:
    text = """
    NOME DO PACIENTE: JOAO DE SOUZA
    GUIA: 9001
    CONTA: 7001
    ATENDIMENTO: 6001
    COMPETENCIA: ; Entresa:2oto't2o.'
    VALOR: 98,70
    """

    rows = parse_tramitando_report_pages(
        [(1, [], text)],
        numero_processo="P335842/2026",
        documento_id="27877736",
        documento_nome="RELATORIO_19218_27877736.pdf",
    )

    assert len(rows) == 1
    assert rows[0]["competencia"] is None


def test_parse_saude_cogestao_joins_tables_across_pdf_pages() -> None:
    first = [
        "123",
        "1.100,00",
        "100,00",
        "1.000,00",
    ]
    second = [
        "124",
        "400,00",
        "10,00",
        "390,00",
    ]
    pages = [
        (1, [[*SUMMARY], [PROTOCOL_HEADER, first]], ""),
        (2, [[PROTOCOL_HEADER, second]], ""),
    ]

    rows = parse_saude_cogestao_pages(
        pages,
        numero_processo="P193251/2026",
        documento_id="doc-1",
        documento_nome="cogestao.pdf",
    )

    assert len(rows) == 2
    assert rows[0]["cnpj"] == "12345678000190"
    assert rows[0]["data_fechamento"] == date(2026, 7, 31)
    assert rows[0]["valor_informado"] == Decimal("1234.56")
    assert rows[0]["valor_aprovado_producao"] == Decimal("1200.00")
    assert rows[0]["valor_aprovado_protocolo"] == Decimal("1000.00")
    assert rows[1]["numero_processo"] == "P193251/2026"
    assert rows[1]["cnpj"] == "12345678000190"
    assert rows[1]["pagina_pdf"] == 2


def test_parse_saude_cogestao_accepts_resource_review_layout() -> None:
    summary = [
        ["Nome Prestador", "Hospital Pronto"],
        ["CNPJ", "08.711.085/0001-28"],
        [
            "Número Processo Origem\nNúmero Processo Recurso",
            "P232397/2025\nP309171/2025",
            "Competência TMS",
            "10/2025",
            "Dt Fechamento",
            "04/11/2025",
        ],
        [
            "Valor Informado Origem\nValor Informado Recurso",
            "R$ 215.229,99\nR$ 32.284,50",
            "Valor glosado",
            "R$ 1.860,53",
            "Valor Liberado",
            "R$ 213.369,46",
        ],
        ["Comp de Produção", "05/2025"],
    ]
    protocol = [
        [
            "NR",
            "",
            "NR Origem",
            "Tipo de Guia",
            "Valor Protocolo",
            "Valor Glosado",
            "Valor Aprovado Rev",
        ],
        [
            "",
            "1789509",
            "1651874",
            "Resumo Internação",
            "R$ 88.592,81",
            "R$ 208,27",
            "R$ 88.384,54",
        ],
    ]

    rows = parse_saude_cogestao_pages(
        [(1, [summary, protocol], "")],
        numero_processo="P309171/2025",
        documento_id="resource-1",
        documento_nome="resource.pdf",
    )

    assert len(rows) == 1
    assert rows[0]["numero_processo"] == "P232397/2025"
    assert rows[0]["processo_recurso"] == "P309171/2025"
    assert rows[0]["nr"] == "1789509"
    assert rows[0]["nr_origem"] == "1651874"
    assert rows[0]["valor_informado"] == Decimal("215229.99")
    assert rows[0]["valor_aprovado_producao"] == Decimal("213369.46")
    assert rows[0]["valor_aprovado_protocolo"] == Decimal("88384.54")


def test_parse_saude_cogestao_treats_dash_as_zero_and_repairs_date() -> None:
    summary = [
        ["Nome Prestador", "Hospital Pronto"],
        ["CNPJ", "08.711.085/0001-28"],
        [
            "Número Processo",
            "P436977/2025",
            "Competência TMS",
            "10/2025",
            "Dt Fechamento",
            "01//06/2026",
        ],
        [
            "Valor Informado",
            "R$ 50.549,34",
            "Valor Glosado",
            "R$ 103,75",
            "Valor Aprovado",
            "R$ 50.445,59",
        ],
        ["Comp de Produção", "06/2025"],
    ]
    protocol = [
        [
            "NR",
            "NR Origem",
            "Valor Protocolo",
            "Valor Glosado",
            "Valor Aprovado Rev",
        ],
        ["1865928", "56784", "R$ 45.485,16", "R$ -", "R$ 45.485,16"],
    ]

    rows = parse_saude_cogestao_pages(
        [(1, [summary, protocol], "")],
        numero_processo="P436977/2025",
        documento_id="dash-1",
        documento_nome="dash.pdf",
    )

    assert rows[0]["data_fechamento"] == date(2026, 6, 1)
    assert rows[0]["nr_origem"] == "56784"
    assert rows[0]["valor_glosado_protocolo"] == Decimal("0")


def test_parse_saude_cogestao_accepts_multiline_legacy_headers() -> None:
    summary = [
        ["Nome Prestador", "Hospital Pronto"],
        ["CPF/CNPJ", "08.711.085/0001-28"],
        [
            "Número Processo",
            "P279014/2025",
            "Competência TMS",
            "07/2025",
            "Dt Fechamento",
            "05/08/2025",
        ],
        [
            "Valor Informado XML\nLimpo",
            "565.988,53",
            "Valor Glosado",
            "50.549,34",
            "Valor Liberado",
            "515.439,19",
        ],
        ["Comp de Produção", "06/2025"],
    ]
    protocol = [
        ["NR", "Valor Protocolo", "Valor Gloado", "Valor"],
        ["", "", "", "Aprovado"],
        ["56784", "105.769,27", "45.485,16", "60.284,11"],
    ]

    rows = parse_saude_cogestao_pages(
        [(1, [summary, protocol], "")],
        numero_processo="P279014/2025",
        documento_id="multiline-1",
        documento_nome="multiline.pdf",
    )

    assert len(rows) == 1
    assert rows[0]["cnpj"] == "08711085000128"
    assert rows[0]["valor_informado"] == Decimal("565988.53")
    assert rows[0]["valor_aprovado_protocolo"] == Decimal("60284.11")


def test_parse_legacy_saude_cogestao_summary_without_protocol_table() -> None:
    pages = [
        (
            1,
            [],
            """
            INSTITUTO DE PREVIDENCIA MUNICIPIO FORTALEZA PAGINA - 001/001
            SISTEMA DE CONTROLE DE PLANO DE SAUDE DATA - 31/01/2024
            RESUMO FECHAMENTO DO PROCESSO PROCESSO: 202401163
            202401163 12/2023 8711085000128 PRONTOCARDIO SERVICOS MEDICOS HOSPI 03/01/2024 78 0 269,056.69
            TOTAL GERAL 1561 269,056.69 267,702.78 266,690.64
            GLOSA: R$2.366,05
            """,
        )
    ]

    rows = parse_legacy_saude_cogestao_pages(
        pages,
        numero_processo="P001963/2024",
        documento_id="relatorio-1",
        documento_nome="RELATORIO.pdf",
    )

    assert len(rows) == 1
    assert rows[0]["cnpj"] == "08711085000128"
    assert rows[0]["numero_processo"] == "P001963/2024"
    assert rows[0]["competencia_tms"] == "01/2024"
    assert rows[0]["competencia_producao"] == "12/2023"
    assert rows[0]["valor_informado"] == Decimal("269056.69")
    assert rows[0]["valor_aprovado_producao"] == Decimal("266690.64")
    assert rows[0]["valor_glosado_producao"] == Decimal("2366.05")
    assert rows[0]["nr"] is None
    assert rows[0]["nr_origem"] is None
    assert rows[0]["processo_recurso"] is None


def test_parse_nuexo_text_extracts_empenho_fields() -> None:
    document = _document("EMPENHO 2026.pdf", "empenho-1")
    rows = parse_nuexo_text(
        """
        NOTA DE EMPENHO
        BANCO/AGÊNCIA: 033 / 1234
        BANCO: Banco Santander
        CONTA: 000123-9
        """,
        document=document,
    )

    assert rows == [
        {
            "id_registro": rows[0]["id_registro"],
            "numero_processo": "P193251/2026",
            "tipo_documento": "EMPENHO",
            "documento_id": "empenho-1",
            "documento_nome": "EMPENHO 2026.pdf",
            "banco": "Banco Santander",
            "codigo_conta": "033",
            "codigo_agencia": "1234",
            "conta": "000123-9",
            "numero_nfse": None,
            "chave_acesso_nfse": None,
            "cnpj_cpf_nif_prestador": None,
        }
    ]


def test_parse_nuexo_text_extracts_nfse_fields() -> None:
    document = _document("NOTA FISCAL.pdf", "nfse-1")
    rows = parse_nuexo_text(
        """
        NÚMERO DA NFS-E: 98765
        CHAVE DE ACESSO DA NFS-E: ABCD-1234-5678
        CNPJ/CPF/NIF (PRESTADOR/FORNECEDOR): 12.345.678/0001-90
        """,
        document=document,
    )

    assert rows[0]["tipo_documento"] == "NFS_E"
    assert rows[0]["numero_nfse"] == "98765"
    assert rows[0]["chave_acesso_nfse"] == "ABCD12345678"
    assert rows[0]["cnpj_cpf_nif_prestador"] == "12345678000190"


def test_parse_nuexo_text_extracts_provider_from_danfse_2026_layout() -> None:
    document = SpuDocument(
        numero_processo="P219199/2026",
        setor="IPM/NUEXO",
        document_id="20358797",
        nome="NF 28012 PRONTOCARDIO",
        path=Path("/tmp/NF_28012_PRONTOCARDIO_20358797.pdf"),
    )
    rows = parse_nuexo_text(
        """
        DANFSe v2.0 Município: FORTALEZA/CE
        CHAVE DE ACESSO DA NFS-E
        23044001208711085000128000000002801226070101577072
        NÚMERO DA NFS-E COMPETÊNCIA DA NFS-E DATA E HORA DA EMISSÃO DA NFS-E
        28012 01/07/2026 07/07/2026 10:31:19
        PRESTADOR/FORNECEDOR CNPJ/CPF/NIF Indicador Municipal (Inscrição) Telefone
        08.711.085/0001-28 2245639 (85)3466-3000
        Nome/Nome Empresarial Município/Sigla UF Código IBGE/CEP
        PRONTOCARDIO SERVICOS MEDICOS HOSPITALARES LTDA FORTALEZA/CE
        TOMADOR/ADQUIRENTE CNPJ/CPF/NIF Indicador Municipal (Inscrição) Telefone
        07.965.184/0001-73 - (85)3255-8443
        """,
        document=document,
    )

    assert len(rows) == 1
    assert rows[0]["numero_nfse"] == "28012"
    assert rows[0]["chave_acesso_nfse"] == (
        "23044001208711085000128000000002801226070101577072"
    )
    assert rows[0]["cnpj_cpf_nif_prestador"] == "08711085000128"


def test_parse_nuexo_text_accepts_real_legacy_empenho_layout() -> None:
    document = SpuDocument(
        numero_processo="P001963/2024",
        setor="IPM/NUEXO",
        document_id="11142585",
        nome="EMP 1946",
        path=Path("/tmp/EMP_1946.pdf"),
    )
    rows = parse_nuexo_text(
        """
        NOTA DE EMPENHO
        DADOS DO CREDOR
        BANCO/AGÊNCIA 237 / Bradesco / 564 CPF/CNPJ: 08.711.085/0001-28
        CONTA: 101555-9 NIT: -
        """,
        document=document,
    )

    assert rows[0]["banco"] == "Banco Bradesco"
    assert rows[0]["codigo_conta"] == "237"
    assert rows[0]["codigo_agencia"] == "564"
    assert rows[0]["conta"] == "101555-9"
    assert rows[0]["id_registro"] == (
        "23712fd0550ca19065bc8feed0d6181605158942f7e86c622f3002d1775eac2d"
    )


def test_parse_nuexo_text_splits_compact_bank_agency_layout() -> None:
    document = _document("EMP_1947.pdf", "empenho-compact")
    rows = parse_nuexo_text(
        """
        NOTA DE EMPENHO
        BANCO/AGÊNCIA 237/Bradesco / 564-9 CPF/CNPJ: 08.711.085/0001-28
        CONTA: 101555-9
        """,
        document=document,
    )

    assert rows[0]["codigo_conta"] == "237"
    assert rows[0]["codigo_agencia"] == "564-9"


def test_parse_nuexo_text_uses_verification_code_as_legacy_nfse_key() -> None:
    document = _document("14652_IPM.pdf", "nfse-legacy")
    rows = parse_nuexo_text(
        """
        NOTA FISCAL ELETRÔNICA DE SERVIÇO - NFS-e
        Número da NFS-e
        14652
        Código de Verificação 893736841
        DADOS DO PRESTADOR DE SERVIÇOS
        CPF/CNPJ 08.711.085/0001-28 Insc Municipal 224.563-9
        DADOS DO TOMADOR DE SERVIÇOS
        """,
        document=document,
    )

    assert rows[0]["numero_nfse"] == "14652"
    assert rows[0]["chave_acesso_nfse"] == "893736841"
    assert rows[0]["cnpj_cpf_nif_prestador"] == "08711085000128"


def _document(name: str, document_id: str) -> SpuDocument:
    return SpuDocument(
        numero_processo="P193251/2026",
        setor="IPM/NUEXO",
        document_id=document_id,
        nome=name,
        path=Path("/tmp") / name,
    )
