from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

STREAMLIT_APP_PATH = Path(__file__).with_name("streamlit_app.py")


def main(host: str = "127.0.0.1", port: int = 8501, argv: list[str] | None = None) -> int:
    if argv is not None:
        parser = argparse.ArgumentParser(prog="label-sheet-ui")
        parser.add_argument("--host", default=host)
        parser.add_argument("--port", type=int, default=port)
        args = parser.parse_args(argv)
        host = args.host
        port = args.port

    # Set these before importing Streamlit so config initialization sees them.
    os.environ["STREAMLIT_SERVER_ADDRESS"] = host
    os.environ["STREAMLIT_SERVER_PORT"] = str(port)

    try:
        from streamlit.web import bootstrap
    except ImportError:
        print(
            "error: Streamlit UI dependencies are not installed. Install the UI extras with `pip install -e .[ui]`.",
            file=sys.stderr,
        )
        return 2

    # Use bootstrap.run directly to keep Streamlit in the current process.
    # This avoids debugger sessions exiting when CLI wrappers spawn/detach.
    bootstrap.run(
        str(STREAMLIT_APP_PATH),
        is_hello=False,
        args=[],
        flag_options={},
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(argv=sys.argv[1:]))