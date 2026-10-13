"""Subproduto 5 — Inteligência para priorização e melhoria da estratégia de manutenção.

Três frentes, todas a partir das paradas do MES e da base de eventos conciliada (Subproduto 2):

1. ranking dinâmico de problemas (critérios + pesos AHP + sensibilidade);
2. sinais de deterioração (teste de Laplace, modelo de Crow-AMSAA, tendência do MTTR);
3. efetividade das intervenções (proxy: planejada × emergencial, por curvas de sobrevivência).

Nenhuma função depende dos nomes fictícios das máquinas ou componentes: a máquina, o componente
e a criticidade da máquina entram como dados.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from scipy import stats

H = pd.Timedelta(hours=1)
_trapz = getattr(np, "trapezoid", None) or np.trapz   # numpy < 2.0 só tem trapz

PARAM = dict(
    JANELA_REINCIDENCIA_DIAS=7,   # mesma janela do critério C5 da priorização de chamados
    MIN_QUEBRAS_TENDENCIA=4,      # abaixo disso o teste de tendência não tem poder: critério neutro
    ALFA_FDR=0.10,                # taxa de falsas descobertas aceita nos alertas de deterioração
    SUAVIZACAO_REINCIDENCIA=2,    # pseudo-quebras puxando a taxa para a média da máquina
    HORIZONTE_PROJECAO_DIAS=30,
)

# Criticidade da máquina = mesmo C2 da priorização de chamados (cadastro da máquina no app).
IMPACTO_PADRAO = {"gargalo": 1.0, "sem_redundancia": 0.7, "com_redundancia": 0.4}


# ============================================================================ base de quebras
def quebras(paradas: pd.DataFrame) -> pd.DataFrame:
    """Uma linha por quebra: parada do MES com algum registro QUEBRA, sem micro-paradas (< 2 min).

    O componente é o dominante da parada (registro válido de maior duração)."""
    q = paradas.loc[paradas["contem_quebra"] & ~paradas["micro_parada"]].copy()
    q = q.rename(columns={"componente_dominante": "componente"})
    q["componente"] = q["componente"].fillna("SEM COMPONENTE")
    return q[["id_parada", "maquina", "equipamento", "componente", "inicio", "fim", "duracao_h"]].sort_values("inicio")


def marcar_reincidencia(q: pd.DataFrame, chave=("maquina", "componente"), p=PARAM) -> pd.Series:
    """True se houve outra quebra da mesma chave nos N dias anteriores ao início desta."""
    janela = pd.Timedelta(days=p["JANELA_REINCIDENCIA_DIAS"])
    s = q.sort_values("inicio")
    anterior = s.groupby(list(chave))["inicio"].shift()
    return ((s["inicio"] - anterior) <= janela).reindex(q.index).fillna(False)


# ============================================================================ testes de tendência
def teste_laplace(tempos_h: np.ndarray, T_h: float) -> tuple[float, float]:
    """Teste de Laplace para tendência num processo de falhas observado em (0, T].

    U = (média dos tempos − T/2) / (T·√(1/12n)). Sob taxa constante (HPP), U ~ N(0, 1).
    U > 0: falhas se concentram no fim do período (deterioração). Retorna (U, p unilateral)."""
    n = len(tempos_h)
    if n == 0 or T_h <= 0:
        return np.nan, np.nan
    U = (np.mean(tempos_h) - T_h / 2) / (T_h * np.sqrt(1 / (12 * n)))
    return float(U), float(stats.norm.sf(U))


def crow_amsaa(tempos_h: np.ndarray, T_h: float) -> float:
    """β do modelo de potência (Crow-AMSAA), EMV para observação truncada no tempo.

    β̂ = n / Σ ln(T / tᵢ). β > 1: taxa de falhas crescente; β < 1: decrescente; β = 1: constante."""
    t = np.clip(np.asarray(tempos_h, float), 1e-3, None)
    n = len(t)
    if n < 2 or T_h <= 0:
        return np.nan
    soma = np.log(T_h / t).sum()
    return float(n / soma) if soma > 0 else np.nan


def projecao_quebras(n: int, beta: float, T_h: float, delta_h: float) -> float:
    """Quebras esperadas em (T, T+Δ] pelo modelo de potência: n·[((T+Δ)/T)^β − 1]."""
    if not np.isfinite(beta) or T_h <= 0:
        return n * delta_h / T_h if T_h > 0 else np.nan
    return float(n * (((T_h + delta_h) / T_h) ** beta - 1))


def benjamini_hochberg(pvals: pd.Series) -> pd.Series:
    """p-valores ajustados por Benjamini-Hochberg (controle da taxa de falsas descobertas)."""
    p = pvals.dropna()
    if p.empty:
        return pd.Series(np.nan, index=pvals.index)
    ordem = p.sort_values()
    m = len(ordem)
    ajust = (ordem * m / np.arange(1, m + 1))[::-1].cummin()[::-1].clip(upper=1)
    return ajust.reindex(pvals.index)


# ============================================================================ AHP
INDICE_ALEATORIO = {1: 0, 2: 0, 3: 0.58, 4: 0.90, 5: 1.12, 6: 1.24, 7: 1.32}


def ahp(matriz: np.ndarray) -> tuple[np.ndarray, float]:
    """Pesos (autovetor principal normalizado) e razão de consistência CR (Saaty, 1980)."""
    a = np.asarray(matriz, float)
    autoval, autovet = np.linalg.eig(a)
    i = np.argmax(autoval.real)
    w = np.abs(autovet[:, i].real)
    w = w / w.sum()
    n = len(a)
    ci = (autoval[i].real - n) / (n - 1)
    cr = ci / INDICE_ALEATORIO[n] if INDICE_ALEATORIO[n] else 0.0
    return w, float(cr)


CRITERIOS = ["C1_impacto", "C2_frequencia", "C3_tendencia", "C4_reincidencia"]
ROTULOS = {"C1_impacto": "Impacto", "C2_frequencia": "Frequência",
           "C3_tendencia": "Tendência", "C4_reincidencia": "Reincidência"}

# Linha é quantas vezes mais importante que a coluna (escala de Saaty).
MATRIZ_AHP = np.array([
    #  C1    C2    C3    C4
    [1,    2,    3,    3],    # C1 impacto: horas paradas × criticidade da máquina
    [1/2,  1,    2,    2],    # C2 frequência de quebras
    [1/3,  1/2,  1,    1],    # C3 tendência (deterioração)
    [1/3,  1/2,  1,    1],    # C4 reincidência em 7 dias
])


# ============================================================================ ranking
def criterios(q: pd.DataFrame, inicio: pd.Timestamp, fim: pd.Timestamp, impacto_maquina: dict,
              nivel=("maquina", "componente"), p=PARAM) -> pd.DataFrame:
    """Critérios brutos e normalizados por item (máquina + componente, por padrão) na janela [inicio, fim)."""
    nivel = list(nivel)
    jan = q.loc[(q["inicio"] >= inicio) & (q["inicio"] < fim)].copy()
    if jan.empty:
        return pd.DataFrame()
    jan["reincidente"] = marcar_reincidencia(q, p=p).reindex(jan.index)  # referência no histórico completo
    T_h = (fim - inicio) / H
    jan["t_h"] = (jan["inicio"] - inicio) / H

    def por_item(g):
        t = g["t_h"].to_numpy()
        n = len(t)
        U, pv = teste_laplace(t, T_h) if n >= p["MIN_QUEBRAS_TENDENCIA"] else (np.nan, np.nan)
        beta = crow_amsaa(t, T_h) if n >= p["MIN_QUEBRAS_TENDENCIA"] else np.nan
        return pd.Series(dict(
            quebras=n, horas=g["duracao_h"].sum(), mttr_h=g["duracao_h"].mean(),
            reincidentes=int(g["reincidente"].sum()), laplace_U=U, p_tendencia=pv, beta=beta,
            ultima_quebra=g["inicio"].max(),
        ))

    c = jan.groupby(nivel)[["t_h", "duracao_h", "reincidente", "inicio"]].apply(por_item).reset_index()
    c["quebras"] = c["quebras"].astype(int)
    c["reincidentes"] = c["reincidentes"].astype(int)
    c["impacto_maquina"] = c["maquina"].map(impacto_maquina).fillna(1.0)

    # taxa de reincidência suavizada para a média da máquina (evita 100% com 2 quebras)
    taxa_mq = c.groupby("maquina")["reincidentes"].transform("sum") / c.groupby("maquina")["quebras"].transform("sum")
    a = p["SUAVIZACAO_REINCIDENCIA"]
    c["taxa_reincidencia"] = (c["reincidentes"] + a * taxa_mq) / (c["quebras"] + a)

    # ---- normalização em [0, 1]
    horas_pond = c["horas"] * c["impacto_maquina"]
    c["C1_impacto"] = horas_pond / horas_pond.max()          # razão ao pior item: preserva proporção
    c["C2_frequencia"] = c["quebras"] / c["quebras"].max()
    c["C3_tendencia"] = stats.norm.cdf(c["laplace_U"]).round(6)     # P(tendência) ∈ [0,1]; 0,5 = sem sinal
    c["C3_tendencia"] = c["C3_tendencia"].fillna(0.5)
    c["C4_reincidencia"] = c["taxa_reincidencia"]
    return c


def ranking(c: pd.DataFrame, pesos: np.ndarray) -> pd.DataFrame:
    """Score 0–100 = 100·Σ wᵢ·Cᵢ, com a contribuição de cada critério em pontos."""
    r = c.copy()
    for k, w in zip(CRITERIOS, pesos):
        r[f"pts_{k}"] = 100 * w * r[k]
    r["score"] = r[[f"pts_{k}" for k in CRITERIOS]].sum(axis=1)
    r = r.sort_values(["score", "horas"], ascending=False).reset_index(drop=True)
    r.insert(0, "posicao", np.arange(1, len(r) + 1))
    return r


def sensibilidade_pesos(c: pd.DataFrame, pesos: np.ndarray, n_sorteios=2000, concentracao=40,
                        top=10, semente=0) -> tuple[pd.DataFrame, np.ndarray]:
    """Sorteia pesos de uma Dirichlet centrada nos pesos AHP e mede a estabilidade do ranking.

    Retorna, por item: frequência no top-N, posição mediana e intervalo 10–90%; e a correlação de
    Spearman de cada sorteio com o ranking base."""
    rng = np.random.default_rng(semente)
    X = c[CRITERIOS].to_numpy()
    base = X @ pesos
    W = rng.dirichlet(pesos * concentracao, size=n_sorteios)
    S = X @ W.T                                            # itens × sorteios
    pos = (-S).argsort(axis=0).argsort(axis=0) + 1         # posição de cada item em cada sorteio
    rho = np.array([stats.spearmanr(base, S[:, j]).statistic for j in range(n_sorteios)])
    out = c[["maquina", "componente"]].copy() if "componente" in c else c[["maquina"]].copy()
    out["freq_top"] = (pos <= top).mean(axis=1)
    out["pos_mediana"] = np.median(pos, axis=1)
    out["pos_p10"] = np.percentile(pos, 10, axis=1)
    out["pos_p90"] = np.percentile(pos, 90, axis=1)
    return out, rho


def ranking_movel(q: pd.DataFrame, datas_corte, janela_dias: int, impacto_maquina: dict, pesos,
                  nivel=("maquina", "componente")) -> pd.DataFrame:
    """Ranking recalculado em cada data de corte, sempre com os últimos `janela_dias` dias."""
    linhas = []
    for corte in datas_corte:
        c = criterios(q, corte - pd.Timedelta(days=janela_dias), corte, impacto_maquina, nivel)
        if c.empty:
            continue
        r = ranking(c, pesos)
        r["corte"] = corte
        linhas.append(r)
    return pd.concat(linhas, ignore_index=True)


# ============================================================================ deterioração
def deterioracao(q: pd.DataFrame, inicio: pd.Timestamp, fim: pd.Timestamp, impacto_maquina: dict,
                 nivel=("maquina", "componente"), p=PARAM) -> pd.DataFrame:
    """Por item: teste de Laplace (com correção FDR), β de Crow-AMSAA, tendência do MTTR,
    quebras projetadas nos próximos dias e horas em risco. Classifica em alerta / atenção / estável."""
    nivel = list(nivel)
    jan = q.loc[(q["inicio"] >= inicio) & (q["inicio"] < fim)].copy()
    T_h = (fim - inicio) / H
    jan["t_h"] = (jan["inicio"] - inicio) / H
    delta_h = p["HORIZONTE_PROJECAO_DIAS"] * 24
    linhas = []
    for chave, g in jan.groupby(nivel):
        t = g["t_h"].to_numpy()
        n = len(t)
        if n < p["MIN_QUEBRAS_TENDENCIA"]:
            continue
        U, pv = teste_laplace(t, T_h)
        beta = crow_amsaa(t, T_h)
        # tendência da duração: τ de Kendall entre ordem no tempo e duração (Mann-Kendall)
        tau, p_mttr = (stats.kendalltau(t, g["duracao_h"].to_numpy()) if n >= 6 else (np.nan, np.nan))
        meio = T_h / 2
        n1, n2 = (t < meio).sum(), (t >= meio).sum()
        proj = projecao_quebras(n, beta, T_h, delta_h)
        chave = chave if isinstance(chave, tuple) else (chave,)
        linhas.append(dict(zip(nivel, chave), quebras=n, quebras_1a_metade=int(n1), quebras_2a_metade=int(n2),
                           laplace_U=U, p_tendencia=pv, beta=beta,
                           tau_mttr=tau, p_mttr=p_mttr, mttr_h=g["duracao_h"].mean(),
                           quebras_proj=proj, horas_em_risco=proj * g["duracao_h"].mean()
                           * impacto_maquina.get(chave[0], 1.0)))
    d = pd.DataFrame(linhas)
    if d.empty:
        return d
    d["p_ajustado"] = benjamini_hochberg(d["p_tendencia"])
    d["sinal"] = np.select(
        [d["p_ajustado"] <= p["ALFA_FDR"], d["p_tendencia"] <= 0.10],
        ["alerta", "atenção"], default="estável")
    d.loc[(d["sinal"] == "estável") & (d["p_mttr"] <= 0.05) & (d["tau_mttr"] > 0), "sinal"] = "atenção"
    ordem = {"alerta": 0, "atenção": 1, "estável": 2}
    return d.sort_values(["sinal", "horas_em_risco"], key=lambda s: s.map(ordem) if s.name == "sinal" else -s)\
            .reset_index(drop=True)


def intervalos_entre_quebras(q: pd.DataFrame, nivel=("maquina", "componente")) -> pd.DataFrame:
    """Tempo entre quebras consecutivas do mesmo item (TBF), em dias."""
    s = q.sort_values("inicio").copy()
    s["tbf_d"] = s.groupby(list(nivel))["inicio"].diff() / pd.Timedelta(days=1)
    return s.dropna(subset=["tbf_d"])


# ============================================================================ efetividade das intervenções
def tempo_ate_proxima_quebra(eventos: pd.DataFrame, q: pd.DataFrame, fim_obs: pd.Timestamp,
                             tipos=("CORRETIVA PLANEJADA", "CORRETIVA")) -> pd.DataFrame:
    """Para cada intervenção (evento MES + SAP confiável), dias até a próxima quebra do mesmo
    máquina + componente, contados do fim da parada. Sem nova quebra até o fim dos dados → censurado.

    `tipos` define os grupos comparados. Quando a base tiver ordens PREVENTIVA, basta incluí-las aqui."""
    e = eventos.loc[(eventos["classe"] == "MES + SAP")
                    & eventos["confianca"].isin(["alta", "media"])
                    & ~eventos["ambiguo"].fillna(False).astype(bool)
                    & eventos["tipo_sap"].isin(tipos)
                    & eventos["componente_mes"].notna()].copy()
    linhas = []
    por_item = {k: g["inicio"].sort_values().to_numpy() for k, g in q.groupby(["maquina", "componente"])}
    for _, r in e.iterrows():
        inicios = por_item.get((r["maquina"], r["componente_mes"]), np.array([], dtype="datetime64[ns]"))
        depois = inicios[inicios > np.datetime64(r["mes_fim"])]
        if len(depois):
            dias, evento = (pd.Timestamp(depois[0]) - r["mes_fim"]) / pd.Timedelta(days=1), True
        else:
            dias, evento = (fim_obs - r["mes_fim"]) / pd.Timedelta(days=1), False
        if dias <= 0:
            continue
        linhas.append(dict(id_evento=r["id_evento"], maquina=r["maquina"], componente=r["componente_mes"],
                           tipo=r["tipo_sap"], tipo_atividade=r["tipo_atividade_sap"],
                           parada_foi_quebra=bool(r["contem_quebra"]), dias=dias, quebrou=evento))
    return pd.DataFrame(linhas)


def logrank_estratificado(s: pd.DataFrame, grupo="tipo", estrato="maquina") -> tuple[float, float, str]:
    """Log-rank estratificado (2 grupos): compara os grupos DENTRO de cada estrato e soma as evidências.

    Evita o paradoxo de misturar máquinas com taxas de falha muito diferentes e proporções
    diferentes de cada grupo. Retorna (qui-quadrado, p bilateral, grupo de referência)."""
    grupos = sorted(s[grupo].unique())
    if len(grupos) != 2:
        raise ValueError("o log-rank estratificado compara exatamente dois grupos")
    ref = grupos[0]
    O = E = V = 0.0
    for _, d in s.groupby(estrato):
        tempos = np.sort(d.loc[d["quebrou"], "dias"].unique())
        for t in tempos:
            em_risco = d["dias"] >= t
            n = em_risco.sum()
            n1 = (em_risco & (d[grupo] == ref)).sum()
            falhas = (d["dias"] == t) & d["quebrou"]
            m = falhas.sum()
            m1 = (falhas & (d[grupo] == ref)).sum()
            if n < 2:
                continue
            O += m1
            E += m * n1 / n
            V += m * (n1 / n) * (1 - n1 / n) * (n - m) / (n - 1)
    chi2 = (O - E) ** 2 / V if V > 0 else np.nan
    return float(chi2), float(stats.chi2.sf(chi2, 1)), ref


def kaplan_meier(dias: np.ndarray, quebrou: np.ndarray):
    """Curva de sobrevivência (Kaplan-Meier) com censura à direita."""
    dados = stats.CensoredData(uncensored=dias[quebrou], right=dias[~quebrou])
    return stats.ecdf(dados).sf


def comparar_grupos(s: pd.DataFrame, grupo="tipo", horizonte_d=30.0) -> tuple[pd.DataFrame, object]:
    """Mediana e RMST (tempo médio sem quebra restrito ao horizonte) por grupo, e teste log-rank."""
    linhas, curvas = [], {}
    for g, d in s.groupby(grupo):
        sf = kaplan_meier(d["dias"].to_numpy(), d["quebrou"].to_numpy())
        curvas[g] = sf
        grade = np.linspace(0, horizonte_d, 601)
        rmst = _trapz(sf.evaluate(grade), grade)
        x, y = sf.quantiles, sf.probabilities
        mediana = float(x[np.argmax(y <= 0.5)]) if (y <= 0.5).any() else np.nan
        linhas.append(dict(grupo=g, n=len(d), quebraram=int(d["quebrou"].sum()),
                           sem_quebra_em_30d=float(sf.evaluate(horizonte_d)),
                           mediana_dias=mediana, rmst_dias=rmst))
    teste = None
    grupos = list(curvas)
    if len(grupos) == 2:
        a, b = (s.loc[s[grupo] == g] for g in grupos)
        teste = stats.logrank(
            stats.CensoredData(uncensored=a.loc[a.quebrou, "dias"].to_numpy(), right=a.loc[~a.quebrou, "dias"].to_numpy()),
            stats.CensoredData(uncensored=b.loc[b.quebrou, "dias"].to_numpy(), right=b.loc[~b.quebrou, "dias"].to_numpy()))
    return pd.DataFrame(linhas), (curvas, teste)
