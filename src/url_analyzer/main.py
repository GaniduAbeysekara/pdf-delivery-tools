"""CLI entry point. Excel batch analysis here uses the SAME engine and exporter as the web tool."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from tqdm import tqdm

from .analysis import (SOURCE_EXCEL, AnalyzerSettings, InputError, URLAnalysisResult, URLAnalyzer, analyze_urls,
                       export_analysis_results, read_excel_records)
from .config import load_config
from .logger import log, setup_logger


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="url_analyzer",
        description="Analyze the URLs in an Excel file (same engine as the URL Analysis web tool).",
    )
    parser.add_argument("--input", "-i", required=True, help="Input Excel file path")
    parser.add_argument("--output", "-o", default="",
                        help="Output Excel file path (default: output/<input>_analyzed.xlsx)")
    parser.add_argument("--url-column", default="", help="Name of the URL column (auto-detect if omitted)")
    parser.add_argument("--id-column", default="", help="Name of the Document ID column (optional)")
    parser.add_argument("--title-column", default="", help="Name of the Book Title column (optional)")
    parser.add_argument("--code-column", default="", help="Name of the Code column (optional)")
    parser.add_argument("--template-column", default="", help="Name of the SpideringTemplate column (optional)")
    parser.add_argument("--workers", type=int, default=0, help="Parallel workers (0 = use config)")
    parser.add_argument("--config", default="", help="Path to config.yaml")
    parser.add_argument("--log-dir", default="logs", help="Directory for log files")
    parser.add_argument("--fresh", action="store_true", help="Ignore cached results")
    return parser.parse_args(argv)


def _default_output(input_path: str) -> str:
    p = Path(input_path)
    return str(p.parent / "output" / (p.stem + "_analyzed.xlsx"))


def process(
    input_path: str,
    output_path: str,
    url_column: str = "",
    workers: int = 0,
    config_path: str = "",
    log_dir: str = "logs",
    on_progress=None,   # callback(current, total, result: URLAnalysisResult)
    on_done=None,       # callback(results, output_path)
    id_column: str = "",
    title_column: str = "",
    force_refresh: bool = False,
    code_column: str = "",
    template_column: str = "",
) -> list[URLAnalysisResult]:
    """Excel in -> shared engine -> shared exporter. Returns the standard results."""
    setup_logger(log_dir)
    cfg = load_config(config_path or None)
    overrides = {"max_workers": workers} if workers > 0 else {}
    analyzer = URLAnalyzer(AnalyzerSettings.from_config(cfg, **overrides))

    log.info("Loading input: %s", input_path)
    data = read_excel_records(input_path, url_column or None, id_column or None, title_column or None,
                              code_column or None, template_column or None)
    for note in data.notes:
        log.info(note)
    total = len(data.records)
    print(f"\nAnalyzing {total} URLs ({analyzer.s.max_workers} workers) from '{data.url_column}'...\n")

    completed = 0
    with tqdm(total=total, unit="url", ncols=80) as bar:
        def on_result(_idx: int, res: URLAnalysisResult) -> None:
            nonlocal completed
            completed += 1
            bar.set_postfix_str((res.url or "")[:60], refresh=False)
            bar.update(1)
            if on_progress:
                try:
                    on_progress(completed, total, res)
                except Exception:
                    log.exception("progress callback failed")

        results = analyze_urls(data.records, analyzer=analyzer, force_refresh=force_refresh, on_result=on_result)

    export_analysis_results(results, output_path, source=SOURCE_EXCEL, source_name=Path(input_path).name,
                            source_columns=data.source_columns)
    print(f"\nDone! Output saved to: {output_path}")
    if on_done:
        try:
            on_done(results, output_path)
        except Exception:
            log.exception("done callback failed")
    return results


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "daily-workflow":
        from .daily_workflow import main as daily_main
        daily_main(argv[1:])
        return
    args = _parse_args(argv)
    output = args.output or _default_output(args.input)
    try:
        process(
            input_path=args.input, output_path=output, url_column=args.url_column, workers=args.workers,
            config_path=args.config, log_dir=args.log_dir, id_column=args.id_column,
            title_column=args.title_column, force_refresh=args.fresh,
            code_column=args.code_column, template_column=args.template_column,
        )
    except InputError as e:
        print(f"\nError: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"\nFatal error: {e}", file=sys.stderr)
        log.exception("Fatal error: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
