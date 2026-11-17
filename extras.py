"""Comandos extras do MeMi BOT: sequências, comparação, duelos, quiz, "neste dia", status e barra.

Fica separado do memi_bot.py e usa o banco e os comandos dele pelo Cog "Musicas" e "Atividade".
"""

import asyncio
import logging
import random
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

import changelog as versoes_bot
import mudae as mudae_tracker
import progressao
import tags
from estilo import COR_PADRAO, campo, cortar, milhar, plural, responder
from estilo import rodape as rodape_do_bot

FUSO = timezone(timedelta(hours=-3))
EPOCA_DISCORD = 1420070400000
QUIZ_TEMPO = 20  # segundos para responder
QUIZ_OPCOES = 4
INTERVALO_DIARIO = 3600  # segundos entre as verificações da rotina diária
MIN_SEQ = ((7, "sequencia_7"), (30, "sequencia_30"), (100, "sequencia_100"))
MADRUGADA = range(2, 6)  # 2h às 5h59, horário de Brasília
MIN_MADRUGADA = 300
MIN_ABRE_ALAS = 50
ANOS_VETERANO = ((1, "um_ano_casa"), (5, "fossil_clubex"))

SQL = """
CREATE TABLE IF NOT EXISTS duelos (
    periodo TEXT NOT NULL, a INTEGER NOT NULL, b INTEGER NOT NULL,
    estado TEXT NOT NULL DEFAULT 'aberto', vencedor INTEGER,
    PRIMARY KEY (periodo, a, b));
CREATE TABLE IF NOT EXISTS quiz_pontos (
    usuario_id INTEGER PRIMARY KEY, pontos INTEGER NOT NULL DEFAULT 0);
"""


def agora_local():
    return datetime.now(FUSO)


def _segundos_locais(message_id):
    return ((message_id >> 22) + EPOCA_DISCORD) // 1000 - 3 * 3600


def dia_de(message_id):
    return _segundos_locais(message_id) // 86400


def sequencias(dias, hoje):
    """(atual, recorde) de dias seguidos. `dias`: números de dia em qualquer ordem; a sequência
    atual continua valendo se a última atividade foi hoje ou ontem."""
    ordenados = sorted(set(dias))
    if not ordenados:
        return 0, 0
    recorde = corrida = 1
    for anterior, dia in zip(ordenados, ordenados[1:]):
        corrida = corrida + 1 if dia == anterior + 1 else 1
        recorde = max(recorde, corrida)
    return (corrida if hoje - ordenados[-1] <= 1 else 0), recorde


def dias_ativos(con, uid):
    return {
        dia_de(mid)
        for (mid,) in con.execute(
            "SELECT message_id FROM mensagens g WHERE autor_id=? AND NOT EXISTS "
            "(SELECT 1 FROM mudae m WHERE m.message_id=g.message_id)",
            (uid,),
        )
    }


def calcular_conquistas(caminho, hoje):
    """{uid: {tags}} das tags secretas. Roda em thread, com conexão própria e só leitura."""
    uri = f"file:{Path(caminho).as_posix()}?mode=ro"
    dias, madrugada, primeiro_do_dia = {}, {}, {}
    with closing(sqlite3.connect(uri, uri=True)) as con:
        for mid, uid in con.execute(
            "SELECT g.message_id, g.autor_id FROM mensagens g WHERE g.eh_bot=0 AND NOT EXISTS "
            "(SELECT 1 FROM mudae m WHERE m.message_id=g.message_id) ORDER BY g.message_id"
        ):
            segundos = _segundos_locais(mid)
            dia = segundos // 86400
            dias.setdefault(uid, set()).add(dia)
            if (segundos % 86400) // 3600 in MADRUGADA:
                madrugada[uid] = madrugada.get(uid, 0) + 1
            primeiro_do_dia.setdefault(dia, uid)
    abre_alas = {}
    for uid in primeiro_do_dia.values():
        abre_alas[uid] = abre_alas.get(uid, 0) + 1
    ganhos = {}
    for uid, dias_uid in dias.items():
        itens = ganhos.setdefault(uid, set())
        _, recorde = sequencias(dias_uid, hoje)
        itens.update(item for minimo, item in MIN_SEQ if recorde >= minimo)
        itens.update(
            item for anos, item in ANOS_VETERANO if hoje - min(dias_uid) >= anos * 365
        )
        if madrugada.get(uid, 0) >= MIN_MADRUGADA:
            itens.add("madrugador")
        if abre_alas.get(uid, 0) >= MIN_ABRE_ALAS:
            itens.add("abre_alas")
    return ganhos


