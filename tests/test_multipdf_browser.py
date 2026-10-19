"""The browser step against a real headless browser (Edge / Chrome) and a local page that behaves like the CNMI code:
a lazy-loaded outline, accordions, pagination and 'load more'. Skipped when Playwright or a browser is not available."""
import http.server
import json
import threading
import time
from urllib.parse import parse_qs, urlparse

import pytest

from url_analyzer.multipdf import browser as B
from url_analyzer.multipdf.extract import extract

pytestmark = pytest.mark.skipif(not B.available(), reason="Playwright is not installed")

CHILDREN = {  # id -> children (what the page's script fetches when a title is clicked)
    "1": [{"id": 11, "title": "Division A", "kids": True, "pdf": None}, {"id": 12, "title": "Division B", "kids": True, "pdf": None}],
    "11": [{"id": 111, "title": "§ 111. First", "kids": False, "pdf": "/docs/111.pdf"}, {"id": 112, "title": "§ 112. Second", "kids": False, "pdf": "/docs/112.pdf"}],
    "12": [{"id": 121, "title": "Chapter 1", "kids": True, "pdf": None}],
    "121": [{"id": 1211, "title": "§ 1211. Deep", "kids": False, "pdf": "/docs/1211.pdf"}],
    "2": [{"id": 21, "title": "§ 21. Other", "kids": False, "pdf": "/docs/21.pdf"}, {"id": 22, "title": "§ 22. Other", "kids": False, "pdf": "/docs/22.pdf"}],
}
TREE = """<html><body><div id="tree"><ul class="tlist">
<li class="tnode" data-id="1"><div class="trow"><button class="ttoggle" aria-expanded="false">&#9656;</button><a class="tlabel" href="/c/1">Title 1: Government</a></div><ul class="tchildren" hidden></ul></li>
<li class="tnode" data-id="2"><div class="trow"><button class="ttoggle" aria-expanded="false">&#9656;</button><a class="tlabel" href="/c/2">Title 2: Resources</a></div><ul class="tchildren" hidden></ul></li>
</ul></div>
<script>
function node(n){ return n.kids
  ? '<li class="tnode" data-id="'+n.id+'"><div class="trow"><button class="ttoggle" aria-expanded="false">&#9656;</button><a class="tlabel" href="/c/'+n.id+'">'+n.title+'</a></div><ul class="tchildren" hidden></ul></li>'
  : '<li class="tleaf"><a class="tlabel tseclink" href="/c/'+n.id+'" data-id="'+n.id+'" data-pdf="'+n.pdf+'">'+n.title+'</a></li>'; }
document.getElementById('tree').addEventListener('click', function(e){
  var b = e.target.closest('.ttoggle'); if(!b) return; e.preventDefault();
  var li = b.closest('.tnode'), ul = li.querySelector(':scope > .tchildren'), open = b.getAttribute('aria-expanded') !== 'true';
  b.setAttribute('aria-expanded', open ? 'true' : 'false'); ul.hidden = !open;
  if(open && !li.dataset.loaded){ li.dataset.loaded = '1'; ul.innerHTML = '<li>Loading&hellip;</li>';
    fetch('/children/'+li.dataset.id).then(function(r){return r.json();}).then(function(d){ ul.innerHTML = d.nodes.map(node).join(''); }); }
});
fetch('http://blocked.invalid/track').catch(function(){});
</script></body></html>"""
ACCORDION = """<html><body><div id="acc">
<details><summary>2024</summary><ul><li><a href="/docs/a24.pdf">A 2024</a></li>
  <li><details><summary>Annexes</summary><a href="/docs/annex24.pdf">Annex 2024</a></details></li></ul></details>
<details open><summary>2023</summary><a href="/docs/a23.pdf">A 2023</a></details>
</div></body></html>"""


def paged(n):
    nxt = f'<a class="next" href="/paged?page={n + 1}">Next</a>' if n < 3 else ""
    return f'<html><body><ul><li><a href="/docs/p{n}a.pdf">P{n}A</a></li><li><a href="/docs/p{n}b.pdf">P{n}B</a></li></ul>{nxt}</body></html>'


LOADMORE = """<html><body><ul id="l"><li><a href="/docs/m0.pdf">M0</a></li></ul><button class="load-more">Load more</button>
<script>var n=0; document.querySelector('.load-more').addEventListener('click', function(){ n++;
  document.getElementById('l').insertAdjacentHTML('beforeend','<li><a href="/docs/m'+n+'.pdf">M'+n+'</a></li>'); if(n>=3) this.remove(); });</script></body></html>"""
