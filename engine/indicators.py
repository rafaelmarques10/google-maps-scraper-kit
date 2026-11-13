"""Indicators from a place's collected reviews, the numbers a sales approach quotes: standard library only.

A row is a dict with: data (ISO date or ""), nota (1-5 or None), temas (list of theme slugs), autor_id,
tem_resposta (the owner answered). Everything here is arithmetic on those rows, so a prospect can check it
against the linked reviews.
"""
from collections import Counter, defaultdict
from datetime import date, timedelta


def compute(rows, weights=None, since_days=365, today=None):
    """{total, periodo, com_queixa, pct_queixa, nota_media, taxa_resposta, temas, tendencia, score_dor}.

    - periodo: reviews dated in the last `since_days` days (undated reviews count in total only)
    - com_queixa / pct_queixa: reviews in the period with at least one complaint theme
    - taxa_resposta: share of complaint reviews the owner answered (the "silence" argument)
    - temas: {slug: {n, pct, autores}} in the period; autores = distinct authors, so one angry customer
      posting three times counts once
    - tendencia: the last 12 months, [{mes, total, queixas}]
    - score_dor: weighted complaints per 100 reviews in the period, to rank leads
    """
    weights = weights or {}
    today = today or date.today()
    start = (today - timedelta(days=since_days)).isoformat()
    period = [r for r in rows if r.get("data") and r["data"] >= start]
    complaints = [r for r in period if r.get("temas")]
    rated = [r["nota"] for r in rows if isinstance(r.get("nota"), int)]
    themes = {}
    count, authors = Counter(), defaultdict(set)
    for r in complaints:
        for t in r["temas"]:
            count[t] += 1
            if r.get("autor_id"):
                authors[t].add(r["autor_id"])
    for t, n in count.most_common():
        themes[t] = {"n": n, "pct": round(100 * n / len(period), 1) if period else 0.0,
                     "autores": len(authors[t]) or n}
    answered = sum(1 for r in complaints if r.get("tem_resposta"))
    months = []
    y, m = today.year, today.month
    for _ in range(12):
        months.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    by_month = {k: [0, 0] for k in months}
    for r in rows:
        k = (r.get("data") or "")[:7]
        if k in by_month:
            by_month[k][0] += 1
            by_month[k][1] += 1 if r.get("temas") else 0
    score = 0.0
    if period:
        score = round(100 * sum(weights.get(t, 1) * n for t, n in count.items()) / len(period), 1)
    return {
        "total": len(rows), "periodo": len(period), "com_queixa": len(complaints),
        "pct_queixa": round(100 * len(complaints) / len(period), 1) if period else 0.0,
        "nota_media": round(sum(rated) / len(rated), 2) if rated else None,
        "taxa_resposta": round(100 * answered / len(complaints), 1) if complaints else None,
        "temas": themes,
        "tendencia": [{"mes": k, "total": by_month[k][0], "queixas": by_month[k][1]} for k in reversed(months)],
        "score_dor": score,
    }
