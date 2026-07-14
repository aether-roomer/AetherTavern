"""Server launcher with automatic port fallback.

When the requested port is already in use we count up to find a free one
(capped at 100 attempts) and print a message so it's obvious that we
moved off the default. Pass ``--no-evade-used-port`` to disable the
fallback and let the bind fail loudly instead.
"""
from __future__ import annotations

import argparse
import os
import socket
import sys

import uvicorn

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
MAX_PORT_FALLBACK_ATTEMPTS = 100


def _try_bind(host: str, port: int) -> None:
    """Briefly bind a socket to (host, port) to confirm the port is free.

    Matches the flags uvicorn sets on its own bind, so success here is a
    strong signal that uvicorn's bind will succeed too. There's a tiny
    race window between our close and uvicorn's rebind, but for a
    single-user localhost server that's never a real problem.
    """
    family = socket.AF_INET6 if host and ":" in host else socket.AF_INET
    with socket.socket(family=family) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))


def _find_free_port(host: str, start_port: int) -> int:
    last_error: OSError | None = None
    for offset in range(MAX_PORT_FALLBACK_ATTEMPTS):
        port = start_port + offset
        try:
            _try_bind(host, port)
        except OSError as exc:
            last_error = exc
            continue
        return port
    raise SystemExit(
        f"Could not find a free port in "
        f"[{start_port}, {start_port + MAX_PORT_FALLBACK_ATTEMPTS}); "
        f"last error: {last_error}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m server",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("AETHER_HOST", DEFAULT_HOST),
        help=f"Bind host (default: {DEFAULT_HOST}, env: AETHER_HOST).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("AETHER_PORT", DEFAULT_PORT)),
        help=f"Initial bind port (default: {DEFAULT_PORT}, env: AETHER_PORT).",
    )
    parser.add_argument(
        "--no-evade-used-port",
        action="store_true",
        help="Disable the port fallback: fail loudly if --port is already bound.",
    )
    parser.add_argument(
        "--proxy-rules",
        default=os.environ.get("AETHER_PROXY_RULES"),
        metavar="PATH",
        help=(
            "Path to a YAML proxy-rules file. Overrides AETHER_PROXY_RULES and "
            "the auto-detected <data_dir>/proxy_config.yaml. See "
            "CLAUDE.md / README for the rule format."
        ),
    )
    args = parser.parse_args(argv)

    if args.proxy_rules:
        rules_path = os.path.abspath(args.proxy_rules)
        if not os.path.isfile(rules_path):
            raise SystemExit(f"--proxy-rules: file not found: {rules_path}")
        # Hand off to the FastAPI lifespan via env var.
        os.environ["AETHER_PROXY_RULES"] = rules_path

    if args.no_evade_used_port:
        port = args.port
    else:
        port = _find_free_port(args.host, args.port)
        if port != args.port:
            print(
                f"Port {args.port} is in use; falling back to port {port}.",
                flush=True,
            )

    uvicorn.run("server.main:app", host=args.host, port=port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
