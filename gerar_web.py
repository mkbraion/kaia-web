# -*- coding: utf-8 -*-
"""Gera os dados criptografados do app de celular (dados.enc).

O app (web/index.html) fica hospedado no GitHub Pages e busca este arquivo.
Os dados sao criptografados (AES-256-GCM, chave derivada da senha por
PBKDF2) - mesmo sendo um link publico, ninguem le sem a senha. O celular
descriptografa no proprio navegador.

Senha: vem da variavel de ambiente KAIA_WEB_PASSWORD (na nuvem, um Secret).
"""

import json
import os
import secrets
import sqlite3
import struct
import sys
import time

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes

PASTA = os.path.dirname(os.path.abspath(__file__))
BANCO = os.path.join(PASTA, "dados", "kaia.db")
SAIDA_DIR = os.path.join(PASTA, "web")
ITERACOES = 200000        # PBKDF2 - o mesmo numero no JS


def _registros(con):
    """Monta a lista enxuta que o celular precisa (texto + campos + fotos)."""
    itens = []
    for l in con.execute("SELECT codigo, titulo, texto, status, fb_em, dados "
                         "FROM imoveis").fetchall():
        d = json.loads(l["dados"])
        cor = d.get("corretor") or {}
        itens.append({
            "codigo": l["codigo"],
            "titulo": l["titulo"] or "",
            "texto": l["texto"] or "",
            "status": l["status"],
            "fb_em": l["fb_em"],
            "tipo": d.get("tipo"),
            "preco": d.get("preco"),
            "quartos": d.get("quartos"),
            "banheiros": d.get("banheiros"),
            "vagas": d.get("vagas"),
            "area": d.get("area_total"),
            "bairro": d.get("bairro"),
            "cidade": d.get("cidade"),
            "endereco": d.get("endereco"),
            "fotos": d.get("fotos") or [],
            "link": d.get("link"),
            "corretor": {
                "nome": cor.get("nome"),
                "telefone": cor.get("telefone"),
                "whatsapp": cor.get("whatsapp"),
                "creci": cor.get("creci"),
                "imobiliaria": cor.get("imobiliaria"),
            } if cor else None,
        })
    return itens


def _criptografar(texto_bytes, senha):
    salt = secrets.token_bytes(16)
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt,
                     iterations=ITERACOES)
    chave = kdf.derive(senha.encode("utf-8"))
    iv = secrets.token_bytes(12)
    ct = AESGCM(chave).encrypt(iv, texto_bytes, None)   # ct ja inclui o tag
    return salt + iv + ct                                # 16 + 12 + resto


def gerar(senha=None, log=print):
    senha = senha or os.environ.get("KAIA_WEB_PASSWORD")
    if not senha:
        log("Falta a senha (KAIA_WEB_PASSWORD).")
        return None

    con = sqlite3.connect(BANCO)
    con.row_factory = sqlite3.Row
    dados = {"gerado_em": int(time.time()), "itens": _registros(con)}
    con.close()

    bruto = json.dumps(dados, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    blob = _criptografar(bruto, senha)

    os.makedirs(SAIDA_DIR, exist_ok=True)
    destino = os.path.join(SAIDA_DIR, "dados.enc")
    with open(destino, "wb") as f:
        f.write(blob)

    log("web/dados.enc gerado: %d imoveis, %.1f KB (cifrado)"
        % (len(dados["itens"]), len(blob) / 1024))
    return destino


if __name__ == "__main__":
    os.chdir(PASTA)
    s = sys.argv[1] if len(sys.argv) > 1 else None
    gerar(senha=s)
