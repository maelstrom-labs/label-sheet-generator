from __future__ import annotations

import argparse
import sys


def main(host: str = "127.0.0.1", port: int = 8000, reload: bool = False, argv: list[str] | None = None) -> int:
    if argv is not None:
        parser = argparse.ArgumentParser(prog="label-sheet-ui")
        parser.add_argument("--host", default=host)
        parser.add_argument("--port", type=int, default=port)
        parser.add_argument("--reload", action="store_true")
        args = parser.parse_args(argv)
        host = args.host
        port = args.port
        reload = args.reload

    try:
        import uvicorn
    except ImportError:
        print(
            "error: FastAPI UI dependencies are not installed. Install the UI extras with `pip install -e .[ui]`.",
            file=sys.stderr,
        )
        return 2

    uvicorn.run("label_sheet_generator.web_api:app", host=host, port=port, reload=reload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(argv=sys.argv[1:]))