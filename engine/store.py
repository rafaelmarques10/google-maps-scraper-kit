"""The reviews database: one sqlite3 file next to the jobs, standard library only.

Tables: review_jobs (the queue), places, reviews (anonymised, one row per review), lexicons (named
keyword dictionaries the web UI edits) and profiles (saved search settings). The reviews table keeps what
a prospect needs to check a claim (date, rating, text without personal data, the link to the review on
Google) and a keyed hash of the author instead of a name.
"""
import csv, io, json, os, re, sqlite3, threading, uuid
from datetime import datetime, timezone

import complaints, indicators, privacy

SCHEMA = """
CREATE TABLE IF NOT EXISTS review_jobs (
  id TEXT PRIMARY KEY, name TEXT, status TEXT, created TEXT, started TEXT, finished TEXT,
  params TEXT, progress TEXT, error TEXT);
CREATE TABLE IF NOT EXISTS places (
  place_id TEXT PRIMARY KEY, title TEXT, link TEXT, category TEXT, address TEXT, phone TEXT,
  website TEXT, rating REAL, review_count INTEGER, source_job TEXT, collected_at TEXT);
CREATE TABLE IF NOT EXISTS reviews (
  place_id TEXT NOT NULL, review_id TEXT NOT NULL, job_id TEXT, review_url TEXT, url_exact INTEGER DEFAULT 0,
  data TEXT, data_precisao TEXT, nota INTEGER, texto TEXT, texto_norm TEXT, autor_id TEXT,
  resposta_dono TEXT, tem_resposta INTEGER DEFAULT 0, temas TEXT, termos TEXT, coletado_em TEXT,
  PRIMARY KEY (place_id, review_id));
CREATE INDEX IF NOT EXISTS reviews_data ON reviews (data);
CREATE TABLE IF NOT EXISTS lexicons (name TEXT PRIMARY KEY, body TEXT, updated TEXT);
CREATE TABLE IF NOT EXISTS profiles (name TEXT PRIMARY KEY, body TEXT, updated TEXT);
"""
CSV_FIELDS = ["lugar", "categoria", "endereco", "telefone", "site", "nota_lugar", "review_url", "url_exata",
              "data", "data_precisao", "nota", "texto", "autor_id", "resposta_dono", "temas", "termos"]
