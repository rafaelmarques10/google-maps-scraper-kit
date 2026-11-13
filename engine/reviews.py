"""Collects a place's full review list (not only the 5 its panel shows) and files it, anonymised.

How: open the place, open its reviews tab, sort by newest, scroll the list. Each scroll makes the page ask
Google for the next batch (`/maps/rpc/listugcposts`); this module reads those responses from the network the
same way maps.py reads the result batches, so no page text is scraped. One place at a time, paced, inside
the job's max_time, through the same Google lock as the lead jobs.

The batches are nested lists read by position (like details.py). Google changes positions without notice:
every position lives in LAYOUT below, text and date have fallbacks that do not depend on a position, and
scripts/reviews_probe.py prints where things are in a live response so LAYOUT can be fixed in minutes.

Reviewer names and photos are never stored: privacy.py turns the author into a keyed hash.
Standard library only.
"""
import csv, io, json, os, random, re, threading, time, urllib.parse
from datetime import datetime, timezone

import browser, details, gate, jobs, maps, privacy
import complaints
from cdp import CDPError, Page

JOB_ID = re.compile(r"[0-9a-f-]{36}")
LIST_URL = re.compile(r"^https://www\.google\.[a-z.]+/maps/rpc/listugcposts")
dig, as_list = details.dig, details.as_list

# Positions inside one /maps/rpc/listugcposts body (after the )]}' line). Verified only against the layout
# of the panel's own review entries (details.REVIEWS), which the list shares: re-check with reviews_probe.py.
LAYOUT = {
    "list": (2,),                 # the reviews
    "next": (1,),                 # token of the next page
    "id": (0, 0),                 # the review's id
    "rating": (0, 2, 0, 0),
    "text": (0, 2, 15, 0, 0),
    "when": (0, 1, 6),            # "2 months ago"
    "author": (0, 1, 4, 5, 0),    # only ever hashed
    "owner_reply": [(3, 14, 0, 0), (3, 14, 1), (3, 14, 0)],  # first one that holds text
}
REVIEW_URL = re.compile(r"https://www\.google\.[a-z.]+/maps/reviews/data=[^\"\\\s]+")
CONTRIB = re.compile(r"/maps/contrib/(\d+)")
TAB_LABELS = ["reviews", "avaliações", "avaliacoes", "reseñas", "resenas", "opiniones", "bewertungen", "avis", "recensioni"]
SORT_LABELS = ["sort", "classificar", "ordenar", "ordenar por", "trier", "sortieren"]
NEWEST_LABELS = ["newest", "mais recentes", "mais recente", "más recientes", "neueste", "plus récents", "più recenti"]

JS_OPEN_TAB = """((labels) => {
  const items = [...document.querySelectorAll('[role="tab"], button')];
  const t = items.find(e => { const s = ((e.getAttribute('aria-label') || '') + ' ' + (e.textContent || '')).trim().toLowerCase();
    return s.length < 60 && labels.some(l => s.includes(l)); });
  if (!t) return false; t.click(); return true; })(%s)"""
JS_SORT_NEWEST = """((sortL, newL) => {
  const text = e => ((e.getAttribute('aria-label') || '') + ' ' + (e.textContent || '')).trim().toLowerCase();
  const b = [...document.querySelectorAll('button')].find(e => text(e).length < 60 && sortL.some(l => text(e).includes(l)));
  if (!b) return 'no-sort'; b.click(); return 'opened'; })(%s, %s)"""
JS_PICK_NEWEST = """((newL) => {
  const o = [...document.querySelectorAll('[role="menuitemradio"], [role="menuitem"]')].find(e =>
    newL.some(l => (e.textContent || '').trim().toLowerCase().includes(l)));
  if (!o) return false; o.click(); return true; })(%s)"""
JS_SCROLL = """(() => {
  const first = document.querySelector('[data-review-id]'); if (!first) return -1;
  let n = first;
  while (n && n !== document.body) { const s = getComputedStyle(n);
    if ((s.overflowY === 'auto' || s.overflowY === 'scroll') && n.scrollHeight > n.clientHeight) break; n = n.parentElement; }
  if (!n || n === document.body) return -2;
  n.scrollTo(0, n.scrollHeight); return document.querySelectorAll('[data-review-id]').length; })()"""


def js(template, *args):
    return template % tuple(json.dumps(a) for a in args)


# ── Parsing ──
def _walk(node, depth=0):
    if depth > 14:
        return
    if isinstance(node, list):
        for x in node:
            yield from _walk(x, depth + 1)
    elif node is not None:
        yield node


