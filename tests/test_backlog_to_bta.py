"""Daily Backlog Dashboard -> Base Template Analysis: all (filtered) or selected books are checked against the Base Templates."""
from pathlib import Path

import pytest
from flask import Flask

from tests.test_handoff import rid  # noqa: F401  (a generated backlog report: D1..D5)
from url_analyzer import transfer
from url_analyzer.backlog.web import create_blueprint
from url_analyzer.base_templates.analysis_web import create_analysis_blueprint
from url_analyzer.base_templates.store import BaseTemplateStore

ROOT = Path(__file__).parents[1]
H = {"X-Requested-With": "fetch"}


@pytest.fixture
def client(tmp_path):
    store = BaseTemplateStore(tmp_path / "bt.json")
    store.add({"template": "BASE-A", "domain": "https://a.gov/"})
    app = Flask(__name__, template_folder=str(ROOT / "templates"))
    app.register_blueprint(create_blueprint(tmp_path / "up", tmp_path / "out"))
    app.register_blueprint(create_analysis_blueprint(tmp_path / "bta_up", store))
    app.testing = True
    return app.test_client()


def sent(client, rid, **body):
    r = client.post(f"/api/backlog/{rid}/send-to-base-templates", json=body)
    return r, (transfer.get(r.get_json()["transfer_id"]) if r.status_code == 200 else None)


def test_selected_books_are_sent_with_their_row_numbers(client, rid):
    r, payload = sent(client, rid, rows=[0, 2, 4])
    assert r.status_code == 200 and r.get_json()["count"] == 3 and r.get_json()["scope"] == "selected"
    assert payload["source"]["tool"] == "backlog" and payload["source"]["name"].startswith("Daily Backlog")
    recs = payload["records"]
    assert [x["document_id"] for x in recs] == ["D1", "D3", "D5"] and [x["row"] for x in recs] == [1, 3, 5]
    assert recs[0]["url"] == "https://a.gov/1.pdf" and recs[0]["domain"] == "a.gov"
    assert recs[1]["url"] == ""                                   # a book with no URL is kept so it can be flagged


def test_all_filtered_books_are_sent_when_nothing_is_selected(client, rid):
    r, payload = sent(client, rid, filters="view=all&domain=a.gov")
    assert r.get_json()["count"] == 2 and r.get_json()["scope"] == "filtered"
    assert [x["document_id"] for x in payload["records"]] == ["D1", "D2"]
    r, payload = sent(client, rid, filters="view=all")
    assert r.get_json()["count"] == 5                              # no filter: every book
    r, payload = sent(client, rid, filters="view=change&book_type=PDF")
    assert sorted(x["document_id"] for x in payload["records"]) == ["D1", "D2", "D5"]


def test_nothing_to_send_is_refused(client, rid):
    assert client.post(f"/api/backlog/{rid}/send-to-base-templates", json={}).status_code == 400
    assert client.post(f"/api/backlog/{rid}/send-to-base-templates", json={"filters": "domain=nothing.here&view=all"}).status_code == 400
    assert client.post("/api/backlog/nope/send-to-base-templates", json={"rows": [0]}).status_code == 404


def test_the_books_are_compared_with_the_base_templates(client, rid):
    tid = sent(client, rid, rows=[0, 1, 2, 3])[0].get_json()["transfer_id"]
    out = client.post("/api/bta/from-transfer", json={"transfer_id": tid}, headers=H).get_json()
    assert out["received"] == 4 and out["source"]["tool"] == "backlog"
    rows = {r["document_id"]: r for r in client.get(f"/api/bta/{out['result_id']}/rows").get_json()["items"]}
    assert "MISSING_URL" in rows["D3"]["flags"] and rows["D3"]["source_row"] == 3
    assert "NO_BASE_TEMPLATE" in rows["D4"]["flags"] and "NO_BASE_TEMPLATE" not in rows["D1"]["flags"]   # only a.gov has a Base Template
    assert rows["D1"]["base_templates"] == ["BASE-A"]


def test_the_pages_have_the_buttons_and_a_source_aware_receiver(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    import dashboard
    c = dashboard.app.test_client()
    page = c.get("/backlog").get_data(as_text=True)
    assert 'id="bta-sel"' in page and 'id="bta-all"' in page and "/send-to-base-templates" in page and "/base-template-analysis?transfer=" in page
    bta = c.get("/base-template-analysis").get_data(as_text=True)
    assert 'id="recvback"' in bta and "Daily Backlog Dashboard" in bta
