# -*- coding: utf-8 -*-
"""Cliente da busca publica da REMAX Brasil.

O site alimenta a listagem com um indice Azure Search exposto em
/search/listing-search/docs/search (JSON, sem autenticacao). Consultamos
o mesmo endpoint que o navegador usa, com paginacao e ritmo controlado.
"""

import json
import os
import time
import urllib.request

BUSCA_URL = "https://www.remax.com.br/search/listing-search/docs/search"
AGENTE_URL = "https://www.remax.com.br/search/agent-search/docs/search"
LOOKUPS_URL = "https://www.remax.com.br/locales_v2/pt-BR/lookups.json"
TRADUZ_URL = "https://www.remax.com.br/locales_v2/pt-BR/translate.json"
CDN_FOTO = "https://cdn.gryphtech.com/userimages/{regiao}/LargeWM/{arquivo}"
SITE = "https://www.remax.com.br/"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# Brasil dentro do indice global da REMAX
MACRO_REGIAO_BR = 55

PAUSA = 1.2  # segundos entre paginas: educado com o servidor


def _post(corpo, tentativas=3, url=BUSCA_URL):
    dados = json.dumps(corpo).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=dados,
        headers={
            "Content-Type": "application/json",
            "User-Agent": UA,
            "Accept": "application/json",
            "Referer": "https://www.remax.com.br/listings",
            "Origin": "https://www.remax.com.br",
        },
        method="POST",
    )
    for n in range(tentativas):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception:
            if n == tentativas - 1:
                raise
            time.sleep(2 * (n + 1))
    return {}


# cache em memoria: varios imoveis costumam ser do mesmo corretor
_CACHE_AGENTE = {}


def _fone_limpo(*valores):
    """Primeiro telefone nao-vazio, mantendo a formatacao original."""
    for v in valores:
        if v and str(v).strip() and str(v).strip() != "+55":
            return str(v).strip()
    return ""


def buscar_corretor(agent_id):
    """Nome e contato do corretor dono do anuncio, via agent-search.

    Uso interno (so aparece no painel): serve para o usuario saber com quem
    falar na REMAX sobre um imovel. Nunca entra no texto publicado.
    """
    if not agent_id:
        return None
    chave = str(agent_id)
    if chave in _CACHE_AGENTE:
        return _CACHE_AGENTE[chave]

    try:
        r = _post({"top": 1, "filter": "content/AgentId eq %s" % chave},
                  url=AGENTE_URL)
        v = r.get("value") or []
        c = (v[0].get("content") if v else None) or {}
    except Exception:
        c = {}

    info = {
        "nome": c.get("AgentName") or (
            ("%s %s" % (c.get("FirstName") or "", c.get("LastName") or "")).strip()) or "",
        "telefone": _fone_limpo(c.get("AgentPhone"), c.get("AgentDirectDialPhone")),
        "whatsapp": _fone_limpo(c.get("WhatsApp")),
        "email": c.get("AgentEmail") or "",
        "creci": c.get("AgentLicenseNumber") or "",
        "imobiliaria": c.get("OfficeName") or "",
        "fone_imobiliaria": _fone_limpo(c.get("OfficePhone")),
    } if c else None

    _CACHE_AGENTE[chave] = info
    return info


def _baixar_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def _cache(nome, gerar, dias=7):
    caminho = os.path.join("dados", nome)
    if os.path.exists(caminho) and time.time() - os.path.getmtime(caminho) < dias * 86400:
        with open(caminho, "r", encoding="utf-8") as f:
            return json.load(f)
    dados = gerar()
    os.makedirs("dados", exist_ok=True)
    with open(caminho, "w", encoding="utf-8") as f:
        json.dump(dados, f, ensure_ascii=False)
    return dados


def carregar_dicionario():
    """UID numerico -> nome em portugues (tipo de imovel, transacao, etc)."""
    def gerar():
        bruto = _baixar_json(LOOKUPS_URL)
        return {str(i.get("ItemName")): i.get("Translation") for i in bruto}
    return _cache("lookups.json", gerar)


def carregar_traducoes():
    """Chave textual -> portugues. E aqui que moram as caracteristicas
    (PropertyFeatures_*); o lookups numerico nao serve para elas, porque os
    IDs colidem com outras categorias (moedas, idiomas)."""
    return _cache("translate.json", lambda: _baixar_json(TRADUZ_URL))


def _montar_filtro(cfg):
    f = [
        "content/MacroRegionId eq %d" % MACRO_REGIAO_BR,
        "content/CityID eq %d" % int(cfg["city_id"]),
        "content/TransactionTypeUID eq %d" % int(cfg.get("transacao", 261)),
        "content/OnHoldListing eq false",
        "content/IsViewable eq true",
        "content/IsRegionalOffice eq false",
    ]
    if cfg.get("preco_min"):
        f.append("content/ListingPrice ge %d" % int(cfg["preco_min"]))
    if cfg.get("preco_max"):
        f.append("content/ListingPrice le %d" % int(cfg["preco_max"]))
    if cfg.get("quartos_min"):
        f.append("content/NumberOfBedrooms ge %d" % int(cfg["quartos_min"]))
    tipos = cfg.get("tipos") or []
    if tipos:
        alvo = " or ".join("content/PropertyTypeUID eq %d" % int(t) for t in tipos)
        f.append("(%s)" % alvo)
    return " and ".join(f)


