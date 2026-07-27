# -*- coding: utf-8 -*-
"""Banco local (SQLite). Guarda o historico e evita republicar o mesmo imovel."""

import json
import os
import sqlite3
import time

CAMINHO = os.path.join("dados", "kaia.db")

NOVO = "novo"              # coletado, texto ainda nao gerado
PRONTO = "pronto"          # texto + fotos prontos para publicar
PUBLICADO = "publicado"    # ja postei no Marketplace
IGNORADO = "ignorado"      # nao quero publicar este


def conectar():
    os.makedirs(os.path.dirname(CAMINHO), exist_ok=True)
    # timeout: o painel pode estar lendo enquanto a coleta grava
    con = sqlite3.connect(CAMINHO, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("""
        CREATE TABLE IF NOT EXISTS imoveis (
            codigo        TEXT PRIMARY KEY,
            dados         TEXT NOT NULL,
            preco         REAL,
            texto         TEXT,
            titulo        TEXT,
            status        TEXT NOT NULL DEFAULT 'novo',
            pasta_fotos   TEXT,
            visto_em      REAL,
            publicado_em  REAL,
            mudou_preco   INTEGER DEFAULT 0,
            preco_antigo  REAL
        )
    """)
    _migrar(con)
    con.commit()
    return con


def _migrar(con):
    """Adiciona colunas novas em bancos criados por versoes anteriores."""
    existentes = {l["name"] for l in con.execute("PRAGMA table_info(imoveis)")}
    novas = {
        "fb_post_id": "TEXT",     # id do post na Pagina
        "fb_em": "REAL",          # quando foi publicado
        "fb_erro": "TEXT",        # ultimo erro, se falhou
        "fb_tentativas": "INTEGER DEFAULT 0",
    }
    for nome, tipo in novas.items():
        if nome not in existentes:
            con.execute("ALTER TABLE imoveis ADD COLUMN %s %s" % (nome, tipo))


def salvar(con, imovel):
    """Insere ou atualiza. Devolve 'novo', 'preco' ou 'igual'."""
    cod = imovel["codigo"]
    agora = time.time()
    linha = con.execute("SELECT preco, status FROM imoveis WHERE codigo=?", (cod,)).fetchone()
    blob = json.dumps(imovel, ensure_ascii=False)

    if linha is None:
        con.execute(
            "INSERT INTO imoveis (codigo, dados, preco, status, visto_em) VALUES (?,?,?,?,?)",
            (cod, blob, imovel.get("preco"), NOVO, agora))
        con.commit()
        return "novo"

    antigo = linha["preco"]
    novo_preco = imovel.get("preco")
    if antigo is not None and novo_preco is not None and float(antigo) != float(novo_preco):
        # preco mudou: vale a pena republicar com o valor novo
        con.execute(
            """UPDATE imoveis SET dados=?, preco=?, visto_em=?, mudou_preco=1,
               preco_antigo=?, status=? WHERE codigo=?""",
            (blob, novo_preco, agora, antigo, NOVO, cod))
        con.commit()
        return "preco"

    con.execute("UPDATE imoveis SET dados=?, visto_em=? WHERE codigo=?", (blob, agora, cod))
    con.commit()
    return "igual"


def sem_texto(con):
    return con.execute(
        "SELECT codigo, dados FROM imoveis WHERE (texto IS NULL OR texto='') AND status!=?",
        (IGNORADO,)).fetchall()


def gravar_texto(con, codigo, titulo, texto, pasta):
    con.execute(
        "UPDATE imoveis SET titulo=?, texto=?, pasta_fotos=?, status=? WHERE codigo=?",
        (titulo, texto, pasta, PRONTO, codigo))
    con.commit()


def listar(con, status=None):
    if status:
        cur = con.execute(
            "SELECT * FROM imoveis WHERE status=? ORDER BY mudou_preco DESC, visto_em DESC",
            (status,))
    else:
        cur = con.execute(
            "SELECT * FROM imoveis ORDER BY mudou_preco DESC, visto_em DESC")
    return cur.fetchall()


def marcar(con, codigo, status):
    quando = time.time() if status == PUBLICADO else None
    con.execute("UPDATE imoveis SET status=?, publicado_em=?, mudou_preco=0 WHERE codigo=?",
                (status, quando, codigo))
    con.commit()


def fila_facebook(con, limite=10, max_tentativas=3):
    """Imoveis com texto pronto que ainda nao foram para a Pagina.

    Ordena por preco mudado primeiro (vale republicar) e depois pelos mais
    recentes. Ignora os que ja falharam varias vezes, para nao ficar batendo
    de cabeca no mesmo erro todo dia.
    """
    return con.execute(
        """SELECT * FROM imoveis
           WHERE fb_post_id IS NULL
             AND status != ?
             AND texto IS NOT NULL AND texto != ''
             AND COALESCE(fb_tentativas,0) < ?
           ORDER BY mudou_preco DESC, visto_em DESC
           LIMIT ?""",
        (IGNORADO, max_tentativas, limite)).fetchall()


def gravar_facebook(con, codigo, post_id=None, erro=None):
    if post_id:
        con.execute("""UPDATE imoveis SET fb_post_id=?, fb_em=?, fb_erro=NULL
                       WHERE codigo=?""", (post_id, time.time(), codigo))
    else:
        con.execute("""UPDATE imoveis
                       SET fb_erro=?, fb_tentativas=COALESCE(fb_tentativas,0)+1
                       WHERE codigo=?""", (erro, codigo))
    con.commit()


def publicados_hoje(con):
    """Quantos foram para a Pagina desde a meia-noite de hoje."""
    inicio = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
    l = con.execute("SELECT COUNT(*) n FROM imoveis WHERE fb_em >= ?",
                    (inicio,)).fetchone()
    return l["n"] if l else 0


def contagem(con):
    linhas = con.execute("SELECT status, COUNT(*) n FROM imoveis GROUP BY status").fetchall()
    return {l["status"]: l["n"] for l in linhas}
