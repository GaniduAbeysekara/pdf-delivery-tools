"""UI Code and SpideringTemplate are carried onto each Power BI record (DocumentId <-> BookSourceId), searchable, and downloadable."""
import io
import time
from pathlib import Path

import pandas as pd
import pytest
from flask import Flask
from openpyxl import load_workbook

from url_analyzer.backlog import filters as F
from url_analyzer.backlog.dataset import build_dataset
from url_analyzer.backlog.excel_report import write_report
from url_analyzer.backlog.web import create_blueprint

UI = pd.DataFrame({
    "DocumentId": ["A001", "A002", "A003", "A004"],
    "Code": ["DE--DEF--REG--ONE", "JO--CBJ--REG--TWO", "FR--AMF--REG--THREE", None],
    "SpideringTemplate": ["DE--DEF--REG--ONE-SPIDER", "JO-TEMPLATE-2", "", "TPL-4"],
    "BookCategory": ["PDF", "PDF", "HTML", "Other"],
})
PBI = pd.DataFrame({
    "BookSourceId": ["A003", "A001", "A002", "ZZZ"],            # different order + one unmatched
    "BookTitle": ["Three", "One", "Two", "Nobody"],
    "API Result": ["Change", "Change", "Change", "Change"],
    "Link to the Issuance": ["https://c.gov/3", "https://a.gov/1", "https://b.gov/2", "https://z.gov/9"],
})


def files(tmp_path, ui=UI, pbi=PBI):
    u, p = tmp_path / "StartPointStatus-01-Oct-2026.csv", tmp_path / "Details_Table_-_2026-10-01T010101_000.xlsx"
    ui.to_csv(u, index=False)
    pbi.to_excel(p, index=False)
    return u, p


@pytest.fixture
def data(tmp_path):
    u, p = files(tmp_path)
    return build_dataset(str(u), str(p))


def test_code_and_template_follow_the_booksourceid_match(data):
    ex = data.export.set_index("BookSourceId")
    assert ex.loc["A001", "Code"] == "DE--DEF--REG--ONE" and ex.loc["A001", "SpideringTemplate"] == "DE--DEF--REG--ONE-SPIDER"
    assert ex.loc["A002", "Code"] == "JO--CBJ--REG--TWO" and ex.loc["A003", "SpideringTemplate"] == ""     # blank stays blank
    assert ex.loc["ZZZ", "Code"] == "" and ex.loc["ZZZ", "SpideringTemplate"] == "" and ex.loc["ZZZ", "Book Type"] == "Unmatched"
    assert data.public_columns[-4:] == ["Book Type", "Domain", "Code", "SpideringTemplate"]
    assert data.stats["code_found"] == 3 and data.stats["template_found"] == 2


def test_missing_ui_columns_give_empty_columns_and_a_warning(tmp_path):
    u, p = files(tmp_path, ui=UI.drop(columns=["Code", "SpideringTemplate"]))
    d = build_dataset(str(u), str(p))
    assert (d.export["Code"] == "").all() and (d.export["SpideringTemplate"] == "").all()
    assert any("no 'Code' column" in w for w in d.warnings) and any("no 'SpideringTemplate' column" in w for w in d.warnings)


def test_column_name_variants(tmp_path):
    u, p = files(tmp_path, ui=UI.rename(columns={"SpideringTemplate": "Spidering Template", "Code": "code"}))
    d = build_dataset(str(u), str(p))
    assert d.export.set_index("BookSourceId").loc["A002", "SpideringTemplate"] == "JO-TEMPLATE-2"


def test_dedicated_searches_and_global_search(data):
    ex = data.export
    ids = lambda spec: set(F.apply_filters(ex, spec)["BookSourceId"])           # noqa: E731
    assert ids(F.FilterSpec(code="cbj")) == {"A002"}                           # case-insensitive contains
    assert ids(F.FilterSpec(template="spider")) == {"A001"}
    assert ids(F.FilterSpec(code="REG", template="TEMPLATE")) == {"A002"}       # combined (AND)
    assert ids(F.FilterSpec(q="AMF")) == {"A003"}                              # the global search reaches Code too
    assert ids(F.FilterSpec(q="template-2")) == {"A002"}                       # ... and SpideringTemplate
    assert ids(F.FilterSpec(code="nothing")) == set()
    assert ids(F.FilterSpec(code="reg", domains=["a.gov"])) == {"A001"}        # works with the other filters
    assert ids(F.FilterSpec(code="  ")) == set(ex["BookSourceId"])             # blank search = no filter


def test_columns_are_in_the_excel_report(data, tmp_path):
    wb = load_workbook(write_report(data, tmp_path / "r.xlsx"))
    for sheet in ("Export", "Change and Potential Change"):
        hdr = [c.value for c in wb[sheet][1]]
        assert "Code" in hdr and "SpideringTemplate" in hdr, sheet
    df = pd.read_excel(tmp_path / "r.xlsx", sheet_name="Export", dtype=object).set_index("BookSourceId")
    assert df.loc["A001", "Code"] == "DE--DEF--REG--ONE"


@pytest.fixture
def client(tmp_path):
    app = Flask(__name__, template_folder=str(Path(__file__).parents[1] / "templates"))
    app.register_blueprint(create_blueprint(tmp_path / "up", tmp_path / "out"))
    c = app.test_client()
    u, p = files(tmp_path)
    jid = c.post("/api/backlog/generate", data={"ui": (io.BytesIO(u.read_bytes()), u.name), "powerbi": (io.BytesIO(p.read_bytes()), p.name)},
                 content_type="multipart/form-data").get_json()["job_id"]
    for _ in range(200):
        if c.get(f"/api/backlog/status/{jid}").get_json()["state"] != "running":
            break
        time.sleep(0.05)
    c.rid = jid
    return c


def test_search_api_table_and_downloads_include_the_columns(client):
    q = client.get(f"/api/backlog/{client.rid}/query?view=all&code=cbj&size=50").get_json()
    assert [r["BookSourceId"] for r in q["rows"]] == ["A002"] and q["total_filtered"] == 1
    assert {"Code", "SpideringTemplate"} <= set(q["columns"]) and q["rows"][0]["Code"] == "JO--CBJ--REG--TWO"
    # the filtered CSV honours the search and carries both columns
    csv = pd.read_csv(io.StringIO(client.get(f"/api/backlog/{client.rid}/download/filtered?view=all&template=spider").data.decode("utf-8-sig")))
    assert csv["BookSourceId"].tolist() == ["A001"] and csv.loc[0, "SpideringTemplate"] == "DE--DEF--REG--ONE-SPIDER"
    # the selected-rows CSV carries them too
    sel = pd.read_csv(io.StringIO(client.post(f"/api/backlog/{client.rid}/download/selected", json={"rows": [0, 1]}).data.decode("utf-8-sig")))
    assert {"Code", "SpideringTemplate"} <= set(sel.columns) and set(sel["Code"]) == {"FR--AMF--REG--THREE", "DE--DEF--REG--ONE"}
    ids = client.get(f"/api/backlog/{client.rid}/ids?view=all&code=reg").get_json()
    assert ids["total"] == 3                                                   # select-all-in-filtered follows the search


def test_page_has_the_search_boxes(client):
    html = client.get("/backlog").get_data(as_text=True)
    assert 'id="code-q"' in html and 'id="tpl-q"' in html and "Spidering Template" in html
