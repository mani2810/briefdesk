"""Start BriefDesk with one command (from the repo root, inside the virtual environment):

    python run.py                 # http://127.0.0.1:8000  (uses backend/.env)
    python run.py --demo          # offline demo: scripted AI + sample web pages, separate database
    python run.py --port 9000 --host 0.0.0.0

The API, the WebSocket chat and the web UI are all served by this one process.
"""
import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def main() -> None:
    ap = argparse.ArgumentParser(description="Run BriefDesk")
    ap.add_argument("--host", default="127.0.0.1", help="interface to bind (default: localhost only)")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--demo", action="store_true", help="offline demo mode (no API key, no internet)")
    ap.add_argument("--reload", action="store_true", help="auto-reload on code changes (development)")
    args = ap.parse_args()

    if args.demo:
        os.environ.update({"LLM_MODE": "fake", "SEARCH_PROVIDER": "fake", "DATABASE_URL": "sqlite:///./demo.db",
                           "CHECKPOINT_DB": "demo_checkpoints.db", "DATA_DIR": "data_demo"})
    sys.path.insert(0, str(ROOT / "backend"))
    os.chdir(ROOT / "backend")

    import uvicorn

    from app.bootstrap import init_db, seed_demo
    from app.db.session import SessionLocal

    init_db()
    with SessionLocal() as db:
        seed_demo(db)
    shown = "localhost" if args.host in ("127.0.0.1", "0.0.0.0") else args.host
    print(f"\n  BriefDesk is running at http://{shown}:{args.port}" + ("   (offline demo mode)" if args.demo else ""))
    uvicorn.run("app.main:app", host=args.host, port=args.port, reload=args.reload, app_dir=str(ROOT / "backend"),
                ws_max_size=64 * 1024, log_level="warning")


if __name__ == "__main__":
    main()
