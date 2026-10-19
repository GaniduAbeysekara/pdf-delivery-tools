"""The CBJ crawler in a real browser, against a local replica of the Superfinanciera viewer:
an outer page with a cookie overlay, an iframe running a single-page app (accordion sidebar, API calls with delays, a request that
never ends, attachments that download on click, DESCARGAR buttons that must never be touched) and a title listed twice."""
import http.server
import json
import re
import threading
import time
from urllib.parse import parse_qs, urlparse

import pytest

from url_analyzer.recipes import cbj
from url_analyzer.recipes.cbj import CbjCrawler, Timing

pytestmark = pytest.mark.skipif(not __import__("importlib").util.find_spec("playwright"), reason="Playwright is not installed")

# the site's own numbers: (parte, titulo) the API uses for each sidebar Titulo - the second "Titulo III" row is really Titulo 6 of Parte 2
PARTES = [
    {"name": "Parte I. Alfa", "p": 1, "titulos": [
        {"name": "Título I. Uno", "p": 1, "t": 1, "caps": ["Capítulo I. A", "Capítulo II. B", "Capítulo III. C"]},
        {"name": "Título II. Dos", "p": 1, "t": 2, "caps": ["Capítulo I. D"]}]},
    {"name": "Parte II. Beta", "p": 2, "titulos": [
        {"name": "Título I. Tres", "p": 2, "t": 1, "caps": ["Capítulo I. E"]},
        {"name": "Título III. Repetido", "p": 2, "t": 3, "caps": ["Capítulo I. F"]},
        {"name": "Título III. Repetido", "p": 2, "t": 6, "caps": ["Capítulo I. G"]},
        {"name": "Título I. Tres", "p": 2, "t": 1, "caps": ["Capítulo I. E"]}]},                       # an identical row, listed again
]
XLS, DOCX = "application/vnd.ms-excel", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
ANEXOS = {  # (parte, titulo, capitulo, element) -> records
    (1, 1, 3, 3): [{"id": 328, "contentType": XLS, "nombre": "Anexo 1 Formato.xls", "elemento": 3, "numeral": 0, "subnumeral": "0"},
                   {"id": 329, "contentType": DOCX, "nombre": "Anexo 1 Instructivo.docx", "elemento": 3, "numeral": 0, "subnumeral": "0"}],
    (1, 1, 3, 4): [{"id": 330, "contentType": "application/pdf", "nombre": "Formato numeral.pdf", "elemento": 4, "numeral": 2, "subnumeral": "0"}],
    (1, 2, 1, 3): [{"id": 328, "contentType": XLS, "nombre": "Anexo 1 Formato.xls", "elemento": 3, "numeral": 0, "subnumeral": "0"}],   # the same file again
    (2, 6, 1, 3): [{"id": 777, "contentType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "nombre": "Fondos.xlsx", "elemento": 3, "numeral": 0, "subnumeral": "0"}],
}
OUTER = """<html><body><a href="/files/general.pdf">Documento general</a>
<iframe src="/visualizadorCBJ/inicio" style="width:100%;height:700px"></iframe>
<iframe src="/docsframe"></iframe>
<div style="position:fixed;left:0;right:0;bottom:0;height:70%;background:#fff;z-index:9999">Cookies <button>Aceptar</button></div></body></html>"""
DOCSFRAME = '<html><body><a href="manual.pdf">Manual</a> <a href="/page.html">not a document</a></body></html>'
VIEWER = r"""<html><body><div id="app"></div><script>
const app = document.getElementById('app');
function api(path, cb) { const x = new XMLHttpRequest(); x.open('GET', '/sfcservices/api/' + path); x.onload = () => cb(JSON.parse(x.responseText)); x.send(); }
fetch('/longpoll').catch(() => {});                                  // a connection that never ends, like Firestore's
const S = { partes: [], parte: null, titulos: [], titulo: null, caps: [], cap: null, content: '' };
function start() {
  app.innerHTML = [1, 2].map(n => '<div class="card">Parte ' + n + ' <button class="btn-descargar" onclick="fetch(\'/trap\')">DESCARGAR</button><button class="flecha" id="btn-ingresar"></button></div>').join('');
  app.onclick = e => { if (e.target.classList.contains('flecha')) enter(); };
}
function enter() {
  app.onclick = null;
  api('listarPartes', partes => { S.partes = partes; openParte(0, true); });
}
function openParte(i, auto) {
  S.parte = i; S.titulo = null; S.caps = []; S.cap = null;
  api('consultarTitulosXParte/' + i, titulos => { S.titulos = titulos; render(); if (auto) openTitulo(0, true); });
}
const CACHE = {};
function openTitulo(i, auto) {
  S.titulo = i; S.cap = null;
  const t = S.titulos[i];
  api('consultarTituloFecha/' + t.t + '/' + t.p + '/false', () => {});
  const show = caps => { S.caps = caps; render(); if (auto) selectCap(0); };
  const k = t.p + '/' + t.t + '/' + i;
  if (CACHE[k]) show(CACHE[k]); else api('consultarCapituloXTitulo/' + t.t + '/' + t.p + '?row=' + i, caps => { CACHE[k] = caps; show(caps); });
}
function selectCap(i) {
  S.cap = i; const t = S.titulos[S.titulo]; render();
  api('consultarCapituloFecha/' + (i + 1) + '/' + t.t + '/' + t.p + '/false', () => {
    api('consultarAnexosElemento?element=3&parte=' + t.p + '&titulo=' + t.t + '&capitulo=' + (i + 1), list3 => {
      api('consultarAnexosElemento?element=4&parte=' + t.p + '&titulo=' + t.t + '&capitulo=' + (i + 1) + '&numeral=2', list4 => {
        S.content = list3.concat(list4).map(a => '<div id="anexos" style="cursor:pointer" onclick="dl(' + a.id + ')"><img alt="' + a.nombre + ' icon" src="data:image/gif;base64,R0lGODlhAQABAAAAACw="><span class="anexo"> ' + a.nombre + ' </span></div>').join('');
        render();
      });
    });
  });
}
function dl(id) { const x = new XMLHttpRequest(); x.open('GET', '/sfcservices/api/descargarAnexo/' + id); x.responseType = 'blob';
  x.onload = () => { const a = document.createElement('a'); a.href = URL.createObjectURL(x.response); a.download = 'f' + id; a.click(); }; x.send(); }
function render() {
  let h = '<div id="fixedColumn"><table><tbody>';
  S.partes.forEach((p, pi) => {
    const open = S.parte === pi;
    h += '<tr class="expanded-row"><td>' + p.name + '</td><td><button id="flechaParte" class="icon-button ' + (open ? 'rotate-180' : '') + '" data-p="' + pi + '"></button></td></tr>';
    if (open) S.titulos.forEach((t, ti) => {
      const to = S.titulo === ti;
      h += '<tr id="titulos"><td>' + t.name + '</td><td><button id="flecha" class="icon-button ' + (to ? 'rotate-180' : '') + '" data-t="' + ti + '"></button></td></tr>';
      if (to) S.caps.forEach((c, ci) => { h += '<tr id="capitulos" class="' + (S.cap === ci ? 'selected' : '') + '" data-c="' + ci + '"><td>' + c + '</td></tr>'; });
    });
  });
  h += '</tbody></table></div><div id="content"><button onclick="fetch(\'/trap\')">DESCARGAR</button>' + S.content + '</div>';
  app.innerHTML = h;
  app.onclick = e => {
    const b = e.target.closest('button'), tr = e.target.closest('tr');
    if (b && b.id === 'flechaParte') openParte(+b.dataset.p);
    else if (b && b.id === 'flecha') { if (S.titulo === +b.dataset.t) { S.titulo = null; S.caps = []; S.cap = null; render(); } else openTitulo(+b.dataset.t); }
    else if (tr && tr.id === 'capitulos') selectCap(+tr.dataset.c);
  };
}
start();
</script></body></html>"""


@pytest.fixture(scope="module")
def site():
    hits = []

    class H(http.server.BaseHTTPRequestHandler):
        def _send(self, body, ctype="text/html; charset=utf-8", code=200, headers=None):
            data = body if isinstance(body, bytes) else body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            u = urlparse(self.path)
            hits.append(self.path)
            pages = {"/outer": OUTER, "/docsframe": DOCSFRAME, "/visualizadorCBJ/inicio": VIEWER, "/trap": "trap", "/files/general.pdf": "%PDF"}
            if u.path == "/longpoll":
                time.sleep(120)
                return self._send("late", "text/plain")
            if u.path in pages:
                return self._send(pages[u.path])
            m = re.match(r"/sfcservices/api/(.+)", u.path)
            if not m:
                return self._send("not found", code=404)
            time.sleep(0.25)                                              # the real API is not instant: the crawler must wait for it
            name, q = m.group(1), parse_qs(u.query)
            if name == "listarPartes":
                return self._json([{"name": p["name"]} for p in PARTES])
            if name.startswith("consultarTitulosXParte/"):
                p = PARTES[int(name.split("/")[1])]
                return self._json([{"name": t["name"], "p": t["p"], "t": t["t"]} for t in p["titulos"]])
            if name.startswith("consultarCapituloXTitulo/"):
                _, tt, pp = name.split("/")                                 # titulo/parte
                for p in PARTES:
                    for t in p["titulos"]:
                        if (t["p"], t["t"]) == (int(pp), int(tt)):
                            return self._json(t["caps"])
            if name.startswith("consultarCapituloFecha/") or name.startswith("consultarTituloFecha/"):
                return self._json({"ok": True})
            if name == "consultarAnexosElemento":
                key = (int(q["parte"][0]), int(q["titulo"][0]), int(q["capitulo"][0]), int(q["element"][0]))
                return self._json(ANEXOS.get(key, []))
            if name.startswith("descargarAnexo/"):
                from tests.helpers import ENGLISH, text_pdf
                body = text_pdf(ENGLISH, 3) if name.endswith("/330") else b"FILEBYTES"            # 330 is a real 3-page PDF
                return self._send(body, "application/pdf" if name.endswith("/330") else "application/octet-stream",
                                  headers={"Last-Modified": "Wed, 01 Oct 2025 08:30:00 GMT", "Access-Control-Allow-Origin": "*"})
            self._send("unknown api " + name, code=404)

        def _json(self, obj):
            self._send(json.dumps(obj), "application/json")

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield type("Site", (), {"url": f"http://127.0.0.1:{srv.server_address[1]}/outer", "hits": hits})
    srv.shutdown()


FAST = Timing(page=30, viewer=25, rows=10, chapter=20, click_capture=5, retries=1)


def run(site, **kw):
    site.hits.clear()
    try:
        return cbj.crawl(site.url, timing=kw.pop("timing", FAST), **kw)
    except RuntimeError as e:                                            # no browser installed
        pytest.skip(str(e))


@pytest.fixture(scope="module")
def full(site):
    return run(site)


def rows_of(res, capitulo):
    return [a for a in res.attachments if a.capitulo.startswith(capitulo)]


def test_every_capitulo_is_visited_across_partes_and_titulos(full):
    assert [(c.parte, c.titulo, c.capitulo, c.title_row, c.status) for c in full.chapters] == [
        ("Parte I. Alfa", "Título I. Uno", "Capítulo I. A", 1, "ok"), ("Parte I. Alfa", "Título I. Uno", "Capítulo II. B", 1, "ok"),
        ("Parte I. Alfa", "Título I. Uno", "Capítulo III. C", 1, "ok"), ("Parte I. Alfa", "Título II. Dos", "Capítulo I. D", 1, "ok"),
        ("Parte II. Beta", "Título I. Tres", "Capítulo I. E", 1, "ok"), ("Parte II. Beta", "Título III. Repetido", "Capítulo I. F", 1, "ok"),
        ("Parte II. Beta", "Título III. Repetido", "Capítulo I. G", 2, "ok"), ("Parte II. Beta", "Título I. Tres", "Capítulo I. E", 2, "duplicate")]
    assert [c.app_ids for c in full.chapters] == ["1/1/1", "1/1/2", "1/1/3", "1/2/1", "2/1/1", "2/3/1", "2/6/1", "2/1/1"]


def test_multiple_attachments_of_a_capitulo_with_their_real_urls_and_types(full, site):
    c = [a for a in rows_of(full, "Capítulo III. C")]
    names = {a.name: a for a in c if a.found_in != "dom" and a.found_in != "iframe-dom"}
    assert set(names) == {"Anexo 1 Formato.xls", "Anexo 1 Instructivo.docx", "Formato numeral.pdf"}
    base = site.url.rsplit("/", 1)[0] + "/sfcservices/api/descargarAnexo/"
    assert names["Anexo 1 Formato.xls"].url == base + "328" and names["Anexo 1 Instructivo.docx"].url == base + "329" and names["Formato numeral.pdf"].url == base + "330"
    assert [names[n].ext for n in names] == ["xls", "docx", "pdf"] or {n: a.ext for n, a in names.items()} == {"Anexo 1 Formato.xls": "xls", "Anexo 1 Instructivo.docx": "docx", "Formato numeral.pdf": "pdf"}
    assert names["Anexo 1 Formato.xls"].content_type == XLS


def test_the_api_the_dom_and_a_click_agree(full):
    for a in rows_of(full, "Capítulo III. C"):
        if a.found_in in ("dom", "iframe-dom"):
            continue
        assert a.verified_by_click and "api" in a.found_in and "click" in a.found_in, a        # one row, three confirmations


def test_a_numeral_level_attachment_is_reported_with_its_level(full):
    n = next(a for a in rows_of(full, "Capítulo III. C") if a.name == "Formato numeral.pdf")
    assert (n.level, n.numeral) == ("Numeral", "2")


def test_main_page_and_iframe_documents_are_found_with_absolute_urls(full, site):
    root = site.url.rsplit("/", 1)[0]
    c = rows_of(full, "Capítulo III. C")
    dom = {a.url: a for a in c if a.found_in in ("dom", "iframe-dom")}
    assert set(dom) == {root + "/files/general.pdf", root + "/manual.pdf"}                       # relative links resolved; the .html link is not a document
    assert dom[root + "/files/general.pdf"].found_in == "dom" and dom[root + "/manual.pdf"].found_in == "iframe-dom"


def test_the_same_file_in_two_capitulos_keeps_both_hierarchies(full):
    f328 = [a for a in full.attachments if a.attachment_id == "328"]
    assert [(a.titulo, a.capitulo) for a in f328] == [("Título I. Uno", "Capítulo III. C"), ("Título II. Dos", "Capítulo I. D")]
    u = [x for x in cbj.unique_urls(full.attachments) if x["name"] == "Anexo 1 Formato.xls"]
    assert len(u) == 1 and u[0]["occurrences"] == 2


def test_a_title_listed_twice_is_read_twice_and_told_apart(full):
    g = rows_of(full, "Capítulo I. G")
    assert [(a.name, a.title_row, a.app_ids) for a in g if a.found_in == "api, click"] == [("Fondos.xlsx", 2, "2/6/1")]
    assert "[title row 2]" in g[0].hierarchy
    assert not any(a.name == "Fondos.xlsx" for a in rows_of(full, "Capítulo I. F"))              # the first row's Capitulo does not get the second's file


def test_a_row_the_site_lists_twice_is_recorded_but_read_and_listed_once(full):
    dup = full.chapters[-1]
    assert (dup.status, dup.title_row, dup.attachments) == ("duplicate", 2, 0) and "same site ids as" in dup.check and "2/1/1" not in dup.error
    assert not any(a.title_row == 2 and a.app_ids == "2/1/1" for a in full.attachments)
    assert full.errors == []                                                                       # a repeated row is not a failure


def test_descargar_buttons_are_never_clicked_and_nothing_is_downloaded(full, site):
    run_hits = site.hits
    assert "/trap" not in run_hits
    assert not any("descargarAnexo" in h for h in run_hits)                                       # the click's request was recorded and cancelled


def test_a_request_that_never_ends_does_not_block_the_crawl(full):
    assert full.seconds < 120 and all(c.status in ("ok", "duplicate") for c in full.chapters)                    # /longpoll hangs for 120 s; the tracker ignores it


def test_a_failing_capitulo_is_retried_logged_and_skipped(site, monkeypatch, caplog):
    real = CbjCrawler.read_chapter
    attempts = {}

    def flaky(self, parte, titulo, capitulo, occ=0):
        attempts[capitulo] = attempts.get(capitulo, 0) + 1
        if capitulo.startswith("Capítulo II. B"):
            raise TimeoutError("page did not settle")
        if capitulo.startswith("Capítulo I. D") and attempts[capitulo] == 1:
            raise RuntimeError("first attempt broke")
        return real(self, parte, titulo, capitulo, occ)
    monkeypatch.setattr(CbjCrawler, "read_chapter", flaky)
    with caplog.at_level("ERROR", logger="cbj"):
        res = run(site)
    by = {c.capitulo[:14]: c for c in res.chapters}
    assert by["Capítulo II. B"].status == "failed" and by["Capítulo II. B"].attempts == 2 and "TimeoutError: page did not settle" in by["Capítulo II. B"].error
    assert by["Capítulo I. D"].status == "ok" and by["Capítulo I. D"].attempts == 2             # recovered on the retry
    assert [c.status for c in res.chapters].count("ok") == 6                                        # everything after the failure was still read
    assert any(a.name == "Fondos.xlsx" for a in res.attachments)
    assert any("Capítulo II. B" in r.getMessage() and "failed" in r.getMessage() for r in caplog.records)
    assert [c.capitulo[:14] for c in res.errors] == ["Capítulo II. B"]


def test_api_only_mode_skips_the_clicks(site):
    res = run(site, click_verify=False, only_parte=1, max_chapters=3)
    a = next(x for x in res.attachments if x.name == "Anexo 1 Formato.xls")
    assert a.found_in.startswith("api") and not a.verified_by_click and len(res.chapters) == 3


def test_filters_for_one_parte_and_a_maximum(site):
    res = run(site, only_parte=2)
    assert [c.capitulo for c in res.chapters] == ["Capítulo I. E", "Capítulo I. F", "Capítulo I. G", "Capítulo I. E"]
    assert len(run(site, max_chapters=2).chapters) == 2


def test_command_line_writes_the_workbook_and_the_json(site, tmp_path):
    out = tmp_path / "cbj.xlsx"
    code = cbj.main(["--url", site.url, "--out", str(out), "--log", str(tmp_path / "cbj.log"), "--parte", "1", "--max-capitulos", "3", "--retries", "0"])
    assert code == 0 and out.exists() and out.with_suffix(".json").exists() and (tmp_path / "cbj.log").read_text(encoding="utf-8").count("[3/3]") == 1