def parse_batch(body, place_link="", now=None):
    """(reviews, next_token) from one listugcposts body. A review is {review_id, review_url, url_exata, data,
    data_precisao, nota, texto, autor_chave, resposta_dono, tem_resposta}. autor_chave is only meant to be
    hashed (store.add_reviews does) and never written anywhere."""
    if body.startswith(")]}'"):
        body = body.split("\n", 1)[-1]
    try:
        data = json.loads(body)
    except ValueError:
        return [], ""
    token = dig(data, LAYOUT["next"])
    out = []
    for e in as_list(data, LAYOUT["list"]):
        try:
            r = parse_review(e, place_link, now)
        except (TypeError, ValueError, IndexError, AttributeError):
            continue
        if r:
            out.append(r)
    return out, token if isinstance(token, str) else ""


def parse_review(e, place_link="", now=None):
    rid = dig(e, LAYOUT["id"])
    rating = dig(e, LAYOUT["rating"])
    if not isinstance(rid, str) or not rid or isinstance(rating, bool) or not isinstance(rating, int) or not 1 <= rating <= 5:
        return None
    dump = json.dumps(e, ensure_ascii=False)
    text = dig(e, LAYOUT["text"])
    text = text.replace(" ", " ").strip() if isinstance(text, str) else ""
    # The date: a timestamp anywhere in the entry first (exact), else the relative text.
    iso, prec = "", ""
    for v in _walk(e):
        if isinstance(v, int) and not isinstance(v, bool) and v > 10**9:
            iso = privacy.from_micros(v)
            if iso:
                prec = "dia"
                break
    if not iso:
        iso, prec = privacy.approx_date(dig(e, LAYOUT["when"]) or "", now)
    url = REVIEW_URL.search(dump)
    contrib = CONTRIB.search(dump)
    author = contrib.group(1) if contrib else dig(e, LAYOUT["author"])
    reply = ""
    for path in LAYOUT["owner_reply"]:
        v = dig(e, path)
        if isinstance(v, str) and v.strip():
            reply = v.strip()
            break
    return {"review_id": rid, "review_url": url.group(0) if url else place_link, "url_exata": bool(url),
            "data": iso, "data_precisao": prec, "nota": rating, "texto": text,
            "autor_chave": author if isinstance(author, str) else "", "resposta_dono": reply,
            "tem_resposta": bool(reply)}


# ── Choosing the places ──
def select_places(rows, p):
    """The lead job's rows worth a review pass: has a link and a place id, enough reviews, a rating at or
    under max_place_rating (a business that rates 4.9 has few complaints to find), the first max_places."""
    out = []
    for r in rows:
        link = r.get("link") or ""
        if not link or not (r.get("place_id") or r.get("data_id")):
            continue
        try:
            n = int(float(r.get("review_count") or 0))
            rating = float(r.get("review_rating") or 0)
        except ValueError:
            continue
        if n < p["min_reviews"] or (rating and rating > p["max_place_rating"]):
            continue
        out.append(r)
    return out[:p["max_places"]]


# ── Collecting ──
def _drain(page, q, pending):
    """Bodies of the listugcposts responses that finished since the last call."""
    bodies = []
    while True:
        try:
            p = q.get_nowait()
        except Exception:  # queue.Empty
            break
        if not isinstance(p, dict):
            raise CDPError(f"connection lost: {page.cdp.closed}")
        if LIST_URL.match(p.get("response", {}).get("url", "")):
            pending[p["requestId"]] = 0
    for rid in list(pending):
        try:
            bodies.append(page.send("Network.getResponseBody", {"requestId": rid})["body"])
            del pending[rid]
        except CDPError:
            if page.cdp.closed:
                raise
            pending[rid] += 1
            if pending[rid] > 20:  # never finished: drop it
                del pending[rid]
    return bodies


def _debug_dump(body, _n=[0]):
    """With REVIEWS_DEBUG_DIR set, keeps the first 3 raw responses of the run so scripts/reviews_probe.py can
    show where each field sits. The files hold real reviews: delete them after use."""
    d = os.environ.get("REVIEWS_DEBUG_DIR")
    if not d or _n[0] >= 3:
        return
    os.makedirs(d, exist_ok=True)
    _n[0] += 1
    with open(os.path.join(d, f"listugcposts-{_n[0]}.json"), "w", encoding="utf-8") as f:
        f.write(body)


