"""Entry point for running the FastAPI server.

Usage:
    python -m src.api                    # Default: host=0.0.0.0, port=8080
    python -m src.api --port 9000        # Custom port
    podwash                       # Via entry point
"""

import argparse
import os

import uvicorn


def main() -> None:
    """Start the FastAPI server with uvicorn."""
    parser = argparse.ArgumentParser(description="Start podwash server")
    parser.add_argument(
        "--host",
        default=os.getenv("HOST", "0.0.0.0"),
        help="Host to bind to (default: 0.0.0.0)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("PORT", "8080")),
        help="Port to bind to (default: 8080)",
    )
    parser.add_argument(
        "--reload",
        action="store_true",
        help="Enable auto-reload for development",
    )
    args = parser.parse_args()

    print(f"Starting podwash on {args.host}:{args.port}")

    uvicorn.run(
        "src.api.app:create_app",
        factory=True,
        host=args.host,
        port=args.port,
        reload=args.reload,
    )


if __name__ == "__main__":
    main()
