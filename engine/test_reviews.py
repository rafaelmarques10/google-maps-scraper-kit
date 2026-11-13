#!/usr/bin/env python3
"""Offline checks for the reviews side: anonymisation, the keyword dictionary, the batch parser (on a
synthetic response built from LAYOUT: it proves the code, not that Google still uses these positions: run
scripts/reviews_probe.py on a real response for that), the database, indicators and the HTTP routes.
No network, no Chrome:  python3 engine/test_reviews.py"""
import http.client, json, os, sys, tempfile, threading, unittest
from datetime import date, datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import complaints, indicators, jobs, privacy, review_api, reviews, serve, store as rstore

NOW = datetime(2026, 11, 13, tzinfo=timezone.utc)


def put(root, path, value):
    node = root
    for i in path[:-1]:
        while len(node) <= i:
            node.append(None)
        if node[i] is None:
            node[i] = []
        node = node[i]
    while len(node) <= path[-1]:
        node.append(None)
    node[path[-1]] = value


def entry(rid, rating, text, when="2 months ago", micros=None, reply="", contrib="111", url=True):
    e = []
    put(e, reviews.LAYOUT["id"], rid)
    put(e, reviews.LAYOUT["rating"], rating)
    put(e, reviews.LAYOUT["text"], text)
    put(e, reviews.LAYOUT["when"], when)
    put(e, (0, 1, 4, 5, 2), f"https://www.google.com/maps/contrib/{contrib}?hl=pt")
    if micros:
        put(e, (0, 1, 2), micros)
    if url:
        put(e, (0, 18), f"https://www.google.com/maps/reviews/data=!4m8!1s{rid}")
    if reply:
        put(e, reviews.LAYOUT["owner_reply"][0], reply)
    return e


def batch(entries, token="TOKEN"):
    data = []
    put(data, reviews.LAYOUT["next"], token)
    put(data, reviews.LAYOUT["list"], entries)
    return ")]}'\n" + json.dumps(data)


class Privacy(unittest.TestCase):
    def test_scrub(self):
        t = privacy.scrub("Liguei (61) 99999-1234 e fulano@x.com.br, cpf 123.456.789-09, cnpj 12.345.678/0001-95 www.site.com @fulano")
        for leak in ("99999", "fulano@", "123.456", "0001-95", "www.site", "@fulano"):
            self.assertNotIn(leak, t)
        self.assertIn("[telefone]", t)
        self.assertEqual(privacy.scrub("Pagamos R$ 1500 em 10x"), "Pagamos R$ 1500 em 10x")  # not a phone

    def test_author_hash_is_keyed_and_stable(self):
        self.assertEqual(privacy.author_id(b"a" * 32, "111"), privacy.author_id(b"a" * 32, "111"))
        self.assertNotEqual(privacy.author_id(b"a" * 32, "111"), privacy.author_id(b"b" * 32, "111"))
        self.assertNotIn("111", privacy.author_id(b"a" * 32, "111"))
        self.assertEqual(privacy.author_id(b"a" * 32, ""), "")

    def test_relative_dates(self):
        for text, prec in (("há 2 meses", "mes"), ("2 months ago", "mes"), ("a month ago", "mes"), ("hace un año", "ano"),
                           ("Editado há 3 semanas", "semana"), ("ontem", "dia"), ("um ano atrás", "ano")):
            iso, p = privacy.approx_date(text, NOW)
            self.assertTrue(iso, text)
            self.assertEqual(p, prec, text)
        self.assertEqual(privacy.approx_date("ontem", NOW)[0], "2026-11-12")
        self.assertEqual(privacy.approx_date("sem data", NOW), ("", ""))
        self.assertEqual(privacy.from_micros(1_762_000_000_000_000), "2025-11-01")
        self.assertEqual(privacy.from_micros(5), "")

    def test_salt_is_created_once_and_private(self):
        with tempfile.TemporaryDirectory() as d:
            a = privacy.load_salt(d)
            self.assertEqual(a, privacy.load_salt(d))
            if os.name != "nt":
                self.assertEqual(os.stat(os.path.join(d, privacy.SALT_FILE)).st_mode & 0o077, 0)


