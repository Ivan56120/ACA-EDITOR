
import os
import sys
import json
import time
import sqlite3
import subprocess
import threading

from pathlib import Path
from flask import Flask, request, jsonify, render_template_string

BASE = Path(__file__).resolve().parent
WORKSPACE = BASE / "workspace"
LOG_DIR = BASE / "logs"

AGENT = BASE / "agent.py"
DB = BASE / "agent.db"
LOG = LOG_DIR / "dashboard-agent.log"
STOP_FILE = BASE / "stop.request"

app = Flask(__name__)

process = None
started_at = None
lock = threading.RLock()


HTML = """
<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ACA Control Center</title>

<style>
* { box-sizing:border-box; }

body {
    margin:0;
    background:#0b1120;
    color:#e5e7eb;
    font-family:Segoe UI,Arial,sans-serif;
}

header {
    padding:22px;
    background:#111827;
    border-bottom:1px solid #263244;
}

h1 { margin:0; color:#60a5fa; }

main {
    max-width:1150px;
    margin:25px auto;
    padding:15px;
}

.grid {
    display:grid;
    grid-template-columns:repeat(auto-fit,minmax(210px,1fr));
    gap:15px;
}

.card {
    background:#172033;
    border:1px solid #2a3850;
    border-radius:12px;
    padding:20px;
    margin-bottom:15px;
}

.value {
    font-size:24px;
    font-weight:bold;
    margin-top:12px;
}

textarea,input {
    width:100%;
    background:#0b1120;
    color:white;
    border:1px solid #40506a;
    border-radius:7px;
    padding:12px;
    margin:8px 0 14px;
}

textarea { min-height:110px; }

button {
    border:0;
    border-radius:7px;
    padding:11px 18px;
    margin:4px;
    cursor:pointer;
    font-weight:bold;
}

.start { background:#16a34a;color:white; }
.stop { background:#dc2626;color:white; }
.resume { background:#2563eb;color:white; }
.refresh { background:#475569;color:white; }

button:hover { opacity:.85; }

#logs {
    background:#080e19;
    color:#a7f3d0;
    padding:15px;
    height:300px;
    overflow:auto;
    white-space:pre-wrap;
    font:13px Consolas,monospace;
    border-radius:8px;
}

.file {
    display:block;
    padding:9px;
    border-bottom:1px solid #334155;
    color:#93c5fd;
    cursor:pointer;
}

#preview {
    white-space:pre-wrap;
    overflow:auto;
    max-height:400px;
    background:#080e19;
    padding:15px;
}

.muted { color:#94a3b8; }
</style>
</head>

<body>

<header>
    <h1>ACA CONTROL CENTER</h1>
    <div class="muted">Autonomous Coding Agent | Local AI</div>
</header>

<main>

<div class="grid">

    <div class="card">
        <div class="muted">Agent status</div>
        <div class="value" id="status">CHECKING</div>
    </div>

    <div class="card">
        <div class="muted">Runtime</div>
        <div class="value" id="runtime">00:00:00</div>
    </div>

    <div class="card">
        <div class="muted">AI Model</div>
        <div class="value">Qwen3-Coder</div>
    </div>

    <div class="card">
        <div class="muted">AI Server</div>
        <div class="value" id="ollama">CHECKING</div>
    </div>

</div>

<div class="card">

    <h2>Project instructions</h2>

    <textarea id="objective"
    placeholder="Describe the software you want ACA to build..."></textarea>

    <label>Development time (hours)</label>

    <input id="hours" type="number" min="0.1"
    max="24" value="10" step="0.5">

    <button class="start" onclick="startAgent()">START AGENT</button>
    <button class="stop" onclick="stopAgent()">STOP AGENT</button>
    <button class="resume" onclick="resumeAgent()">RESUME AGENT</button>

    <p id="message" class="muted"></p>

</div>

<div class="card">
    <h2>Development activity</h2>
    <pre id="logs">Waiting for agent...</pre>
    <button class="refresh" onclick="refreshAll()">Refresh</button>
</div>

<div class="grid">

    <div class="card">
        <h2>Project files</h2>
        <div id="files">Loading...</div>
    </div>

    <div class="card">
        <h2>File preview</h2>
        <pre id="preview">Select a file to inspect it.</pre>
    </div>

</div>

</main>

<script>

async function api(url, options={}) {
    const response = await fetch(url, options);
    const data = await response.json();

    if (!response.ok) {
        throw new Error(data.error || "Request failed");
    }

    return data;
}

function message(text) {
    document.getElementById("message").textContent = text;
}

async function startAgent() {
    try {
        const objective = document.getElementById("objective").value;
        const hours = document.getElementById("hours").value;

        const data = await api("/api/start", {
            method:"POST",
            headers:{"Content-Type":"application/json"},
            body:JSON.stringify({objective,hours})
        });

        message(data.message);
        refreshAll();
    } catch(e) { message(e.message); }
}

async function stopAgent() {
    try {
        const data = await api("/api/stop", {method:"POST"});
        message(data.message);
        refreshAll();
    } catch(e) { message(e.message); }
}

async function resumeAgent() {
    try {
        const data = await api("/api/resume", {method:"POST"});
        message(data.message);
        refreshAll();
    } catch(e) { message(e.message); }
}

async function getStatus() {
    try {
        const data = await api("/api/status");

        document.getElementById("status").textContent = data.status;
        document.getElementById("runtime").textContent = data.runtime;
        document.getElementById("ollama").textContent = data.ollama;

    } catch(e) {
        document.getElementById("status").textContent = "ERROR";
    }
}

async function getLogs() {
    try {
        const data = await api("/api/logs");
        const box = document.getElementById("logs");

        const wasAtBottom =
            box.scrollTop + box.clientHeight >= box.scrollHeight - 30;

        box.textContent = data.logs;

        if (wasAtBottom) box.scrollTop = box.scrollHeight;

    } catch(e) {}
}

async function getFiles() {
    try {
        const data = await api("/api/files");
        const box = document.getElementById("files");

        box.innerHTML = "";

        data.files.forEach(file => {
            const element = document.createElement("div");
            element.className = "file";
            element.textContent = "📄 " + file;
            element.onclick = () => previewFile(file);
            box.appendChild(element);
        });

    } catch(e) {}
}

async function previewFile(file) {
    try {
        const data = await api("/api/file?path=" + encodeURIComponent(file));
        document.getElementById("preview").textContent = data.content;
    } catch(e) {
        message(e.message);
    }
}

async function refreshAll() {
    await Promise.all([getStatus(),getLogs(),getFiles()]);
}

refreshAll();
setInterval(refreshAll,3000);

</script>

</body>
</html>
"""