FRAME_SHELL = '<html><body><iframe id="lawService" name="lawService" src="/frame-inner"></iframe></body></html>'
# the frame builds its list with JavaScript after loading (like the law.go.kr list of attachments)
FRAME_INNER = """<html><body><ul id="files"></ul><script>
fetch('/files-json').then(r => r.json()).then(d => { document.getElementById('files').innerHTML = d.map(n =>
  '<li><a class="blu" href="#AJAX">[Annex '+n+'] Title '+n+'</a><a href="dl.do?id='+n+'1"><img alt="HWP file" src="h.png"></a>'
  + '<a href="dl.do?id='+n+'2"><img alt="PDF file download" src="p.png"></a></li>').join(''); });
</script></body></html>"""
# a lazy outline inside a frame: the scope's region (and the clicking) is in the frame, not in the top page
# the list is requested only after a few seconds of complete silence (no request, no change) - what law.go.kr does
FRAME_SLOW = """<html><body><ul id="files"></ul><script>
setTimeout(function(){ fetch('/files-json').then(r => r.json()).then(d => { document.getElementById('files').innerHTML = d.map(n =>
  '<li><a class="blu" href="#x">[Annex '+n+'] Title '+n+'</a><a href="dl.do?id='+n+'1"><img alt="HWP file" src="h.png"></a>'
  + '<a href="dl.do?id='+n+'2"><img alt="PDF file download" src="p.png"></a></li>').join(''); }); }, 4500);
</script></body></html>"""
SLOW_SHELL = '<html><body><iframe name="svc" src="/frame-slow"></iframe></body></html>'
NESTED_SHELL = '<html><body><iframe src="/framed"></iframe></body></html>'
SRCDOC_SHELL = ("<html><body><iframe srcdoc=\"&lt;ul&gt;&lt;li&gt;&lt;a href='a.pdf'&gt;A&lt;/a&gt;&lt;/li&gt;&lt;li&gt;&lt;a href='b.pdf'&gt;B&lt;/a&gt;&lt;/li&gt;&lt;/ul&gt;\">"
                "</iframe></body></html>")
EMPTY = "<html><body><ul><li><a href='/about'>About</a></li></ul></body></html>"
FRAME_TREE_SHELL = '<html><body><h1>Code</h1><iframe src="/frame-tree"></iframe></body></html>'
DEADLINKS = """<html><body><ul>
<li><a class="pdf" href="javascript:void(0)" onclick="window.open('/gen?id=7&name=a b')">PDF 7</a></li>
<li><a href="#" onclick="location.href='/gen?id=8'">PDF 8</a></li>
<li><a href="javascript:void(0)" onclick="alert('this does nothing useful')">Alert only</a></li>
<li><a href="/real.pdf">Real</a></li></ul></body></html>"""
NAVAWAY = """<html><body><div id="menu"><a href="/elsewhere" aria-expanded="false">Menu</a></div><a href="/docs/stay.pdf">Stay</a></body></html>"""


@pytest.fixture(scope="module")
def site():
    hits, paths = [], []

    class H(http.server.BaseHTTPRequestHandler):
        def _send(self, body, ctype="text/html; charset=utf-8", code=200):
            data = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            u = urlparse(self.path)
            paths.append(u.path)
            if u.path.startswith("/children/"):
                cid = u.path.rsplit("/", 1)[1]
                hits.append(cid)
                time.sleep(0.05)                                   # a real server takes a moment: the page must wait for it
                return self._send(json.dumps({"nodes": CHILDREN.get(cid, [])}), "application/json")
            if u.path == "/files-json":
                return self._send(json.dumps([1, 2, 3, 4]), "application/json")
            pages = {"/dead": DEADLINKS, "/frame-slow": FRAME_SLOW, "/slow": SLOW_SHELL, "/nested": NESTED_SHELL, "/srcdoc": SRCDOC_SHELL, "/empty": EMPTY,
                     "/framed": FRAME_SHELL, "/frame-inner": FRAME_INNER, "/framed-tree": FRAME_TREE_SHELL, "/frame-tree": TREE, "/": TREE, "/accordion": ACCORDION, "/loadmore": LOADMORE, "/navaway": NAVAWAY, "/elsewhere": "<html>elsewhere</html>"}
            if u.path == "/paged":
                return self._send(paged(int(parse_qs(u.query).get("page", ["1"])[0])))
            if u.path == "/xorigin":
                return self._send(H.xorigin)
            if u.path in pages:
                return self._send(pages[u.path])
            self._send("not found", code=404)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    other = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)                      # a second origin (different port)
    for x in (srv, other):
        threading.Thread(target=x.serve_forever, daemon=True).start()
    base, base2 = (f"http://127.0.0.1:{x.server_address[1]}" for x in (srv, other))
    H.xorigin = f'<html><body><iframe src="{base2}/frame-inner"></iframe></body></html>'
    yield type("Site", (), {"base": base, "base2": base2, "hits": hits, "paths": paths})
    srv.shutdown()
    other.shutdown()


