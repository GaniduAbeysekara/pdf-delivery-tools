"""Several levels of pages (list -> detail pages -> PDFs), scopes copied from other tools, links only a script knows,
and stopping a search or an analysis."""
import re
import time

import pytest
from flask import Flask

from tests.helpers import ENGLISH, PDF_HEADERS, FakeResp, make_analyzer, text_pdf
from tests.test_multipdf import HTML, FakeBrowser, page_html
from url_analyzer.analysis import analyze_urls
from url_analyzer.multipdf import browser as browser_mod
from url_analyzer.multipdf import scope as S
from url_analyzer.multipdf.crawl import Crawler
from url_analyzer.multipdf.extract import browser_url, count_unresolved, extract
from url_analyzer.multipdf.finder import FindError, PdfFinder
from url_analyzer.services.url_analysis_service import UrlAnalysisService
from url_analyzer.ui.url_analysis import create_url_analysis_blueprint

H = {"X-Requested-With": "fetch"}
SITE = "https://example.gov"
L1, L2, L3 = "a[href*='agency']", ".rowEven td a", "td a:nth-of-type(1)"


# ── scopes copied from other tools ───────────────────────────────────────────
def test_runs_of_child_combinators_mean_anywhere_below():
    raw = "#application>>>>>>>>>table:nth-of-type(3) tr>td:nth-of-type(1) a:nth-of-type(1)"
    assert S.validate(raw) == "#application table:nth-of-type(3) tr>td:nth-of-type(1) a:nth-of-type(1)"
    assert S.normalise("a > > b") == "a b" and S.normalise("a>b") == "a>b" and S.normalise("a >> b") == "a b"
    assert S.normalise("[title='a>>b'] c") == "[title='a>>b'] c"                         # inside quotes it is just text
    assert S.roots(S.validate(raw)) == ["#application table:nth-of-type(3) tr>td:nth-of-type(1)"]


def test_the_scopes_from_the_colorado_example_are_valid_and_split_sensibly():
    s1 = "tr:nth-of-type(170)~*:not(tr:nth-of-type(194)~*) a"
    assert S.validate(s1) == s1 and S.split_last_compound(s1) == ("tr:nth-of-type(170)~*:not(tr:nth-of-type(194)~*)", "a")
    assert S.validate(".rowEven td a") == ".rowEven td a"
    html = "".join(f"<table><tr><td>row {i}</td><td><a href='/agency?id={i}'>A{i}</a></td></tr></table>" for i in range(3))
    assert len(extract(html, SITE, ".rowEven td a").found) == 0


def test_row_range_scope_selects_only_the_rows_in_between():
    rows = "".join(f"<tr><td><a href='/agency?id={i}'>Agency {i}</a></td></tr>" for i in range(1, 11))
    html = f"<table>{rows}</table>"
    r = extract(html, SITE, "tr:nth-of-type(3)~*:not(tr:nth-of-type(7)~*) a")
    assert [f.title for f in r.found] == ["Agency 4", "Agency 5", "Agency 6", "Agency 7"]


# ── addresses ────────────────────────────────────────────────────────────────
def test_spaces_in_links_are_encoded_like_a_browser():
    html = "<a href=\"/CCR/List.do?deptName=500,1008 Department of Human Services&agencyName=501 Mental Health\">x</a>"
    f = extract(html, SITE, "a").found[0]
    assert f.url == f"{SITE}/CCR/List.do?deptName=500,1008%20Department%20of%20Human%20Services&agencyName=501%20Mental%20Health"
    assert browser_url("https://a.org/x y/%20z?q=a b&r=%41") == "https://a.org/x%20y/%20z?q=a%20b&r=%41"                  # existing %XX kept


def test_javascript_links_are_counted_as_unresolved_and_a_resolved_address_is_used():
    html = ('<table><tr><td><a href="javascript:void(0)" onclick="OpenRuleWindow(\'5421\', \'2 CCR 501-1\')">11/01/2013 (PDF)</a></td></tr>'
            '<tr><td><a href="/ok.pdf">ok</a></td><td><a href="javascript:void(0)">nothing</a></td></tr></table>')
    assert count_unresolved(html, "td a") == 1                              # the one with a click handler; the plain "void(0)" has nothing to learn
    assert extract(html, SITE, "td a:nth-of-type(1)").found[0].url == f"{SITE}/ok.pdf"
    resolved = html.replace('onclick="OpenRuleWindow', 'data-mpf-url="https://example.gov/GenerateRulePdf.do?ruleVersionId=5421" onclick="OpenRuleWindow')
    got = extract(resolved, SITE, "td a:nth-of-type(1)").found
    assert got[0].url == "https://example.gov/GenerateRulePdf.do?ruleVersionId=5421" and got[0].attr == "data-mpf-url"
    assert got[0].title == "11/01/2013 (PDF)"


