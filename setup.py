"""שרת הגדרות מקומי — הופך את setup.html לכלי שעובד לבד.

    python setup.py

נפתח דפדפן עם הטופס. הוא נטען עם ההגדרות הקיימות, כותב את config.yaml
ואת .env ישירות לתיקייה, ויודע להריץ ולתזמן — בלי להוריד ולהעביר קבצים.

מאזין רק ל-127.0.0.1, כלומר נגיש אך ורק מהמחשב הזה.
"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
HOST, PORT = "127.0.0.1", 8791
RUN_TIMEOUT = 900


def read_env() -> dict:
    path = ROOT / ".env"
    if not path.exists():
        return {}
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    return values


def read_config() -> dict:
    path = ROOT / "config.yaml"
    if not path.exists():
        return {}
    try:
        import yaml
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001 - קובץ פגום לא מפיל את השרת
        print(f"אזהרה: לא הצלחתי לקרוא את config.yaml ({exc})")
        return {}


def run_tool(args: list[str]) -> dict:
    cmd = [sys.executable, "-m", "src.main", *args]
    try:
        proc = subprocess.run(
            cmd, cwd=ROOT, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=RUN_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "output": "ההרצה לא הסתיימה בזמן."}
    output = (proc.stdout or "") + (proc.stderr or "")
    return {"ok": proc.returncode == 0, "output": output.strip()}


def install_schedule() -> dict:
    script = ROOT / "scripts" / "install_task.ps1"
    if sys.platform != "win32":
        return {"ok": False, "output": "התקנת תזמון אוטומטית נתמכת כרגע בווינדוס בלבד.\n"
                                       "בלינוקס ובמק: crontab -e"}
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=120,
    )
    output = (proc.stdout or "") + (proc.stderr or "")
    return {"ok": proc.returncode == 0, "output": output.strip()}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # שקט — הפלט המעניין הוא של הכלי עצמו

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: dict, code: int = 200) -> None:
        self._send(code, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except json.JSONDecodeError:
            return {}

    def do_GET(self):
        if self.path in ("/", "/setup.html"):
            html = (ROOT / "setup.html").read_bytes()
            self._send(200, html, "text/html; charset=utf-8")
        elif self.path == "/api/state":
            self._json({
                "server": True,
                "config": read_config(),
                "env": read_env(),
                "has_config": (ROOT / "config.yaml").exists(),
            })
        else:
            self._send(404, b"not found", "text/plain; charset=utf-8")

    def do_POST(self):
        data = self._body()

        if self.path == "/api/save":
            yaml_text = data.get("yaml", "")
            env_text = data.get("env", "")
            if not yaml_text.strip():
                self._json({"ok": False, "output": "לא התקבל תוכן ל-config.yaml"}, 400)
                return
            (ROOT / "config.yaml").write_text(yaml_text, encoding="utf-8")
            written = ["config.yaml"]
            if env_text.strip():
                (ROOT / ".env").write_text(env_text, encoding="utf-8")
                written.append(".env")
            print("נשמר:", ", ".join(written))
            self._json({"ok": True, "output": "נשמרו: " + ", ".join(written)})

        elif self.path == "/api/run":
            args = ["--force"]
            if data.get("dry"):
                args.append("--dry-run")
            print("מריץ", " ".join(args), "...")
            self._json(run_tool(args))

        elif self.path == "/api/schedule":
            print("מתקין תזמון ...")
            self._json(install_schedule())

        else:
            self._json({"ok": False, "output": "נתיב לא מוכר"}, 404)


def main() -> int:
    if not (ROOT / "setup.html").exists():
        print("לא נמצא setup.html ליד setup.py")
        return 1

    server = ThreadingHTTPServer((HOST, PORT), Handler)
    url = f"http://{HOST}:{PORT}/"
    print(f"טופס ההגדרות פתוח בכתובת {url}")
    print("לסיום: Ctrl+C\n")
    threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nהשרת נסגר.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