def placar_duelo(banco, uid, desde, ate):
    """Pontos do duelo: mensagens (sem rolls do Mudae) + 25 por música pedida."""
    mensagens = banco.con.execute(
        "SELECT COUNT(*) FROM mensagens g WHERE autor_id=? AND message_id>=? AND message_id<? "
        "AND NOT EXISTS (SELECT 1 FROM mudae m WHERE m.message_id=g.message_id)",
        (uid, desde, ate),
    ).fetchone()[0]
    pedidos = banco.con.execute(
        "SELECT COUNT(*) FROM pedidos WHERE usuario_id=? AND message_id>=? AND message_id<?",
        (uid, desde, ate),
    ).fetchone()[0]
    return mensagens + progressao.XP_POR_PEDIDO * pedidos


def _snowflake(momento):
    return discord.utils.time_snowflake(momento)


def limites_mes(periodo):
    ano, mes = map(int, periodo.split("-"))
    inicio = datetime(ano, mes, 1, tzinfo=FUSO)
    fim = datetime(ano + (mes == 12), 1 if mes == 12 else mes + 1, 1, tzinfo=FUSO)
    return _snowflake(inicio), _snowflake(fim)


class DueloView(discord.ui.View):
    def __init__(self, extras, periodo, desafiante, desafiado):
        super().__init__(timeout=300)
        self.extras, self.periodo = extras, periodo
        self.desafiante, self.desafiado = desafiante, desafiado
        self.message = None

    async def _so_desafiado(self, interaction):
        if interaction.user.id == self.desafiado.id:
            return True
        await interaction.response.send_message(
            "Só quem foi desafiado pode responder.", ephemeral=True
        )
        return False

    @discord.ui.button(label="Aceitar", emoji="⚔️", style=discord.ButtonStyle.success)
    async def aceitar(self, interaction, button):
        if not await self._so_desafiado(interaction):
            return
        self.extras.criar_duelo(self.periodo, self.desafiante.id, self.desafiado.id)
        self.stop()
        await interaction.response.edit_message(
            content=f"⚔️ Duelo de {self.periodo} valendo! Acompanhe com `mm!duelo`.", view=None
        )

    @discord.ui.button(label="Recusar", style=discord.ButtonStyle.secondary)
    async def recusar(self, interaction, button):
        if not await self._so_desafiado(interaction):
            return
        self.stop()
        await interaction.response.edit_message(content="Duelo recusado.", view=None)

    async def on_timeout(self):
        if self.message:
            try:
                await self.message.edit(content="O desafio de duelo expirou.", view=None)
            except discord.HTTPException:
                pass