def ollama_status():
    try:
        import urllib.request

        with urllib.request.urlopen(
            "http://127.0.0.1:11434/api/tags",
            timeout=3
        ) as response:
            return "ONLINE" if response.status == 200 else "OFFLINE"

    except Exception:
        return "OFFLINE"


def database_value(key, default=""):
    if not DB.exists():
        return default

    try:
        with sqlite3.connect(
            f"file:{DB.as_posix()}?mode=ro",
            uri=True,
            timeout=2
        ) as connection:
            row = connection.execute(
                "SELECT value FROM state WHERE key=?",
                (key,)
            ).fetchone()

            return row[0] if row else default

    except sqlite3.Error:
        return default


def runtime():
    if started_at is None:
        return "00:00:00"

    seconds = int(time.monotonic() - started_at)

    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)

    return f"{hours:02}:{minutes:02}:{seconds:02}"


def launch_agent(args):
    global process, started_at

    if not AGENT.exists():
        raise RuntimeError("agent.py was not found.")

    if ollama_status() != "ONLINE":
        raise RuntimeError("Ollama is offline. Start Ollama first.")

    with lock:
        if process is not None and process.poll() is None:
            raise RuntimeError("Agent is already running.")

        STOP_FILE.unlink(missing_ok=True)

        LOG_DIR.mkdir(exist_ok=True)

        log_handle = open(LOG, "a", encoding="utf-8")

        try:
            process = subprocess.Popen(
                [sys.executable, "-u", str(AGENT)] + args,
                cwd=str(BASE),
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                shell=False
            )
        finally:
            log_handle.close()

        started_at = time.monotonic()

        return process.pid


@app.route("/")
def home():
    return render_template_string(HTML)


@app.route("/api/start", methods=["POST"])
def start():
    data = request.get_json(silent=True) or {}

    objective = data.get("objective", "").strip()

    try:
        hours = float(data.get("hours", 10))
    except (ValueError, TypeError):
        return jsonify(error="Invalid runtime."), 400

    if not objective:
        return jsonify(error="Enter a project objective."), 400

    if not 0 < hours <= 24:
        return jsonify(error="Runtime must be between 0 and 24 hours."), 400

    try:
        pid = launch_agent([objective, "--hours", str(hours)])
        return jsonify(message=f"Agent started. PID: {pid}")

    except Exception as e:
        return jsonify(error=str(e)), 400


@app.route("/api/resume", methods=["POST"])
def resume():
    if not database_value("objective"):
        return jsonify(error="No saved project objective exists."), 400

    try:
        pid = launch_agent(["--resume", "--hours", "10"])
        return jsonify(message=f"Agent resumed. PID: {pid}")

    except Exception as e:
        return jsonify(error=str(e)), 400


@app.route("/api/stop", methods=["POST"])
def stop():
    if process is None or process.poll() is not None:
        return jsonify(error="No active agent process."), 400

    STOP_FILE.write_text("stop", encoding="utf-8")

    return jsonify(
        message="Stop requested. Agent will stop after its current AI operation."
    )


@app.route("/api/status")
def status():
    if process is not None and process.poll() is None:
        state = "RUNNING"
    else:
        state = database_value("status", "READY")

    return jsonify(
        status=state,
        runtime=runtime(),
        ollama=ollama_status()
    )


@app.route("/api/logs")
def logs():
    content = ""

    if LOG.exists():
        with open(LOG, "r", encoding="utf-8", errors="replace") as f:
            f.seek(max(0, LOG.stat().st_size - 30000))
            content = f.read()

    return jsonify(logs=content)


@app.route("/api/files")
def files():
    result = []

    for path in WORKSPACE.rglob("*"):
        if not path.is_file():
            continue

        if any(part in {".git", "__pycache__", ".venv"}
               for part in path.relative_to(WORKSPACE).parts):
            continue

        result.append(str(path.relative_to(WORKSPACE)))

        if len(result) >= 200:
            break

    return jsonify(files=result)


@app.route("/api/file")
def file_preview():
    relative = request.args.get("path", "")

    target = (WORKSPACE / relative).resolve()

    try:
        target.relative_to(WORKSPACE.resolve())
    except ValueError:
        return jsonify(error="Access denied."), 403

    if not target.is_file():
        return jsonify(error="File not found."), 404

    if target.stat().st_size > 200000:
        return jsonify(error="File too large."), 400

    try:
        content = target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return jsonify(error="Cannot preview binary files."), 400

    return jsonify(content=content)


if __name__ == "__main__":
    print("ACA Dashboard: http://127.0.0.1:5000")

    app.run(
        host="127.0.0.1",
        port=5000,
        debug=False,
        use_reloader=False
    )
