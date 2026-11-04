"""One click: start Saarthi on this laptop with a PUBLIC HTTPS link for phones and the office.

    Windows: double-click start_public.bat      Mac/Linux: ./start_public.sh

1. checks .env, installs Python packages
2. downloads cloudflared (free Cloudflare quick tunnel, no account) if needed
3. opens the tunnel -> https://<random>.trycloudflare.com
4. creates the demo fleets on first run (synthetic) and prints every link + QR codes
5. runs Saarthi (Ctrl+C to stop). The link changes each time you restart.
"""
from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent.parent
BIN = ROOT / "bin"
PORT = os.getenv("HTTP_PORT", "8080")


def step(msg: str) -> None:
    print(f"\n=== {msg}")


def ensure_env() -> None:
    env = ROOT / ".env"
    if not env.exists():
        shutil.copy(ROOT / ".env.example", env)
        print("Created .env from .env.example. Open it, add GNANI_API_KEY, LLM_API_KEY (and DRIVER_BOT_TOKEN), then run again.")
        sys.exit(1)
    text = env.read_text(encoding="utf-8")
    missing = [k for k in ("GNANI_API_KEY", "LLM_API_KEY") if re.search(rf"^{k}=(your_|\s*$)", text, re.M)]
    if missing:
        print(f"Please fill {', '.join(missing)} in .env first.")
        sys.exit(1)
    if re.search(r"^API_SHARED_SECRET=change_me", text, re.M):
        import secrets
        key = secrets.token_urlsafe(8)
        env.write_text(re.sub(r"^API_SHARED_SECRET=change_me", f"API_SHARED_SECRET={key}", text, flags=re.M), encoding="utf-8")
        print(f"Set a random office key in .env: {key}")


def cloudflared() -> str:
    found = shutil.which("cloudflared")
    if found:
        return found
    system, machine = platform.system().lower(), platform.machine().lower()
    arch = "arm64" if machine in ("arm64", "aarch64") else "amd64"
    if system == "windows":
        name, url = "cloudflared.exe", f"https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-{arch}.exe"
    elif system == "linux":
        name, url = "cloudflared", f"https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-{arch}"
    else:
        print("On macOS install it once with:  brew install cloudflared")
        sys.exit(1)
    path = BIN / name
    if not path.exists():
        BIN.mkdir(exist_ok=True)
        print(f"Downloading cloudflared from {url}")
        urllib.request.urlretrieve(url, path)
        path.chmod(0o755)
    return str(path)


def open_tunnel(exe: str) -> tuple[subprocess.Popen, str]:
    proc = subprocess.Popen([exe, "tunnel", "--no-autoupdate", "--url", f"http://localhost:{PORT}"],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    deadline = time.time() + 60
    while time.time() < deadline:
        line = proc.stdout.readline()
        m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", line or "")
        if m:
            import threading
            # keep reading cloudflared's log so its output pipe never fills up and blocks the tunnel
            threading.Thread(target=lambda: [None for _ in proc.stdout], daemon=True).start()
            return proc, m.group(0)
    proc.kill()
    print("Could not open the tunnel (check your internet).")
    sys.exit(1)


def qr(text: str) -> None:
    try:
        import qrcode
        q = qrcode.QRCode(border=1)
        q.add_data(text)
        q.print_ascii(invert=True)
    except Exception:
        pass


def main() -> None:
    os.chdir(ROOT)
    step("Checking settings")
    ensure_env()
    step("Installing Python packages (first run takes a few minutes)")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "-r", "requirements.txt", "qrcode"])
    step("Opening a public HTTPS link (Cloudflare quick tunnel)")
    tunnel, url = open_tunnel(cloudflared())
    os.environ["PUBLIC_BASE_URL"] = url
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    os.environ["PUBLIC_BASE_URL"] = url
    db_path = os.getenv("DB_PATH", "data/saarthi.db")
    if not Path(db_path).exists():
        step("Creating demo fleets (synthetic)")
        subprocess.check_call([sys.executable, "-m", "saarthi.seed", "--demo"], env=os.environ)
    import sqlite3
    con = sqlite3.connect(db_path)
    step("LINKS — share these")
    print(f"\nOWNER / OFFICE (laptop browser):  {url}/office")
    print(f"   office key: {os.getenv('API_SHARED_SECRET')}")
    for code, reg, token in con.execute("SELECT code, reg_label, access_token FROM vehicles ORDER BY code"):
        link = f"{url}/driver?v={code}&k={token}"
        print(f"\nDRIVER {reg} ({code})")
        print(f"   phone browser: {link}")
        print(f"   Android app setup: saarthi://setup?url={quote(link, safe='')}")
    first = con.execute("SELECT code, access_token FROM vehicles ORDER BY code LIMIT 1").fetchone()
    if first:
        print("\nScan with the first driver's phone:")
        qr(f"{url}/driver?v={first[0]}&k={first[1]}")
    for fcode, in con.execute("SELECT code FROM fleets"):
        print(f"Telegram (owner): send  /start OWNER-{fcode}  to your bot")
    con.close()
    step("Starting Saarthi (Ctrl+C to stop)")
    try:
        subprocess.call([sys.executable, "-m", "saarthi.main"], env=os.environ)
    finally:
        tunnel.terminate()


if __name__ == "__main__":
    main()