class QuizView(discord.ui.View):
    def __init__(self, extras, opcoes, correta, titulo):
        super().__init__(timeout=QUIZ_TEMPO)
        self.extras, self.correta, self.titulo = extras, correta, titulo
        self.tentaram, self.vencedor, self.message = set(), None, None
        for i, opcao in enumerate(opcoes):
            botao = discord.ui.Button(
                label=cortar(opcao, 70), style=discord.ButtonStyle.secondary, custom_id=str(i)
            )
            botao.callback = self._responder(opcao)
            self.add_item(botao)

    def _responder(self, opcao):
        async def callback(interaction):
            if interaction.user.id in self.tentaram:
                await interaction.response.send_message("Você já tentou.", ephemeral=True)
                return
            self.tentaram.add(interaction.user.id)
            if opcao != self.correta:
                await interaction.response.send_message("❌ Errou!", ephemeral=True)
                return
            self.vencedor = interaction.user
            self.extras.somar_ponto(interaction.user.id)
            self.stop()
            await interaction.response.edit_message(content=self._final(), view=None)

        return callback

    def _final(self):
        resposta = f"**{discord.utils.escape_markdown(self.correta)}** canta *{self.titulo}*."
        if self.vencedor:
            nome = discord.utils.escape_markdown(self.vencedor.display_name)
            return f"🎉 **{nome}** acertou! {resposta}"
        return f"⏰ Ninguém acertou. {resposta}"

    async def on_timeout(self):
        if self.message and not self.vencedor:
            try:
                await self.message.edit(content=self._final(), embed=None, view=None)
            except discord.HTTPException:
                pass


