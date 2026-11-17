"""Testes dos extras: sequências, ranking sem rolls, limite de XP, nomes do Mudae e backup extra."""

import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import extras
import memi_bot as m
import mudae


def sid(segundos):
    return (int(segundos * 1000) << 22) + 1


def mensagem(uid, mid, texto="oi"):
    return NS(
        id=mid,
        content=texto,
        embeds=[],
        author=NS(id=uid, bot=False, name=f"u{uid}", global_name=None, display_name=f"u{uid}"),
        channel=NS(id=5),
    )


class SequenciasTests(unittest.TestCase):
    def test_sem_dias(self):
        self.assertEqual(extras.sequencias([], 100), (0, 0))

    def test_sequencia_atual_vale_ate_ontem(self):
        self.assertEqual(extras.sequencias([97, 98, 99], 100), (3, 3))

    def test_sequencia_quebrada_mantem_recorde(self):
        self.assertEqual(extras.sequencias([1, 2, 3, 4, 50], 100), (0, 4))


class BancoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.b = m.Banco(Path(self.temp.name) / "memi.db")

    def tearDown(self):
        self.b.con.close()
        self.temp.cleanup()

    def test_rolls_nao_contam_no_ranking_de_mensagens(self):
        for i in range(5):
            self.b.receber(mensagem(1, sid(1000 + i), "$w"))
        for i in range(2):
            self.b.receber(mensagem(2, sid(2000 + i)))
        self.assertEqual(self.b.ranking_contagens("mensagens", eh_bot=False), [(2, 2)])
        self.assertEqual(self.b.ranking_contagens("mensagens", 1, eh_bot=False), [(2, 2)])
        self.assertEqual(self.b.total_usuario(1, "mudae"), 5)

    def test_limite_de_xp_so_corta_o_excesso_ao_vivo(self):
        for i in range(m.LIMITE_XP_POR_MINUTO + 5):
            self.b.receber(mensagem(1, sid(1000 + i / 10)), limitar_xp=True)
        self.assertEqual(self.b.xp_usuario(1), m.LIMITE_XP_POR_MINUTO)
        self.assertEqual(self.b.total_usuario(1, "mensagens"), m.LIMITE_XP_POR_MINUTO + 5)
        self.assertEqual(self.b.ranking_xp()[0], (1, m.LIMITE_XP_POR_MINUTO))

    def test_mensagem_estranha_do_mudae_nao_interrompe(self):
        msg = mensagem(1, sid(1000))
        msg.author.bot, msg.author.id = True, mudae.MUDAE_ID
        original = mudae.registrar
        mudae.registrar = lambda *_: (_ for _ in ()).throw(ValueError("x"))
        try:
            with self.assertLogs(level="ERROR"):
                self.b.receber(msg)
        finally:
            mudae.registrar = original
        self.assertEqual(self.b.con.execute("SELECT COUNT(*) FROM mensagens").fetchone()[0], 1)

    def test_nome_antigo_e_deduzido_de_quem_rolou(self):
        con = self.b.con
        for i in range(6):
            con.execute(
                "INSERT INTO mudae_casamentos VALUES (?,1,NULL,'gui.','P','p',?,42,NULL)", (i, i)
            )
        con.execute("INSERT INTO mudae_kakera VALUES (99,0,NULL,'gui.','kakera',10)")
        self.assertEqual(mudae.reatribuir_nomes(con), 7)
        self.assertEqual(mudae.resolver_nome(con, "GUI."), 42)

    def test_nome_dividido_nao_e_deduzido(self):
        con = self.b.con
        for i in range(6):
            con.execute(
                "INSERT INTO mudae_casamentos VALUES (?,1,NULL,'x','P','p',?,?,NULL)",
                (i, i, 40 + i % 2),
            )
        self.assertEqual(mudae.reatribuir_nomes(con), 0)


class ConquistasTests(unittest.TestCase):
    def test_sequencia_e_veterano(self):
        with tempfile.TemporaryDirectory() as pasta:
            caminho = Path(pasta) / "memi.db"
            banco = m.Banco(caminho)
            base = 500 * 86400 + 12 * 3600
            for d in range(8):
                banco.receber(mensagem(1, sid(base + d * 86400)))
            banco.con.close()
            hoje = extras.dia_de(sid(base + 7 * 86400))
            self.assertIn("sequencia_7", extras.calcular_conquistas(caminho, hoje)[1])
            self.assertIn("um_ano_casa", extras.calcular_conquistas(caminho, hoje + 400)[1])


