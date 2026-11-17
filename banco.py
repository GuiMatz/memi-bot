"""Camada de dados do MeMi BOT: banco SQLite, backups e leitura das mensagens de música e Mudae.

Não depende do Discord em execução; memi_bot.py cuida dos comandos e da conexão.
"""

import gzip
import json
import logging
import os
import re
import shutil
import sqlite3
import time
import unicodedata
from collections import Counter
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import discord

import mudae as mudae_tracker
import progressao
import tags

FUSO = timezone(timedelta(hours=-3))
VERSAO_BANCO = 4  # PRAGMA user_version esperado; versões menores migram com backup
PREFIXO_MUDAE = "$"
COMANDOS_MUDAE = {"w", "wa", "wg", "wx", "h", "ha", "hg", "hx", "m", "ma", "mg", "mx"}
LIMITE_XP_POR_MINUTO = 15  # mensagens por minuto que valem XP (rolls do Mudae não entram)
BACKUPS_EXTRAS = 4  # cópias semanais guardadas em backup_extra_dir
CATALOGO = tags.CATALOGO
NOMES_BOTS_MUSICA = ("pancake", "jockie")
RE_PEDIDO = re.compile(r"^\s*[mp]!\s*(?:play|p)\b", re.I)
YOUTUBE_OEMBED = "https://www.youtube.com/oembed"  # títulos de links: públicos, sem chave
SPOTIFY_OEMBED = "https://open.spotify.com/oembed"
RE_TRACO = re.compile(r"\s+[-–—]\s+")  # Pancake: "Título - Artista"
RE_BY = re.compile(r"\s+by\s+", re.I)  # Jockie:  "Título by Artista"
RE_JOCKIE = re.compile(r"started playing\s*(.+)", re.I | re.S)
RE_LINK = re.compile(r"https?://[^\s<>]+", re.I)
RE_ID = re.compile(r"[\w-]+")
TIPOS_SPOTIFY = {
    "track": "música",
    "album": "álbum",
    "playlist": "playlist",
    "artist": "artista",
    "episode": "episódio",
    "show": "podcast",
}


def limpar(txt: str) -> str:
    txt = re.sub(r"\]\(https?://[^)]+\)", "]", txt)  # tira link markdown
    return re.sub(r"[\[\]*_`]", "", txt).strip()


def normalizar(txt: str) -> str:
    """Ignora maiúsculas e espaços extras."""
    return re.sub(r"\s+", " ", txt.casefold()).strip()


def normalizar_artista(artista: str) -> str:
    # o YouTube às vezes coloca "Artista - Topic"
    return normalizar(re.sub(r"\s*-\s*topic$", "", artista, flags=re.I))


def chave_musica(titulo: str, artista: str) -> str:
    """Identifica a música de forma única: título + artista normalizados."""
    return f"{normalizar(titulo)}|{normalizar_artista(artista)}"


def dividir(nome: str, separador):
    """Divide no ÚLTIMO separador (assim 'Stand by Me by Ben E. King' funciona)."""
    achados = list(separador.finditer(nome))
    if not achados:
        return nome, ""
    m = achados[-1]
    titulo, artista = nome[: m.start()].strip(), nome[m.end() :].strip()
    if not titulo or not artista:
        return nome, ""
    return titulo, artista


def extrair_musica(msg):
    """Retorna (titulo, artista) se a mensagem for um aviso de 'tocando agora', senão None."""
    if not msg.author.bot:
        return None
    nome_autor = msg.author.name.lower()
    if NOMES_BOTS_MUSICA and not any(n in nome_autor for n in NOMES_BOTS_MUSICA):
        return None

    for e in msg.embeds:
        titulo = e.title or ""
        desc = e.description or ""

        # Pancake: título "Now Playing", "Título - Artista" na 1ª linha da descrição
        if "now playing" in titulo.lower():
            linhas = [l for l in desc.split("\n") if l.strip()]
            if linhas:
                nome = limpar(linhas[0])
                if nome:
                    return dividir(nome, RE_TRACO)

        # Jockie: "Started playing [Título by Artista](link)"
        m = RE_JOCKIE.search(f"{titulo}\n{desc}")
        if m:
            nome = limpar(m.group(1).split("\n")[0])
            if nome:
                return dividir(nome, RE_BY)
    return None


def eh_pedido(msg) -> bool:
    """True se uma PESSOA escreveu m!play / p!play (ou m!p / p!p)."""
    return (not msg.author.bot) and bool(RE_PEDIDO.match(getattr(msg, "content", "") or ""))


def consulta_do_pedido(conteudo):
    """Tira o 'm!play' do começo e devolve só o que a pessoa pediu.
    Retorna None se o texto não foi guardado (pedido antigo) e '' se não sobrou nada."""
    if conteudo is None:
        return None
    return RE_PEDIDO.sub("", conteudo, count=1).strip()


def sem_acento(texto):
    return "".join(
        c for c in unicodedata.normalize("NFKD", normalizar(texto)) if not unicodedata.combining(c)
    )


def eh_mudae(msg):
    if msg.author.bot:
        return False
    partes = (getattr(msg, "content", "") or "").strip().casefold().split()
    return bool(
        partes
        and partes[0].startswith(PREFIXO_MUDAE)
        and partes[0][len(PREFIXO_MUDAE) :] in COMANDOS_MUDAE
    )


def limites_periodo(tipo, periodo):
    if tipo == "mes":
        ano, mes = map(int, periodo.split("-"))
        inicio = datetime(ano, mes, 1, tzinfo=FUSO)
        fim = datetime(ano + (mes == 12), 1 if mes == 12 else mes + 1, 1, tzinfo=FUSO)
    else:
        ano = int(periodo)
        inicio, fim = datetime(ano, 1, 1, tzinfo=FUSO), datetime(ano + 1, 1, 1, tzinfo=FUSO)
    return discord.utils.time_snowflake(inicio), discord.utils.time_snowflake(fim)


def chave_deezer(texto):
    return normalizar((texto or "").strip()[:100])


def consulta_musical(conteudo):
    """Exclui playlists/álbuns do favorito; normaliza links sem misturar faixas."""
    texto = consulta_do_pedido(conteudo)
    if not texto:
        return None
    analise = analisar_link(texto)
    if analise:
        url, _, tipo = analise
        if tipo not in ("", "música"):
            return None
        return url, texto
    if RE_LINK.search(texto):
        return None  # Link desconhecido não é classificado como faixa individual.
    return normalizar(texto), texto


def backup_sqlite(con, destino):
    """Snapshot consistente, comprimido e publicado por rename atômico."""
    destino = Path(destino)
    destino.parent.mkdir(parents=True, exist_ok=True)
    temporario = destino.with_suffix(".sqlite.tmp")
    comprimido = destino.with_suffix(".gz.tmp")
    try:
        # sqlite3.Connection.__exit__ finaliza a transação, mas não fecha o arquivo.
        # O Windows exige o fechamento explícito antes de excluir o temporário.
        with closing(sqlite3.connect(temporario)) as copia:
            con.backup(copia)
        with temporario.open("rb") as origem, gzip.open(comprimido, "wb") as saida:
            shutil.copyfileobj(origem, saida)
        comprimido.replace(destino)
    finally:
        temporario.unlink(missing_ok=True)
        comprimido.unlink(missing_ok=True)


