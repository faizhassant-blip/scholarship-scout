import copy
import ctypes
import json
import logging
import mimetypes
import os
import shutil
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

import scholarship_scout


HOST = "127.0.0.1"
PORT = 8000
STATIC_DIR = Path(__file__).resolve().parent / "static"
SAVED_FILE = Path(__file__).resolve().parent / "data" / "saved_scholarships.json"
SAVED_LOCK = threading.Lock()
STATE_LOCK = threading.Lock()
STATE = {
    "status": "idle",
    "started_at": None,
    "finished_at": None,
    "result": None,
    "error": None,
}
STUDY_LEVELS = {"school", "undergraduate", "masters", "doctoral", "vocational", "any"}
SCHOLARSHIP_TYPES = {
    "any", "need_based", "merit_based", "field_specific",
    "research", "competition", "study_abroad",
}


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def scholarship_key(scholarship):
    source_url = scholarship.get("source_url")
    if isinstance(source_url, str) and source_url.strip():
        return "url:" + source_url.strip().rstrip("/").casefold()
    return "name:" + scholarship["name"].strip().casefold()


def read_saved_scholarships():
    if not SAVED_FILE.exists():
        return {}
    with SAVED_FILE.open("r", encoding="utf-8") as saved_file:
        saved = json.load(saved_file)
    if not isinstance(saved, dict):
        raise ValueError("Saved scholarship data must be a JSON object.")
    return saved


def write_saved_scholarships(saved):
    SAVED_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary_file = SAVED_FILE.with_suffix(SAVED_FILE.suffix + ".tmp")
    try:
        with temporary_file.open("w", encoding="utf-8") as saved_file:
            json.dump(saved, saved_file, ensure_ascii=False, indent=2)
            saved_file.write("\n")
        os.replace(temporary_file, SAVED_FILE)
    except OSError:
        try:
            temporary_file.unlink()
        except FileNotFoundError:
            pass
        raise


def system_metrics():
    disk = shutil.disk_usage(Path(__file__).resolve().parent)
    if os.name != "nt":
        raise OSError("Live memory metrics are currently supported on Windows only.")

    class MemoryStatus(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    memory = MemoryStatus()
    memory.dwLength = ctypes.sizeof(MemoryStatus)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(MemoryStatus)]
    kernel32.GlobalMemoryStatusEx.restype = ctypes.c_bool
    if not kernel32.GlobalMemoryStatusEx(ctypes.byref(memory)):
        raise ctypes.WinError(ctypes.get_last_error())

    return {
        "storage": {
            "total_bytes": disk.total,
            "free_bytes": disk.free,
            "used_percent": round((disk.used / disk.total) * 100, 1) if disk.total else 0,
        },
        "memory": {
            "total_bytes": memory.ullTotalPhys,
            "available_bytes": memory.ullAvailPhys,
            "used_percent": memory.dwMemoryLoad,
        },
    }


def validate_profile(profile):
    if not isinstance(profile, dict):
        raise ValueError("Student profile is required.")
    age = profile.get("age")
    if isinstance(age, bool) or not isinstance(age, int) or not 5 <= age <= 100:
        raise ValueError("Age must be a whole number from 5 to 100.")

    country = profile.get("country")
    if not isinstance(country, str) or not 2 <= len(country.strip()) <= 80:
        raise ValueError("Enter a country name between 2 and 80 characters.")

    study_level = profile.get("study_level")
    if not isinstance(study_level, str) or study_level not in STUDY_LEVELS:
        raise ValueError("Choose a valid study level.")

    grade = profile.get("grade")
    if grade in ("", None):
        grade = None
    elif isinstance(grade, bool) or not isinstance(grade, int) or not 1 <= grade <= 30:
        raise ValueError("Class/year must be a whole number from 1 to 30.")

    field = profile.get("field_of_study", "")
    scholarship_type = profile.get("scholarship_type", "any")
    if not isinstance(field, str) or len(field) > 120:
        raise ValueError("Study field must be 120 characters or fewer.")
    if (
        not isinstance(scholarship_type, str)
        or scholarship_type not in SCHOLARSHIP_TYPES
    ):
        raise ValueError("Choose a valid scholarship type.")

    return {
        "age": age,
        "country": country.strip(),
        "study_level": study_level,
        "grade": grade,
        "field_of_study": field.strip(),
        "scholarship_type": scholarship_type,
    }


def run_scan(profile):
    try:
        result = scholarship_scout.run_pipeline(profile=profile, post_webhook=False)
    except Exception as error:
        logging.exception("Scholarship scan failed")
        with STATE_LOCK:
            STATE.update(
                status="error",
                finished_at=utc_now(),
                result=None,
                error=str(error),
            )
        return

    with STATE_LOCK:
        STATE.update(
            status="complete",
            finished_at=utc_now(),
            result=result,
            error=None,
        )


