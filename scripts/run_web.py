"""Run the PublicDB2 local UI foundation."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8000, type=int)
    args = parser.parse_args()

    import uvicorn

    uvicorn.run(
        "app.main:app", host=args.host, port=args.port, reload=False,
        proxy_headers=True, forwarded_allow_ips="127.0.0.1,::1",
    )


if __name__ == "__main__":
    main()