def copiar_backup_extra(origem, pasta_extra, agora=None):
    """Uma cópia por semana em outra pasta (nuvem, HD externo). Nunca derruba o backup diário."""
    if not pasta_extra:
        return None
    try:
        pasta = Path(pasta_extra)
        pasta.mkdir(parents=True, exist_ok=True)
        agora = agora or time.time()
        existentes = sorted(pasta.glob("memi_????-??-??.db.gz"))
        if existentes and agora - existentes[-1].stat().st_mtime < 6.5 * 86400:
            return None
        destino = pasta / Path(origem).name
        shutil.copy2(origem, destino)
        os.utime(destino, (agora, agora))
        for velho in sorted(pasta.glob("memi_????-??-??.db.gz"))[:-BACKUPS_EXTRAS]:
            velho.unlink()
        return destino
    except OSError:
        logging.exception("Não consegui copiar o backup para %s.", pasta_extra)
        return None


def backup_antes_migracao(caminho):
    caminho = Path(caminho)
    if not caminho.exists():
        return
    destino = (
        caminho.parent / "backups" / ("migracao_" + datetime.now(FUSO).strftime("%Y%m%d_%H%M%S_%f"))
    )
    destino.mkdir(parents=True, exist_ok=True)
    for codigo in Path(__file__).parent.glob("*.py"):
        if not codigo.name.startswith("test_"):
            shutil.copy2(codigo, destino / codigo.name)
    with closing(sqlite3.connect(caminho)) as origem:
        backup_sqlite(origem, destino / "memi.db.gz")