# ── the crawl ────────────────────────────────────────────────────────────────
def agency(*rules, odd=""):
    rows = "".join(f'<tr class="rowEven"><td><a href="/rule?id={r}">Rule {r}</a></td></tr>' for r in rules)
    return page_html(f"<table>{rows}{odd}</table>")


def rule_script(n):                                                      # a rule page whose PDF link is javascript:void(0)
    return page_html(f'<table><tr><td><a href="javascript:void(0)" onclick="OpenRuleWindow(\'{n}\',\'x\')">Current (PDF)</a></td></tr></table>')


class Scripted:
    """A pretend browser: returns what the real one would after clicking the script links of a page."""
    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def __call__(self, url, **kw):
        self.calls.append((url, kw.get("resolve_links"), kw.get("session")))
        return browser_mod.BrowserResult(snapshots=[(url, self.pages.get(url, "<html><body><p>nothing here</p></body></html>"))], clicks=0, rounds=0, pages=1, resolved=1)


def resolved_rule(n):
    return (f'<html><body><table><tr><td><a href="javascript:void(0)" data-mpf-url="{SITE}/gen?id={n}" '
            f'onclick="Open({n})">Current (PDF)</a></td></tr></table></body></html>')


def crawl_env(extra=None, cancel_after=None):
    routes = {
        f"{SITE}/depts": page_html('<a href="/agency?id=1">Agency One</a> <a href="/agency?id=2">Agency Two</a> <a name="top">top</a>'
                                   '<a href="/other">Elsewhere</a>'),
        f"{SITE}/agency?id=1": agency(11, 12, odd='<tr class="rowOdd"><td><a href="/rule?id=13">Rule 13</a></td></tr>'),
        f"{SITE}/agency?id=2": agency(21, 11),                              # rule 11 again: opened once
        f"{SITE}/rule?id=11": rule_script(11), f"{SITE}/rule?id=12": rule_script(12),
        f"{SITE}/rule?id=21": page_html('<table><tr><td><a href="/files/21.pdf">Current (PDF)</a></td></tr></table>'),
        **(extra or {}),
    }
    a, sess = make_analyzer(routes)
    br = Scripted({f"{SITE}/rule?id=11": resolved_rule(11), f"{SITE}/rule?id=12": resolved_rule(12)})
    return Crawler(PdfFinder(a, browser_fn=br, browser_ok=lambda: True)), br, sess


def test_three_levels_end_to_end():
    c, br, sess = crawl_env()
    prog = []
    d = c.run(f"{SITE}/depts#top", [L1, L2, L3], progress=prog.append)
    assert [f.url for f in d.found] == [f"{SITE}/gen?id=11", f"{SITE}/gen?id=12", f"{SITE}/files/21.pdf"]
    assert [f.path for f in d.found] == ["Agency One > Rule 11", "Agency One > Rule 12", "Agency Two > Rule 21"]
    assert d.found[0].source == f"{SITE}/rule?id=11" and d.found[0].title == "Current (PDF)"
    assert d.stats["levels"] == [{"level": 1, "pages": 1, "links": 2}, {"level": 2, "pages": 2, "links": 3}, {"level": 3, "pages": 3, "links": 3}]
    assert d.stats["pages_opened"] == 6 and d.mode == "crawl"
    assert d.notes[0].startswith("Opened 6 pages over 3 levels")
    # the browser was needed only for the two pages whose links are scripts, and only on the last level
    assert [(u.rsplit("=", 1)[1], resolve) for u, resolve, _ in br.calls] == [("11", True), ("12", True)]
    assert any(p.get("message", "").startswith("Level 3 of 3") for p in prog)
    assert not any("Rule 13" in f.title for f in d.found)                       # .rowOdd was not in the scope


def test_one_browser_is_shared_by_all_pages():
    c, br, _ = crawl_env()
    c.run(f"{SITE}/depts", [L1, L2, L3])
    sessions = {id(s) for _, _, s in br.calls}
    assert len(sessions) == 1 and None not in {s for _, _, s in br.calls}


