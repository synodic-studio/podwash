"""Worker CLI entrypoint — installed as `podwash-worker`."""

import argparse
import asyncio
import os
import socket
import sys

from src.config import load_settings
from src.worker.loop import run_forever
from src.worker.preflight import run_preflight


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="podwash-worker",
        description="Process podwash jobs claimed from the Vultr queue API.",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="Path to config.yml (default: $CONFIG_PATH or ./config.yml)",
    )
    parser.add_argument(
        "--worker-id",
        default=os.getenv("WORKER_ID", socket.gethostname()),
        help="Identifier recorded on claimed episodes (default: hostname)",
    )
    parser.add_argument(
        "--skip-preflight",
        action="store_true",
        help="Skip the startup health checks (only for local debugging).",
    )
    args = parser.parse_args()

    settings = load_settings(args.config)

    if not args.skip_preflight:
        if not run_preflight(settings, args.worker_id):
            print("[worker] Preflight failed — exiting 1", file=sys.stderr)
            sys.exit(1)

    try:
        asyncio.run(run_forever(settings, args.worker_id))
    except KeyboardInterrupt:
        print("[worker] Shutting down")
        sys.exit(0)


if __name__ == "__main__":
    main()
