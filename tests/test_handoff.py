"""Daily Backlog -> URL Analysis hand-off, state restore endpoints, and app navigation."""
import io
import time
from pathlib import Path

import pandas as pd
import pytest
from flask import Flask

from url_analyzer.backlog.web import create_blueprint
from url_analyzer.ui.url_analysis import create_url_analysis_blueprint

TEMPLATES = str(Path(__file__).parents[1] / "templates")

UI = pd.DataFrame({"DocumentId": ["D1", "D2", "D3", "D4", "D5"],
                   "BookCategory": ["PDF", "PDF", "Other", "HTML", "PDF"]})
PBI = pd.DataFrame({
    "BookSourceId": ["D1", "D2", "D3", "D4", "D5"],
    "BookTitle": ["One", "Two", "Three", "Four", "Five"],
    "API Result": ["Change", "Change", "change ", "Change", "Stream 3 - Potential Changes BAU skim"],
    "Link to the Issuance": ["https://a.gov/1.pdf", "https://a.gov/1.pdf#again", "", "https://b.org/4", "https://b.org/5.pdf"],
})


@pytest.fixture
def client(tmp_path):
    app = Flask(__name__, template_folder=TEMPLATES)
    app.register_blueprint(create_blueprint(tmp_path / "up", tmp_path / "out"))
    app.register_blueprint(create_url_analysis_blueprint(tmp_path / "out2", tmp_path / "up2"))
    return app.test_client()


@pytest.fixture
def rid(client, tmp_path):
    ui, pbi = tmp_path / "StartPointStatus-01-Oct-2026.csv", tmp_path / "Details_Table_-_2026-10-01T010101_000.xlsx"
    UI.to_csv(ui, index=False)
    PBI.to_excel(pbi, index=False)
    r = client.post("/api/backlog/generate", data={"ui": (io.BytesIO(ui.read_bytes()), ui.name),
                                                   "powerbi": (io.BytesIO(pbi.read_bytes()), pbi.name)},
                    content_type="multipart/form-data")
    jid = r.get_json()["job_id"]
    for _ in range(100):
        if client.get(f"/api/backlog/status/{jid}").get_json()["state"] != "running":
            return jid
        time.sleep(0.05)
    raise AssertionError("timeout")


def rows_of(client, rid, **q):
    qs = "&".join(f"{k}={v}" for k, v in q.items())
    return client.get(f"/api/backlog/{rid}/query?view=all&size=100&{qs}").get_json()["rows"]


def test_rows_carry_ids_for_selection(client, rid):
    rows = rows_of(client, rid)
    assert sorted(r["_row"] for r in rows) == [0, 1, 2, 3, 4]


def test_send_selected_books_dedupes_and_drops_blank_urls(client, rid):
    r = client.post(f"/api/backlog/{rid}/send-to-url-tool", json={"rows": [0, 1, 2, 3]})
    d = r.get_json()
    assert r.status_code == 200 and d["count"] == 2 and d["skipped_blank"] == 1 and d["duplicates_removed"] == 1
    payload = client.get(f"/api/transfer/{d['transfer_id']}").get_json()
    assert payload["source"]["tool"] == "backlog" and payload["source"]["rid"] == rid
    first, second = payload["items"]
    assert first == {"document_id": "D1", "book_title": "One", "url": "https://a.gov/1.pdf", "domain": "a.gov",
                     "api_result": "Change", "book_type": "PDF"}
    assert second["document_id"] == "D4" and second["book_type"] == "HTML"     # Book Type travels with the book


def test_send_requires_a_selection_and_a_url(client, rid):
    assert client.post(f"/api/backlog/{rid}/send-to-url-tool", json={"rows": []}).status_code == 400
    r = client.post(f"/api/backlog/{rid}/send-to-url-tool", json={"rows": [2]})
    assert r.status_code == 400 and "source URL" in r.get_json()["error"]
    assert client.post("/api/backlog/nope/send-to-url-tool", json={"rows": [0]}).status_code == 404


def test_transferred_books_can_be_analysed_and_keep_their_ids(client, rid):
    tid = client.post(f"/api/backlog/{rid}/send-to-url-tool", json={"rows": [0, 4]}).get_json()["transfer_id"]
    items = client.get(f"/api/transfer/{tid}").get_json()["items"]
    from url_analyzer.analysis import parse_urls
    assert [i["url"] for i in items] == [i.url for i in parse_urls("\n".join(i["url"] for i in items)).items]


def test_select_all_in_filtered_view(client, rid):
    d = client.get(f"/api/backlog/{rid}/ids?view=all&domain=a.gov").get_json()
    assert sorted(d["rows"]) == [0, 1] and d["total"] == 2 and not d["truncated"]
    d = client.get(f"/api/backlog/{rid}/ids?view=change&book_type=PDF").get_json()
    assert sorted(d["rows"]) == [0, 1, 4]                   # PDF books that are Change or Potential Change


def test_meta_endpoint_restores_dashboard_after_navigation(client, rid):
    d = client.get(f"/api/backlog/{rid}/meta").get_json()
    assert d["report"]["id"] == rid and d["report"]["stats"]["matched"] == 5 and d["excel"]["state"] in ("pending", "done")
    assert d["report"]["options"]["book_types"] == ["PDF", "Other", "HTML"]       # from the data, not hardcoded
    assert client.get("/api/backlog/unknown/meta").status_code == 404


def test_navigation_pages_render(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    import dashboard
    c = dashboard.app.test_client()
    home = c.get("/").get_data(as_text=True)
    assert "Open Daily Backlog Dashboard" in home and "Open URL Analysis Tool" in home and "PDF DELIVERY TOOLS" in home
    for path in ("/backlog", "/url-analysis", "/url-analysis?tab=excel"):
        page = c.get(path).get_data(as_text=True)
        side = page.split("</aside>")[0]
        assert "Daily Backlog Dashboard" in side and ">URL Analysis<" in side                  # sidebar on every page
        assert "nav sub" not in side and "Paste URLs" not in side and "Excel Batch Upload" not in side   # input methods are NOT nav items
    assert c.get("/url-analysis/excel").status_code == 302                                  # old address redirects to the Excel tab
    assert "Analyze Selected URLs" in c.get("/backlog").get_data(as_text=True)
    url_page = c.get("/url-analysis").get_data(as_text=True)
    assert "Paste URLs" in url_page and "Excel Batch Upload" in url_page and "Select URL Column" in url_page and "Re-analyze" in url_page
    assert "Overview by domain" in url_page and "Download Selected" in url_page             # inside the tool, as tabs


def test_download_selected_books_as_csv(client, rid):
    r = client.post(f"/api/backlog/{rid}/download/selected", json={"rows": [0, 3]})
    assert r.status_code == 200 and r.mimetype == "text/csv" and "selected.csv" in r.headers["Content-Disposition"]
    df = pd.read_csv(io.StringIO(r.data.decode("utf-8-sig")))
    assert sorted(df["BookSourceId"]) == ["D1", "D4"] and "Book Type" in df.columns and "Domain" in df.columns
    assert all(not c.startswith("_") for c in df.columns)
    assert client.post(f"/api/backlog/{rid}/download/selected", json={"rows": []}).status_code == 400
    assert client.post("/api/backlog/nope/download/selected", json={"rows": [0]}).status_code == 404
