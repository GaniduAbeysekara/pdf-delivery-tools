"""Shared test helpers: a fake HTTP session (no real network), PDF/DOCX builders, analyzer factory."""
import io
import zipfile

from url_analyzer.analysis import AnalyzerSettings, URLAnalyzer

PUBLIC = lambda host: ["93.184.216.34"]  # noqa: E731  (no real DNS in tests)
ENGLISH = ("This annual report describes the activities of the regulator during the year and sets out "
           "the principal findings of the supervisory reviews that were carried out.")
PDF_HEADERS = {"Content-Type": "application/pdf", "Last-Modified": "Wed, 01 Oct 2025 08:30:00 GMT"}


class FakeResp:
    def __init__(self, status=200, body=b"", headers=None):
        self.status_code, self.headers, self._body, self.closed = status, headers or {}, body, False

    def iter_content(self, chunk_size=65536):
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i:i + chunk_size]

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw.get("verify")))
        r = self.routes[url]
        if isinstance(r, Exception):
            raise r
        return r(kw) if callable(r) else r


def make_analyzer(routes, resolver=PUBLIC, **settings):
    sess = FakeSession(routes)
    base = dict(retries=0, max_workers=3, read_timeout=30)
    base.update(settings)
    return URLAnalyzer(AnalyzerSettings(**base), session_factory=lambda: sess, resolver=resolver), sess


def text_pdf(text="", pages=3):
    objs = ["<< /Type /Catalog /Pages 2 0 R >>",
            "<< /Type /Pages /Kids [" + " ".join(f"{3 + 2 * i} 0 R" for i in range(pages)) + f"] /Count {pages} >>"]
    for i in range(pages):
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {4 + 2 * i} 0 R "
                    f"/Resources << /Font << /F1 {3 + 2 * pages} 0 R >> >> >>")
        stream = f"BT /F1 12 Tf 50 700 Td ({text}) Tj ET"
        objs.append(f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream")
    objs.append("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    out, offsets = b"%PDF-1.4\n", []
    for n, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{n} 0 obj\n{o}\nendobj\n".encode()
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{off:010d} 00000 n \n".encode() for off in offsets)
    return out + f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode()


def docx_bytes(pages=7, text="The quick brown fox jumps over the lazy dog. " * 10):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("docProps/app.xml", f"<Properties><Pages>{pages}</Pages></Properties>")
        z.writestr("word/document.xml", f"<w:document><w:body><w:p><w:t>{text}</w:t></w:p></w:body></w:document>")
    return buf.getvalue()