class StatusTests(unittest.IsolatedAsyncioTestCase):
    async def test_alerta_so_depois_de_tres_falhas_e_uma_vez(self):
        falsa = NS(_falhas_seguidas=0, alertar_dono=AsyncMock())
        for _ in range(4):
            await m.Musicas._registrar_resultado(falsa, False)
        falsa.alertar_dono.assert_awaited_once()
        await m.Musicas._registrar_resultado(falsa, True)
        self.assertEqual(falsa._falhas_seguidas, 0)
        self.assertEqual(falsa.alertar_dono.await_count, 2)


class BackupExtraTests(unittest.TestCase):
    def test_copia_semanal_e_retencao(self):
        with tempfile.TemporaryDirectory() as pasta:
            origem = Path(pasta) / "memi_2026-01-01.db.gz"
            origem.write_bytes(b"x")
            extra = Path(pasta) / "nuvem"
            agora = time.time()
            self.assertIsNotNone(m.copiar_backup_extra(origem, extra, agora))
            self.assertIsNone(m.copiar_backup_extra(origem, extra, agora + 86400))
            self.assertIsNotNone(m.copiar_backup_extra(origem, extra, agora + 8 * 86400))
            self.assertIsNone(m.copiar_backup_extra(origem, "", agora))

    def test_pasta_invalida_nao_gera_erro(self):
        with tempfile.TemporaryDirectory() as pasta:
            arquivo = Path(pasta) / "ocupado"
            arquivo.write_text("x")
            with self.assertLogs(level="ERROR"):
                self.assertIsNone(m.copiar_backup_extra(arquivo, arquivo / "sub"))
            self.assertTrue(os.path.exists(arquivo))


class FechamentoIncrementalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.b = m.Banco(Path(self.temp.name) / "memi.db")

    def tearDown(self):
        self.b.con.close()
        self.temp.cleanup()

    def em(self, ano, mes, uid=1, extra=0):
        momento = m.datetime(ano, mes, 10, 12, tzinfo=m.FUSO).timestamp() + extra
        self.b.receber(mensagem(uid, sid(momento - 1420070400)))

    def test_so_le_depois_do_ultimo_mes_fechado(self):
        self.em(2025, 11)
        self.em(2025, 12)
        self.assertEqual(self.b.inicio_meses_abertos(), 0)
        self.b.fechar_periodos(m.datetime(2026, 1, 5, tzinfo=m.FUSO))
        desde = self.b.inicio_meses_abertos()
        self.assertEqual(desde, m.limites_periodo("mes", "2026-01")[0])
        self.em(2026, 1)
        self.assertEqual(self.b.meses_ativos(desde), ["2026-01"])
        self.assertEqual(self.b.meses_ativos(desde, isolado=True), ["2026-01"])
        fechados = self.b.fechar_periodos(m.datetime(2026, 2, 2, tzinfo=m.FUSO))
        self.assertEqual(fechados, [("mes", "2026-01")])

    def test_ano_fecha_mesmo_com_os_meses_ja_fechados(self):
        self.em(2025, 12)
        with self.b.con:
            self.b.con.execute("INSERT INTO periodos_fechados VALUES ('mes','2025-12',1)")
        self.assertEqual(
            self.b.fechar_periodos(m.datetime(2026, 1, 2, tzinfo=m.FUSO)), [("ano", "2025")]
        )

    def test_mes_antigo_trazido_depois_so_fecha_na_varredura_completa(self):
        self.em(2025, 12)
        self.b.fechar_periodos(m.datetime(2026, 1, 2, tzinfo=m.FUSO))
        self.em(2025, 6, uid=2)  # canal antigo liberado depois (mm!scan)
        self.assertNotIn(
            ("mes", "2025-06"), self.b.fechar_periodos(m.datetime(2026, 1, 3, tzinfo=m.FUSO))
        )
        self.assertIn(
            ("mes", "2025-06"),
            self.b.fechar_periodos(m.datetime(2026, 1, 3, tzinfo=m.FUSO), completo=True),
        )


if __name__ == "__main__":
    unittest.main()