def allow_local(url):                                                  # tests talk to 127.0.0.1; the real tool refuses it
    return "blocked" if "blocked.invalid" in url else ""


def run(site, path, **kw):
    try:
        return B.render_and_expand(site.base + path, validate=allow_local, **kw)
    except B.BrowserUnavailable as e:
        pytest.skip(str(e))


def pdfs(res, scope=""):
    out = []
    for url, html in res.snapshots:
        out += [f.url.rsplit("/", 1)[1] for f in extract(html, url, scope).found]
    return out


def test_only_the_scopes_region_is_opened_by_clicking(site):
    scope = "#tree>.tlist>li:nth-of-type(1) a[data-pdf*='.pdf']"
    site.hits.clear()
    res = run(site, "/", scope=scope)
    assert sorted(pdfs(res, scope)) == ["111.pdf", "112.pdf", "1211.pdf"]
    assert sorted(site.hits) == ["1", "11", "12", "121"]               # Title 2 was never touched
    assert res.clicks == 4 and res.pages == 1


def test_without_a_scope_everything_is_opened(site):
    site.hits.clear()
    res = run(site, "/")
    assert sorted(pdfs(res)) == ["111.pdf", "112.pdf", "1211.pdf", "21.pdf", "22.pdf"]
    assert sorted(site.hits) == ["1", "11", "12", "121", "2"]


def test_requests_to_disallowed_addresses_are_blocked_and_counted(site):
    res = run(site, "/")
    assert res.blocked_requests >= 1


def test_details_and_summary_sections_are_opened(site):
    res = run(site, "/accordion")
    assert sorted(pdfs(res)) == ["a23.pdf", "a24.pdf", "annex24.pdf"]


def test_next_page_links_are_followed_and_merged(site):
    res = run(site, "/paged", next_selector="a.next")
    assert res.pages == 3
    assert sorted(set(pdfs(res))) == ["p1a.pdf", "p1b.pdf", "p2a.pdf", "p2b.pdf", "p3a.pdf", "p3b.pdf"]


def test_load_more_button_is_clicked_until_it_is_gone(site):
    res = run(site, "/loadmore", next_selector=".load-more")
    assert sorted(set(pdfs(res))) == ["m0.pdf", "m1.pdf", "m2.pdf", "m3.pdf"]


def test_page_limit_is_reported(site):
    res = run(site, "/paged", next_selector="a.next", limits=B.Limits(max_pages=2))
    assert res.pages == 2 and any("2 pages" in n for n in res.notes)


def test_a_control_that_is_a_link_does_not_take_the_browser_away(site):
    site.paths.clear()
    res = run(site, "/navaway")
    assert pdfs(res) == ["stay.pdf"] and res.snapshots[-1][0].endswith("/navaway")
    assert "/elsewhere" not in site.paths                               # the link was never followed


def test_click_limit_is_reported(site):
    res = run(site, "/", limits=B.Limits(max_clicks=2))
    assert res.clicks == 2 and any("limit" in n for n in res.notes)


def test_cancel_stops_early(site):
    res = run(site, "/", cancelled=lambda: True)
    assert res.clicks == 0 and any("Stopped by the user" in n for n in res.notes)


def test_unreachable_page_is_a_clear_error(site):
    with pytest.raises(B.BrowserError, match="could not be opened"):
        B.render_and_expand("http://127.0.0.1:9/never", validate=allow_local)


def test_start_page_on_a_blocked_address_is_refused_by_the_request_filter(site):
    with pytest.raises(B.BrowserError):
        B.render_and_expand("http://blocked.invalid/", validate=allow_local)


def test_links_that_a_frame_builds_with_javascript_are_found_with_the_users_scope(site):
    res = run(site, "/framed", scope="ul li a:nth-of-type(3)")
    assert any(u.endswith("/frame-inner") for u, _ in res.snapshots)                  # the frame's own document was captured
    found = [f for u, h in res.snapshots for f in extract(h, u, "ul li a:nth-of-type(3)").found]
    assert [f.url.rsplit("=", 1)[1] for f in found] == ["12", "22", "32", "42"]
    assert found[0].url.startswith(site.base + "/dl.do?id=") and found[0].title == "[Annex 1] Title 1"
    assert extract(res.snapshots[0][1], res.snapshots[0][0], "ul li a:nth-of-type(3)").found == []         # nothing in the top page itself


