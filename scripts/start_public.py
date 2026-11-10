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


def ensure_env() -> list[str]:
    """Creates .env if needed, sets a permanent office key + link secret. Returns the keys still missing."""
    import secrets
    env = ROOT / ".env"
    if not env.exists():
        shutil.copy(ROOT / ".env.example", env)
        print("Created .env from .env.example.")
    text = env.read_text(encoding="utf-8")
    if re.search(r"^API_SHARED_SECRET=(change_me)?\s*$", text, re.M):
        text = re.sub(r"^API_SHARED_SECRET=.*$", f"API_SHARED_SECRET={secrets.token_urlsafe(8)}", text, flags=re.M)
    elif not re.search(r"^API_SHARED_SECRET=", text, re.M):
        text += f"\nAPI_SHARED_SECRET={secrets.token_urlsafe(8)}\n"
    if not re.search(r"^LINK_SECRET=\S", text, re.M):   # keeps every driver link the same between restarts
        text = re.sub(r"^LINK_SECRET=.*\n?", "", text, flags=re.M).rstrip("\n") + f"\nLINK_SECRET={secrets.token_urlsafe(12)}\n"
    env.write_text(text, encoding="utf-8")
    missing = [k for k in ("GNANI_API_KEY", "LLM_API_KEY", "CF_TURN_KEY_ID", "CF_TURN_API_TOKEN")
               if not re.search(rf"^{k}=(?!your_)\S+", text, re.M)]
    return missing


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
    missing = ensure_env()
    step("Installing Python packages (first run takes a few minutes)")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "--disable-pip-version-check",
                           "-r", "requirements.txt", "qrcode"])
    step("Opening a public HTTPS link (Cloudflare quick tunnel)")
    tunnel, url = open_tunnel(cloudflared())
    os.environ["PUBLIC_BASE_URL"] = url
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    os.environ["PUBLIC_BASE_URL"] = url
    os.environ.setdefault("DEMO_DRIVE", "1")              # SIMULATED demo truck drives NH48 for the video
    os.environ.setdefault("SENTINEL_FOLLOWUP_MIN", "3")   # video speed-up (real use: 30)
    os.environ["PYTHONIOENCODING"] = "utf-8"
    db_path = os.getenv("DB_PATH", "data/saarthi.db")
    step("Creating a fresh demo day (synthetic fleets)")
    if Path(db_path).exists():
        Path(db_path).unlink()
    subprocess.check_call([sys.executable, "-m", "saarthi.seed", "--demo"], env=os.environ,
                          stdout=subprocess.DEVNULL)
    import sqlite3
    con = sqlite3.connect(db_path)
    key = os.getenv("API_SHARED_SECRET")
    lines = [f"CEO AGENT (owner's office):  {url}/office?k={key}", f"ALL LINKS PAGE:              {url}/links?k={key}", ""]
    for code, reg, token in con.execute("SELECT code, reg_label, access_token FROM vehicles ORDER BY code"):
        tag = "  <- SIMULATED demo truck (you play the driver)" if code == "MW-T3" else ""
        lines.append(f"DRIVER AGENT {reg} ({code}): {url}/driver?v={code}&k={token}{tag}")
    con.close()
    (ROOT / "data").mkdir(exist_ok=True)
    (ROOT / "data" / "live_links.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    step("LIVE LINKS (also saved in data\\live_links.txt)")
    print("\n".join(lines))
    print("\nGive the test driver the MW-T1 link (his phone GPS moves that truck on the CEO map).")
    if missing:
        print(f"\n!! Not set in .env yet: {', '.join(missing)}")
        print("   Pages work now; live voice needs GNANI_API_KEY + LLM_API_KEY, mobile data needs the CF_TURN pair.")
    print("\nKeep this window open. Closing it stops the links. The address changes on every restart.")
    try:
        import webbrowser
        webbrowser.open(f"{url}/links?k={key}")
    except Exception:
        pass
    step("Starting Saarthi (Ctrl+C to stop)")
    try:
        subprocess.call([sys.executable, "-m", "saarthi.main", "--allow-missing-keys"], env=os.environ)
    finally:
        tunnel.terminate()


if __name__ == "__main__":
    main()