def collect(page, link, p, deadline, stop, log=print, now=None):
    """(reviews, reason) for one place. reason: "" when the list ended or the limits were reached, else why it
    stopped early ("blocked", "no-tab", "time", "stopped")."""
    q = page.cdp.subscribe("Network.responseReceived", page.sid)
    pending, seen, out = {}, set(), []
    since = ""
    if p["since_days"]:
        since = datetime.fromtimestamp(time.time() - p["since_days"] * 86400, timezone.utc).date().isoformat()
    try:
        page.navigate(link + f"&hl={p['lang']}" + (f"&gl={p['country']}" if p.get("country") else ""))
        if "/sorry/" in (page.url() or ""):
            return out, "blocked"
        if not page.wait_for(js(JS_OPEN_TAB, TAB_LABELS), timeout=15):
            return out, "no-tab"
        sorted_new = False
        if p["sort_newest"]:
            time.sleep(1.5)
            if page.eval(js(JS_SORT_NEWEST, SORT_LABELS, NEWEST_LABELS)) == "opened":
                time.sleep(0.8)
                sorted_new = bool(page.eval(js(JS_PICK_NEWEST, NEWEST_LABELS)))
                time.sleep(1.5)
        idle = 0
        while len(out) < p["max_reviews"]:
            if stop.is_set():
                return out, "stopped"
            if time.monotonic() >= deadline:
                return out, "time"
            fresh = []
            for body in _drain(page, q, pending):
                _debug_dump(body)
                batch, _ = parse_batch(body, link, now)
                for r in batch:
                    if r["review_id"] not in seen:
                        seen.add(r["review_id"])
                        fresh.append(r)
            if sorted_new and since and fresh and all(r["data"] and r["data"] < since for r in fresh):
                out.extend(r for r in fresh if not r["data"] or r["data"] >= since)
                return out, ""  # newest first: everything after this is older than the window
            out.extend(r for r in fresh if not since or not r["data"] or r["data"] >= since)
            idle = 0 if fresh else idle + 1
            if idle >= 4:
                return out, ""  # the list ended (or Google stopped answering): four scrolls, nothing new
            page.eval(JS_SCROLL)
            time.sleep(p["pace"] * random.uniform(1.0, 1.5))
        return out[:p["max_reviews"]], ""
    finally:
        page.cdp.unsubscribe("Network.responseReceived", q, page.sid)


# ── The job body ──
def validate(body):
    """(name, params) from a create-review-job body, or ValueError with a message for the UI."""
    if not isinstance(body, dict):
        raise ValueError("o corpo deve ser um objeto JSON")
    name = str(body.get("name") or "coleta de reviews")[:200]
    src, places = body.get("source_job"), body.get("places")
    if src is not None and not (isinstance(src, str) and JOB_ID.fullmatch(src)):
        raise ValueError("source_job inválido")
    if places is not None and not (isinstance(places, list) and len(places) <= 500
                                   and all(isinstance(x, dict) and isinstance(x.get("link"), str) for x in places)):
        raise ValueError("places: lista de até 500 lugares, cada um com \"link\"")
    if not src and not places:
        raise ValueError("informe source_job (um job de leads concluído) ou places")
    rating = body.get("max_place_rating", 5)
    if isinstance(rating, bool) or not isinstance(rating, (int, float)) or not 1 <= rating <= 5:
        raise ValueError("max_place_rating: um número de 1 a 5")
    pace = body.get("pace", 1.5)
    if isinstance(pace, bool) or not isinstance(pace, (int, float)) or not 0.8 <= pace <= 10:
        raise ValueError("pace: segundos entre rolagens, de 0,8 a 10")
    lang = body.get("lang") or "pt"
    country = body.get("country") or "br"
    if not (isinstance(lang, str) and jobs.LANG.fullmatch(lang)) or not (isinstance(country, str) and jobs.LANG.fullmatch(country)):
        raise ValueError("lang e country: 2 letras (pt, br)")
    p = {"source_job": src or "", "places": places or [],
         "max_reviews": jobs._int(body, "max_reviews", 100, 1, 2000, "max_reviews"),
         "since_days": jobs._int(body, "since_days", 365, 0, 3650, "since_days"),
         "min_reviews": jobs._int(body, "min_reviews", 5, 0, 100000, "min_reviews"),
         "max_places": jobs._int(body, "max_places", 50, 1, 500, "max_places"),
         "max_time": jobs._int(body, "max_time", 1800, 60, 86400, "max_time"),
         "sort_newest": jobs._bool(body, "sort_newest") if body.get("sort_newest") is not None else True,
         "max_place_rating": float(rating), "pace": float(pace), "lang": lang.lower(), "country": country.lower(),
         "proxies": jobs._proxies(body.get("proxies")),
         "lexicon": str(body.get("lexicon") or "ptbr-geral")[:60]}
    return name, p