def test_a_page_that_fails_does_not_stop_the_crawl():
    c, _, _ = crawl_env({f"{SITE}/agency?id=2": FakeResp(500, b"", HTML)})
    d = c.run(f"{SITE}/depts", [L1, L2, L3])
    assert len(d.found) == 2 and d.stats["pages_failed"] == 1
    assert any("could not be read" in n and "agency?id=2" in n for n in d.notes)


def test_the_first_page_must_work():
    c, _, _ = crawl_env({f"{SITE}/depts": FakeResp(404, b"", HTML)})
    with pytest.raises(FindError, match="HTTP 404"):
        c.run(f"{SITE}/depts", [L1, L2, L3])


def test_a_level_that_finds_nothing_says_which_scope_to_check():
    c, _, _ = crawl_env()
    with pytest.raises(FindError, match=r"Level 2 found no links to open .*\.nothing td a"):
        c.run(f"{SITE}/depts", [L1, ".nothing td a", L3])


def test_page_limit_stops_the_crawl_and_keeps_what_it_has():
    c, _, _ = crawl_env()
    d = c.run(f"{SITE}/depts", [L1, L2, L3], max_pages=4)
    assert d.stats["pages_opened"] == 4 and any("Stopped after opening 4 pages" in n for n in d.notes)
    assert [f.url for f in d.found] == [f"{SITE}/gen?id=11"]


def test_document_maximum_is_enforced():
    c, _, _ = crawl_env()
    d = c.run(f"{SITE}/depts", [L1, L2, L3], max_items=2)
    assert len(d.found) == 2 and any("maximum of 2" in n for n in d.notes)


def test_stop_ends_the_crawl_between_pages():
    c, _, _ = crawl_env()
    state = {"n": 0}

    def stop_after_three_pages():
        state["n"] += 1
        return state["n"] > 5
    d = c.run(f"{SITE}/depts", [L1, L2, L3], cancelled=stop_after_three_pages)
    assert any("Stopped by the user" in n for n in d.notes) and d.stats["pages_opened"] < 6


def test_a_single_scope_is_an_ordinary_search_and_levels_need_scopes():
    c, _, _ = crawl_env()
    assert c.run(f"{SITE}/depts", ["a[href*='agency']"], strict=False).mode == "page"            # no crawl: the links are the results
    with pytest.raises(FindError, match="needs a scope"):
        c.run(f"{SITE}/depts", [L1, ""])
    with pytest.raises(FindError, match="At most 5"):
        c.run(f"{SITE}/depts", [L1] * 6)


# ── HTTP: follow-levels parameters ───────────────────────────────────────────
def make_app(tmp_path, extra=None):
    routes = {
        f"{SITE}/depts": page_html('<a href="/agency?id=1">Agency One</a>'), f"{SITE}/agency?id=1": agency(11),
        f"{SITE}/rule?id=11": rule_script(11), f"{SITE}/gen?id=11": FakeResp(200, text_pdf(ENGLISH, 3), {**PDF_HEADERS, "Content-Length": "2048"}),
        **(extra or {}),
    }
    a, _ = make_analyzer(routes)
    br = Scripted({f"{SITE}/rule?id=11": resolved_rule(11)})
    svc = UrlAnalysisService(tmp_path / "out", a)
    app = Flask(__name__, template_folder="../templates")
    app.register_blueprint(create_url_analysis_blueprint(tmp_path / "out", tmp_path / "up", service=svc,
                                                         finder=PdfFinder(a, browser_fn=br, browser_ok=lambda: True)))
    app.testing = True
    return app.test_client()


def wait(c, url, until=lambda s: s["state"] != "running", tries=400):
    for _ in range(tries):
        s = c.get(url).get_json()
        if until(s):
            return s
        time.sleep(0.02)
    raise AssertionError("timeout")


def test_levels_through_the_api_to_analysed_pdfs(tmp_path):
    c = make_app(tmp_path)
    body = {"url": f"{SITE}/depts", "scope": L1, "follow": [L2, "#application>>>>td a:nth-of-type(1)".replace("#application>>>>", "")], "max_pages": 50}
    fid = c.post("/api/url-analysis/find-pdfs", json=body, headers=H).get_json()["find_id"]
    st = wait(c, f"/api/url-analysis/find-pdfs/{fid}")
    assert st["state"] == "done" and st["found"] == 1 and st["mode_used"] == "crawl" and st["job_id"]
    job = wait(c, f"/api/url-analysis/{st['job_id']}/status?from=0", lambda s: s["state"] == "done")
    row = job["events"][0]
    assert (row["status"], row["content_type"], row["page_count"]) == ("WORKING", "PDF", "3")
    assert row["extra"]["Section Path"] == "Agency One > Rule 11" and row["extra"]["Source Page"] == f"{SITE}/rule?id=11"
    assert row["extra"]["Scope"].count(" | ") == 2


