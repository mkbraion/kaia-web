# -*- coding: utf-8 -*-
"""Escreve o anuncio pronto para o Facebook Marketplace.

Por padrao trabalha 100% offline: monta o texto a partir da descricao que a
propria REMAX ja publica, das caracteristicas do imovel e de uma ficha tecnica
sempre exata (nunca inventa numero). Varia as aberturas para que 300 anuncios
nao saiam com a mesma cara.

Opcionalmente usa um LLM externo, se voce configurar em config.json:
    "provedor_ia": "groq",  "api_key_ia": "gsk_..."
Provedores aceitos: local (padrao), groq, gemini.
"""

import json
import re
import urllib.request

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) KAIAImoveis/1.0"


# ---------------------------------------------------------------- formatacao

def moeda(v):
    if not v:
        return "Consulte"
    return "R$ " + ("{:,.0f}".format(float(v))).replace(",", ".")


def _medida(v, unidade="m2"):
    if not v:
        return None
    return "%s %s" % ("{:g}".format(float(v)), unidade)


def _plural(n, sing, plur):
    n = int(n)
    return "%d %s" % (n, sing if n == 1 else plur)


def titulo(im):
    partes = [im.get("tipo") or "Imovel"]
    if im.get("quartos"):
        partes.append("%dQ" % int(im["quartos"]))
    area = im.get("area_util") or im.get("area_total")
    if area:
        partes.append(_medida(area))
    if im.get("bairro"):
        partes.append("- %s" % im["bairro"])
    partes.append("- %s" % moeda(im.get("preco")))
    return " ".join(p for p in partes if p)


def ficha(im):
    """Dados objetivos. Sempre corretos - nunca passam pela IA."""
    linhas = []

    def add(rot, val):
        if val:
            linhas.append("%s %s" % (rot, val))

    # Ficam de fora a pedido: Valor, Tipo, Ano, IPTU (nao mostra preco),
    # e tambem Area util e Andar.
    add("Quartos:", int(im["quartos"]) if im.get("quartos") else None)
    add("Suites:", int(im["suites"]) if im.get("suites") else None)
    add("Banheiros:", int(im["banheiros"]) if im.get("banheiros") else None)
    add("Vagas:", int(im["vagas"]) if im.get("vagas") else None)
    add("Area total:", _medida(im.get("area_total")))
    add("Condominio:", moeda(im["condominio"]) if im.get("condominio") else None)
    add("Local:", ", ".join(p for p in [im.get("bairro"), im.get("cidade"),
                                        im.get("uf")] if p))
    return "\n".join(linhas)


def _assinatura(cfg, im):
    l = []
    if cfg.get("seu_whatsapp"):
        zap = re.sub(r"\D", "", str(cfg["seu_whatsapp"]))
        if zap:
            l.append("Chama no WhatsApp: https://wa.me/%s" % zap)
    quem = (cfg.get("seu_nome") or "").strip()
    if cfg.get("seu_creci"):
        quem = ("%s - CRECI %s" % (quem, cfg["seu_creci"])).strip(" -")
    if quem:
        l.append(quem)
    return "\n".join(l)


# ------------------------------------------------------------ gerador local

# Aberturas por tipo. A escolha e estavel por imovel (hash do codigo),
# entao o mesmo imovel sempre gera o mesmo texto, mas a lista inteira varia.
ABERTURAS = {
    "casa": [
        "Casa pronta para morar {local}.",
        "Sua proxima casa pode estar {local}.",
        "Otima casa a venda {local}.",
        "Casa bem localizada {local}, digna de visita.",
    ],
    "apartamento": [
        "Apartamento muito bem localizado {local}.",
        "Apartamento pronto para morar {local}.",
        "Excelente apartamento a venda {local}.",
        "Apartamento com otima planta {local}.",
    ],
    "terreno": [
        "Terreno a venda {local}.",
        "Otimo terreno para construir {local}.",
        "Terreno bem localizado {local}.",
    ],
    "_": [
        "Otima oportunidade {local}.",
        "Imovel a venda {local}.",
        "Excelente oportunidade {local}.",
    ],
}


