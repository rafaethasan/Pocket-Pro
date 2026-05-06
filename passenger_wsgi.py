import os
import sys
import traceback

BASE_DIR = os.path.dirname(__file__)
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

try:
    from app import app as application
except Exception:
    _startup_error = traceback.format_exc()

    def application(environ, start_response):  # type: ignore[override]
        message = "Soft X startup error\n\n" + _startup_error
        start_response(
            "500 Internal Server Error",
            [("Content-Type", "text/plain; charset=utf-8")],
        )
        return [message.encode("utf-8")]