class Extras(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.musicas = bot.get_cog("Musicas")
        self.atividade = bot.get_cog("Atividade")
        self.banco = self.musicas.banco
        self.banco.con.executescript(SQL)
        self.inicio = time.time()
        self._rotina = None

    @commands.Cog.listener()
    async def on_ready(self):
        if self._rotina is None:
            self._rotina = asyncio.create_task(self.rotina_diaria(), name="memi-extras")

    async def cog_unload(self):
        if self._rotina:
            self._rotina.cancel()
            await asyncio.gather(self._rotina, return_exceptions=True)

    async def cog_check(self, ctx):
        return await self.musicas.cog_check(ctx)

    # ----- rotina diária ----------------------------------------------------
    async def rotina_diaria(self):
        while not self.bot.is_closed():
            try:
                await self.bot.wait_until_ready()
                await self.executar_rotina()
            except Exception:  # noqa: BLE001 - extra nunca derruba o bot
                logging.exception("Falha na rotina diária dos extras.")
            await asyncio.sleep(INTERVALO_DIARIO)

    async def executar_rotina(self, agora=None):
        agora = agora or agora_local()
        dia = agora.strftime("%Y-%m-%d")
        banco = self.banco
        if banco.estado("importacao_concluida") != "1" or banco.estado("extras_dia") == dia:
            return False
        hoje = (agora - datetime(1970, 1, 1, tzinfo=FUSO)).days
        ganhos = await asyncio.to_thread(calcular_conquistas, banco.caminho, hoje)
        # A primeira passada é silenciosa: quem já tinha direito não gera uma enxurrada de avisos.
        avisar = banco.estado("extras_iniciado") == "1"
        elegiveis = self.musicas.elegiveis()
        async with self.musicas.lock:
            with banco.con:
                mudae_tracker.reatribuir_nomes(banco.con)
                for uid, itens in ganhos.items():
                    if elegiveis is None or uid in elegiveis:
                        for item in sorted(itens):
                            banco._conceder(uid, item, avisar=avisar)
                self._fechar_duelos(agora)
        banco.definir_estado("extras_iniciado", "1")
        banco.definir_estado("extras_dia", dia)
        await self.musicas.enviar_avisos()
        return True

    def _fechar_duelos(self, agora):
        mes_atual = agora.strftime("%Y-%m")
        for periodo, a, b in self.banco.con.execute(
            "SELECT periodo,a,b FROM duelos WHERE estado='aberto' AND periodo<?", (mes_atual,)
        ).fetchall():
            desde, ate = limites_mes(periodo)
            pa, pb = placar_duelo(self.banco, a, desde, ate), placar_duelo(self.banco, b, desde, ate)
            vencedor = a if pa > pb else b if pb > pa else None
            if vencedor is not None:
                self.banco._conceder(vencedor, "duelista", periodo, avisar=True)
            self.banco.con.execute(
                "UPDATE duelos SET estado='fechado',vencedor=? WHERE periodo=? AND a=? AND b=?",
                (vencedor, periodo, a, b),
            )

    # ----- sequência e comparação -------------------------------------------
    @commands.command(name="sequencia", aliases=["seq", "streak", "sequência"])
    @commands.guild_only()
    async def sequencia(self, ctx, pessoa: discord.Member = None):
        """Dias seguidos conversando (rolls do Mudae não contam)."""
        pessoa = pessoa or ctx.author
        hoje = (agora_local() - datetime(1970, 1, 1, tzinfo=FUSO)).days
        dias = dias_ativos(self.banco.con, pessoa.id)
        atual, recorde = sequencias(dias, hoje)
        embed = discord.Embed(title=f"🔥 Sequência de {pessoa.display_name}"[:256], color=COR_PADRAO)
        campo(embed, "Atual", plural(atual, "dia", "dias"))
        campo(embed, "Recorde", plural(recorde, "dia", "dias"))
        campo(embed, "Dias com conversa", milhar(len(dias)))
        await ctx.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    def _numeros(self, uid):
        con, banco = self.banco.con, self.banco
        totais = con.execute(
            "SELECT mensagens-roletadas,pedidos,roletadas FROM totais WHERE usuario_id=?", (uid,)
        ).fetchone() or (0, 0, 0)
        xp = banco.xp_usuario(uid)
        mudae = mudae_tracker.perfil(con, uid)
        return {
            "Nível": progressao.nivel(xp),
            "XP": xp,
            "Mensagens": totais[0],
            "Músicas pedidas": totais[1],
            "Rolls": totais[2],
            "Casamentos": mudae["casamentos"],
            "Kakera": mudae["kakera"],
            "Snipes": mudae["snipes"],
            "Tags": len(banco.tags_usuario(uid)),
        }

    @commands.command(name="comparar", aliases=["vs", "versus"])
    @commands.guild_only()
    async def comparar(self, ctx, pessoa: discord.Member, outra: discord.Member = None):
        a, b = outra and pessoa or ctx.author, outra or pessoa
        na, nb = self._numeros(a.id), self._numeros(b.id)
        linhas = []
        for chave in na:
            x, y = na[chave], nb[chave]
            linhas.append(
                f"{'🟢' if x > y else '⚪'} **{milhar(x)}** · {chave} · **{milhar(y)}** "
                f"{'🟢' if y > x else '⚪'}"
            )
        nome_a = discord.utils.escape_markdown(a.display_name)
        nome_b = discord.utils.escape_markdown(b.display_name)
        embed = discord.Embed(
            title="⚖️ Comparação", description=f"**{nome_a}** x **{nome_b}**\n\n" + "\n".join(linhas),
            color=COR_PADRAO,
        )
        await ctx.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    # ----- duelos -----------------------------------------------------------
    def criar_duelo(self, periodo, a, b):
        a, b = sorted((a, b))
        with self.banco.con:
            self.banco.con.execute(
                "INSERT OR IGNORE INTO duelos(periodo,a,b) VALUES (?,?,?)", (periodo, a, b)
            )

    @commands.command(name="duelo", aliases=["duelar"])
    @commands.guild_only()
    async def duelo(self, ctx, pessoa: discord.Member = None):
        """Desafia alguém para o mês, ou mostra os seus duelos com `mm!duelo`."""
        periodo = agora_local().strftime("%Y-%m")
        if pessoa is None:
            await self._mostrar_duelos(ctx, periodo)
            return
        if pessoa.id == ctx.author.id or pessoa.bot:
            await responder(ctx, "aviso", "Escolha outra pessoa para duelar.")
            return
        a, b = sorted((ctx.author.id, pessoa.id))
        if self.banco.con.execute(
            "SELECT 1 FROM duelos WHERE periodo=? AND a=? AND b=?", (periodo, a, b)
        ).fetchone():
            await responder(ctx, "aviso", "Vocês já têm um duelo neste mês. Veja com `mm!duelo`.")
            return
        view = DueloView(self, periodo, ctx.author, pessoa)
        view.message = await ctx.send(
            f"⚔️ **{discord.utils.escape_markdown(ctx.author.display_name)}** desafiou "
            f"{pessoa.mention} para o duelo do mês! Vale mensagens (sem rolls) e músicas pedidas; "
            "quem ganhar leva a tag Duelista.",
            view=view,
            allowed_mentions=discord.AllowedMentions(users=[pessoa]),
        )

    async def _mostrar_duelos(self, ctx, periodo):
        uid = ctx.author.id
        desde, ate = limites_mes(periodo)
        nome = self.musicas.nome_pessoa
        linhas = []
        for a, b in self.banco.con.execute(
            "SELECT a,b FROM duelos WHERE periodo=? AND (a=? OR b=?)", (periodo, uid, uid)
        ).fetchall():
            pa, pb = placar_duelo(self.banco, a, desde, ate), placar_duelo(self.banco, b, desde, ate)
            linhas.append(f"**{nome(a)}** {milhar(pa)} x {milhar(pb)} **{nome(b)}**")
        if not linhas:
            await responder(ctx, "aviso", "Você não tem duelos neste mês. Use `mm!duelo @pessoa`.")
            return
        embed = discord.Embed(
            title=f"⚔️ Seus duelos · {periodo}", description="\n".join(linhas), color=COR_PADRAO
        )
        await ctx.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    # ----- neste dia --------------------------------------------------------
    @commands.command(name="nestedia", aliases=["ndh", "hojenahistoria"])
    @commands.guild_only()
    async def nestedia(self, ctx):
        """O que aconteceu neste dia nos anos anteriores."""
        agora = agora_local()
        con = self.banco.con
        blocos = []
        primeiro = con.execute("SELECT MIN(message_id) FROM mensagens").fetchone()[0]
        ano_inicial = discord.utils.snowflake_time(primeiro).year if primeiro else agora.year
        for ano in range(agora.year - 1, ano_inicial - 1, -1):
            try:
                inicio = agora.replace(year=ano, hour=0, minute=0, second=0, microsecond=0)
            except ValueError:  # 29/02 em ano comum
                continue
            desde, ate = _snowflake(inicio), _snowflake(inicio + timedelta(days=1))
            total = con.execute(
                "SELECT COUNT(*) FROM mensagens g WHERE eh_bot=0 AND message_id>=? AND message_id<? "
                "AND NOT EXISTS (SELECT 1 FROM mudae m WHERE m.message_id=g.message_id)",
                (desde, ate),
            ).fetchone()[0]
            if not total:
                continue
            top = con.execute(
                "SELECT autor_id,COUNT(*) FROM mensagens g WHERE eh_bot=0 AND message_id>=? AND "
                "message_id<? AND NOT EXISTS (SELECT 1 FROM mudae m WHERE m.message_id=g.message_id) "
                "GROUP BY autor_id ORDER BY 2 DESC LIMIT 1",
                (desde, ate),
            ).fetchone()
            musica = self.banco.ranking_musicas(desde, ate)[:1]
            texto = (
                f"**{ano}** · {plural(total, 'mensagem', 'mensagens')} · quem mais falou: "
                f"**{self.musicas.nome_pessoa(top[0])}** ({milhar(top[1])})"
            )
            if musica:
                texto += f"\n🎵 {cortar(musica[0][0], 60)}"
            blocos.append(texto)
        embed = discord.Embed(
            title=f"📅 Neste dia · {agora.strftime('%d/%m')}",
            description=cortar("\n\n".join(blocos) or "Nada registrado nesta data.", 4000),
            color=COR_PADRAO,
        )
        await ctx.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    # ----- quiz -------------------------------------------------------------
    def somar_ponto(self, uid):
        with self.banco.con:
            self.banco.con.execute(
                "INSERT INTO quiz_pontos VALUES (?,1) ON CONFLICT(usuario_id) "
                "DO UPDATE SET pontos=pontos+1",
                (uid,),
            )

    def montar_quiz(self):
        """(título, artista certo, opções embaralhadas) ou None se não há faixas suficientes."""
        con = self.banco.con
        linha = con.execute(
            "SELECT titulo,artista,artista_chave FROM musicas WHERE artista_chave<>'' "
            "ORDER BY RANDOM() LIMIT 1"
        ).fetchone()
        if not linha:
            return None
        errados = [
            r[0]
            for r in con.execute(
                "SELECT MIN(artista) FROM musicas WHERE artista_chave NOT IN ('',?) "
                "GROUP BY artista_chave ORDER BY RANDOM() LIMIT ?",
                (linha[2], QUIZ_OPCOES - 1),
            )
        ]
        if len(errados) < QUIZ_OPCOES - 1:
            return None
        opcoes = [linha[1], *errados]
        random.shuffle(opcoes)
        return linha[0], linha[1], opcoes

    @commands.group(name="quiz", invoke_without_command=True, case_insensitive=True)
    @commands.guild_only()
    @commands.cooldown(1, 15, commands.BucketType.channel)
    async def quiz(self, ctx):
        """Quem canta? Vale um ponto para o primeiro que acertar."""
        rodada = self.montar_quiz()
        if rodada is None:
            await responder(ctx, "aviso", "Ainda não há músicas suficientes para o quiz.")
            return
        titulo, certo, opcoes = rodada
        titulo = discord.utils.escape_markdown(titulo)
        view = QuizView(self, opcoes, certo, titulo)
        embed = discord.Embed(
            title="🎤 Quem canta?", description=f"**{titulo}**", color=COR_PADRAO
        )
        embed.set_footer(text=rodape_do_bot(f"{QUIZ_TEMPO}s", "uma tentativa por pessoa"))
        view.message = await ctx.send(embed=embed, view=view)

    @quiz.command(name="top", aliases=["ranking"])
    async def quiz_top(self, ctx):
        linhas = self.banco.con.execute(
            "SELECT usuario_id,pontos FROM quiz_pontos ORDER BY pontos DESC, usuario_id"
        ).fetchall()
        await self.musicas.enviar_ranking(
            ctx,
            "🎤 Ranking do quiz",
            [f"**{self.musicas.nome_pessoa(u)}** · {plural(p, 'ponto', 'pontos')}" for u, p in linhas],
            rodape_partes=(f"{len(linhas)} participantes",),
        )

    # ----- administração ----------------------------------------------------
    @commands.command(name="mudaenome", hidden=True)
    @commands.guild_only()
    async def mudaenome(self, ctx, pessoa: discord.Member, *, nome: str):
        """Liga um nome antigo do Mudae a uma pessoa (só admins)."""
        if not self.musicas.eh_admin(ctx.author.id):
            await responder(ctx, "erro", "Só os admins do bot podem usar esse comando.")
            return
        with self.banco.con:
            total = mudae_tracker.vincular_nome(self.banco.con, nome, pessoa.id)
        await responder(ctx, "sucesso", f"Nome vinculado. {plural(total, 'registro', 'registros')}.")

    def embed_status(self):
        banco, musicas = self.banco, self.musicas

        def conta(tabela):
            return banco.con.execute(
                f"SELECT COUNT(*) FROM {tabela} WHERE estado='pendente'"
            ).fetchone()[0]

        ultimo = musicas.segundos_desde_sync()
        backups = sorted((banco.caminho.parent / "backups").glob("memi_????-??-??.db.gz"))
        canal = musicas.bot.get_channel(musicas.id_canal_avisos())
        uptime = timedelta(seconds=int(time.time() - self.inicio))
        embed = discord.Embed(title="🩺 Status do bot", color=COR_PADRAO)
        campo(embed, "Versão", versoes_bot.VERSAO_ATUAL)
        campo(embed, "Ligado há", str(uptime))
        campo(embed, "Latência", f"{self.bot.latency * 1000:.0f} ms")
        campo(embed, "Canal de avisos", "✅ ok" if canal else "❌ inacessível")
        campo(embed, "Última leitura", f"há {ultimo:.0f}s" if ultimo is not None else "ainda não")
        campo(embed, "Falhas seguidas", str(musicas.falhas_seguidas()))
        campo(
            embed,
            "Fila",
            f"tags {conta('avisos')} · níveis {conta('avisos_nivel')} · resumos {conta('resumos')}",
        )
        campo(embed, "Último backup", backups[-1].name[5:15] if backups else "nenhum")
        campo(embed, "Banco", f"{banco.caminho.stat().st_size / 1_048_576:.0f} MB")
        campo(embed, "Chats ignorados", str(len(musicas.canais_ignorados)))
        return embed

    @commands.command(name="status", hidden=True)
    @commands.guild_only()
    async def status(self, ctx):
        if not self.musicas.eh_admin(ctx.author.id):
            await responder(ctx, "erro", "Só os admins do bot podem usar esse comando.")
            return
        await ctx.send(embed=self.embed_status(), allowed_mentions=discord.AllowedMentions.none())

    # ----- comandos de barra ------------------------------------------------
    async def _ctx(self, interaction):
        return await commands.Context.from_interaction(interaction)

    async def cog_app_command_error(self, interaction, error):
        logging.error("Erro no comando de barra", exc_info=error)
        enviar = (
            interaction.followup.send
            if interaction.response.is_done()
            else interaction.response.send_message
        )
        try:
            await enviar("Não consegui concluir o comando.", ephemeral=True)
        except discord.HTTPException:
            pass

    async def _rodar(self, interaction, cog, comando, *args, **kwargs):
        await getattr(cog, comando).callback(cog, await self._ctx(interaction), *args, **kwargs)

    @app_commands.command(name="perfil", description="Mostra o perfil de alguém")
    @app_commands.guild_only()
    async def barra_perfil(self, interaction, pessoa: discord.Member = None):
        await self._rodar(interaction, self.musicas, "perfil", pessoa)

    @app_commands.command(name="cartao", description="Cartão de perfil em imagem")
    @app_commands.guild_only()
    async def barra_cartao(self, interaction, pessoa: discord.Member = None):
        await self._rodar(interaction, self.atividade, "cartao", pessoa)

    @app_commands.command(name="levels", description="Ranking de níveis")
    @app_commands.guild_only()
    async def barra_levels(self, interaction):
        await self._rodar(interaction, self.atividade, "levels")

    @app_commands.command(name="wrapped", description="Seu resumo dos últimos 12 meses")
    @app_commands.guild_only()
    async def barra_wrapped(self, interaction):
        await self._rodar(interaction, self.musicas, "wrapped")

    @app_commands.command(name="frase", description="Define a frase do seu perfil")
    @app_commands.guild_only()
    async def barra_frase(self, interaction, texto: str = ""):
        await self._rodar(interaction, self.atividade, "frase", texto=texto)

    @app_commands.command(name="favorita", description="Define sua música favorita")
    @app_commands.guild_only()
    async def barra_favorita(self, interaction, musica: str):
        await self._rodar(interaction, self.atividade, "favorita", texto=musica)

    @app_commands.command(name="cor", description="Muda a cor do seu perfil (#hex ou nome)")
    @app_commands.guild_only()
    async def barra_cor(self, interaction, cor: str):
        await self._rodar(interaction, self.atividade, "ec", texto=cor)

    @app_commands.command(name="titulo", description="Escolhe o título exibido no perfil")
    @app_commands.guild_only()
    async def barra_titulo(self, interaction, nome: str):
        await self._rodar(interaction, self.atividade, "titulo", nome=nome)

    @app_commands.command(name="sequencia", description="Seus dias seguidos conversando")
    @app_commands.guild_only()
    async def barra_sequencia(self, interaction, pessoa: discord.Member = None):
        await self._rodar(interaction, self, "sequencia", pessoa)

