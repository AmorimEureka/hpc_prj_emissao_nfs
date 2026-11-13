import re
from pathlib import Path

DAG = Path(__file__).resolve().parents[1] / "dags" / "extracao_glosas_maida.py"
ENV_EXAMPLE = Path(__file__).resolve().parents[1] / ".env.example"
AGENDA = "0 2,8,14,20 * * *"


def test_extracao_maida_comeca_as_8h_e_roda_a_cada_6h():
    texto = DAG.read_text(encoding="utf-8")
    padrao = re.search(r'MAIDA_EXTRACTION_SCHEDULE", "([^"]+)"', texto)
    assert padrao and padrao.group(1) == AGENDA
    assert 'tz="America/Fortaleza"' in texto
    horas = sorted(int(h) for h in AGENDA.split()[1].split(","))
    assert 8 in horas
    assert [b - a for a, b in zip(horas, horas[1:])] == [6, 6, 6]


def test_env_example_usa_a_mesma_agenda():
    linhas = ENV_EXAMPLE.read_text(encoding="utf-8").splitlines()
    assert f"MAIDA_EXTRACTION_SCHEDULE={AGENDA}" in linhas