JOB_STATUSES = ("queued", "running", "ok", "failed", "stopped")


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ReviewStore:
    def __init__(self, data_dir):
        os.makedirs(data_dir, exist_ok=True)
        self.path = os.path.join(data_dir, "reviews.db")
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self.salt = privacy.load_salt(data_dir)

    def close(self):
        with self.lock:
            self.db.close()

    def q(self, sql, args=()):
        with self.lock:
            return self.db.execute(sql, args)

    # ── queue ──
    def create_job(self, name, params):
        jid = str(uuid.uuid4())
        self.q("INSERT INTO review_jobs (id, name, status, created, params, progress) VALUES (?,?,?,?,?,?)",
               (jid, name, "queued", utc_now(), json.dumps(params, ensure_ascii=False), "{}"))
        return jid

    def claim(self):
        with self.lock:
            row = self.db.execute("SELECT * FROM review_jobs WHERE status='queued' ORDER BY created LIMIT 1").fetchone()
            if not row:
                return None
            self.db.execute("UPDATE review_jobs SET status='running', started=? WHERE id=?", (utc_now(), row["id"]))
            return self._job(row)

    def recover(self):
        """Jobs that were running when the engine stopped go back to the queue (reviews already saved are kept)."""
        return self.q("UPDATE review_jobs SET status='queued' WHERE status='running'").rowcount

    def requeue(self, jid):
        self.q("UPDATE review_jobs SET status='queued' WHERE id=? AND status='running'", (jid,))

    def set_progress(self, jid, progress):
        self.q("UPDATE review_jobs SET progress=? WHERE id=?", (json.dumps(progress), jid))

    def finish(self, jid, status, error=""):
        self.q("UPDATE review_jobs SET status=?, finished=?, error=? WHERE id=? AND status IN ('running','queued')",
               (status, utc_now(), error[:500], jid))

    def stop_job(self, jid):
        return self.q("UPDATE review_jobs SET status='stopped', finished=? WHERE id=? AND status IN ('queued','running')",
                      (utc_now(), jid)).rowcount

    def job_status(self, jid):
        r = self.q("SELECT status FROM review_jobs WHERE id=?", (jid,)).fetchone()
        return r["status"] if r else None

    def get_job(self, jid):
        r = self.q("SELECT * FROM review_jobs WHERE id=?", (jid,)).fetchone()
        return self._job(r) if r else None

    def list_jobs(self):
        return [self._job(r) for r in self.q("SELECT * FROM review_jobs ORDER BY created DESC LIMIT 200")]

    def delete_job(self, jid):
        """Deletes the job and the reviews it collected (a place keeps its other jobs' reviews)."""
        with self.lock:
            self.db.execute("DELETE FROM reviews WHERE job_id=?", (jid,))
            return self.db.execute("DELETE FROM review_jobs WHERE id=?", (jid,)).rowcount

    @staticmethod
    def _job(r):
        d = dict(r)
        d["params"] = json.loads(d.get("params") or "{}")
        d["progress"] = json.loads(d.get("progress") or "{}")
        return d

    # ── places and reviews ──
    def upsert_place(self, p, job_id=""):
        pid = p.get("place_id") or p.get("data_id") or p.get("link")
        if not pid:
            return ""
        def num(v, cast):
            try:
                return cast(v)
            except (TypeError, ValueError):
                return None
        self.q("""INSERT INTO places (place_id,title,link,category,address,phone,website,rating,review_count,source_job,collected_at)
                  VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(place_id) DO UPDATE SET title=excluded.title, link=excluded.link,
                  category=excluded.category, address=excluded.address, phone=excluded.phone, website=excluded.website,
                  rating=excluded.rating, review_count=excluded.review_count, source_job=excluded.source_job""",
               (pid, p.get("title", ""), p.get("link", ""), p.get("category", ""), p.get("address", ""), p.get("phone", ""),
                p.get("website", ""), num(p.get("review_rating"), float), num(p.get("review_count"), int), job_id, utc_now()))
        return pid

    def add_reviews(self, place_id, job_id, reviews, matcher):
        """Stores parsed reviews (reviews.parse_batch output), anonymised and classified. How many were new."""
        new = 0
        now = utc_now()
        with self.lock:
            self.db.execute("BEGIN")
            try:
                for r in reviews:
                    text = privacy.scrub(r.get("texto", ""))
                    reply = privacy.scrub(r.get("resposta_dono", ""))
                    found = matcher.match(text, r.get("nota")) if matcher else {}
                    cur = self.db.execute(
                        """INSERT OR IGNORE INTO reviews (place_id,review_id,job_id,review_url,url_exact,data,data_precisao,nota,
                           texto,texto_norm,autor_id,resposta_dono,tem_resposta,temas,termos,coletado_em)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (place_id, r["review_id"], job_id, r.get("review_url", ""), 1 if r.get("url_exata") else 0,
                         r.get("data", ""), r.get("data_precisao", ""), r.get("nota"), text, privacy.fold(text),
                         privacy.author_id(self.salt, r.get("autor_chave")), reply, 1 if (reply or r.get("tem_resposta")) else 0,
                         json.dumps(sorted(found)), json.dumps(found, ensure_ascii=False), now))
                    new += cur.rowcount
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise
        return new

    def reclassify(self, matcher, place_ids=None):
        """Applies a (new) lexicon to the reviews already collected: no new request to Google."""
        n = 0
        with self.lock:
            sql = "SELECT place_id, review_id, texto, nota FROM reviews"
            args = ()
            if place_ids:
                sql += " WHERE place_id IN (%s)" % ",".join("?" * len(place_ids))
                args = tuple(place_ids)
            rows = self.db.execute(sql, args).fetchall()
            self.db.execute("BEGIN")
            try:
                for r in rows:
                    found = matcher.match(r["texto"], r["nota"])
                    self.db.execute("UPDATE reviews SET temas=?, termos=? WHERE place_id=? AND review_id=?",
                                    (json.dumps(sorted(found)), json.dumps(found, ensure_ascii=False), r["place_id"], r["review_id"]))
                    n += 1
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise
        return n

    def place_rows(self, place_id):
        out = []
        for r in self.q("SELECT data, nota, temas, autor_id, tem_resposta FROM reviews WHERE place_id=?", (place_id,)):
            out.append({"data": r["data"], "nota": r["nota"], "temas": json.loads(r["temas"] or "[]"),
                        "autor_id": r["autor_id"], "tem_resposta": bool(r["tem_resposta"])})
        return out

    def leaderboard(self, lexicon=None, since_days=365, min_reviews=1, theme=None):
        """Places with their indicators, the worst first (by score_dor). `theme` keeps places with that complaint."""
        weights = {k: t["peso"] for k, t in (lexicon or {}).get("temas", {}).items()}
        out = []
        for p in self.q("SELECT * FROM places").fetchall():
            ind = indicators.compute(self.place_rows(p["place_id"]), weights, since_days)
            if ind["total"] < min_reviews or (theme and theme not in ind["temas"]):
                continue
            out.append({"place_id": p["place_id"], "lugar": p["title"], "link": p["link"], "categoria": p["category"],
                        "endereco": p["address"], "telefone": p["phone"], "site": p["website"],
                        "nota_lugar": p["rating"], "indicadores": ind})
        out.sort(key=lambda x: x["indicadores"]["score_dor"], reverse=True)
        return out

    def search(self, terms, mode="any", place_id=None, since=None, max_rating=None, theme=None, limit=200):
        """Reviews whose text holds the keywords (folded, so "demora" finds "Demorá"). mode: any | all.
        `terms` are plain words or phrases; "*" at the end of a word is allowed."""
        clauses, args = [], []
        words = [t for t in (privacy.fold(x).strip() for x in terms) if t]
        if not words:
            return []
        parts = []
        for w in words:
            parts.append("texto_norm LIKE ? ESCAPE '\\'")
            esc = w.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_").rstrip("*")
            args.append(f"%{esc}%")
        clauses.append("(" + (" AND " if mode == "all" else " OR ").join(parts) + ")")
        if place_id:
            clauses.append("r.place_id=?"); args.append(place_id)
        if since:
            clauses.append("data>=?"); args.append(since)
        if max_rating:
            clauses.append("nota<=?"); args.append(int(max_rating))
        if theme:
            clauses.append("temas LIKE ?"); args.append(f'%"{theme}"%')
        sql = ("SELECT r.*, p.title AS lugar FROM reviews r LEFT JOIN places p USING(place_id) WHERE "
               + " AND ".join(clauses) + " ORDER BY data DESC LIMIT ?")
        args.append(max(1, min(int(limit), 1000)))
        return [self._review(r) for r in self.q(sql, tuple(args))]

    def reviews_of(self, place_id, theme=None, limit=500):
        sql, args = "SELECT r.*, p.title AS lugar FROM reviews r LEFT JOIN places p USING(place_id) WHERE r.place_id=?", [place_id]
        if theme:
            sql += " AND temas LIKE ?"; args.append(f'%"{theme}"%')
        sql += " ORDER BY data DESC LIMIT ?"; args.append(max(1, min(int(limit), 2000)))
        return [self._review(r) for r in self.q(sql, tuple(args))]

    @staticmethod
    def _review(r):
        return {"place_id": r["place_id"], "lugar": r["lugar"], "review_id": r["review_id"], "review_url": r["review_url"],
                "url_exata": bool(r["url_exact"]), "data": r["data"], "data_precisao": r["data_precisao"], "nota": r["nota"],
                "texto": r["texto"], "autor_id": r["autor_id"], "resposta_dono": r["resposta_dono"],
                "temas": json.loads(r["temas"] or "[]"), "termos": json.loads(r["termos"] or "{}")}

    def export_csv(self, place_ids=None):
        """CSV of the reviews with their place; cells that start like a formula get a quote (spreadsheets run them)."""
        sql = ("SELECT r.*, p.title, p.category, p.address, p.phone, p.website, p.rating FROM reviews r "
               "LEFT JOIN places p USING(place_id)")
        args = ()
        if place_ids:
            sql += " WHERE r.place_id IN (%s)" % ",".join("?" * len(place_ids))
            args = tuple(place_ids)
        sql += " ORDER BY p.title, r.data DESC"
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(CSV_FIELDS)
        for r in self.q(sql, args):
            row = [r["title"], r["category"], r["address"], r["phone"], r["website"], r["rating"], r["review_url"],
                   "sim" if r["url_exact"] else "nao", r["data"], r["data_precisao"], r["nota"], r["texto"], r["autor_id"],
                   r["resposta_dono"], ",".join(json.loads(r["temas"] or "[]")),
                   "; ".join(f"{k}: {'/'.join(v)}" for k, v in json.loads(r["termos"] or "{}").items())]
            w.writerow([("'" + c) if isinstance(c, str) and c[:1] in "=+-@\t\r" and c != "" else c for c in row])
        return buf.getvalue().encode("utf-8-sig")

    # ── named lexicons and saved profiles ──
    def save_named(self, table, name, body):
        if table not in ("lexicons", "profiles"):
            raise ValueError("table")
        name = re.sub(r"[^A-Za-z0-9_. -]+", "", name or "").strip()[:60]
        if not name:
            raise ValueError("nome inválido: use letras, números, espaço, ponto, traço")
        self.q(f"INSERT INTO {table} (name, body, updated) VALUES (?,?,?) ON CONFLICT(name) DO UPDATE SET body=excluded.body, updated=excluded.updated",
               (name, json.dumps(body, ensure_ascii=False), utc_now()))
        return name

    def get_named(self, table, name):
        r = self.q(f"SELECT body FROM {table} WHERE name=?", (name,)).fetchone()
        return json.loads(r["body"]) if r else None

    def list_named(self, table):
        return [r["name"] for r in self.q(f"SELECT name FROM {table} ORDER BY name")]

    def delete_named(self, table, name):
        return self.q(f"DELETE FROM {table} WHERE name=?", (name,)).rowcount

    def lexicon(self, name):
        """A validated lexicon by name: the user's own first, then the presets that ship with the kit."""
        body = self.get_named("lexicons", name)
        if body is None and name in complaints.list_presets():
            with open(complaints.preset_path(name), encoding="utf-8") as f:
                body = json.load(f)
        return complaints.validate(body) if body is not None else None
