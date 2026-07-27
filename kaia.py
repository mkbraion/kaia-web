# -*- coding: utf-8 -*-
"""KAIA Imoveis - coleta imoveis da REMAX e deixa tudo pronto pro Marketplace.

Uso:
    python kaia.py coletar     # busca imoveis, baixa fotos, escreve os anuncios
    python kaia.py painel      # abre o painel no navegador
    python kaia.py publicar    # agenda os posts na Pagina do Facebook
    python kaia.py facebook    # configura a Pagina (uma vez so)
    python kaia.py tudo        # coletar + publicar (usado pelo agendamento)
    python kaia.py status      # resumo rapido
"""

import json
import os
import sys
import time
import urllib.request

import banco
import ia
import remax

RAIZ = os.path.dirname(os.path.abspath(__file__))
os.chdir(RAIZ)

PASTA_FOTOS = os.path.join("dados", "imoveis")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


def _mesclar(base, extra):
    """Sobrepoe 'extra' em 'base', descendo um nivel nos dicionarios."""
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            base[k].update(v)
        else:
            base[k] = v
    return base


def ler_config():
    with open("config.json", "r", encoding="utf-8") as f:
        cfg = json.load(f)

    # config.local.json guarda o que e segredo (o token da Pagina). Ele fica
    # fora do Git, entao o config.json pode ir para o repositorio sem risco.
    if os.path.exists("config.local.json"):
        with open("config.local.json", "r", encoding="utf-8") as f:
            _mesclar(cfg, json.load(f))

    # Modo nuvem (GitHub Actions): o token vem de variavel de ambiente, nunca
    # do arquivo - assim o config.json pode ir para o repositorio sem segredo.
    fb = cfg.setdefault("facebook", {})
    if os.environ.get("FB_PAGE_TOKEN"):
        fb["page_token"] = os.environ["FB_PAGE_TOKEN"]
    if os.environ.get("FB_PAGE_ID"):
        fb["page_id"] = os.environ["FB_PAGE_ID"]

    if os.environ.get("KAIA_NUVEM"):
        fb["ativo"] = True
        fb["modo_teste"] = False
        # na nuvem as fotos nao servem para nada: o post usa a URL do CDN.
        # baixa-las seria 300 MB por execucao a toa.
        cfg["baixar_fotos"] = False

    return cfg


def baixar_fotos(imovel, log=print):
    """Baixa as fotos para dados/imoveis/<codigo>/. Pula o que ja existe."""
    pasta = os.path.join(PASTA_FOTOS, imovel["codigo"])
    os.makedirs(pasta, exist_ok=True)
    baixadas = 0

    for i, url in enumerate(imovel.get("fotos") or [], start=1):
        destino = os.path.join(pasta, "foto_%02d.jpg" % i)
        if os.path.exists(destino) and os.path.getsize(destino) > 1024:
            continue
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=60) as r:
                conteudo = r.read()
            if len(conteudo) < 1024:
                continue
            with open(destino, "wb") as f:
                f.write(conteudo)
            baixadas += 1
            time.sleep(0.25)
        except Exception as e:
            log("      ! foto %d falhou (%s)" % (i, type(e).__name__))

    return os.path.abspath(pasta), baixadas