def _onde(im):
    """Trecho de localizacao ja com preposicao ('no bairro X, em Y').

    Usa 'no bairro X' porque o genero do nome do bairro varia (o Centro,
    a Nossa Senhora de Fatima) e 'em Centro' sai torto.
    """
    bairro, cidade = (im.get("bairro") or "").strip(), (im.get("cidade") or "").strip()
    if bairro and cidade:
        return "no bairro %s, em %s" % (bairro, cidade)
    if bairro:
        return "no bairro %s" % bairro
    if cidade:
        return "em %s" % cidade
    return "em otima localizacao"

# Caracteristicas que realmente vendem num anuncio de Marketplace
PESO = ("piscina churrasqueira sacada varanda elevador portaria mobiliad "
        "suite garagem academia salao playground quintal jardim vista "
        "condominio seguranca hidromassagem closet escritorio lareira").split()


def _hash(txt):
    h = 0
    for ch in str(txt):
        h = (h * 31 + ord(ch)) & 0xFFFFFFFF
    return h


def _limpar(txt, limite=520):
    txt = txt or ""
    # a descricao da REMAX vem com HTML solto (<br />, <p>, &nbsp;)
    txt = re.sub(r"(?i)<\s*br\s*/?\s*>", " ", txt)
    txt = re.sub(r"<[^>]+>", " ", txt)
    txt = (txt.replace("&nbsp;", " ").replace("&amp;", "&")
              .replace("&quot;", '"').replace("&#39;", "'")
              .replace("&lt;", "<").replace("&gt;", ">"))
    txt = re.sub(r"\s+", " ", txt).strip()
    # tira contatos/links que venham da descricao original
    txt = re.sub(r"(https?://\S+|www\.\S+)", "", txt)
    txt = re.sub(r"\(?\d{2}\)?\s*9?\d{4}[-\s]?\d{4}", "", txt)
    txt = re.sub(r"\s+", " ", txt).strip()
    if len(txt) <= limite:
        return txt
    corte = txt[:limite]
    ponto = max(corte.rfind("."), corte.rfind("!"))
    return corte[:ponto + 1] if ponto > 180 else corte.rstrip() + "..."


# Destaques que nao entram no anuncio (a pedido: nada sobre idade da construcao)
_FORA_DESTAQUE = ("constru", "nova construcao", "nova construção",
                  "construcao antiga", "construção antiga")


def _fora(c):
    b = c.lower().strip()
    return any(t in b for t in _FORA_DESTAQUE)


def _destaques(im, quantos=6):
    """Ordena as caracteristicas colocando as que mais vendem primeiro."""
    itens = [c for c in (im.get("caracteristicas") or [])
             if c and len(c) > 2 and not _fora(c)]
    vistos, unicos = set(), []
    for c in itens:
        k = c.lower().strip()
        if k not in vistos:
            vistos.add(k)
            unicos.append(c)

    def nota(c):
        b = c.lower()
        for i, p in enumerate(PESO):
            if p in b:
                return i
        return len(PESO)

    return sorted(unicos, key=nota)[:quantos]


def _resumo_specs(im):
    p = []
    if im.get("quartos"):
        s = _plural(im["quartos"], "quarto", "quartos")
        if im.get("suites"):
            s += " (%s)" % _plural(im["suites"], "suite", "suites")
        p.append(s)
    if im.get("banheiros"):
        p.append(_plural(im["banheiros"], "banheiro", "banheiros"))
    if im.get("vagas"):
        p.append(_plural(im["vagas"], "vaga de garagem", "vagas de garagem"))
    # usa area total (a util foi removida a pedido)
    area = im.get("area_total")
    if area:
        p.append("%s de area" % _medida(area))
    if not p:
        return ""
    if len(p) == 1:
        return "Sao %s." % p[0]
    return "Sao %s e %s." % (", ".join(p[:-1]), p[-1])


