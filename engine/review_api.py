"""HTTP routes for the reviews side of the engine, called from serve.py's Handler: standard library only.

    GET  /parametrizacao                      the settings page (search, reviews, keyword dictionary, results)
    GET/PUT/DELETE /api/v1/lexicons[/<name>]  keyword dictionaries (presets that ship + the user's own)
    GET/PUT/DELETE /api/v1/profiles[/<name>]  saved settings of the page (a search recipe)
    POST/GET /api/v1/review-jobs              queue / list review collections
    GET/DELETE /api/v1/review-jobs/<id>       one job / stop and delete it with its reviews
    GET  /api/v1/reviews/leaderboard          places with their indicators, worst first
    GET  /api/v1/reviews/place?id=            one place's reviews (anonymised)
    GET  /api/v1/reviews/search?q=            keyword search over everything collected
    GET  /api/v1/reviews/export               CSV of the reviews
    POST /api/v1/lexicons/test                try a dictionary on one text
    POST /api/v1/reviews/reclassify           apply a dictionary to reviews already collected (no new request to Google)
"""
import json, os, re, secrets, urllib.parse

import complaints, reviews

PAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui", "parametrizacao.html")
ROUTE = re.compile(r"^/(parametrizacao|api/v1/(?:review-jobs|reviews|lexicons|profiles)(?:/.*)?)$")
JOB = re.compile(r"^/api/v1/review-jobs/([0-9a-f-]{36})(/stop)?$")
NAMED = re.compile(r"^/api/v1/(lexicons|profiles)(?:/([^/]+))?$")


def handles(path):
    return bool(ROUTE.match(path))


def _public(job):
    p = dict(job["params"])
    p["places"] = len(p.get("places") or [])
    p.pop("proxies", None)  # may hold a password
    return {**job, "params": p}


