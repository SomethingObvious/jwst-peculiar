"""The local server behind `view`, which serves a list and saves its verdicts."""

from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import mimetypes
from pathlib import Path
import sys
import threading
from typing import TYPE_CHECKING, Any, ClassVar
from urllib.parse import unquote, urlsplit
import webbrowser

from typing_extensions import override

from jwst_peculiar.verdicts import save_verdict

if TYPE_CHECKING:
    from jwst_peculiar.view import ObjectBundle

APP_DIR = Path(__file__).resolve().parent / "viewer_static"
logger = logging.getLogger(__name__)


def built_viewer() -> Path:
    """Return the folder of the built viewer.

    Returns:
        APP_DIR.

    Raises:
        SystemExit: If the viewer hasn't been built.
    """
    if not (APP_DIR / "index.html").is_file():
        msg = (
            f"The viewer isn't built, as {APP_DIR / 'index.html'} is missing. "
            "Run `npm ci && npm run build` in web/."
        )
        raise SystemExit(msg)
    return APP_DIR


def serve(bundle: ObjectBundle, *, port: int, browser: bool) -> None:
    """Serve `bundle` and the viewer on localhost until Ctrl+C."""
    server = open_server(bundle, port)
    url = f"http://127.0.0.1:{server.server_port}/"
    logger.info("Serving %d objects at %s, Ctrl+C to stop", len(bundle.tiles), url)
    if browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def open_server(bundle: ObjectBundle, port: int) -> Server:
    """Bind the viewer to `port` on localhost, or to a free port if it's taken."""
    handler = make_handler(bundle)
    try:
        return Server(("127.0.0.1", port), handler)
    except OSError:
        logger.warning("Port %d is taken, so the viewer is using a free one", port)
        return Server(("127.0.0.1", 0), handler)


class Server(ThreadingHTTPServer):
    """A server that won't share its port with one already running."""

    # Windows lets a second socket bind a port that has SO_REUSEADDR set, so a
    # second viewer would quietly share the first one's port.
    allow_reuse_address = sys.platform != "win32"


def make_handler(bundle: ObjectBundle) -> type[BaseHTTPRequestHandler]:
    """Return a request handler class that serves `bundle` and the viewer."""

    class Handler(_ViewerHandler):
        pass

    Handler.bundle = bundle
    Handler.lock = threading.Lock()
    return Handler


class _ViewerHandler(BaseHTTPRequestHandler):
    bundle: ClassVar[ObjectBundle]
    lock: ClassVar[threading.Lock]
    # Seconds a request may stall, so a body shorter than its Content-Length can't
    # hold a thread forever.
    timeout: ClassVar[float | None] = 10

    @override
    def log_message(self, format: str, *args: Any) -> None:
        logger.debug(format, *args)

    def _send(
        self, status: int, body: bytes, ctype: str, *, cache: bool = False
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "max-age=3600" if cache else "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, message: str) -> None:
        self._send(status, json.dumps({"error": message}).encode(), "application/json")

    def _from_elsewhere(self) -> bool:
        """Tell a request from the viewer's own page from one sent by another site.

        A browser lets any page send requests to localhost, and a hostile one can
        point its own domain here to read the answers. Neither can fake these headers.

        Returns:
            True when the Host or Origin isn't this server, or the browser says the
            request came from another site.
        """
        port = self.connection.getsockname()[1]
        here = {f"127.0.0.1:{port}", f"localhost:{port}"}
        origin = self.headers.get("Origin")
        return (
            self.headers.get("Host") not in here
            or (origin is not None and origin.removeprefix("http://") not in here)
            # A cross-site GET carries no Origin, but a current browser marks it here.
            or self.headers.get("Sec-Fetch-Site") == "cross-site"
        )

    def _read(self, path: str) -> tuple[bytes, str, bool]:
        # A backslash or a drive would let a Windows path climb out of the folders.
        if "\\" in path or ":" in path:
            raise FileNotFoundError(path)
        if path.startswith("run/"):
            # List files are named by rank, so another list on this port reuses the
            # names and a cached copy would show the wrong object.
            return (*self.bundle.read(path[4:]), False)
        target = (APP_DIR / path).resolve()
        if not target.is_relative_to(APP_DIR) or not target.is_file():
            raise FileNotFoundError(path)
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        return target.read_bytes(), ctype, path != "index.html"

    def do_GET(self) -> None:
        """Serve a list file under run/, or a file of the built viewer otherwise."""
        if self._from_elsewhere():
            self._error(HTTPStatus.FORBIDDEN, "This viewer only answers its own page")
            return
        path = unquote(urlsplit(self.path).path).lstrip("/") or "index.html"
        try:
            body, ctype, cache = self._read(path)
        except (OSError, ValueError):
            self._error(HTTPStatus.NOT_FOUND, f"There is nothing at {path}")
            return
        except Exception as exc:
            # A hand-edited verdicts.csv, for one. The page shows this instead of a
            # dropped connection.
            logger.exception("Couldn't read %s", path)
            self._error(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                f"Couldn't read {path}: {type(exc).__name__}: {exc}",
            )
            return
        self._send(HTTPStatus.OK, body, ctype, cache=cache)

    def do_POST(self) -> None:
        """Save the verdict posted to run/api/verdict."""
        if self._from_elsewhere():
            self._error(HTTPStatus.FORBIDDEN, "This viewer only answers its own page")
            return
        if urlsplit(self.path).path != "/run/api/verdict":
            self._error(HTTPStatus.NOT_FOUND, "Only run/api/verdict takes writes")
            return
        try:
            saved = self._save_posted_verdict()
        except (TimeoutError, ValueError, KeyError, TypeError) as exc:
            # A timeout is a body that stopped short of its Content-Length.
            self._error(HTTPStatus.BAD_REQUEST, str(exc))
            return
        except OSError as exc:
            # verdicts.csv open in a spreadsheet, for one. The viewer tries again.
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, f"Couldn't save it: {exc}")
            return
        self._send(
            HTTPStatus.OK, json.dumps({"saved": saved}).encode(), "application/json"
        )

    def _save_posted_verdict(self) -> str:
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(max(0, min(length, 1 << 20))))
        key = str(body["key"])
        if key not in {t.key for t in self.bundle.tiles}:
            msg = f"{key} isn't in this list"
            raise KeyError(msg)
        star = body.get("star", False)
        if not isinstance(star, bool):
            msg = f"star has to be true or false, not {star!r}"
            raise TypeError(msg)
        with self.lock:
            return save_verdict(
                self.bundle.run_dir,
                key,
                body.get("verdict"),
                star=star,
                note=str(body.get("note") or ""),
            )
