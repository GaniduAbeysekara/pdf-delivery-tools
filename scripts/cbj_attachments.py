"""Attachments of every Capitulo of the Superfinanciera Circular Basica Juridica.

    python scripts/cbj_attachments.py --out output/cbj_attachments.xlsx
    python scripts/cbj_attachments.py --parte 1 --max-capitulos 3 --headed      (a quick look)

See src/url_analyzer/recipes/cbj.py for how it works and the options.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from url_analyzer.recipes.cbj import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
