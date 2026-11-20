"""Start Saarthi AI locally:  .venv\\Scripts\\python run.py   ->  http://localhost:8080"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

from aiohttp import web  # noqa: E402

from app import main  # noqa: E402

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    application = main.make_app()
    print(f"Saarthi AI on http://localhost:{port} | Gemini: {main.gemini.status()}", flush=True)
    web.run_app(application, port=port, print=None)
