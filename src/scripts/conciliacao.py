from pathlib import Path
import re
import numpy as np
import pandas as pd

P = dict(
    JANELA_MAX_MIN=240,     # só vira candidato se a distância entre intervalos for ≤ isto
    TAU_GAP_MIN=30,         # decaimento da proximidade quando NÃO há sobreposição
    TAU_INICIO_MIN=60,      # decaimento pela diferença entre os inícios
    W_T=0.50, W_E=0.25, W_C=0.25,
    ANCORA_CONTENCAO=0.5,   # âncora: sobreposição ≥ 50% do menor intervalo...
    ANCORA_DINI_MIN=60,     # ...inícios a até 60 min...
    ANCORA_MAX_H=24,        # ...ordem não é de "calendário longo"
    ALFA=1.0,               # suavização de Laplace nas tabelas aprendidas
    SUPORTE_MIN_TOKEN=3,    # token precisa aparecer em ≥ 3 âncoras para virar evidência
    NEUTRO=0.5,             # valor quando não há informação (ex.: local só no nível máquina)
    LIMIAR_ALTA=0.75, LIMIAR_MEDIA=0.60, LIMIAR_BAIXA=0.45,
    MARGEM_AMBIGUO=0.05,    # 2º melhor a menos disso do 1º (outra parada) → ambíguo
    DURACAO_MAX_H=24,       # ordem acima disto (calendário longo): confiança limitada a "baixa"
    DIF_DURACAO_MIN=15, DIF_DURACAO_REL=0.30,   # divergência relevante de duração
    SIMILARIDADE_DUP=0.6,   # Jaccard de tokens entre ordens da mesma parada → duplicidade
)

STOPWORDS = set("""A O E DE DA DO DAS DOS NO NA NOS NAS EM COM SEM PARA POR AO AOS
    UM UMA SE QUE OU AS OS""".split())


def carregar(dirpath="./data/processed"):
    d = Path(dirpath)
    def ler(nome):
        pq, pk = d / f"{nome}.parquet", d / f"{nome}.pkl"
        return pd.read_parquet(pq) if pq.exists() else pd.read_pickle(pk)
    return ler("mes_registros"), ler("mes_paradas"), ler("sap_ordens")

def tokens(texto):
    if pd.isna(texto):
        return frozenset()
    toks = re.split(r"[^A-Z0-9]+", str(texto))
    return frozenset(t for t in toks if len(t) >= 2 and t not in STOPWORDS and not t.isdigit())


# candidatos + score temporal
def gerar_candidatos(paradas, sap, p=P):
    s = sap.loc[sap["valido"]].copy()
    # ordem sem fim (alerta): tratada como instante no início
    s["_fim"] = s["fim"].fillna(s["inicio"])
    m = paradas[["id_parada", "maquina", "equipamento", "inicio", "fim", "duracao_h"]]

    c = s[["n_ordem", "maquina", "descricao_equip", "subconjunto_local", "inicio", "_fim", "horas",
           "texto_padronizado", "tipo", "tipo_atividade"]].merge(
        m, on="maquina", suffixes=("_s", "_m"))

    ov = (np.minimum(c["_fim"], c["fim"]) - np.maximum(c["inicio_s"], c["inicio_m"])).dt.total_seconds() / 60
    c["sobreposicao_min"] = ov.clip(lower=0)
    c["gap_min"] = (-ov).clip(lower=0)
    c = c.loc[c["gap_min"] <= p["JANELA_MAX_MIN"]].copy()

    dur_s = ((c["_fim"] - c["inicio_s"]).dt.total_seconds() / 60).clip(lower=1)
    dur_m = (c["duracao_h"] * 60).clip(lower=1)
    c["contencao"] = (c["sobreposicao_min"] / np.minimum(dur_s, dur_m)).clip(upper=1)
    c["dif_inicio_min"] = (c["inicio_s"] - c["inicio_m"]).dt.total_seconds() / 60   # >0: SAP depois do MES
    c["S_tempo"] = (0.4 * c["contencao"]
                    + 0.3 * np.exp(-c["gap_min"] / p["TAU_GAP_MIN"])
                    + 0.3 * np.exp(-c["dif_inicio_min"].abs() / p["TAU_INICIO_MIN"]))
    return c.reset_index(drop=True)