class Dictionary(unittest.TestCase):
    def setUp(self):
        self.m = complaints.Matcher(complaints.load_file(complaints.preset_path()))

    def test_matches_and_negation(self):
        self.assertIn("demora", self.m.match("Demorou mais de uma hora", 1))
        self.assertEqual(self.m.match("Não demorou nada", 2), {})
        self.assertIn("demora", self.m.match("Não gostei, demorou demais", 2))  # a far negation does not cancel
        self.assertIn("sem_resposta", self.m.match("Ninguém atendeu o telefone", 1))

    def test_rating_gate(self):
        self.assertEqual(self.m.match("demorou", 5), {})
        lex = complaints.load_file(complaints.preset_path())
        lex["incluir_notas_altas"] = True
        self.assertIn("demora", complaints.Matcher(lex).match("demorou", 5))

    def test_wildcard_and_word_boundary(self):
        lex = {"temas": {"a": {"termos": ["demora*"]}}}
        m = complaints.Matcher(lex)
        self.assertTrue(m.match("A demorada espera", 1))
        self.assertEqual(complaints.Matcher({"temas": {"a": {"termos": ["fila"]}}}).match("filante", 1), {})

    def test_validation(self):
        for bad in (None, {}, {"temas": {}}, {"temas": {"a": {"termos": []}}}, {"temas": {"a": {"termos": ["a;b"]}}},
                    {"temas": {"a": {"termos": ["x"]}}, "nota_maxima_queixa": 9}):
            with self.assertRaises(complaints.LexiconError):
                complaints.validate(bad)
        ok = complaints.validate({"temas": {"Demora Muito": {"termos": ["Demora*", "demora*", " Fila "]}}})
        self.assertEqual(list(ok["temas"]), ["demora_muito"])
        self.assertEqual(ok["temas"]["demora_muito"]["termos"], ["demora*", "fila"])

    def test_preset_is_valid(self):
        self.assertIn("ptbr-geral", complaints.list_presets())
        self.assertGreaterEqual(len(self.m.lex["temas"]), 10)


class Parser(unittest.TestCase):
    def test_batch(self):
        body = batch([entry("R1", 1, "Demorou muito. Tel 61 99999-0000", micros=1_762_000_000_000_000, reply="Sentimos muito"),
                      entry("R2", 5, "Ótimo", url=False), [None]])
        got, token = reviews.parse_batch(body, "https://www.google.com/maps/place/x", NOW)
        self.assertEqual(token, "TOKEN")
        self.assertEqual([r["review_id"] for r in got], ["R1", "R2"])
        r1, r2 = got
        self.assertEqual((r1["nota"], r1["data"], r1["data_precisao"]), (1, "2025-11-01", "dia"))
        self.assertTrue(r1["url_exata"] and r1["review_url"].startswith("https://www.google.com/maps/reviews/data="))
        self.assertEqual((r1["resposta_dono"], r1["autor_chave"]), ("Sentimos muito", "111"))
        self.assertFalse(r2["url_exata"])
        self.assertEqual(r2["review_url"], "https://www.google.com/maps/place/x")  # falls back to the place
        self.assertEqual(r2["data_precisao"], "mes")  # no timestamp: the relative date

    def test_garbage(self):
        for body in ("", "not json", ")]}'\n[]", ")]}'\n{}", ")]}'\n[null,null,[[1,2],[[]]]]"):
            self.assertEqual(reviews.parse_batch(body)[0], [])

    def test_select_places(self):
        p = {"min_reviews": 10, "max_place_rating": 4.5, "max_places": 2}
        rows = [{"link": "l1", "place_id": "a", "review_count": "50", "review_rating": "4.0"},
                {"link": "l2", "place_id": "b", "review_count": "50", "review_rating": "4.9"},   # rates too well
                {"link": "l3", "place_id": "c", "review_count": "3", "review_rating": "3.0"},    # too few reviews
                {"link": "", "place_id": "d", "review_count": "50", "review_rating": "3.0"},     # no link
                {"link": "l5", "place_id": "e", "review_count": "20", "review_rating": "3.5"},
                {"link": "l6", "place_id": "f", "review_count": "20", "review_rating": "3.5"}]
        self.assertEqual([r["place_id"] for r in reviews.select_places(rows, p)], ["a", "e"])

    def test_job_body(self):
        name, p = reviews.validate({"source_job": "0" * 8 + "-0000-0000-0000-" + "0" * 12})
        self.assertEqual((p["max_reviews"], p["since_days"], p["lexicon"]), (100, 365, "ptbr-geral"))
        for bad in ({}, {"places": [{"x": 1}]}, {"places": [{"link": "u"}], "pace": 0.1}, {"places": [{"link": "u"}], "max_reviews": 0},
                    {"places": [{"link": "u"}], "max_place_rating": 9}, {"source_job": "nope"}):
            with self.assertRaises(ValueError):
                reviews.validate(bad)