def cmd_coletar(limite=None):
    cfg = ler_config()
    con = banco.conectar()

    print("=" * 58)
    print(" KAIA IMOVEIS - coleta")
    print(" Cidade: %s" % cfg.get("cidade_nome"))
    print("=" * 58)

    print("\n[1/4] Carregando dicionarios de termos...")
    dic = remax.carregar_dicionario()
    traducoes = remax.carregar_traducoes()
    print("   %d termos + %d traducoes." % (len(dic), len(traducoes)))

    print("\n[2/4] Consultando a REMAX...")
    crus = remax.buscar(cfg, limite=limite)

    print("\n[3/4] Registrando novidades...")
    novos, precos, iguais = [], [], 0
    max_fotos = int(cfg.get("max_fotos", 12))
    for c in crus:
        im = remax.normalizar(c, dic, max_fotos, traducoes)
        if cfg.get("somente_com_foto", True) and not im["fotos"]:
            continue
        r = banco.salvar(con, im)
        if r == "novo":
            novos.append(im)
        elif r == "preco":
            precos.append(im)
        else:
            iguais += 1

    print("   %d novos | %d mudaram de preco | %d sem alteracao"
          % (len(novos), len(precos), iguais))

    fila = novos + precos

    # Se uma coleta anterior foi interrompida, sobraram imoveis sem texto.
    # Eles ficariam invisiveis no painel, entao entram na fila agora.
    ja = {i["codigo"] for i in fila}
    for l in banco.sem_texto(con):
        if l["codigo"] not in ja:
            fila.append(json.loads(l["dados"]))
    pendentes = len(fila) - len(novos) - len(precos)
    if pendentes:
        print("   + %d pendentes de uma coleta anterior" % pendentes)

    if not fila:
        print("\n[4/4] Nada novo por coletar.")
        if cfg.get("baixar_fotos", True):
            garantir_fotos(con)
        _resumo(con)
        return

    com_fotos = cfg.get("baixar_fotos", True)
    print("\n[4/4] %sEscrevendo os anuncios..."
          % ("Baixando fotos e " if com_fotos else ""))
    for n, im in enumerate(fila, start=1):
        print("   (%d/%d) %s - %s" % (n, len(fila), im["codigo"], ia.titulo(im)[:52]))
        if com_fotos:
            pasta, qtd = baixar_fotos(im)
            print("      %d fotos" % qtd if qtd else "      fotos ja estavam baixadas")
        else:
            pasta = ""
        titulo, texto, origem = ia.escrever(im, cfg)
        banco.gravar_texto(con, im["codigo"], titulo, texto, pasta)
        print("      anuncio escrito (%s)" % origem)

    if com_fotos:
        garantir_fotos(con)
    _resumo(con)


def garantir_fotos(con, log=print):
    """Baixa fotos de imoveis que estao no banco mas sem foto no PC.

    Acontece com o que a NUVEM descobriu: ela cadastra o imovel mas nao
    baixa foto nenhuma (nao precisa, o post usa a URL do CDN). Sem esta
    passagem, esses imoveis ficariam sem foto no painel e fora do Drive.
    Tambem se cura sozinho se alguma pasta for apagada.
    """
    faltando = []
    for l in con.execute("SELECT codigo, dados FROM imoveis").fetchall():
        pasta = os.path.join(PASTA_FOTOS, l["codigo"])
        tem = os.path.isdir(pasta) and any(
            f.lower().endswith((".jpg", ".jpeg", ".png")) for f in os.listdir(pasta))
        if not tem:
            im = json.loads(l["dados"])
            if im.get("fotos"):
                faltando.append(im)

    if not faltando:
        return 0

    log("   %d imovel(is) sem foto no PC - baixando..." % len(faltando))
    total = 0
    for n, im in enumerate(faltando, start=1):
        _, qtd = baixar_fotos(im, log=log)
        total += qtd
        log("      (%d/%d) %s: %d fotos" % (n, len(faltando), im["codigo"], qtd))
    return total


def _resumo(con):
    c = banco.contagem(con)
    print("\n" + "-" * 58)
    print(" Prontos para publicar: %d" % c.get(banco.PRONTO, 0))
    print(" Ja publicados........: %d" % c.get(banco.PUBLICADO, 0))
    print(" Ignorados............: %d" % c.get(banco.IGNORADO, 0))
    print("-" * 58)
    print(" Rode:  python kaia.py painel     (ou clique em Painel.bat)")


def cmd_status():
    _resumo(banco.conectar())


def cmd_painel():
    import painel
    painel.servir(abrir="--sem-navegador" not in sys.argv)


def cmd_publicar():
    import publicar
    publicar.rodar(ler_config())


def cmd_facebook():
    import publicar
    publicar.configurar()


def cmd_diagnosticar():
    import publicar
    publicar.diagnosticar()


def cmd_drive():
    import drive_sync
    drive_sync.enviar()


def cmd_pcdiario():
    """O que o agendamento do PC roda: coleta (traz fotos novas) e sincroniza
    o Drive. Publicar no Facebook e tarefa da nuvem, nao do PC."""
    cmd_coletar()
    print()
    import drive_sync
    if drive_sync.configurado():
        drive_sync.enviar()
        # mantem o pacote do notebook atualizado no Drive
        drive_sync.enviar_pacote_notebook()
    else:
        print("Drive nao conectado ainda - pulei a sincronizacao.")
        print("Rode o Conectar-Drive.bat uma vez para ativar.")