def marcar_ancoras(c, p=P):
    forte = ((c["contencao"] >= p["ANCORA_CONTENCAO"])
             & (c["dif_inicio_min"].abs() <= p["ANCORA_DINI_MIN"])
             & (c["horas"] <= p["ANCORA_MAX_H"]))
    n_fortes = forte.groupby(c["n_ordem"]).transform("sum")
    return forte & (n_fortes == 1)


# Compatibilidade aprendida
def compatibilidade(prob_alvo, prob_max, k, p=P):
    """Converte uma distribuição aprendida em score de compatibilidade em [0, 1].

    r       = prob_alvo / prob_max     -> o alvo está entre os mais prováveis? (1 = é o mais provável)
    inform  = (prob_max - 1/k) / (1 - 1/k) -> quão concentrada é a evidência (0 = uniforme)
    score   = NEUTRO + (r - NEUTRO) * inform

    Evidência fraca puxa para o neutro; evidência forte a favor puxa para 1, contra puxa para 0.
    """
    k = np.maximum(k, 2)
    r = prob_alvo / prob_max
    inform = np.clip((prob_max - 1 / k) / (1 - 1 / k), 0, 1)
    return p["NEUTRO"] + (r - p["NEUTRO"]) * inform


def score_equipamento(c, ancora, p=P):
    """P(equipamento_MES | descricao_SAP) aprendida das âncoras (Laplace + leave-one-out).
    Mesmo nome nos dois sistemas -> 1. Localização só no nível da máquina -> neutro."""
    a = c.loc[ancora]
    n = a.groupby(["maquina", "descricao_equip", "equipamento"]).size().to_dict()
    n_s = a.groupby(["maquina", "descricao_equip"]).size().to_dict()
    eqs = c.groupby("maquina")["equipamento"].unique().to_dict()

    out = np.empty(len(c))
    for i, (mq, ds, em, anc) in enumerate(zip(c["maquina"], c["descricao_equip"], c["equipamento"], ancora)):
        k = len(eqs[mq]); loo = 1 if anc else 0
        den = n_s.get((mq, ds), 0) - loo + p["ALFA"] * k
        probs = {e: (n.get((mq, ds, e), 0) - (loo if e == em else 0) + p["ALFA"]) / den for e in eqs[mq]}
        out[i] = compatibilidade(probs[em], max(probs.values()), k, p)

    s = pd.Series(out, index=c.index)
    s[c["descricao_equip"] == c["equipamento"]] = 1.0
    s[c["subconjunto_local"].isna()] = p["NEUTRO"]
    return s