def _local(im, cfg):
    tipo = (im.get("tipo") or "").lower()
    chave = ("casa" if "casa" in tipo or "sobrado" in tipo
             else "apartamento" if "apart" in tipo or "cobertura" in tipo or "studio" in tipo
             else "terreno" if "terreno" in tipo or "lote" in tipo
             else "_")
    opcoes = ABERTURAS[chave]
    abertura = opcoes[_hash(im.get("codigo", "")) % len(opcoes)].format(local=_onde(im))

    blocos = [abertura]

    specs = _resumo_specs(im)
    if specs:
        blocos.append(specs)

    # A descricao longa da REMAX foi removida a pedido: o anuncio fica
    # so com a abertura, o resumo, os destaques e a ficha.

    dest = _destaques(im)
    if dest:
        blocos.append("Destaques:\n" + "\n".join("- %s" % d for d in dest))

    blocos.append(ficha(im))
    blocos.append("Agende sua visita sem compromisso.")
    blocos.append(_assinatura(cfg, im))
    return "\n\n".join(b for b in blocos if b)


# --------------------------------------------------------- LLM (opcional)

def _prompt(im):
    return (
        "Voce e um corretor de imoveis brasileiro escrevendo um anuncio para o "
        "Facebook Marketplace. Escreva em portugues do Brasil, tom direto e "
        "vendedor. NAO invente nenhuma informacao que nao esteja nos dados. "
        "Nao use markdown nem asteriscos. Maximo 5 linhas curtas. "
        "Nao repita a ficha tecnica, ela sera adicionada depois. "
        "Termine pedindo para chamar no WhatsApp.\n\n"
        "DADOS:\n%s\n\nDESCRICAO ORIGINAL:\n%s"
        % (ficha(im), (im.get("descricao_original") or "")[:900])
    )


def _groq(im, cfg):
    req = urllib.request.Request(
        "https://api.groq.com/openai/v1/chat/completions",
        data=json.dumps({
            "model": cfg.get("modelo_ia") or "llama-3.3-70b-versatile",
            "messages": [{"role": "user", "content": _prompt(im)}],
            "temperature": 0.8, "max_tokens": 400,
        }).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": UA,
                 "Authorization": "Bearer %s" % cfg["api_key_ia"]},
        method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        j = json.loads(r.read().decode("utf-8"))
    return j["choices"][0]["message"]["content"].strip()


def _gemini(im, cfg):
    modelo = cfg.get("modelo_ia") or "gemini-2.0-flash"
    url = ("https://generativelanguage.googleapis.com/v1beta/models/"
           "%s:generateContent?key=%s" % (modelo, cfg["api_key_ia"]))
    req = urllib.request.Request(
        url,
        data=json.dumps({"contents": [{"parts": [{"text": _prompt(im)}]}]}).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": UA},
        method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        j = json.loads(r.read().decode("utf-8"))
    return j["candidates"][0]["content"]["parts"][0]["text"].strip()


def escrever(im, cfg):
    """Devolve (titulo, texto_do_anuncio, origem)."""
    prov = (cfg.get("provedor_ia") or "local").lower()
    if prov in ("groq", "gemini") and cfg.get("api_key_ia"):
        try:
            bruto = _groq(im, cfg) if prov == "groq" else _gemini(im, cfg)
            bruto = re.sub(r"[*#`]", "", bruto).strip()
            if len(bruto) >= 60:
                texto = "%s\n\n%s\n\n%s" % (bruto, ficha(im), _assinatura(cfg, im))
                return titulo(im), texto, prov
        except Exception:
            pass  # cai no gerador local
    return titulo(im), _local(im, cfg), "local"
