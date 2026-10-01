"""PDF Delivery Tools: Home, Daily Backlog Dashboard, URL Analysis (paste / Excel / from backlog)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from flask import Flask, render_template  # noqa: E402

from url_analyzer.backlog.web import create_blueprint  # noqa: E402
from url_analyzer.ui.url_analysis import create_url_analysis_blueprint  # noqa: E402

UPLOAD_DIR = Path("uploads")
OUTPUT_DIR = Path("output")


def create_app() -> Flask:
    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", os.urandom(16).hex())
    app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024  # UI CSV exports can be ~45 MB

    for d in (UPLOAD_DIR, OUTPUT_DIR):
        d.mkdir(exist_ok=True)
    app.register_blueprint(create_blueprint(UPLOAD_DIR / "backlog", OUTPUT_DIR / "backlog"))
    app.register_blueprint(create_url_analysis_blueprint(OUTPUT_DIR / "url_analysis", UPLOAD_DIR / "url_analysis"))

    @app.route("/")
    def home():
        return render_template("home.html")

    return app


app = create_app()

if __name__ == "__main__":
    host = os.environ.get("HOST", "127.0.0.1")   # local tool: not exposed to the network unless HOST is set
    port = int(os.environ.get("PORT", 8080))
    print(f"\n  PDF Delivery Tools  ->  http://localhost:{port}\n")
    app.run(host=host, port=port, debug=False, threaded=True)