def analisar_link(texto: str):
    """(url_canonica, endpoint_oembed, tipo) se o texto tem um link de YouTube/Spotify que dá pra ler;
    senão None. O link é limpo (sem ?si=, idioma /intl-pt/ etc.) pra o cache não se repetir."""
    achado = RE_LINK.search(texto or "")
    if not achado:
        return None
    try:
        u = urlparse(achado.group(0).rstrip(".,;"))
    except ValueError:
        return None
    host = (u.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    partes = [p for p in u.path.split("/") if p]
    params = parse_qs(u.query)

    if host in ("youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"):
        # Links watch?v=...&list=... também representam uma playlist.
        lista = (params.get("list") or [""])[0]
        if lista and RE_ID.fullmatch(lista):
            return f"https://www.youtube.com/playlist?list={lista}", YOUTUBE_OEMBED, "playlist"
        video = ""
        if host == "youtu.be" and partes:
            video = partes[0]
        elif len(partes) > 1 and partes[0] == "shorts":
            video = partes[1]
        elif partes and partes[0] == "watch":
            video = (params.get("v") or [""])[0]
        if video and RE_ID.fullmatch(video):
            return f"https://www.youtube.com/watch?v={video}", YOUTUBE_OEMBED, ""
        lista = (params.get("list") or [""])[0]
        if partes and partes[0] == "playlist" and lista and RE_ID.fullmatch(lista):
            return f"https://www.youtube.com/playlist?list={lista}", YOUTUBE_OEMBED, "playlist"
        return None

    if host == "open.spotify.com":
        if partes and partes[0].startswith("intl-"):
            partes = partes[1:]
        if len(partes) >= 2 and partes[0] in TIPOS_SPOTIFY and RE_ID.fullmatch(partes[1]):
            return (
                f"https://open.spotify.com/{partes[0]}/{partes[1]}",
                SPOTIFY_OEMBED,
                TIPOS_SPOTIFY[partes[0]],
            )
    return None


class BancoLegado:
    """
    musicas -> UMA linha por música (título + artista únicos). Nunca repete.
    tocadas -> UMA linha por vez que tocou (ID da mensagem do Discord é a chave: não duplica).
    pedidos -> UMA linha por mensagem de pessoa pedindo música (ID da mensagem: não duplica).
    """

    def __init__(self, caminho):
        self.con = sqlite3.connect(caminho)

        # Esquemas antigos incompatíveis exigem migração específica; nunca descartar.
        colunas = [r[1] for r in self.con.execute("PRAGMA table_info(tocadas)")]
        if colunas and "musica_id" not in colunas:
            raise RuntimeError("Esquema antigo incompatível; nenhum dado foi apagado.")

        self.con.executescript("""
            CREATE TABLE IF NOT EXISTS musicas (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                chave         TEXT NOT NULL UNIQUE,
                titulo        TEXT NOT NULL,
                artista       TEXT NOT NULL DEFAULT '',
                artista_chave TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS tocadas (
                message_id INTEGER PRIMARY KEY,
                channel_id INTEGER NOT NULL,
                musica_id  INTEGER NOT NULL REFERENCES musicas(id),
                bot        TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_tocadas_musica ON tocadas(musica_id);
            CREATE TABLE IF NOT EXISTS pedidos (
                message_id INTEGER PRIMARY KEY,
                channel_id INTEGER NOT NULL,
                usuario_id INTEGER NOT NULL,
                conteudo   TEXT
            );
            CREATE TABLE IF NOT EXISTS canais_lidos (
                channel_id    INTEGER PRIMARY KEY,
                ultimo_msg_id INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS deezer_cache (
                chave      TEXT PRIMARY KEY,
                capa       TEXT NOT NULL DEFAULT '',
                genero     TEXT NOT NULL DEFAULT '',
                buscado_em INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS links_cache (
                chave      TEXT PRIMARY KEY,
                nome       TEXT NOT NULL DEFAULT '',
                buscado_em INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS atividade (
                autor_id  INTEGER PRIMARY KEY,
                eh_bot    INTEGER NOT NULL,
                mensagens INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS atividade_scan (
                id    INTEGER PRIMARY KEY CHECK (id = 1),
                quando INTEGER NOT NULL
            );
            """)

        # banco criado por versão anterior: ganha a coluna do artista normalizado
        if "artista_chave" not in [r[1] for r in self.con.execute("PRAGMA table_info(musicas)")]:
            self.con.execute(
                "ALTER TABLE musicas ADD COLUMN artista_chave TEXT NOT NULL DEFAULT ''"
            )
            for mid, artista in self.con.execute("SELECT id, artista FROM musicas").fetchall():
                self.con.execute(
                    "UPDATE musicas SET artista_chave = ? WHERE id = ?",
                    (normalizar_artista(artista), mid),
                )

        # banco criado antes do perfil/wrapped: a tabela de pedidos ganha a coluna do texto
        if "conteudo" not in [r[1] for r in self.con.execute("PRAGMA table_info(pedidos)")]:
            self.con.execute("ALTER TABLE pedidos ADD COLUMN conteudo TEXT")
        self.con.commit()

    def total(self) -> int:
        return self.con.execute("SELECT COUNT(*) FROM tocadas").fetchone()[0]

    def total_pedidos(self) -> int:
        return self.con.execute("SELECT COUNT(*) FROM pedidos").fetchone()[0]

    def inserir(self, linhas):
        """linhas: (message_id, channel_id, titulo, artista, bot). Mensagem repetida é ignorada."""
        cur = self.con.cursor()
        for message_id, channel_id, titulo, artista, bot in linhas:
            chave = chave_musica(titulo, artista)
            cur.execute(
                "INSERT OR IGNORE INTO musicas (chave, titulo, artista, artista_chave) "
                "VALUES (?, ?, ?, ?)",
                (chave, titulo, artista, normalizar_artista(artista)),
            )
            musica_id = cur.execute("SELECT id FROM musicas WHERE chave = ?", (chave,)).fetchone()[
                0
            ]
            cur.execute(
                "INSERT OR IGNORE INTO tocadas (message_id, channel_id, musica_id, bot) "
                "VALUES (?, ?, ?, ?)",
                (message_id, channel_id, musica_id, bot),
            )
        self.con.commit()

    def marca(self, channel_id: int) -> int:
        row = self.con.execute(
            "SELECT ultimo_msg_id FROM canais_lidos WHERE channel_id = ?", (channel_id,)
        ).fetchone()
        return row[0] if row else 0

    def salvar_marca(self, channel_id: int, msg_id: int):
        self.con.execute(
            "INSERT INTO canais_lidos (channel_id, ultimo_msg_id) VALUES (?, ?) "
            "ON CONFLICT(channel_id) DO UPDATE SET ultimo_msg_id = excluded.ultimo_msg_id",
            (channel_id, msg_id),
        )
        self.con.commit()

    def ranking_musicas(self, desde=0, ate=None):
        """[(titulo, artista, vezes_tocada), ...] da mais tocada pra menos tocada.
        desde/ate: só conta IDs de mensagem em [desde, ate) (o ID carrega a data)."""
        filtro = "t.message_id >= ?" + ("" if ate is None else " AND t.message_id < ?")
        parametros = (desde,) if ate is None else (desde, ate)
        return self.con.execute(
            "SELECT m.titulo, m.artista, COUNT(*) AS c "
            "FROM tocadas t JOIN musicas m ON m.id = t.musica_id "
            f"WHERE {filtro} "
            "GROUP BY m.id ORDER BY c DESC, m.titulo COLLATE NOCASE",
            parametros,
        ).fetchall()

    def ranking_artistas(self, desde=0, ate=None):
        """[(artista, musicas_diferentes, vezes_tocadas), ...] por nº de músicas ÚNICAS."""
        filtro = "t.message_id >= ?" + ("" if ate is None else " AND t.message_id < ?")
        parametros = (desde,) if ate is None else (desde, ate)
        return self.con.execute(
            "SELECT MIN(m.artista) AS artista, "
            "       COUNT(DISTINCT m.id) AS musicas, "
            "       COUNT(t.message_id) AS tocadas "
            "FROM musicas m JOIN tocadas t ON t.musica_id = m.id "
            f"WHERE m.artista_chave <> '' AND {filtro} "
            "GROUP BY m.artista_chave "
            "ORDER BY musicas DESC, tocadas DESC, artista COLLATE NOCASE",
            parametros,
        ).fetchall()

    def ranking_usuarios(self, desde=0):
        """[(usuario_id, pedidos), ...] de quem mais pediu pra quem menos pediu."""
        return self.con.execute(
            "SELECT usuario_id, COUNT(*) AS c FROM pedidos WHERE message_id >= ? "
            "GROUP BY usuario_id ORDER BY c DESC, usuario_id",
            (desde,),
        ).fetchall()

    def pedidos_do_usuario(self, usuario_id, desde=0):
        """[(message_id, conteudo), ...] dos pedidos da pessoa, do mais antigo pro mais novo."""
        return self.con.execute(
            "SELECT message_id, conteudo FROM pedidos "
            "WHERE usuario_id = ? AND message_id >= ? ORDER BY message_id",
            (usuario_id, desde),
        ).fetchall()

    def musica_aleatoria(self):
        """(titulo, artista, vezes_tocada) de uma música sorteada do histórico, ou None."""
        return self.con.execute(
            "SELECT m.titulo, m.artista, COUNT(t.message_id) "
            "FROM musicas m JOIN tocadas t ON t.musica_id = m.id "
            "GROUP BY m.id ORDER BY RANDOM() LIMIT 1"
        ).fetchone()

    def musica_top_do_artista(self, artista: str, desde=0):
        """(titulo, artista) da música mais tocada desse artista (usada pra pegar a capa)."""
        return self.con.execute(
            "SELECT m.titulo, m.artista FROM musicas m JOIN tocadas t ON t.musica_id = m.id "
            "WHERE m.artista_chave = ? AND t.message_id >= ? "
            "GROUP BY m.id ORDER BY COUNT(*) DESC LIMIT 1",
            (normalizar_artista(artista), desde),
        ).fetchone()

    def cache_deezer(self, chave: str):
        """(capa, genero, buscado_em) guardados pra essa chave, ou None."""
        return self.con.execute(
            "SELECT capa, genero, buscado_em FROM deezer_cache WHERE chave = ?", (chave,)
        ).fetchone()

    def salvar_cache_deezer(self, chave: str, capa: str, genero: str, quando: int):
        self.con.execute(
            "INSERT INTO deezer_cache (chave, capa, genero, buscado_em) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(chave) DO UPDATE SET capa = excluded.capa, genero = excluded.genero, "
            "buscado_em = excluded.buscado_em",
            (chave, capa, genero, quando),
        )
        self.con.commit()

    def cache_link(self, chave: str):
        """(nome, buscado_em) guardados pra esse link, ou None."""
        return self.con.execute(
            "SELECT nome, buscado_em FROM links_cache WHERE chave = ?", (chave,)
        ).fetchone()

    def salvar_cache_link(self, chave: str, nome: str, quando: int):
        self.con.execute(
            "INSERT INTO links_cache (chave, nome, buscado_em) VALUES (?, ?, ?) "
            "ON CONFLICT(chave) DO UPDATE SET nome = excluded.nome, buscado_em = excluded.buscado_em",
            (chave, nome, quando),
        )
        self.con.commit()

    def historico_tocadas(self):
        """[(message_id, titulo, artista, bot, channel_id), ...] em ordem cronológica."""
        return self.con.execute(
            "SELECT t.message_id, m.titulo, m.artista, t.bot, t.channel_id "
            "FROM tocadas t JOIN musicas m ON m.id = t.musica_id ORDER BY t.message_id"
        ).fetchall()

    def historico_pedidos(self):
        """[(message_id, usuario_id, conteudo, channel_id), ...] em ordem cronológica."""
        return self.con.execute(
            "SELECT message_id, usuario_id, conteudo, channel_id FROM pedidos ORDER BY message_id"
        ).fetchall()


class Banco(BancoLegado):
    """Estende o esquema original sem apagar tabelas nem recriar o histórico."""

    pasta_backup_extra = ""  # definida pelo config.json em memi_bot.py

    FONTES = {
        "mensagens": ("mensagens", "autor_id", "mensagens", "ultima_msg"),
        "pedidos": ("pedidos", "usuario_id", "pedidos", "ultimo_pedido"),
        "mudae": ("mudae", "usuario_id", "roletadas", "ultima_roletada"),
    }

    def __init__(self, caminho):
        self.caminho = Path(caminho)
        versao = 0
        if self.caminho.exists():
            with closing(sqlite3.connect(caminho)) as antes:
                versao = antes.execute("PRAGMA user_version").fetchone()[0]
                colunas = [r[1] for r in antes.execute("PRAGMA table_info(tocadas)")]
            # 3: resumos, marcos de nível e cor do perfil; 4: níveis de 1 a 100, tags unificadas e
            # Mudae tracker. Toda migração começa com uma cópia do banco e do código em backups/.
            if versao < VERSAO_BANCO:
                backup_antes_migracao(caminho)
            if colunas and "musica_id" not in colunas:
                raise RuntimeError(
                    "Banco muito antigo: preservado no backup; a migração precisa ser revisada. Nenhuma tabela foi apagada."
                )
        super().__init__(caminho)
        self.con.execute("PRAGMA journal_mode=WAL")
        # Em WAL, NORMAL não faz fsync a cada commit: o banco continua íntegro após uma queda
        # (só as últimas transações podem se perder, e o histórico é relido do Discord).
        self.con.execute("PRAGMA synchronous=NORMAL")
        self.con.execute("PRAGMA busy_timeout=5000")
        self.con.create_function("sem_acento", 1, sem_acento, deterministic=True)
        self.con.create_function("chave_deezer", 1, chave_deezer, deterministic=True)
        self.con.executescript("""
            CREATE TABLE IF NOT EXISTS mensagens (
                message_id INTEGER PRIMARY KEY, autor_id INTEGER NOT NULL,
                canal_id INTEGER NOT NULL, eh_bot INTEGER NOT NULL);
            CREATE INDEX IF NOT EXISTS idx_mensagens_autor ON mensagens(autor_id, message_id);
            CREATE TABLE IF NOT EXISTS mudae (message_id INTEGER PRIMARY KEY, usuario_id INTEGER NOT NULL);
            CREATE INDEX IF NOT EXISTS idx_mudae_usuario ON mudae(usuario_id, message_id);
            CREATE INDEX IF NOT EXISTS idx_pedidos_usuario ON pedidos(usuario_id, message_id);
            CREATE TABLE IF NOT EXISTS autores (
                usuario_id INTEGER PRIMARY KEY, eh_bot INTEGER NOT NULL DEFAULT 0, nome TEXT NOT NULL DEFAULT '');
            CREATE TABLE IF NOT EXISTS totais (
                usuario_id INTEGER PRIMARY KEY,
                mensagens INTEGER NOT NULL DEFAULT 0, ultima_msg INTEGER NOT NULL DEFAULT 0,
                pedidos INTEGER NOT NULL DEFAULT 0, ultimo_pedido INTEGER NOT NULL DEFAULT 0,
                roletadas INTEGER NOT NULL DEFAULT 0, ultima_roletada INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS perfil_usuario (
                usuario_id INTEGER PRIMARY KEY, frase TEXT NOT NULL DEFAULT '',
                favorita TEXT NOT NULL DEFAULT '', capa TEXT NOT NULL DEFAULT '',
                titulo TEXT NOT NULL DEFAULT '', cor INTEGER);
            CREATE TABLE IF NOT EXISTS posses (
                usuario_id INTEGER NOT NULL, item TEXT NOT NULL, detalhe TEXT NOT NULL DEFAULT '',
                titulo INTEGER NOT NULL, insignia INTEGER NOT NULL, desbloqueado_em INTEGER NOT NULL,
                PRIMARY KEY (usuario_id, item, detalhe));
            CREATE TABLE IF NOT EXISTS periodos_fechados (
                tipo TEXT NOT NULL, periodo TEXT NOT NULL, fechado_em INTEGER NOT NULL,
                PRIMARY KEY (tipo, periodo));
            CREATE TABLE IF NOT EXISTS resumos (
                tipo TEXT NOT NULL, periodo TEXT NOT NULL, dados TEXT NOT NULL,
                estado TEXT NOT NULL DEFAULT 'pendente', PRIMARY KEY (tipo, periodo));
            CREATE TABLE IF NOT EXISTS niveis_marco (
                usuario_id INTEGER PRIMARY KEY, marco INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS avisos_nivel (
                id INTEGER PRIMARY KEY AUTOINCREMENT, usuario_id INTEGER NOT NULL,
                marco INTEGER NOT NULL, estado TEXT NOT NULL DEFAULT 'pendente',
                UNIQUE (usuario_id, marco));
            CREATE TABLE IF NOT EXISTS estado (chave TEXT PRIMARY KEY, valor TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS progresso_mensagens (
                channel_id INTEGER PRIMARY KEY, ultimo_msg_id INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS avisos (
                id INTEGER PRIMARY KEY AUTOINCREMENT, chave TEXT NOT NULL UNIQUE,
                usuario_id INTEGER NOT NULL, item TEXT NOT NULL, titulo INTEGER NOT NULL,
                insignia INTEGER NOT NULL, detalhe TEXT NOT NULL DEFAULT '',
                estado TEXT NOT NULL DEFAULT 'pendente');
            CREATE TABLE IF NOT EXISTS classificacao_pedidos (
                chave TEXT PRIMARY KEY, texto TEXT NOT NULL, nome TEXT NOT NULL DEFAULT '',
                genero TEXT NOT NULL DEFAULT '', consultado_em INTEGER NOT NULL DEFAULT 0);
        """)
        mudae_tracker.criar_tabelas(self.con)
        if "sem_xp" not in [r[1] for r in self.con.execute("PRAGMA table_info(totais)")]:
            self.con.execute("ALTER TABLE totais ADD COLUMN sem_xp INTEGER NOT NULL DEFAULT 0")
        if "cor" not in [r[1] for r in self.con.execute("PRAGMA table_info(perfil_usuario)")]:
            self.con.execute("ALTER TABLE perfil_usuario ADD COLUMN cor INTEGER")
        if versao < 2:
            with self.con:
                self.con.execute(
                    "INSERT OR IGNORE INTO autores(usuario_id, eh_bot) SELECT autor_id, eh_bot FROM atividade"
                )
                self.con.execute(
                    "INSERT OR IGNORE INTO autores(usuario_id) SELECT DISTINCT usuario_id FROM pedidos"
                )
                for fonte, (tabela, autor, contagem, ultimo) in self.FONTES.items():
                    for uid, n, mid in self.con.execute(
                        f"SELECT {autor}, COUNT(*), MAX(message_id) FROM {tabela} GROUP BY {autor}"
                    ).fetchall():
                        self.con.execute(
                            "INSERT OR IGNORE INTO totais(usuario_id) VALUES (?)", (uid,)
                        )
                        self.con.execute(
                            f"UPDATE totais SET {contagem}=?, {ultimo}=? WHERE usuario_id=?",
                            (n, mid, uid),
                        )
                for (conteudo,) in self.con.execute(
                    "SELECT DISTINCT conteudo FROM pedidos WHERE conteudo IS NOT NULL"
                ).fetchall():
                    self._registrar_consulta(conteudo)
                self.con.execute("PRAGMA user_version=2")
        if versao < 3:
            with self.con:
                self.con.execute("PRAGMA user_version=3")
        if versao < 4:
            with self.con:
                # Níveis mudaram de escala (1-1000 -> 1-100): os marcos antigos não valem mais.
                # Sem marco guardado, a próxima avaliação só grava o nível (nenhum aviso retroativo).
                self.con.execute("DELETE FROM niveis_marco")
                self.con.execute("DELETE FROM avisos_nivel")
                # Tags unificadas: tudo que a pessoa já tem vira título e insígnia.
                self.con.execute("UPDATE posses SET titulo=1, insignia=1")
                self.con.execute("PRAGMA user_version=4")

    def estado(self, chave, padrao=""):
        row = self.con.execute("SELECT valor FROM estado WHERE chave=?", (chave,)).fetchone()
        return row[0] if row else padrao

    def definir_estado(self, chave, valor):
        with self.con:
            self.con.execute(
                "INSERT INTO estado VALUES (?,?) ON CONFLICT(chave) DO UPDATE SET valor=excluded.valor",
                (chave, str(valor)),
            )

    def marca(self, channel_id):
        row = self.con.execute(
            "SELECT ultimo_msg_id FROM progresso_mensagens WHERE channel_id=?", (channel_id,)
        ).fetchone()
        return row[0] if row else 0

    def _marca(self, channel_id, msg_id):
        self.con.execute(
            "INSERT INTO progresso_mensagens VALUES (?,?) ON CONFLICT(channel_id) DO UPDATE "
            "SET ultimo_msg_id=MAX(ultimo_msg_id, excluded.ultimo_msg_id)",
            (channel_id, msg_id),
        )

    def salvar_marca(self, channel_id, msg_id):
        with self.con:
            self._marca(channel_id, msg_id)

    def _registrar_consulta(self, conteudo):
        consulta = consulta_musical(conteudo)
        if consulta:
            self.con.execute(
                "INSERT OR IGNORE INTO classificacao_pedidos(chave,texto) VALUES (?,?)", consulta
            )

    def _incrementar(self, uid, fonte, mid):
        _, _, contador, ultimo = self.FONTES[fonte]
        self.con.execute("INSERT OR IGNORE INTO totais(usuario_id) VALUES (?)", (uid,))
        self.con.execute(
            f"UPDATE totais SET {contador}={contador}+1,{ultimo}=MAX({ultimo},?) WHERE usuario_id=?",
            (mid, uid),
        )

    def _passou_limite_xp(self, uid, mid):
        """True se a pessoa já mandou mensagens demais no último minuto (rolls não contam)."""
        inicio = mid - (60_000 << 22)
        return (
            self.con.execute(
                "SELECT COUNT(*) FROM mensagens g WHERE autor_id=? AND message_id>? AND message_id<=? "
                "AND NOT EXISTS (SELECT 1 FROM mudae m WHERE m.message_id=g.message_id)",
                (uid, inicio, mid),
            ).fetchone()[0]
            > LIMITE_XP_POR_MINUTO
        )

    def receber(
        self, msg, *, avancar=False, recompensar=False, avisar=False, elegiveis=None, limitar_xp=False
    ):
        """Mensagem, seus eventos e recompensas são salvos na mesma transação."""
        with self.con:
            uid, mid = msg.author.id, msg.id
            self.con.execute(
                "INSERT INTO autores VALUES (?,?,?) ON CONFLICT(usuario_id) DO UPDATE "
                "SET eh_bot=excluded.eh_bot,nome=excluded.nome",
                (uid, int(msg.author.bot), msg.author.display_name),
            )
            nova = self.con.execute(
                "INSERT OR IGNORE INTO mensagens VALUES (?,?,?,?)",
                (mid, uid, msg.channel.id, int(msg.author.bot)),
            ).rowcount
            if nova:
                self._incrementar(uid, "mensagens", mid)
                if (
                    limitar_xp
                    and not msg.author.bot
                    and not eh_mudae(msg)
                    and self._passou_limite_xp(uid, mid)
                ):
                    self.con.execute("UPDATE totais SET sem_xp=sem_xp+1 WHERE usuario_id=?", (uid,))
            pedido_novo = 0
            if eh_pedido(msg):
                conteudo = (msg.content or "")[:2000]
                pedido_novo = self.con.execute(
                    "INSERT OR IGNORE INTO pedidos VALUES (?,?,?,?)",
                    (mid, msg.channel.id, uid, conteudo),
                ).rowcount
                self.con.execute(
                    "UPDATE pedidos SET conteudo=? WHERE message_id=? AND (conteudo IS NULL OR conteudo='')",
                    (conteudo, mid),
                )
                self._registrar_consulta(conteudo)
                if pedido_novo:
                    self._incrementar(uid, "pedidos", mid)
            if eh_mudae(msg):
                if self.con.execute(
                    "INSERT OR IGNORE INTO mudae VALUES (?,?)", (mid, uid)
                ).rowcount:
                    self._incrementar(uid, "mudae", mid)
            if not msg.author.bot:
                mudae_tracker.registrar_nomes(self.con, msg.author)
            elif mudae_tracker.eh_do_mudae(msg):
                self._registrar_mudae(msg)
            musica = extrair_musica(msg)
            if musica:
                t, a = musica
                chave = chave_musica(t, a)
                self.con.execute(
                    "INSERT OR IGNORE INTO musicas(chave,titulo,artista,artista_chave) VALUES (?,?,?,?)",
                    (chave, t, a, normalizar_artista(a)),
                )
                musica_id = self.con.execute(
                    "SELECT id FROM musicas WHERE chave=?", (chave,)
                ).fetchone()[0]
                self.con.execute(
                    "INSERT OR IGNORE INTO tocadas VALUES (?,?,?,?)",
                    (mid, msg.channel.id, musica_id, msg.author.name),
                )
            if avancar:
                self._marca(msg.channel.id, mid)
            if recompensar and not msg.author.bot:
                self._avaliar_usuario(uid, avisar)
                self._rotativos(avisar, str(mid), elegiveis)
        return bool(nova), bool(pedido_novo)

    def _registrar_mudae(self, msg):
        """Uma mensagem estranha do Mudae não pode impedir o resto da leitura."""
        self.con.execute("SAVEPOINT mudae_msg")
        try:
            mudae_tracker.registrar(self.con, msg)
        except Exception:  # noqa: BLE001
            self.con.execute("ROLLBACK TO mudae_msg")
            logging.exception("Mensagem %s do Mudae ignorada.", msg.id)
        finally:
            self.con.execute("RELEASE mudae_msg")

    def _consulta_ranking(self, fonte, desde=0, ate=None, eh_bot=None):
        """SQL (já ordenado) e parâmetros de um ranking de contagens por pessoa."""
        tabela, autor, contador, ultimo = self.FONTES[fonte]
        parametros = []
        # Comandos de roleta do Mudae não contam como conversa no ranking de mensagens.
        quantidade = "(t.mensagens-t.roletadas)" if fonte == "mensagens" else f"t.{contador}"
        if not desde and ate is None:
            sql = f"SELECT t.usuario_id, {quantidade}, t.{ultimo} FROM totais t LEFT JOIN autores a ON a.usuario_id=t.usuario_id WHERE {quantidade}>0"
            ordem = f" ORDER BY {quantidade} DESC,t.{ultimo},t.usuario_id"
        else:
            sql = f"SELECT t.{autor},COUNT(*),MAX(t.message_id) FROM {tabela} t LEFT JOIN autores a ON a.usuario_id=t.{autor} WHERE t.message_id>=?"
            parametros.append(desde)
            if ate is not None:
                sql += " AND t.message_id<?"
                parametros.append(ate)
            if fonte == "mensagens":
                sql += " AND NOT EXISTS (SELECT 1 FROM mudae x WHERE x.message_id=t.message_id)"
            ordem = f" GROUP BY t.{autor} ORDER BY COUNT(*) DESC,MAX(t.message_id),t.{autor}"
        if eh_bot is not None:
            sql += " AND COALESCE(a.eh_bot,0)=?"
            parametros.append(int(eh_bot))
        return sql + ordem, parametros

    def ranking_contagens(self, fonte, desde=0, ate=None, eh_bot=None, elegiveis=None):
        sql, parametros = self._consulta_ranking(fonte, desde, ate, eh_bot)
        return [
            (uid, n)
            for uid, n, _ in self.con.execute(sql, parametros)
            if elegiveis is None or uid in elegiveis
        ]

    def lider(self, fonte, eh_bot=None, elegiveis=None):
        """Quem ocupa o 1º lugar do total histórico (ou None), sem montar o ranking inteiro."""
        sql, parametros = self._consulta_ranking(fonte, eh_bot=eh_bot)
        for uid, _, _ in self.con.execute(sql, parametros):
            if elegiveis is None or uid in elegiveis:
                return uid
        return None

    def ranking_usuarios(self, desde=0, elegiveis=None):
        return self.ranking_contagens("pedidos", desde, eh_bot=False, elegiveis=elegiveis)

    def ranking_atividade(self, eh_bot=None, desde=0):
        return self.ranking_contagens("mensagens", desde, eh_bot=eh_bot)

    def total_usuario(self, uid, fonte, desde=0):
        tabela, autor, contador, _ = self.FONTES[fonte]
        if not desde:
            row = self.con.execute(
                f"SELECT {contador} FROM totais WHERE usuario_id=?", (uid,)
            ).fetchone()
            return row[0] if row else 0
        return self.con.execute(
            f"SELECT COUNT(*) FROM {tabela} WHERE {autor}=? AND message_id>=?", (uid, desde)
        ).fetchone()[0]

    def ranking_generos(self, desde=0, genero=None):
        sql = "FROM tocadas t JOIN musicas m ON m.id=t.musica_id LEFT JOIN deezer_cache d ON d.chave=chave_deezer(m.titulo||' '||m.artista) WHERE t.message_id>=?"
        if genero is not None:
            return self.con.execute(
                "SELECT m.titulo,m.artista,COUNT(*) "
                + sql
                + " AND sem_acento(COALESCE(d.genero,''))=? GROUP BY m.id ORDER BY COUNT(*) DESC,MAX(t.message_id),m.id",
                (desde, sem_acento(genero)),
            ).fetchall()
        return self.con.execute(
            "SELECT MIN(d.genero),COUNT(*),COUNT(DISTINCT m.id) "
            + sql
            + " AND COALESCE(d.genero,'')<>'' GROUP BY sem_acento(d.genero) ORDER BY COUNT(*) DESC,MIN(d.genero)",
            (desde,),
        ).fetchall()

    def generos_pendentes(self, desde=0):
        return self.con.execute(
            "SELECT COUNT(DISTINCT m.id) FROM musicas m JOIN tocadas t ON t.musica_id=m.id LEFT JOIN deezer_cache d ON d.chave=chave_deezer(m.titulo||' '||m.artista) WHERE t.message_id>=? AND COALESCE(d.genero,'')=''",
            (desde,),
        ).fetchone()[0]

    def consultas_usuario(self, uid):
        contagem, exemplos = Counter(), {}
        for _, conteudo in self.pedidos_do_usuario(uid):
            consulta = consulta_musical(conteudo)
            if consulta:
                chave, texto = consulta
                contagem[chave] += 1
                exemplos.setdefault(chave, texto)
        return contagem, exemplos

    def perfil(self, uid):
        row = self.con.execute(
            "SELECT frase,favorita,capa,titulo,cor FROM perfil_usuario WHERE usuario_id=?", (uid,)
        ).fetchone()
        return dict(zip(("frase", "favorita", "capa", "titulo", "cor"), row or ("",) * 4 + (None,)))

    def salvar_perfil(self, uid, **campos):
        if not set(campos) <= {"frase", "favorita", "capa", "titulo", "cor"}:
            raise ValueError("Campo de perfil inválido")
        with self.con:
            self.con.execute("INSERT OR IGNORE INTO perfil_usuario(usuario_id) VALUES (?)", (uid,))
            for campo, valor in campos.items():
                self.con.execute(
                    f"UPDATE perfil_usuario SET {campo}=? WHERE usuario_id=?", (valor, uid)
                )

    def itens(self, uid, tipo="titulo"):
        """[(tag, quantidade)] da pessoa, na ordem em que foram ganhas. `tipo` existe por
        compatibilidade: desde a v2.2 toda tag é título e insígnia."""
        if tipo not in ("titulo", "insignia"):
            raise ValueError("Tipo inválido")
        return [
            (item, n)
            for item, n in self.con.execute(
                f"SELECT item,COUNT(*) FROM posses WHERE usuario_id=? AND {tipo}=1 GROUP BY item ORDER BY MIN(desbloqueado_em),item",
                (uid,),
            )
            if item in CATALOGO
        ]

    def tags_usuario(self, uid):
        """[(tag, quantidade, último detalhe)] na ordem das categorias do catálogo; o detalhe é
        o período mais recente (ex.: '2026-08') das tags de mês/ano."""
        linhas = self.con.execute(
            "SELECT item, COUNT(*), MAX(detalhe), MIN(desbloqueado_em) FROM posses "
            "WHERE usuario_id=? GROUP BY item",
            (uid,),
        ).fetchall()
        ordem = {c: i for i, c in enumerate(tags.CATEGORIAS)}
        posicao = {k: i for i, k in enumerate(CATALOGO)}
        return [
            (item, n, detalhe)
            for item, n, detalhe, _ in sorted(
                (x for x in linhas if x[0] in CATALOGO),
                key=lambda x: (ordem[CATALOGO[x[0]]["categoria"]], posicao[x[0]]),
            )
        ]

    def selecionar_titulo(self, uid, nome):
        ident = tags.buscar(nome)
        if ident and any(item == ident for item, _ in self.itens(uid)):
            self.salvar_perfil(uid, titulo=ident)
            return True
        return False

    def _conceder(self, uid, item, detalhe="", avisar=False, chave_aviso=None, tipo=None):
        """Registra a tag (título e insígnia). `tipo` é ignorado (compatibilidade)."""
        CATALOGO[item]  # identificador desconhecido é erro de programação
        novo = self.con.execute(
            "INSERT OR IGNORE INTO posses VALUES (?,?,?,1,1,?)",
            (uid, item, detalhe, int(time.time())),
        ).rowcount
        if not novo:
            return False
        if avisar:
            self.con.execute(
                "INSERT OR IGNORE INTO avisos(chave,usuario_id,item,titulo,insignia,detalhe) VALUES (?,?,?,1,1,?)",
                (chave_aviso or f"{uid}:{item}:{detalhe}", uid, item, detalhe),
            )
        return True

    def conceder_manual(self, uid, nome, eh_bot=False):
        """Concede uma tag manual (Cartola, eventos...). True se era nova; ValueError se o nome não
        for de uma tag manual ou se o alvo for um bot."""
        if eh_bot:
            raise ValueError("Tags são só para pessoas, não para bots.")
        item = tags.buscar(nome)
        if item is None or not CATALOGO[item]["manual"]:
            raise ValueError(
                "Tag inexistente ou automática. Tags manuais: "
                + ", ".join(CATALOGO[k]["nome"] for k in tags.manuais())
            )
        with self.con:
            if self.con.execute(
                "SELECT 1 FROM posses WHERE usuario_id=? AND item=?", (uid, item)
            ).fetchone():
                return False
            return self._conceder(uid, item, "manual")

    def xp_usuario(self, uid):
        row = self.con.execute(
            "SELECT mensagens-sem_xp, pedidos, roletadas FROM totais WHERE usuario_id=?", (uid,)
        ).fetchone()
        return progressao.xp(*row) if row else 0

    def ranking_xp(self, eh_bot=False, elegiveis=None):
        """[(uid, xp)] do maior para o menor XP (desempate: quem chegou antes)."""
        sql = (
            "SELECT t.usuario_id, (t.mensagens-t.sem_xp)*? + t.pedidos*? + t.roletadas/? AS xp "
            "FROM totais t LEFT JOIN autores a ON a.usuario_id=t.usuario_id "
            "WHERE COALESCE(a.eh_bot,0)=? "
            "ORDER BY xp DESC, MAX(t.ultima_msg, t.ultimo_pedido, t.ultima_roletada), t.usuario_id"
        )
        parametros = (
            progressao.XP_POR_MENSAGEM,
            progressao.XP_POR_PEDIDO,
            progressao.ROLETADAS_POR_XP,
            int(eh_bot),
        )
        return [
            (uid, xp)
            for uid, xp in self.con.execute(sql, parametros)
            if xp > 0 and (elegiveis is None or uid in elegiveis)
        ]

    def _avaliar_usuario(self, uid, avisar):
        autor = self.con.execute("SELECT eh_bot FROM autores WHERE usuario_id=?", (uid,)).fetchone()
        if autor and autor[0]:
            return
        atual = progressao.nivel(self.xp_usuario(uid))
        guardado = self.con.execute(
            "SELECT marco FROM niveis_marco WHERE usuario_id=?", (uid,)
        ).fetchone()
        # niveis_marco.marco guarda o último nível avaliado (1 a 100).
        if guardado is None or atual > guardado[0]:
            self.con.execute(
                "INSERT INTO niveis_marco(usuario_id, marco) VALUES (?,?) "
                "ON CONFLICT(usuario_id) DO UPDATE SET marco=excluded.marco",
                (uid, atual),
            )
            # A primeira avaliação só grava (evita avisar níveis antigos ao atualizar o bot).
            if avisar and guardado is not None and atual > 1:
                # Recuperação offline reaplica mensagem por mensagem: fica só o nível mais alto,
                # mas uma troca de patente no meio do caminho continua sendo anunciada.
                trocas = ",".join(str(int(x["minimo"])) for x in progressao.PATENTES)
                self.con.execute(
                    "DELETE FROM avisos_nivel WHERE usuario_id=? AND estado='pendente' AND marco<? "
                    f"AND marco NOT IN ({trocas})",
                    (uid, atual),
                )
                for item in progressao.PATENTES:
                    if guardado[0] < item["minimo"] < atual:
                        self.con.execute(
                            "INSERT OR IGNORE INTO avisos_nivel(usuario_id, marco) VALUES (?,?)",
                            (uid, item["minimo"]),
                        )
                self.con.execute(
                    "INSERT OR IGNORE INTO avisos_nivel(usuario_id, marco) VALUES (?,?)",
                    (uid, atual),
                )
        for item in progressao.patentes_ate(atual):
            self._conceder(uid, tags.id_patente(item))  # o aviso de nível já anuncia a patente
        for fonte, limites in (
            ("mensagens", tags.METAS_MENSAGENS),
            ("pedidos", tags.METAS_MUSICAS),
        ):
            n = self.total_usuario(uid, fonte)
            for limite, item in limites:
                if n >= limite:
                    self._conceder(uid, item, avisar=avisar)
        if self.total_usuario(uid, "mudae") >= tags.ROLETADAS_ROLETADOR:
            self._conceder(uid, "roletador", avisar=avisar)

    def _rotativos(self, avisar, evento, elegiveis=None):
        for fonte, item in (("mensagens", "tagarela_chat"), ("pedidos", "dj_call")):
            vencedor = self.lider(fonte, eh_bot=False, elegiveis=elegiveis)
            antigos = [
                r[0]
                for r in self.con.execute(
                    "SELECT DISTINCT usuario_id FROM posses WHERE item=?", (item,)
                )
            ]
            for uid in antigos:
                if uid != vencedor:
                    self.con.execute(
                        "DELETE FROM posses WHERE usuario_id=? AND item=?", (uid, item)
                    )
                    self.con.execute(
                        "UPDATE perfil_usuario SET titulo='' WHERE usuario_id=? AND titulo=?",
                        (uid, item),
                    )
            if vencedor is not None and vencedor not in antigos:
                self._conceder(
                    vencedor, item, "geral", avisar, f"rotativo:{item}:{vencedor}:{evento}"
                )

    def reconciliar(self, *, avisar=False, elegiveis=None):
        with self.con:
            for (uid,) in self.con.execute(
                "SELECT usuario_id FROM autores WHERE eh_bot=0"
            ).fetchall():
                self._avaliar_usuario(uid, avisar)
            self._rotativos(avisar, str(time.time_ns()), elegiveis)

    def resumo_periodo(self, tipo, periodo, elegiveis=None):
        """Números do período fechado, prontos para virar JSON (só pessoas, sem bots)."""
        desde, ate = limites_periodo(tipo, periodo)
        totais = {}
        for fonte in ("mensagens", "pedidos"):
            tabela, autor, _, _ = self.FONTES[fonte]
            totais[fonte] = self.con.execute(
                f"SELECT COUNT(*) FROM {tabela} t LEFT JOIN autores a ON a.usuario_id=t.{autor} "
                "WHERE t.message_id>=? AND t.message_id<? AND COALESCE(a.eh_bot,0)=0"
                + (
                    " AND NOT EXISTS (SELECT 1 FROM mudae x WHERE x.message_id=t.message_id)"
                    if fonte == "mensagens"
                    else ""
                ),
                (desde, ate),
            ).fetchone()[0]

        def topo(fonte):
            linhas = self.ranking_contagens(fonte, desde, ate, False, elegiveis)
            return [[uid, n] for uid, n in linhas[:3]]

        return {
            "djs": topo("pedidos"),
            "tagarelas": topo("mensagens"),
            "mensagens": totais["mensagens"],
            "pedidos": totais["pedidos"],
            "musicas": [[t, a, q] for t, a, q in self.ranking_musicas(desde, ate)[:5]],
            "artistas": [[a, n, q] for a, n, q in self.ranking_artistas(desde, ate)[:5]],
            "mudae": mudae_tracker.resumo(self.con, desde, ate),
        }

    def _guardar_resumo(self, tipo, periodo, elegiveis=None):
        dados = self.resumo_periodo(tipo, periodo, elegiveis)
        if not (dados["mensagens"] or dados["pedidos"]):
            return
        self.con.execute(
            "INSERT OR IGNORE INTO resumos(tipo, periodo, dados) VALUES (?,?,?)",
            (tipo, periodo, json.dumps(dados, ensure_ascii=False)),
        )

    def resumos_pendentes(self, limite=5):
        """[(tipo, periodo, dados)] a enviar: por ano, o mês antes do ano, depois o período."""
        return [
            (tipo, periodo, json.loads(dados))
            for tipo, periodo, dados in self.con.execute(
                "SELECT tipo, periodo, dados FROM resumos WHERE estado='pendente' "
                "ORDER BY substr(periodo,1,4), tipo='ano', periodo LIMIT ?",
                (limite,),
            ).fetchall()
        ]

    def marcar_resumo(self, tipo, periodo, estado):
        with self.con:
            self.con.execute(
                "UPDATE resumos SET estado=? WHERE tipo=? AND periodo=?", (estado, tipo, periodo)
            )

    def ler(self, sql, parametros=()):
        """Consulta só de leitura numa conexão própria: pode rodar em outra thread sem travar o
        bot enquanto o disco está ocupado."""
        uri = self.caminho.resolve().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=30)) as con:
            con.create_function("sem_acento", 1, sem_acento, deterministic=True)
            con.create_function("chave_deezer", 1, chave_deezer, deterministic=True)
            return con.execute(sql, parametros).fetchall()

    def inicio_meses_abertos(self):
        """Snowflake do começo do mês seguinte ao último mês fechado (0 se nenhum foi fechado)."""
        ultimo = self.con.execute(
            "SELECT MAX(periodo) FROM periodos_fechados WHERE tipo='mes'"
        ).fetchone()[0]
        if not ultimo:
            return 0
        ano, mes = map(int, ultimo.split("-"))
        return limites_periodo("mes", f"{ano + (mes == 12)}-{1 if mes == 12 else mes + 1:02d}")[0]

    SQL_MESES = (
        "SELECT DISTINCT strftime('%Y-%m', ((message_id >> 22)+1420070400000)/1000, 'unixepoch','-3 hours') FROM mensagens WHERE message_id>=? "
        "UNION SELECT DISTINCT strftime('%Y-%m', ((message_id >> 22)+1420070400000)/1000, 'unixepoch','-3 hours') FROM pedidos WHERE message_id>=?"
    )

    def meses_ativos(self, desde=0, *, isolado=False):
        """Meses ('AAAA-MM', Brasília) com mensagens ou pedidos a partir do snowflake `desde`.
        Com um `desde`, a busca usa a chave primária e lê só o trecho recente do banco."""
        parametros = (desde, desde)
        linhas = (
            self.ler(self.SQL_MESES, parametros)
            if isolado
            else self.con.execute(self.SQL_MESES, parametros).fetchall()
        )
        return [r[0] for r in linhas if r[0]]

    def fechar_periodos(self, agora=None, *, avisar=False, elegiveis=None, datas=None, completo=False):
        """Fecha os meses e anos encerrados. Por padrão só olha as mensagens depois do último mês
        fechado; `completo` varre o histórico inteiro (importação e mm!scan, que podem trazer
        meses antigos). `datas` permite passar os meses já consultados em outra thread."""
        agora = (agora or datetime.now(FUSO)).astimezone(FUSO)
        if datas is None:
            datas = self.meses_ativos(0 if completo else self.inicio_meses_abertos())
        periodos = [("mes", p) for p in datas if p < agora.strftime("%Y-%m")]
        # Anos: os das mensagens novas e os dos meses já fechados (o fechamento do ano pode ter
        # ficado para depois do fechamento do seu último mês).
        anos = {p[:4] for p in datas} | {
            r[0][:4]
            for r in self.con.execute("SELECT periodo FROM periodos_fechados WHERE tipo='mes'")
        }
        anos -= {
            r[0] for r in self.con.execute("SELECT periodo FROM periodos_fechados WHERE tipo='ano'")
        }
        periodos += [("ano", p) for p in sorted(anos) if p < str(agora.year)]
        fechados = []
        for tipo, p in sorted(periodos, key=lambda x: limites_periodo(*x)[1]):
            with self.con:
                if self.con.execute(
                    "SELECT 1 FROM periodos_fechados WHERE tipo=? AND periodo=?", (tipo, p)
                ).fetchone():
                    continue
                desde, ate = limites_periodo(tipo, p)
                for fonte, prefixo in (("mensagens", "tagarela"), ("pedidos", "dj")):
                    rank = self.ranking_contagens(fonte, desde, ate, False, elegiveis)
                    if rank:
                        self._conceder(rank[0][0], prefixo + "_" + tipo, p, avisar)
                if avisar:
                    self._guardar_resumo(tipo, p, elegiveis)
                self.con.execute(
                    "INSERT INTO periodos_fechados VALUES (?,?,?)", (tipo, p, int(time.time()))
                )
                fechados.append((tipo, p))
        return fechados

    def avisos_nivel_pendentes(self, limite=10):
        return self.con.execute(
            "SELECT id, usuario_id, marco FROM avisos_nivel WHERE estado='pendente' "
            "ORDER BY id LIMIT ?",
            (limite,),
        ).fetchall()

    def marcar_aviso_nivel(self, aviso_id, estado):
        with self.con:
            self.con.execute("UPDATE avisos_nivel SET estado=? WHERE id=?", (estado, aviso_id))

    def atualizar_mudae(self, msg):
        """Edição de uma mensagem do Mudae (o dono entra no rodapé do roll ao casar)."""
        with self.con:
            return mudae_tracker.atualizar_roll(self.con, msg)

    def canais_do_mudae(self):
        return [
            r[0]
            for r in self.con.execute(
                "SELECT DISTINCT canal_id FROM mensagens WHERE autor_id=?",
                (mudae_tracker.MUDAE_ID,),
            )
        ]

    def hall(self, tipo):
        """[(periodo, dj_uid, tagarela_uid)] dos períodos fechados, do mais recente ao mais antigo."""
        linhas = {}
        for chave, item in (("dj", f"dj_{tipo}"), ("tagarela", f"tagarela_{tipo}")):
            for uid, periodo in self.con.execute(
                "SELECT usuario_id, detalhe FROM posses WHERE item=? "
                "AND detalhe GLOB '[0-9][0-9][0-9][0-9]*'",
                (item,),
            ):
                linhas.setdefault(periodo, {})[chave] = uid
        return [
            (periodo, valores.get("dj"), valores.get("tagarela"))
            for periodo, valores in sorted(linhas.items(), reverse=True)
        ]

    def backup_diario(self, agora=None, *, isolado=False):
        """Uma cópia comprimida por dia, guardando as 7 mais recentes.
        isolado=True usa uma conexão própria, para rodar em outra thread sem travar o bot."""
        dia = (agora or datetime.now(FUSO)).astimezone(FUSO).strftime("%Y-%m-%d")
        pasta = self.caminho.parent / "backups"
        destino = pasta / f"memi_{dia}.db.gz"
        if not destino.exists():
            if isolado:
                with closing(sqlite3.connect(self.caminho)) as copia:
                    backup_sqlite(copia, destino)
            else:
                backup_sqlite(self.con, destino)
            for velho in sorted(pasta.glob("memi_????-??-??.db.gz"))[:-7]:
                velho.unlink()
            copiar_backup_extra(destino, self.pasta_backup_extra)
        return destino
