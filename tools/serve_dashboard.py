#!/usr/bin/env python3
"""Serve the AMI lab dashboard LOCALLY with live auto-refresh — no Claude needed.

The dashboard already ships a poll hook: if window.LAB_DATA_URL is set it fetches
that snapshot on an interval and repaints. This script injects that hook into a
copy of the page (window.LAB_DATA_URL -> tools/lab_e2e_snapshot.json), serves the
tools/ folder over http, and opens a browser. The scheduled lab_refresh.py job
keeps the snapshot fresh; this page just re-reads it every --poll seconds.

  python tools/serve_dashboard.py [--port 8770] [--poll 5]

Ctrl-C stops the server. It does NOT touch the refresh task.
"""
import argparse
import functools
import http.server
import pathlib
import re
import socketserver
import webbrowser

HERE = pathlib.Path(__file__).resolve().parent


def build_served(poll_ms: int) -> str:
    tpl = (HERE / "lab_dashboard.html").read_text(encoding="utf-8")
    hook = ('<script>window.LAB_DATA_URL="lab_e2e_snapshot.json";'
            f'window.LAB_POLL_MS={poll_ms};</script>\n')
    out, n = re.subn(r'(<script>\s*\n\(function \(\) \{)', hook + r'\1', tpl, count=1)
    if n != 1:
        raise SystemExit("could not find the main <script> to inject the poll hook")
    dst = HERE / "lab_dashboard_served.html"
    dst.write_text(out, encoding="utf-8")
    return dst.name


class Server(socketserver.TCPServer):
    allow_reuse_address = True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8770)
    ap.add_argument("--poll", type=int, default=5, help="browser refresh seconds")
    args = ap.parse_args()

    page = build_served(args.poll * 1000)
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(HERE))
    url = f"http://localhost:{args.port}/{page}"
    with Server(("127.0.0.1", args.port), handler) as httpd:
        print(f"serving {HERE}\n  -> {url}  (browser polls every {args.poll}s; Ctrl-C to stop)")
        try:
            webbrowser.open(url)
        except Exception:
            pass
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nstopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