def score_componente(c, comps_parada, ancora, p=P):
    """Naive Bayes multinomial: P(componente | tokens do texto SAP), aprendido das âncoras
    da mesma máquina (leave-one-out). Componente citado literalmente no texto -> 1
    (prefixo, ex. QMD -> QMDCF, -> 0.8). Sem token conhecido -> neutro."""
    c = c.assign(_tok=c["texto_padronizado"].map(tokens), _comp=c["id_parada"].map(comps_parada))

    a = c.loc[ancora, ["maquina", "_tok", "_comp"]].explode("_comp").explode("_tok").dropna()
    n_tc = a.groupby(["maquina", "_comp", "_tok"]).size().to_dict()
    n_c_tok = a.groupby(["maquina", "_comp"]).size().to_dict()            # nº de tokens vistos com c
    suporte = a.groupby(["maquina", "_tok"]).size().to_dict()
    pa = c.loc[ancora, ["maquina", "_comp"]].explode("_comp").dropna()
    n_c = pa.groupby(["maquina", "_comp"]).size().to_dict()               # nº de âncoras com c
    comps_mq = {mq: sorted(g.unique()) for mq, g in pa.groupby("maquina")["_comp"]}
    vocab = {mq: g.nunique() for mq, g in a.groupby("maquina")["_tok"]}

    out = np.full(len(c), p["NEUTRO"])
    for i, (mq, toks, comps, anc) in enumerate(zip(c["maquina"], c["_tok"], c["_comp"], ancora)):
        if not toks or not isinstance(comps, frozenset) or not comps:
            continue
        lit = [1.0 if t == cp else 0.8 for t in toks for cp in comps
               if len(t) >= 3 and (t == cp or cp.startswith(t))]
        if lit:
            out[i] = max(lit)
            continue
        K = comps_mq.get(mq, [])
        V = vocab.get(mq, 1)
        # leave-one-out: se este par é âncora, retira dele próprio as contagens
        loo_c = comps if anc else frozenset()
        conhecidos = [t for t in toks if suporte.get((mq, t), 0) - (len(comps) if anc else 0)
                      >= p["SUPORTE_MIN_TOKEN"]]
        if not conhecidos or not K:
            continue
        total = sum(v for (m_, _), v in n_c.items() if m_ == mq)
        logp = {}
        for cp in K:
            own = 1 if cp in loo_c else 0
            nc = n_c.get((mq, cp), 0) - own
            ntok = n_c_tok.get((mq, cp), 0) - own * len(toks)
            lp = np.log((nc + p["ALFA"]) / (total - (1 if anc else 0) + p["ALFA"] * len(K)))
            for t in conhecidos:
                lp += np.log((n_tc.get((mq, cp, t), 0) - (own if t in toks else 0) + p["ALFA"])
                             / (ntok + p["ALFA"] * V))
            logp[cp] = lp
        mx = max(logp.values())
        post = {k_: np.exp(v_ - mx) for k_, v_ in logp.items()}
        z = sum(post.values())
        post = {k_: v_ / z for k_, v_ in post.items()}
        alvo = max(post.get(cp, 0) for cp in comps)
        out[i] = compatibilidade(alvo, max(post.values()), len(K), p)
    return pd.Series(out, index=c.index)


# ---------------------------------------------------------------- 5. atribuição e confiança
def classificar(score, p=P):
    return np.select([score >= p["LIMIAR_ALTA"], score >= p["LIMIAR_MEDIA"], score >= p["LIMIAR_BAIXA"]],
                     ["alta", "media", "baixa"], default="sem correspondencia")


def conciliar(mes, paradas, sap, p=P):
    comps = (mes.loc[mes["valido"]].groupby("id_parada")["componente"]
                .agg(lambda s: frozenset(s.dropna())))

    c = gerar_candidatos(paradas, sap, p)
    c["ancora"] = marcar_ancoras(c, p)
    c["S_equip"] = score_equipamento(c, c["ancora"], p)
    c["S_comp"] = score_componente(c, comps, c["ancora"], p)
    c["score"] = p["W_T"] * c["S_tempo"] + p["W_E"] * c["S_equip"] + p["W_C"] * c["S_comp"]
    c = c.sort_values(["n_ordem", "score"], ascending=[True, False])
    c["rank"] = c.groupby("n_ordem").cumcount() + 1

    # melhor parada de cada ordem; 2º melhor para medir ambiguidade
    best = c.loc[c["rank"] == 1].set_index("n_ordem")
    seg = c.loc[c["rank"] == 2].set_index("n_ordem")
    v = sap.loc[sap["valido"], ["n_ordem"]].set_index("n_ordem")
    v["id_parada"] = best["id_parada"]
    v["score"] = best["score"]
    for col in ["S_tempo", "S_equip", "S_comp", "dif_inicio_min", "contencao", "ancora"]:
        v[col] = best[col]
    v["score_2o"] = seg["score"]
    v["parada_2a"] = seg["id_parada"]
    v["score"] = v["score"].fillna(0)
    v["confianca"] = classificar(v["score"], p)
    v["ambiguo"] = ((v["confianca"] != "sem correspondencia")
                    & (v["score_2o"] >= v["score"] - p["MARGEM_AMBIGUO"])
                    & (v["score_2o"] >= p["LIMIAR_BAIXA"]))
    v.loc[v["confianca"] == "sem correspondencia", "id_parada"] = pd.NA
    v["n_candidatos"] = c.groupby("n_ordem").size().reindex(v.index).fillna(0).astype(int)
    return c.reset_index(drop=True), v.reset_index()