def dispatch(h, method, path, query, REPLIED):
    """Answers the request on handler `h`; True when it was a reviews route."""
    svc = getattr(h.server, "reviews", None)
    if svc is None:
        return h.fail(503, "o módulo de reviews não está ativo neste engine")
    store, worker = svc.store, svc.worker
    qs = urllib.parse.parse_qs(query)

    def arg(k, default=""):
        return qs.get(k, [default])[0]

    def num(k, default, lo, hi):
        try:
            return max(lo, min(hi, int(arg(k, str(default)))))
        except ValueError:
            return default

    if path == "/parametrizacao" and method == "GET":
        nonce = secrets.token_urlsafe(16)
        with open(PAGE, encoding="utf-8") as f:
            page = f.read().replace("__NONCE__", nonce).encode()
        csp = (f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; connect-src 'self'; "
               "img-src 'self' data:; form-action 'none'; base-uri 'none'; frame-ancestors 'none'")
        return h.reply(200, page, "text/html; charset=utf-8", [("Content-Security-Policy", csp)])

    if path == "/api/v1/lexicons/test" and method == "POST":  # try a dictionary on one text before saving it
        body = h.body()
        if body is REPLIED:
            return
        try:
            lex = complaints.validate(body.get("lexicon") if isinstance(body, dict) else None)
            nota = body.get("nota")
            nota = nota if isinstance(nota, int) and not isinstance(nota, bool) else None
            found = complaints.Matcher(lex).match(str(body.get("texto") or "")[:5000], nota)
        except ValueError as e:
            return h.fail(422, str(e))
        return h.reply(200, {"temas": found})

    # ── named dictionaries and profiles ──
    m = NAMED.match(path)
    if m:
        kind, name = m.group(1), urllib.parse.unquote(m.group(2) or "")
        if not name:
            if method != "GET":
                return h.fail(405, "use GET")
            body = {"meus": store.list_named(kind)}
            if kind == "lexicons":
                body["presets"] = complaints.list_presets()
            return h.reply(200, body)
        if method == "GET":
            data = store.lexicon(name) if kind == "lexicons" else store.get_named(kind, name)
            return h.reply(200, data) if data is not None else h.fail(404, "não encontrado")
        if method == "PUT":
            body = h.body()
            if body is REPLIED:
                return
            try:
                if kind == "lexicons":
                    body = complaints.validate(body)
                elif not isinstance(body, dict) or len(json.dumps(body)) > 200_000:
                    raise ValueError("o perfil deve ser um objeto JSON de até 200 KB")
                saved = store.save_named(kind, name, body)
            except (ValueError, TypeError) as e:  # LexiconError is a ValueError
                return h.fail(422, str(e))
            return h.reply(200, {"nome": saved})
        if method == "DELETE":
            return h.reply(200, {"message": "deleted"}) if store.delete_named(kind, name) else h.fail(404, "não encontrado")
        return h.fail(405, "método não permitido")

    # ── review jobs ──
    if path == "/api/v1/review-jobs":
        if method == "GET":
            return h.reply(200, [_public(j) for j in store.list_jobs()])
        if method == "POST":
            body = h.body()
            if body is REPLIED:
                return
            try:
                name, params = reviews.validate(body)
                if not store.lexicon(params["lexicon"]):
                    raise ValueError(f"dicionário não encontrado: {params['lexicon']}")
            except ValueError as e:
                return h.fail(422, str(e))
            jid = store.create_job(name, params)
            worker.wake.set()
            return h.reply(201, {"id": jid})
        return h.fail(405, "método não permitido")
    m = JOB.match(path)
    if m:
        jid = m.group(1)
        job = store.get_job(jid)
        if not job:
            return h.fail(404, "job não encontrado")
        if m.group(2):
            if method != "POST":
                return h.fail(405, "use POST")
            store.stop_job(jid)
            worker.cancel(jid)
            return h.reply(200, {"message": "stopped"})
        if method == "GET":
            return h.reply(200, _public(job))
        if method == "DELETE":
            store.stop_job(jid)
            worker.cancel(jid)
            store.delete_job(jid)
            return h.reply(200, {"message": "deleted"})
        return h.fail(405, "método não permitido")

    # ── reading what was collected ──
    if path == "/api/v1/reviews/leaderboard" and method == "GET":
        lex = store.lexicon(arg("lexicon", "ptbr-geral")) or store.lexicon("ptbr-geral")
        return h.reply(200, store.leaderboard(lex, num("since_days", 365, 1, 3650), num("min_reviews", 1, 0, 100000),
                                              arg("theme") or None))
    if path == "/api/v1/reviews/place" and method == "GET":
        return h.reply(200, store.reviews_of(arg("id"), arg("theme") or None, num("limit", 500, 1, 2000)))
    if path == "/api/v1/reviews/search" and method == "GET":
        terms = [t.strip() for t in re.split(r"[,\n;]", arg("q")) if t.strip()][:30]
        if not terms:
            return h.fail(422, "informe q: palavras separadas por vírgula")
        return h.reply(200, store.search(terms, "all" if arg("mode") == "all" else "any", arg("place") or None,
                                         arg("since") or None, num("max_rating", 0, 0, 5) or None, arg("theme") or None,
                                         num("limit", 200, 1, 1000)))
    if path == "/api/v1/reviews/export" and method == "GET":
        ids = [x for x in qs.get("place", []) if x] or None
        return h.reply(200, store.export_csv(ids), "text/csv; charset=utf-8",
                       [("Content-Disposition", 'attachment; filename="reviews-anonimizadas.csv"')])
    if path == "/api/v1/reviews/reclassify" and method == "POST":
        body = h.body()
        if body is REPLIED:
            return
        try:
            spec = body.get("lexicon") if isinstance(body, dict) else None
            lex = complaints.validate(spec) if isinstance(spec, dict) else store.lexicon(str(spec or "ptbr-geral"))
            if not lex:
                raise ValueError("dicionário não encontrado")
            n = store.reclassify(complaints.Matcher(lex))
        except ValueError as e:
            return h.fail(422, str(e))
        return h.reply(200, {"reviews": n})
    return h.fail(404, "not found")
