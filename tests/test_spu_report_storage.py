from nfs_fortaleza.spu_report_storage import ensure_process_report_table


class CursorStub:
    def __init__(self) -> None:
        self.queries = []

    def execute(self, query, params=None) -> None:
        self.queries.append((query, params))

    def fetchone(self):
        return (True,)


def test_migracao_legada_nao_sobrescreve_registro_canonico() -> None:
    cursor = CursorStub()

    ensure_process_report_table(cursor, "api_prontocardio")

    comandos = "\n".join(str(query) for query, _ in cursor.queries)
    assert "NOT EXISTS" in comandos
    assert "atual.id_registro = rel.id_registro" in comandos
    assert "ON CONFLICT (id_registro) DO NOTHING" in comandos
