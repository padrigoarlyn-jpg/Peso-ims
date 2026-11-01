"""Waitress application entry point behind an HTTPS reverse proxy."""
import os
from waitress import serve
from website import create_app

app = create_app()

if __name__ == "__main__":
    host = os.getenv("BIND_HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "5000"))
    # Only trust forwarding headers from a controlled reverse proxy.
    serve(app, host=host, port=port, threads=int(os.getenv("WEB_THREADS", "8")),
          trusted_proxy=os.getenv("TRUSTED_PROXY", "127.0.0.1"),
          trusted_proxy_headers={"x-forwarded-proto", "x-forwarded-host", "x-forwarded-for"},
          clear_untrusted_proxy_headers=True)