def cmd_corretores():
    """Preenche o contato do corretor nos imoveis que ainda nao tem.

    Uso interno: aparece so no painel, para voce saber com quem falar na
    REMAX sobre cada imovel. Nunca vai para o texto publicado.
    """
    cfg = ler_config()
    con = banco.conectar()

    # Os registros antigos foram salvos antes de existir o campo agent_id.
    # Buscamos os imoveis crus da REMAX de novo so para pegar o AgentId,
    # cruzando pelo codigo publico.
    print("Recarregando os imoveis da REMAX para achar os AgentIds...")
    dic = remax.carregar_dicionario()
    traducoes = remax.carregar_traducoes()
    id_por_codigo = {}
    for c in remax.buscar(cfg):
        base = remax.normalizar(c, dic, 0, traducoes, com_corretor=False)
        if c.get("AgentId"):
            id_por_codigo[base["codigo"]] = c.get("AgentId")

    linhas = con.execute("SELECT codigo, dados FROM imoveis").fetchall()
    print("\nBuscando o corretor de %d imoveis..." % len(linhas))

    achados = faltando = 0
    for n, l in enumerate(linhas, start=1):
        im = json.loads(l["dados"])
        if im.get("corretor"):
            achados += 1
            continue
        agente = im.get("agent_id") or id_por_codigo.get(l["codigo"])
        info = remax.buscar_corretor(agente) if agente else None
        if info:
            im["agent_id"] = agente
            im["corretor"] = info
            con.execute("UPDATE imoveis SET dados=? WHERE codigo=?",
                        (json.dumps(im, ensure_ascii=False), l["codigo"]))
            achados += 1
        else:
            faltando += 1
        if n % 40 == 0:
            con.commit()
            print("   %d/%d" % (n, len(linhas)))
        time.sleep(0.15)
    con.commit()
    print("\nPronto. %d com corretor, %d sem." % (achados, faltando))


def cmd_regerar():
    """Reescreve os anuncios com os dados atuais do config.json.

    Use depois de mudar seu nome/WhatsApp/CRECI, ou de trocar o provedor
    de IA. Nao mexe no que ja foi publicado no Facebook.
    """
    cfg = ler_config()
    con = banco.conectar()

    if not (cfg.get("seu_whatsapp") or cfg.get("seu_nome")):
        print("AVISO: 'seu_nome' e 'seu_whatsapp' estao vazios em config.json.")
        print("Os anuncios vao sair sem forma de contato.\n")
        if input("Continuar mesmo assim? (s/N): ").strip().lower() != "s":
            print("Cancelado. Preencha o config.json e rode de novo.")
            return

    linhas = con.execute("SELECT codigo, dados FROM imoveis").fetchall()
    print("Reescrevendo %d anuncios..." % len(linhas))

    for n, l in enumerate(linhas, start=1):
        im = json.loads(l["dados"])
        titulo, texto, _ = ia.escrever(im, cfg)
        con.execute("UPDATE imoveis SET titulo=?, texto=? WHERE codigo=?",
                    (titulo, texto, l["codigo"]))
        if n % 50 == 0:
            con.commit()
            print("   %d/%d" % (n, len(linhas)))
    con.commit()

    print("Pronto. %d anuncios atualizados." % len(linhas))
    ja = con.execute(
        "SELECT COUNT(*) n FROM imoveis WHERE fb_post_id IS NOT NULL").fetchone()["n"]
    if ja:
        print("\n%d posts ja existem no Facebook com o texto antigo." % ja)
        print("Rode 'python kaia.py sincronizar' para reescrever tambem la.")


def cmd_tudo(limite=None):
    """O que o agendamento do Windows roda: coleta e depois publica."""
    cmd_coletar(limite)
    cfg = ler_config()
    if (cfg.get("facebook") or {}).get("ativo"):
        print()
        import publicar
        publicar.rodar(cfg)


if __name__ == "__main__":
    acao = (sys.argv[1] if len(sys.argv) > 1 else "coletar").lower()
    lim = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2].isdigit() else None
    try:
        if acao.startswith("cole"):
            cmd_coletar(lim)
        elif acao.startswith("pain"):
            cmd_painel()
        elif acao.startswith("publ"):
            cmd_publicar()
        elif acao.startswith("face"):
            cmd_facebook()
        elif acao.startswith("diag"):
            cmd_diagnosticar()
        elif acao.startswith("reger"):
            cmd_regerar()
        elif acao.startswith("sincr"):
            import publicar
            publicar.sincronizar(ler_config())
        elif acao.startswith("corret"):
            cmd_corretores()
        elif acao.startswith("drive"):
            cmd_drive()
        elif acao.startswith("pcdiario"):
            cmd_pcdiario()
        elif acao.startswith("tudo"):
            cmd_tudo(lim)
        elif acao.startswith("stat"):
            cmd_status()
        else:
            print(__doc__)
    except KeyboardInterrupt:
        print("\nInterrompido.")