class Database(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = rstore.ReviewStore(self.tmp.name)
        self.lex = complaints.load_file(complaints.preset_path())
        self.m = complaints.Matcher(self.lex)
        self.s.upsert_place({"place_id": "P1", "title": "Bar do Zé", "link": "https://www.google.com/maps/place/z", "review_rating": "3.9", "review_count": "80"}, "J")
        today = date.today().isoformat()
        base = {"data": today, "data_precisao": "dia", "url_exata": True, "review_url": "https://www.google.com/maps/reviews/data=1"}
        self.s.add_reviews("P1", "J", [
            {**base, "review_id": "a", "nota": 1, "texto": "Demorou demais, ligue 61 98888-7777", "autor_chave": "u1", "resposta_dono": ""},
            {**base, "review_id": "b", "nota": 2, "texto": "Demora e fila enorme", "autor_chave": "u2", "resposta_dono": "Desculpe"},
            {**base, "review_id": "c", "nota": 5, "texto": "Adorei, voltarei", "autor_chave": "u3"},
            {**base, "review_id": "d", "nota": 1, "texto": "=HYPERLINK(\"http://x\")", "autor_chave": "u1"}], self.m)

    def tearDown(self):
        self.s.close()
        self.tmp.cleanup()

    def test_anonymised(self):
        rows = self.s.reviews_of("P1")
        text = " ".join(r["texto"] for r in rows)
        self.assertNotIn("98888", text)
        self.assertTrue(all(len(r["autor_id"]) == 12 and not r["autor_id"].startswith("u") for r in rows))
        self.assertEqual(len({r["autor_id"] for r in rows}), 3)  # u1 twice -> same hash
        self.assertEqual(self.s.add_reviews("P1", "J", [{"review_id": "a", "nota": 1, "texto": "x"}], self.m), 0)  # no duplicates

    def test_indicators_and_ranking(self):
        board = self.s.leaderboard(self.lex)
        self.assertEqual(len(board), 1)
        i = board[0]["indicadores"]
        self.assertEqual((i["total"], i["periodo"], i["com_queixa"]), (4, 4, 2))
        self.assertEqual(i["pct_queixa"], 50.0)
        self.assertEqual(i["temas"]["demora"]["n"], 2)
        self.assertEqual(i["taxa_resposta"], 50.0)  # one of the two complaints was answered
        self.assertEqual(self.s.leaderboard(self.lex, theme="limpeza"), [])
        self.assertEqual(self.s.leaderboard(self.lex, min_reviews=99), [])

    def test_search(self):
        self.assertEqual(len(self.s.search(["demor"])), 2)       # a substring: finds demorou and demora
        self.assertEqual(len(self.s.search(["demora"])), 1)      # only "Demora e fila"
        self.assertEqual(len(self.s.search(["Demorá"])), 1)      # accents and case do not matter
        self.assertEqual(len(self.s.search(["fila", "adorei"], mode="any")), 2)
        self.assertEqual(len(self.s.search(["fila", "adorei"], mode="all")), 0)
        self.assertEqual(len(self.s.search(["demor"], max_rating=1)), 1)
        self.assertEqual(len(self.s.search(["demor*"], theme="demora")), 2)
        self.assertEqual(self.s.search(["100%"]), [])  # LIKE wildcards are literal

    def test_reclassify_with_new_words(self):
        self.assertEqual(self.s.search(["voltarei"], theme="demora"), [])
        lex = complaints.validate({"temas": {"demora": {"termos": ["voltarei"]}}, "incluir_notas_altas": True})
        self.s.reclassify(complaints.Matcher(lex))
        self.assertEqual(len(self.s.search(["voltarei"], theme="demora")), 1)

    def test_csv_has_no_formula_and_no_names(self):
        out = self.s.export_csv().decode("utf-8-sig")
        self.assertIn("'=HYPERLINK", out)
        self.assertNotIn("\n=HYPERLINK", out)
        self.assertNotIn("98888", out)

    def test_named_and_jobs(self):
        self.s.save_named("lexicons", "meu", {"temas": {"x": {"termos": ["fila"]}}})
        self.assertEqual(self.s.lexicon("meu")["temas"]["x"]["termos"], ["fila"])
        self.assertIsNotNone(self.s.lexicon("ptbr-geral"))
        self.assertIsNone(self.s.lexicon("nao-existe"))
        with self.assertRaises(ValueError):
            self.s.save_named("lexicons", "  ///  ", {})
        jid = self.s.create_job("t", {"max_reviews": 1})
        self.assertEqual(self.s.claim()["id"], jid)
        self.assertIsNone(self.s.claim())
        self.assertEqual(self.s.recover(), 1)
        self.assertEqual(self.s.claim()["id"], jid)
        self.s.finish(jid, "ok")
        self.assertEqual(self.s.get_job(jid)["status"], "ok")


class Http(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        leads = jobs.Store(cls.tmp.name)
        cls.rs = rstore.ReviewStore(cls.tmp.name)
        dummy = type("W", (), {"wake": threading.Event(), "cancel": lambda self, j: None})()
        svc = type("S", (), {"store": cls.rs, "worker": dummy})()
        cls.srv = serve.make_servers(0, leads, dummy, "", "127.0.0.1", svc)[0]
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.rs.upsert_place({"place_id": "P1", "title": "Bar", "link": "https://www.google.com/maps/place/z"}, "J")
        cls.rs.add_reviews("P1", "J", [{"review_id": "a", "nota": 1, "texto": "demorou", "data": date.today().isoformat(),
                                        "autor_chave": "u"}], complaints.Matcher(cls.rs.lexicon("ptbr-geral")))

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.rs.close()
        cls.tmp.cleanup()

    def call(self, method, path, body=None, host=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port)
        c.request(method, path, json.dumps(body) if body is not None else None,
                  {"Host": host or f"localhost:{self.port}", "Content-Type": "application/json"})
        r = c.getresponse()
        raw = r.read()
        c.close()
        try:
            return r.status, json.loads(raw), r
        except ValueError:
            return r.status, raw, r

    def test_page_has_csp_and_nonce(self):
        st, body, r = self.call("GET", "/parametrizacao")
        self.assertEqual(st, 200)
        csp = r.getheader("Content-Security-Policy")
        nonce = csp.split("'nonce-")[1].split("'")[0]
        self.assertIn(f'nonce="{nonce}"', body.decode())
        self.assertNotIn("__NONCE__", body.decode())
        self.assertNotIn("innerHTML", body.decode())  # scraped text must never be parsed as HTML

    def test_foreign_host_refused(self):
        self.assertEqual(self.call("GET", "/api/v1/reviews/leaderboard", host="evil.example")[0], 421)

    def test_dictionary_roundtrip(self):
        self.assertIn("ptbr-geral", self.call("GET", "/api/v1/lexicons")[1]["presets"])
        self.assertEqual(self.call("PUT", "/api/v1/lexicons/teste", {"temas": {"a": {"termos": ["fila"]}}})[0], 200)
        self.assertEqual(self.call("PUT", "/api/v1/lexicons/ruim", {"temas": {}})[0], 422)
        st, body, _ = self.call("POST", "/api/v1/lexicons/test", {"lexicon": {"temas": {"a": {"termos": ["fila"]}}}, "texto": "fila enorme", "nota": 1})
        self.assertEqual(body["temas"], {"a": ["fila"]})
        self.assertEqual(self.call("DELETE", "/api/v1/lexicons/teste")[0], 200)

    def test_review_job_and_reads(self):
        self.assertEqual(self.call("POST", "/api/v1/review-jobs", {"foo": 1})[0], 422)
        st, body, _ = self.call("POST", "/api/v1/review-jobs", {"places": [{"link": "https://www.google.com/maps/place/x", "place_id": "x"}]})
        self.assertEqual(st, 201)
        self.assertEqual(self.call("POST", "/api/v1/review-jobs", {"places": [{"link": "u"}], "lexicon": "nao-existe"})[0], 422)
        st, jobs_, _ = self.call("GET", "/api/v1/review-jobs")
        self.assertTrue(any(j["id"] == body["id"] for j in jobs_))
        self.assertNotIn("proxies", jobs_[0]["params"])
        self.assertEqual(self.call("DELETE", f"/api/v1/review-jobs/{body['id']}")[0], 200)
        st, board, _ = self.call("GET", "/api/v1/reviews/leaderboard?min_reviews=1")
        self.assertEqual(board[0]["lugar"], "Bar")
        self.assertEqual(self.call("GET", "/api/v1/reviews/search?q=demor")[1][0]["review_id"], "a")
        self.assertEqual(self.call("GET", "/api/v1/reviews/search")[0], 422)
        self.assertEqual(self.call("GET", "/api/v1/reviews/place?id=P1")[1][0]["temas"], ["demora"])
        st, csv_, r = self.call("GET", "/api/v1/reviews/export")
        self.assertIn("text/csv", r.getheader("Content-Type"))
        self.assertEqual(self.call("POST", "/api/v1/reviews/reclassify", {"lexicon": "ptbr-geral"})[1], {"reviews": 1})


if __name__ == "__main__":
    unittest.main(verbosity=1)