# base consolidada de eventos
def tokens_dup(texto):
    """Tokens para detectar duplicidade: mantém números (QMD 02 ≠ QMD 03), sem zeros à esquerda."""
    if pd.isna(texto):
        return frozenset()
    toks = re.split(r"[^A-Z0-9]+", str(texto))
    return frozenset(t.lstrip("0") or "0" if t.isdigit() else t
                     for t in toks if t and t not in STOPWORDS)


def jaccard(a, b):
    return len(a & b) / len(a | b) if a and b else 0.0


def uniao_intervalos_h(ini, fim):
    """Duração da união de intervalos (horas). Tempo sobreposto conta uma vez."""
    iv = sorted((a, b) for a, b in zip(ini, fim) if pd.notna(a) and pd.notna(b) and b > a)
    tot, cur_a, cur_b = 0.0, None, None
    for a, b in iv:
        if cur_b is None or a > cur_b:
            if cur_b is not None:
                tot += (cur_b - cur_a).total_seconds()
            cur_a, cur_b = a, b
        else:
            cur_b = max(cur_b, b)
    if cur_b is not None:
        tot += (cur_b - cur_a).total_seconds()
    return tot / 3600


def consolidar(mes, paradas, sap, vinculo, p=P):
    s = sap.loc[sap["valido"]].set_index("n_ordem")
    vin = vinculo.set_index("n_ordem").join(
        s[["maquina", "descricao_equip", "tipo", "tipo_atividade", "texto_padronizado", "inicio", "fim", "horas"]])

    # ordens de calendário longo: o tempo não discrimina -> no máximo "baixa", vai para revisão
    longa = vin["horas"] > p["DURACAO_MAX_H"]
    rebaixar = longa & vin["confianca"].isin(["alta", "media"])
    vin.loc[rebaixar, "confianca"] = "baixa"
    vin["ordem_longa"] = longa
    vin["revisar"] = vin["confianca"].eq("baixa") | vin["ambiguo"]

    ligado = vin.loc[vin["confianca"] != "sem correspondencia"].reset_index()
    eventos = []

    # (a) paradas do MES, com ou sem ordem
    por_parada = dict(tuple(ligado.groupby("id_parada")))
    for _, r in paradas.iterrows():
        g = por_parada.get(r["id_parada"])
        e = dict(id_parada=r["id_parada"], maquina=r["maquina"], equipamento_mes=r["equipamento"],
                 familia_mes=r["familia_dominante"], componente_mes=r["componente_dominante"],
                 contem_quebra=r["contem_quebra"], micro_parada=r["micro_parada"],
                 mes_inicio=r["inicio"], mes_fim=r["fim"], indisponibilidade_h=r["duracao_h"],
                 ids_registro_mes=r["ids_registro"])
        if g is None:
            e["classe"] = "MES sem SAP"
        else:
            g = g.sort_values("score", ascending=False)
            toks = [tokens_dup(t) for t in g["texto_padronizado"]]
            dup = any(jaccard(toks[i], toks[j]) >= p["SIMILARIDADE_DUP"]
                      for i in range(len(toks)) for j in range(i + 1, len(toks)))
            e.update(classe="MES + SAP", ordens=",".join(map(str, g["n_ordem"])), n_ordens=len(g),
                     score=g["score"].iloc[0], confianca=g["confianca"].iloc[0],
                     revisar=bool(g["revisar"].any()), ambiguo=bool(g["ambiguo"].any()),
                     ordem_longa=bool(g["ordem_longa"].any()), possivel_duplicidade_sap=dup,
                     descricao_equip_sap=g["descricao_equip"].iloc[0],
                     tipo_sap=g["tipo"].iloc[0], tipo_atividade_sap=g["tipo_atividade"].iloc[0],
                     texto_sap=" | ".join(g["texto_padronizado"].fillna("")),
                     sap_inicio=g["inicio"].min(), sap_fim=g["fim"].max(),
                     atuacao_h=uniao_intervalos_h(g["inicio"], g["fim"]),
                     atuacao_na_parada_h=uniao_intervalos_h(g["inicio"].clip(lower=r["inicio"], upper=r["fim"]),
                                                            g["fim"].clip(lower=r["inicio"], upper=r["fim"])))
        eventos.append(e)

    # (b) ordens do SAP sem parada
    for n, r in vin.loc[vin["confianca"] == "sem correspondencia"].iterrows():
        eventos.append(dict(classe="SAP sem MES", maquina=r["maquina"], ordens=str(n), n_ordens=1,
                            score=r["score"], confianca="sem correspondencia",
                            revisar=False, ordem_longa=bool(r["ordem_longa"]),
                            descricao_equip_sap=r["descricao_equip"], tipo_sap=r["tipo"],
                            tipo_atividade_sap=r["tipo_atividade"], texto_sap=r["texto_padronizado"],
                            sap_inicio=r["inicio"], sap_fim=r["fim"], atuacao_h=r["horas"],
                            n_candidatos=int(r["n_candidatos"])))

    ev = pd.DataFrame(eventos)
    ev["inicio_evento"] = ev["mes_inicio"].fillna(ev["sap_inicio"])
    ev = ev.sort_values(["maquina", "inicio_evento"]).reset_index(drop=True)
    ev.insert(0, "id_evento", ev["maquina"] + "-EV" + (ev.groupby("maquina").cumcount() + 1).astype(str).str.zfill(4))

    # linha do tempo do evento: t0 parada | t1 início atuação | t2 fim atuação | t3 retomada
    ev["espera_ate_atuacao_min"] = (ev["sap_inicio"] - ev["mes_inicio"]).dt.total_seconds() / 60
    ev["retomada_apos_atuacao_min"] = (ev["mes_fim"] - ev["sap_fim"]).dt.total_seconds() / 60
    ev["dif_duracao_h"] = ev["atuacao_h"] - ev["indisponibilidade_h"]
    lim = np.maximum(p["DIF_DURACAO_MIN"] / 60, p["DIF_DURACAO_REL"] * ev["indisponibilidade_h"])
    ev["divergencia_duracao"] = np.where(
        ev["classe"] != "MES + SAP", pd.NA,
        np.where(ev["dif_duracao_h"] > lim, "SAP maior",
                 np.where(ev["dif_duracao_h"] < -lim, "MES maior", "compatível")))
    ev["sap_antes_da_parada"] = ev["espera_ate_atuacao_min"] < -p["DIF_DURACAO_MIN"]

    # motivo provável de não conciliar (orienta a investigação, não é conclusão)
    curta = ev["indisponibilidade_h"] * 60 < 30
    ev["motivo_sem_par"] = np.select(
        [(ev["classe"] == "MES sem SAP") & (ev["familia_mes"] == "AJUSTE") & curta,
         (ev["classe"] == "MES sem SAP") & (ev["familia_mes"] == "QUEBRA"),
         ev["classe"] == "MES sem SAP",
         (ev["classe"] == "SAP sem MES") & (ev["n_candidatos"] == 0),
         ev["classe"] == "SAP sem MES"],
        ["ajuste curto: provável ação do próprio operador",
         "quebra sem ordem: lacuna de apontamento na manutenção",
         "ajuste longo sem ordem: verificar",
         "nenhuma parada na janela: intervenção sem parada ou parada não apontada no MES",
         "parada próxima, mas incompatível (equipamento/texto): revisar"],
        default="")
    return ev, vin.reset_index()