class Handler(BaseHTTPRequestHandler):
    server_version = "ScholarshipScout/1.0"

    def send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/health":
            return self.send_json({"ok": True})
        if path == "/api/saved":
            try:
                with SAVED_LOCK:
                    saved = read_saved_scholarships()
                scholarships = []
                for entry in saved.values():
                    if not isinstance(entry, dict) or not isinstance(
                        entry.get("scholarship"), dict
                    ):
                        raise ValueError("Saved scholarship data has an invalid entry.")
                    scholarships.append({
                        **entry["scholarship"],
                        "status": entry.get("status", "Interested"),
                        "saved_at": entry.get("saved_at"),
                    })
                return self.send_json({"scholarships": scholarships})
            except (OSError, ValueError) as error:
                logging.exception("Could not read saved scholarships")
                return self.send_json({"error": str(error)}, 500)
        if path == "/api/status":
            with STATE_LOCK:
                status = copy.deepcopy(STATE)
            return self.send_json(status)
        if path == "/api/system":
            try:
                return self.send_json(system_metrics())
            except OSError as error:
                logging.exception("Could not read system metrics")
                return self.send_json({"error": str(error)}, 500)
        if path.startswith("/api/"):
            return self.send_json({"error": "Not found"}, 404)
        self.serve_static(path)

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/save":
            try:
                content_length = int(self.headers.get("Content-Length", "0"))
                if content_length <= 0 or content_length > 65536:
                    return self.send_json({"error": "A valid scholarship is required."}, 400)
                body = json.loads(self.rfile.read(content_length).decode("utf-8"))
                scholarship = body.get("scholarship") if isinstance(body, dict) else None
                if not isinstance(scholarship, dict):
                    return self.send_json({"error": "Scholarship must be an object."}, 400)
                name = scholarship.get("name")
                source_url = scholarship.get("source_url", "")
                if not isinstance(name, str) or not name.strip() or len(name) > 500:
                    return self.send_json({"error": "Scholarship name is required."}, 400)
                if not isinstance(source_url, str) or len(source_url) > 2048:
                    return self.send_json({"error": "Scholarship source URL is invalid."}, 400)
                key = scholarship_key(scholarship)
                with SAVED_LOCK:
                    saved = read_saved_scholarships()
                    if key in saved:
                        del saved[key]
                        is_saved = False
                    else:
                        saved[key] = {
                            "scholarship": scholarship,
                            "status": "Interested",
                            "saved_at": utc_now(),
                        }
                        is_saved = True
                    write_saved_scholarships(saved)
                return self.send_json({"saved": is_saved, "status": "Interested"})
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
                return self.send_json({"error": str(error)}, 400)
            except OSError as error:
                logging.exception("Could not save scholarship")
                return self.send_json({"error": str(error)}, 500)
        if path != "/api/scan":
            return self.send_json({"error": "Not found"}, 404)

        try:
            content_length = int(self.headers.get("Content-Length", "0"))
            if content_length <= 0 or content_length > 8192:
                return self.send_json({"error": "A valid student profile is required."}, 400)
            body = json.loads(self.rfile.read(content_length).decode("utf-8"))
            if not isinstance(body, dict):
                return self.send_json({"error": "Request body must be an object."}, 400)
            profile = validate_profile(body.get("profile"))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            return self.send_json({"error": str(error)}, 400)

        with STATE_LOCK:
            if STATE["status"] == "running":
                return self.send_json(
                    {"error": "A scholarship scan is already running."}, 409,
                )
            STATE.update(
                status="running",
                started_at=utc_now(),
                finished_at=None,
                result=None,
                error=None,
            )

        worker = threading.Thread(target=run_scan, args=(profile,), daemon=True)
        worker.start()
        self.send_json({"status": "running"}, 202)

    def serve_static(self, request_path):
        relative = unquote(request_path).lstrip("/") or "index.html"
        target = (STATIC_DIR / relative).resolve()
        try:
            if os.path.commonpath((str(STATIC_DIR), str(target))) != str(STATIC_DIR):
                return self.send_json({"error": "Not found"}, 404)
        except ValueError:
            return self.send_json({"error": "Not found"}, 404)
        if not target.is_file():
            return self.send_json({"error": "Not found"}, 404)

        body = target.read_bytes()
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in {
            "application/javascript",
            "application/json",
        }:
            content_type += "; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format_string, *args):
        logging.info("%s - %s", self.address_string(), format_string % args)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.daemon_threads = True
    print(f"Scholarship Scout is running at http://{HOST}:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping Scholarship Scout.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