class ReviewsWorker(threading.Thread):
    """Runs queued review jobs one at a time, after any lead job in progress (gate.GOOGLE)."""

    def __init__(self, store, leads, log=print, headless=True):
        super().__init__(daemon=True, name="reviews")
        self.store, self.leads, self.log, self.headless = store, leads, log, headless
        self.wake, self.stopped = threading.Event(), threading.Event()
        self._stop_job = (None, threading.Event())
        self._browser = None

    def cancel(self, jid):
        if self._stop_job[0] == jid:
            self._stop_job[1].set()

    def shutdown(self):
        self.stopped.set()
        self._stop_job[1].set()
        self.wake.set()

    def run(self):
        while not self.stopped.is_set():
            if not gate.GOOGLE.acquire(timeout=5):
                continue
            job = None
            try:
                job = self.store.claim()
                if job:
                    self.run_job(job)
            except Exception as e:  # noqa: BLE001 - one bad job must never stop the worker
                self.log(f"reviews job {job['id'] if job else '-'} failed: {type(e).__name__}: {e}")
                if job:
                    self.store.finish(job["id"], "failed", f"{type(e).__name__}: {e}")
            finally:
                gate.GOOGLE.release()
                if self._browser:
                    self._browser.close()
                    self._browser = None
            if not job:
                self.wake.wait(5)
                self.wake.clear()

    def run_job(self, job):
        jid, p = job["id"], job["params"]
        stop = threading.Event()
        self._stop_job = (jid, stop)
        matcher = complaints.Matcher(self.store.lexicon(p["lexicon"]) or self.store.lexicon("ptbr-geral"))
        if p["source_job"]:
            src = self.leads.get(p["source_job"])
            if not src:
                self.store.finish(jid, "failed", "o job de leads de origem não existe mais")
                return
            if src["Status"] in ("pending", "working"):  # its leads are not all in yet: wait, do not read half a file
                self.store.requeue(jid)
                self.stopped.wait(5)
                return
            data = self.leads.read_csv(p["source_job"])
            rows = list(csv.DictReader(io.StringIO(data.decode("utf-8", "replace")))) if data else []
        else:
            rows = p["places"]
        todo = select_places(rows, p)
        deadline = time.monotonic() + p["max_time"]
        prog = {"places_total": len(todo), "places_done": 0, "reviews": 0, "notes": []}
        self.store.set_progress(jid, prog)
        if not todo:
            self.store.finish(jid, "failed", "nenhum lugar passou nos filtros (reviews mínimas, nota máxima do lugar)")
            return
        self.log(f"reviews job {jid} started: {len(todo)} place(s), up to {p['max_reviews']} reviews each")
        self._browser = browser.Browser(lang=jobs.locale(p), headless=self.headless)
        proxy = p["proxies"][0] if p["proxies"] else None
        ctx = self._browser.context(proxy)
        page = Page(self._browser.cdp, context=ctx)
        auth = None
        try:
            _, user, password = browser.split_proxy(proxy) if proxy else ("", "", "")
            if user or password:
                auth = browser.ProxyAuth(page, user, password)
            for k, v in maps.CONSENT_COOKIES.items():
                page.set_cookie(k, v, ".google.com")
            status = "ok"
            for i, r in enumerate(todo):
                if stop.is_set() or self.stopped.is_set() or self.store.job_status(jid) != "running":
                    status = "stopped"
                    break
                try:
                    got, reason = collect(page, r["link"], p, deadline, stop, self.log)
                except CDPError as e:
                    prog["notes"].append(f"{r.get('title', '?')[:40]}: {e}")
                    if self._browser.cdp.closed:
                        break
                    continue
                pid = self.store.upsert_place(r, jid)
                prog["reviews"] += self.store.add_reviews(pid, jid, got, matcher)
                prog["places_done"] = i + 1
                if reason:
                    prog["notes"].append(f"{r.get('title', '?')[:40]}: parou ({reason})")
                self.store.set_progress(jid, prog)
                if reason == "blocked":
                    status = "failed"
                    self.store.finish(jid, status, "o Google mostrou a página de tráfego incomum: pare e tente depois, com proxy")
                    return
                if reason == "time":
                    break
                time.sleep(max(2.0, p["pace"] * 2) * random.uniform(1.0, 1.6))
            if status == "ok" and prog["places_done"] < len(todo) and time.monotonic() >= deadline:
                prog["notes"].append(f"tempo esgotado: {prog['places_done']} de {len(todo)} lugares")
            self.store.set_progress(jid, prog)
            if status == "ok" and not prog["places_done"]:  # nothing was read: say why instead of a hollow "ok"
                status = "failed"
                self.store.finish(jid, status, (prog["notes"] or ["nenhum lugar foi lido"])[0][:300])
            else:
                self.store.finish(jid, status, "")
            self.log(f"reviews job {jid} {status}: {prog['reviews']} new reviews in {prog['places_done']} place(s)")
        finally:
            if auth:
                auth.close()
            if not self._browser.cdp.closed:
                page.close()
                self._browser.dispose(ctx)
