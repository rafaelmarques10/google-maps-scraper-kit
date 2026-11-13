"""Finds complaints in review texts from a lexicon of themes and keywords: standard library only.

A lexicon is plain JSON, so the web UI can edit it and a file can carry it between machines:

    {"nome": "geral-ptbr", "versao": 1,
     "nota_maxima_queixa": 3,          # a review rated above this is not a complaint ...
     "incluir_notas_altas": false,     # ... unless this is true (a 5-star "but it takes long" counts)
     "negacoes": ["nao", "nunca", "sem"],
     "temas": {"demora": {"rotulo": "Demora no atendimento", "solucao": "Agendamento online",
                          "termos": ["demora*", "esperei", "muito tempo", "fila"], "peso": 1}}}

Terms are matched on folded text (lower case, no accents), on word boundaries; a trailing "*" matches any
ending ("demora*" -> demora, demorou, demorada); several words are a phrase. A term right after a
negation ("nao demorou", "nunca e lento") does not count; a negation farther back ("nao gostei, demorou")
does not cancel it.
"""
import json, os, re
from privacy import fold

DEFAULT_NEGATIONS = ["nao", "nunca", "jamais", "sem", "nenhum", "nenhuma", "never", "not"]
FILLERS = {"foi", "era", "ser", "e", "so", "tao", "muito", "mais", "tem", "ha", "esta", "estava", "fica", "ficou", "eh"}
LIMITS = {"temas": 60, "termos": 200, "termo_len": 80}
TOKEN = re.compile(r"[a-z0-9_]+")


class LexiconError(ValueError):
    pass


def validate(lex):
    """The lexicon cleaned up (terms folded and de-duplicated), or LexiconError saying what is wrong."""
    if not isinstance(lex, dict):
        raise LexiconError("o dicionário deve ser um objeto JSON")
    themes = lex.get("temas")
    if not isinstance(themes, dict) or not themes:
        raise LexiconError("o dicionário precisa de ao menos um tema em \"temas\"")
    if len(themes) > LIMITS["temas"]:
        raise LexiconError(f"no máximo {LIMITS['temas']} temas")
    out = {"nome": str(lex.get("nome") or "personalizado")[:80], "versao": int(lex.get("versao") or 1),
           "nota_maxima_queixa": _rating(lex.get("nota_maxima_queixa", 3)),
           "incluir_notas_altas": bool(lex.get("incluir_notas_altas", False)),
           "negacoes": [fold(str(n)).strip() for n in lex.get("negacoes", DEFAULT_NEGATIONS) if str(n).strip()][:50],
           "temas": {}}
    for key, t in themes.items():
        slug = re.sub(r"[^a-z0-9_]+", "_", fold(str(key))).strip("_")[:40]
        if not slug or not isinstance(t, dict):
            raise LexiconError(f"tema inválido: {key!r}")
        terms, seen = [], set()
        for raw in t.get("termos", []):
            term = " ".join(fold(str(raw)).split())
            if not term or len(term) > LIMITS["termo_len"] or term in seen:
                continue
            if not re.fullmatch(r"[a-z0-9_ *]+", term) or term.strip("*") == "":
                raise LexiconError(f"termo inválido em {slug}: {raw!r} (use letras, números, espaço e * no fim)")
            seen.add(term)
            terms.append(term)
        if not terms:
            raise LexiconError(f"o tema {slug} não tem termos")
        if len(terms) > LIMITS["termos"]:
            raise LexiconError(f"o tema {slug} passa de {LIMITS['termos']} termos")
        out["temas"][slug] = {"rotulo": str(t.get("rotulo") or slug)[:80], "solucao": str(t.get("solucao") or "")[:120],
                              "termos": terms, "peso": max(0.1, min(10.0, float(t.get("peso", 1))))}
    return out


def _rating(v):
    if isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= 5:
        raise LexiconError("nota_maxima_queixa: um número de 1 a 5")
    return v


def load_file(path):
    with open(path, encoding="utf-8") as f:
        return validate(json.load(f))


class Matcher:
    """A compiled lexicon. match(text, rating) -> {theme: [terms found]} for the complaint themes."""

    def __init__(self, lex):
        self.lex = validate(lex)
        self.neg = set(self.lex["negacoes"])
        self.rx = {}
        for slug, t in self.lex["temas"].items():
            parts = []
            for term in t["termos"]:
                words = [re.escape(w.rstrip("*")) + (r"[a-z0-9_]*" if w.endswith("*") else "") for w in term.split()]
                parts.append(r"\s+".join(words))
            self.rx[slug] = re.compile(r"(?<![a-z0-9_])(?:" + "|".join(parts) + r")(?![a-z0-9_])")

    def is_complaint_rating(self, rating):
        if rating is None:
            return True
        return rating <= self.lex["nota_maxima_queixa"] or self.lex["incluir_notas_altas"]

    def match(self, text, rating=None):
        """{theme slug: [matched words]} for a review text; {} when its rating rules it out, or nothing matched."""
        if not self.is_complaint_rating(rating):
            return {}
        folded = fold(text)
        found = {}
        for slug, rx in self.rx.items():
            hits = []
            for m in rx.finditer(folded):
                before = TOKEN.findall(folded[max(0, m.start() - 40):m.start()])
                prev = before[-1] if before else ""
                if prev in self.neg or (prev in FILLERS and len(before) > 1 and before[-2] in self.neg):
                    continue
                hits.append(m.group(0))
            if hits:
                found[slug] = sorted(set(hits))
        return found


def preset_path(name="ptbr-geral"):
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "presets", f"lexicon-{name}.json")


def list_presets():
    d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "presets")
    return sorted(f[len("lexicon-"):-len(".json")] for f in os.listdir(d) if f.startswith("lexicon-") and f.endswith(".json"))