def test_sections_inside_a_frame_are_opened_by_clicking_there(site):
    scope = "#tree>.tlist>li:nth-of-type(1) a[data-pdf*='.pdf']"
    site.hits.clear()
    res = run(site, "/framed-tree", scope=scope)
    found = [f.url.rsplit("/", 1)[1] for u, h in res.snapshots for f in extract(h, u, scope).found]
    assert sorted(found) == ["111.pdf", "112.pdf", "1211.pdf"] and sorted(site.hits) == ["1", "11", "12", "121"]
    assert res.clicks == 4


SCOPE3 = "ul li a:nth-of-type(3)"


def found_in(res, scope):
    return [f for u, h in res.snapshots for f in extract(h, u, scope).found]


def test_a_list_requested_only_after_seconds_of_silence_is_still_waited_for(site):
    # regression: the page is "quiet" for ~4 s (no request, no change) before the frame asks for its list
    res = run(site, "/slow", scope=SCOPE3, limits=B.Limits(scope_wait_seconds=20))
    assert [f.url.rsplit("=", 1)[1] for f in found_in(res, SCOPE3)] == ["12", "22", "32", "42"]


def test_without_a_scope_it_also_waits_for_document_links_to_appear(site):
    res = run(site, "/slow", limits=B.Limits(docs_wait_seconds=20))
    assert len(found_in(res, "")) == 4


def test_a_scope_that_matches_nothing_gives_up_after_the_wait_and_says_so(site):
    t0 = time.monotonic()
    res = run(site, "/empty", scope=SCOPE3, limits=B.Limits(scope_wait_seconds=3))
    assert found_in(res, SCOPE3) == [] and any("matched nothing" in n and "3 seconds" in n for n in res.notes)
    assert time.monotonic() - t0 < 30


def test_a_frame_on_another_website_is_read(site):
    res = run(site, "/xorigin", scope=SCOPE3)
    assert any(u.startswith(site.base2) for u, _ in res.snapshots)
    assert [f.url.rsplit("=", 1)[1] for f in found_in(res, SCOPE3)] == ["12", "22", "32", "42"]
    assert found_in(res, SCOPE3)[0].url.startswith(site.base2 + "/dl.do")           # relative links resolve against THE FRAME's site


def test_a_frame_inside_a_frame_is_read(site):
    res = run(site, "/nested", scope=SCOPE3)
    assert len(found_in(res, SCOPE3)) == 4


def test_srcdoc_frames_are_read(site):
    res = run(site, "/srcdoc")
    assert sorted(f.url.rsplit("/", 1)[1] for f in found_in(res, "")) == ["a.pdf", "b.pdf"]


def test_links_with_no_address_are_clicked_and_where_they_lead_is_recorded_without_downloading(site):
    site.paths.clear()
    res = run(site, "/dead", scope="li a", resolve_links=True)
    found = {f.title: f.url for u, h in res.snapshots for f in extract(h, u, "li a").found}
    assert found == {"PDF 7": site.base + "/gen?id=7&name=a%20b", "PDF 8": site.base + "/gen?id=8", "Real": site.base + "/real.pdf"}
    assert (res.resolved, res.unresolved) == (2, 1)
    assert any("had no address of their own" in n and "1 led nowhere" in n for n in res.notes)
    assert "/gen" not in site.paths                                    # the target was noted, never requested: nothing is downloaded


def test_without_resolve_links_nothing_is_clicked_but_literal_addresses_in_onclick_are_still_read(site):
    site.paths.clear()
    res = run(site, "/dead", scope="li a")
    found = {f.title: f.url for u, h in res.snapshots for f in extract(h, u, "li a").found}
    assert res.resolved == res.unresolved == 0                          # nothing was clicked
    assert found == {"PDF 7": site.base + "/gen?id=7&name=a%20b", "PDF 8": site.base + "/gen?id=8", "Real": site.base + "/real.pdf"}   # read from the onclick text
    assert "Alert only" not in found                                    # an onclick with no address stays unresolved


def test_one_browser_serves_many_pages(site):
    with B.BrowserSession() as session:
        assert session.browser is None                                  # nothing starts until it is needed
        try:
            first = B.render_and_expand(site.base + "/dead", scope="li a", validate=allow_local, session=session, resolve_links=True)
        except B.BrowserUnavailable as e:
            pytest.skip(str(e))
        browser_a = session.browser
        second = B.render_and_expand(site.base + "/accordion", validate=allow_local, session=session)
        assert session.browser is browser_a and browser_a.is_connected()          # reused, and still open after a page is done
        assert first.resolved == 2 and len(pdfs(second)) == 3
    assert session.browser is None                                       # closed when the with-block ends