def test_follow_validation(tmp_path):
    c = make_app(tmp_path)
    for body, msg in (({"url": f"{SITE}/depts", "scope": L1, "follow": ["a["]}, "level 2 scope selector is not valid"),
                      ({"url": f"{SITE}/depts", "scope": L1, "follow": [""]}, "Level 2 needs a scope"),
                      ({"url": f"{SITE}/depts", "scope": "", "follow": [L2]}, "Level 1 needs a scope"),
                      ({"url": f"{SITE}/depts", "scope": L1, "follow": [L2] * 5}, "At most"),
                      ({"url": f"{SITE}/depts", "scope": L1, "follow": "x"}, "At most"),
                      ({"url": f"{SITE}/depts", "scope": L1, "follow": [L2], "max_pages": "lots"}, "must be a number")):
        r = c.post("/api/url-analysis/find-pdfs", json=body, headers=H)
        assert r.status_code == 400 and msg in r.get_json()["error"], body


# ── stopping an analysis ─────────────────────────────────────────────────────
def slow_pdf(kw=None):
    time.sleep(0.25)
    return FakeResp(200, text_pdf(ENGLISH, 2), {**PDF_HEADERS, "Content-Length": "2048"})


def test_engine_batch_can_be_stopped_and_keeps_what_finished():
    urls = [f"https://example.gov/d{i}.pdf" for i in range(30)]
    a, _ = make_analyzer({u: slow_pdf for u in urls}, max_workers=2)
    got, t0 = [], time.monotonic()
    analyze_urls([{"url": u} for u in urls], analyzer=a, on_result=lambda i, r: got.append(i), cancelled=lambda: len(got) >= 3)
    assert 3 <= len(got) < 30 and time.monotonic() - t0 < 5          # 30 x 0.25 s / 2 workers would be ~4 s: it stopped well before


def test_a_running_analysis_can_be_stopped_through_the_api(tmp_path):
    urls = [f"https://example.gov/s{i}.pdf" for i in range(40)]
    a, _ = make_analyzer({u: slow_pdf for u in urls}, max_workers=2)
    svc = UrlAnalysisService(tmp_path / "out", a)
    app = Flask(__name__, template_folder="../templates")
    app.register_blueprint(create_url_analysis_blueprint(tmp_path / "out", tmp_path / "up", service=svc))
    app.testing = True
    c = app.test_client()
    job = c.post("/api/url-analysis/analyze", json={"mode": "urls", "text": "\n".join(urls)}).get_json()["job_id"]
    wait(c, f"/api/url-analysis/{job}/status?from=0", lambda s: s["done"] >= 2)
    assert c.post(f"/api/url-analysis/{job}/cancel").status_code == 400                         # needs the header
    assert c.post(f"/api/url-analysis/{job}/cancel", headers=H).get_json() == {"stopping": True, "state": "running"}
    st = wait(c, f"/api/url-analysis/{job}/status?from=0")
    assert st["state"] == "stopped" and 2 <= st["done"] < 40
    assert c.get(f"/api/url-analysis/{job}/download").status_code == 200                       # what finished can be downloaded
    assert c.post(f"/api/url-analysis/{job}/cancel", headers=H).get_json() == {"stopping": False, "state": "stopped"}
    assert c.post("/api/url-analysis/nope/cancel", headers=H).status_code == 404
    assert c.post(f"/api/url-analysis/{job}/reanalyze", json={"indices": [0]}).status_code == 200  # a stopped job can be continued


def test_the_hidden_attribute_really_hides_and_the_stop_buttons_exist():
    import importlib
    c = importlib.import_module("dashboard").create_app().test_client()
    assert "[hidden] { display:none !important; }" in c.get("/").data.decode()
    html = c.get("/url-analysis").data.decode()
    for needle in ('id="stop-an"', 'id="find-stop"', 'id="f-levels"', 'id="f-addlevel"', 'id="f-pages"'):
        assert needle in html
