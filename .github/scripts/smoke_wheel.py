"""Post-install smoke test: prove the wheel shipped its data and frontend.

Runs against a real uvicorn server over a real socket, using only the standard
library, so it needs nothing beyond the ``[web]`` extra the deployment uses.
Starting the actual server also exercises the lifespan, the middleware stack
and the static mount, none of which an in-process test client fully covers.

Run this from a directory that is NOT the source checkout, so any accidental
reliance on the repo layout fails here rather than in production.
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

HOST = "127.0.0.1"
STARTUP_TIMEOUT_S = 45.0


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind((HOST, 0))
        return int(sock.getsockname()[1])


def get(url: str, timeout: float = 10.0) -> tuple[int, bytes, dict[str, str]]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310
            return response.status, response.read(), dict(response.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers)


def post_json(url: str, payload: dict) -> tuple[int, bytes, dict[str, str]]:
    request = urllib.request.Request(  # noqa: S310
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
            return response.status, response.read(), dict(response.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers)


def wait_until_live(base: str, process: subprocess.Popen) -> None:
    deadline = time.monotonic() + STARTUP_TIMEOUT_S
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise SystemExit(f"server exited early with code {process.returncode}")
        try:
            if get(f"{base}/api/livez", timeout=2)[0] == 200:
                return
        except OSError:
            pass
        time.sleep(0.5)
    raise SystemExit("server did not become live in time")


def main() -> int:
    port = free_port()
    base = f"http://{HOST}:{port}"
    process = subprocess.Popen(  # noqa: S603
        [
            sys.executable,
            "-m",
            "uvicorn",
            "label_sheet_generator.api.app:create_app",
            "--factory",
            "--host",
            HOST,
            "--port",
            str(port),
        ],
    )
    try:
        wait_until_live(base, process)

        status, body, headers = get(f"{base}/")
        assert status == 200, f"frontend did not ship in the wheel ({status})"
        assert b"<html" in body.lower(), "index.html is not HTML"
        assert "content-security-policy" in {k.lower() for k in headers}, "CSP missing"

        for asset in ("/static/app.css", "/static/app.js"):
            assert get(f"{base}{asset}")[0] == 200, f"{asset} did not ship"

        status, body, _ = get(f"{base}/api/bootstrap")
        assert status == 200, f"bootstrap failed ({status})"
        payload = json.loads(body)
        assert payload["templates"], "no templates shipped in the wheel"
        assert not payload["broken"], f"templates failed to load: {payload['broken']}"

        first = payload["templates"][0]["id"]
        status, body, headers = post_json(
            f"{base}/api/render/pdf", {"template_id": first, "document": ""}
        )
        assert status == 200, f"render failed ({status}): {body[:200]!r}"
        assert body.startswith(b"%PDF"), "response was not a PDF"

        status, _, _ = get(f"{base}/api/readyz")
        assert status == 200, f"readiness failed ({status})"
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover
            process.kill()

    print("wheel smoke test passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