def contar(cfg):
    r = _post({"count": True, "top": 0, "filter": _montar_filtro(cfg)})
    return r.get("@odata.count", 0)


def buscar(cfg, por_pagina=50, limite=None, log=print):
    """Percorre todas as paginas do resultado e devolve os registros crus."""
    filtro = _montar_filtro(cfg)
    total = contar(cfg)
    if limite:
        total = min(total, limite)
    log("   %d imoveis encontrados em %s" % (total, cfg.get("cidade_nome", "")))

    itens, pulo = [], 0
    while pulo < total:
        r = _post({
            "count": True,
            "skip": pulo,
            "top": min(por_pagina, total - pulo),
            "filter": filtro,
            "orderby": "content/LastUpdatedOnWeb desc",
        })
        lote = r.get("value") or []
        if not lote:
            break
        itens.extend(x.get("content") or {} for x in lote)
        pulo += len(lote)
        log("   ... %d/%d" % (min(pulo, total), total))
        if pulo < total:
            time.sleep(PAUSA)
    return itens


def _num(v):
    """Converte para numero limpo, tratando None e 0 como ausente."""
    try:
        if v in (None, "", 0, "0"):
            return None
        n = float(v)
        return int(n) if n == int(n) else n
    except (TypeError, ValueError):
        return None


def urls_fotos(c, maximo=12):
    imgs = c.get("ListingImages") or []
    def ordem(i):
        try:
            return int(i.get("Order") or 999)
        except (TypeError, ValueError):
            return 999
    imgs = sorted(imgs, key=ordem)
    regiao = c.get("RegionId")
    saida = []
    for i in imgs[:maximo]:
        arq = i.get("FileName")
        if arq and regiao is not None:
            saida.append(CDN_FOTO.format(regiao=regiao, arquivo=arq))
    return saida


def _link_publico(c):
    for s in (c.get("ShortLinks") or []):
        if str(s.get("ISOLanguageCode", "")).lower().startswith("pt"):
            return SITE + str(s.get("ShortLink", "")).lstrip("/")
    return SITE


def _codigo(c):
    """Codigo publico e estavel do imovel (ex.: 610471031-35)."""
    link = _link_publico(c)
    ultimo = link.rstrip("/").split("/")[-1]
    if ultimo and any(ch.isdigit() for ch in ultimo):
        return ultimo
    return str(c.get("ListingKey") or c.get("ListingId") or "sem-codigo")


def _caracteristicas(c, traducoes):
    """Nomes das caracteristicas, so as que existem em portugues.

    A chave certa e o FeatureName ('PropertyFeatures_Barbecue'), nunca o
    FeatureID: os IDs sao reaproveitados entre categorias e traduzir por ID
    devolve coisas como 'German Mark' no lugar de 'Churrasqueira'.
    """
    saida = []
    for f in (c.get("ListingFeatures") or []):
        chave = f.get("FeatureName") or ""
        if not chave:
            continue
        pt = traducoes.get(chave)
        cru = chave.split("_", 1)[-1]
        # sem traducao, ou traducao identica ao ingles: fora do anuncio
        if not pt or pt.strip().lower() == cru.strip().lower():
            continue
        if pt not in saida:
            saida.append(pt)
    return saida


def normalizar(c, dic, max_fotos=12, traducoes=None, com_corretor=True):
    """Traduz um registro cru da REMAX para campos em portugues."""
    t = lambda uid: dic.get(str(uid)) if uid is not None else None
    caracteristicas = _caracteristicas(c, traducoes or {})

    corretor = buscar_corretor(c.get("AgentId")) if com_corretor else None

    descricao = ""
    for d in (c.get("ListingDescriptions") or []):
        txt = (d.get("Description") or "").strip()
        if txt and len(txt) > len(descricao):
            descricao = txt

    fotos = urls_fotos(c, max_fotos)

    return {
        "codigo": _codigo(c),
        "tipo": t(c.get("PropertyTypeUID")) or "Imovel",
        "transacao": t(c.get("TransactionTypeUID")) or "Venda",
        "preco": _num(c.get("ListingPrice")),
        "quartos": _num(c.get("NumberOfBedrooms")),
        "suites": _num(c.get("TotalNumberOfSuites")),
        "banheiros": _num(c.get("NumberOfBathrooms")),
        "vagas": _num(c.get("ParkingSpaces")) or _num(c.get("NumberOfGarages")),
        "area_total": _num(c.get("TotalArea")),
        "area_util": _num(c.get("LivingArea")) or _num(c.get("BuiltArea")),
        "bairro": c.get("LocalZone") or c.get("District") or "",
        "cidade": c.get("City") or "",
        "uf": c.get("Province") or "",
        "endereco": c.get("FullAddress") or c.get("TitleAddress") or "",
        "cep": c.get("PostalCode") or "",
        "condominio": _num(c.get("CondoFees")),
        "iptu": _num(c.get("PropertyTax")),
        "ano": _num(c.get("YearBuilt")),
        "andar": _num(c.get("FloorNumber")),
        "descricao_original": descricao,
        "caracteristicas": caracteristicas,
        "fotos": fotos,
        "n_fotos": len(fotos),
        "link": _link_publico(c),
        "atualizado_em": c.get("LastUpdatedOnWeb") or "",
        # Uso interno: contato do corretor da REMAX. Nunca vai para o post.
        "agent_id": c.get("AgentId"),
        "corretor": corretor,
    }
