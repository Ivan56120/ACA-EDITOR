
import os
import sys
import json
import time
import sqlite3
import argparse
import subprocess
import urllib.request
import urllib.error
import re
from pathlib import Path
from datetime import datetime

# ==========================================
# ACA - AUTONOMOUS CODING AGENT v1.0
# ==========================================

BASE = Path(__file__).resolve().parent
WORKSPACE = BASE / "workspace"
LOG_DIR = BASE / "logs"
DB_PATH = BASE / "agent.db"
STOP_FILE = BASE / "stop.request"

OLLAMA_URL = "http://127.0.0.1:11434/api/chat"
MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5-coder:3b")
GIT_EXE = r"C:\Program Files\Git\cmd\git.exe"
MAX_FILE_SIZE = 200_000
RECOVERY_CHECKPOINT_INTERVAL = 1024
RECOVERY_CONTEXT_LIMIT = 10_000
MAX_STEPS = 2000
AI_REQUEST_TIMEOUT = int(os.getenv("ACA_AI_TIMEOUT", "300"))
AI_RETRIES = int(os.getenv("ACA_AI_RETRIES", "2"))
MAX_CONSECUTIVE_MODEL_FAILURES = 3
MAX_IDENTICAL_ACTIONS = 2
MAX_REPEAT_RECOVERIES = 2
MAX_INSPECTION_ACTIONS_WITHOUT_WRITE = 3

WORKSPACE.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)


# ==========================================
# DATABASE AND PERSISTENT MEMORY
# ==========================================

db = sqlite3.connect(DB_PATH)
db.execute("""
CREATE TABLE IF NOT EXISTS state (
    key TEXT PRIMARY KEY,
    value TEXT
)
""")

db.execute("""
CREATE TABLE IF NOT EXISTS history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT,
    action TEXT,
    details TEXT
)
""")

# Structured project memory survives sessions and is supplied to the model.
db.execute("""
CREATE TABLE IF NOT EXISTS project_memory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT NOT NULL,
    memory_key TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(category, memory_key)
)
""")
db.commit()


def save_state(key, value):
    db.execute(
        "INSERT OR REPLACE INTO state VALUES (?, ?)",
        (key, str(value))
    )
    db.commit()


def get_state(key, default=None):
    row = db.execute(
        "SELECT value FROM state WHERE key=?",
        (key,)
    ).fetchone()

    return row[0] if row else default


def save_interrupted_write(path, content, log_event=True):
    """Persist a partial streamed file so a resumed run can finish it."""
    if not path or not content:
        return False

    try:
        safe_path(path)
        if len(content.encode("utf-8")) > MAX_FILE_SIZE:
            if log_event:
                log("INTERRUPTED_WRITE_TOO_LARGE", f"{path}: {len(content)} characters")
            return False

        db.executemany(
            "INSERT OR REPLACE INTO state(key, value) VALUES (?, ?)",
            [
                ("interrupted_write_path", path),
                ("interrupted_write_content", content)
            ]
        )
        db.commit()
        if log_event:
            log("INTERRUPTED_WRITE_SAVED", f"{path}: {len(content)} characters")
        return True
    except Exception as error:
        if log_event:
            log("INTERRUPTED_WRITE_SAVE_ERROR", f"{path}: {error}")
        return False


def get_interrupted_write_context():
    """Build a bounded prompt excerpt for the most recent interrupted write."""
    path = get_state("interrupted_write_path", "") or ""
    content = get_state("interrupted_write_content", "") or ""
    if not path or not content:
        return None

    truncated = len(content) > RECOVERY_CONTEXT_LIMIT
    if truncated:
        half = RECOVERY_CONTEXT_LIMIT // 2
        excerpt = (
            content[:half]
            + "\n\n... middle of saved draft omitted from prompt ...\n\n"
            + content[-half:]
        )
    else:
        excerpt = content

    return {
        "path": path,
        "saved_characters": len(content),
        "excerpt_truncated": truncated,
        "partial_content": excerpt
    }


def clear_interrupted_write():
    db.executemany(
        "INSERT OR REPLACE INTO state(key, value) VALUES (?, ?)",
        [
            ("interrupted_write_path", ""),
            ("interrupted_write_content", "")
        ]
    )
    db.commit()


def save_memory(category, memory_key, content):
    """Create or update one durable project-memory entry."""
    category = str(category).strip().lower()[:40]
    memory_key = str(memory_key).strip()[:160]
    content = str(content).strip()[:6000]

    if not category or not memory_key or not content:
        raise ValueError("Memory category, key and content are required.")

    now = datetime.now().isoformat()
    db.execute(
        """
        INSERT INTO project_memory
            (category, memory_keji
            
            y, content, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(category, memory_key) DO UPDATE SET
            content=excluded.content,
            updated_at=excluded.updated_at
        """,
        (category, memory_key, content, now, now)
    )
    db.commit()
    log("MEMORY_SAVED", f"{category}/{memory_key}")
    return f"Saved project memory: [{category}] {memory_key}"


def get_project_memory(limit=30):
    """Return recent durable memories in a compact, model-readable format."""
    rows = db.execute(
        """
        SELECT category, memory_key, content, updated_at
        FROM project_memory
        ORDER BY updated_at DESC
        LIMIT ?
        """,
        (limit,)
    ).fetchall()

    if not rows:
        return "No project memories have been saved yet."

    return "\n".join(
        f"- [{category}] {key}: {content}"
        for category, key, content, _updated in rows
    )


def log(action, details):
    timestamp = datetime.now().isoformat()

    db.execute(
        "INSERT INTO history(timestamp, action, details) VALUES (?, ?, ?)",
        (timestamp, action, str(details)[:10000])
    )
    db.commit()

    with open(LOG_DIR / "agent.log", "a", encoding="utf-8") as f:
        f.write(f"\n[{timestamp}] {action}: {details}\n")


# ==========================================
# SAFE WORKSPACE MANAGEMENT
# ==========================================

def safe_path(relative_path):
    path = (WORKSPACE / relative_path).resolve()

    try:
        path.relative_to(WORKSPACE.resolve())
    except ValueError:
        raise ValueError("Access outside workspace denied.")

    return path


def list_files():
    files = []

    for path in WORKSPACE.rglob("*"):
        if any(part in {".git", "__pycache__", ".venv"}
               for part in path.parts):
            continue

        if path.is_file():
            files.append(str(path.relative_to(WORKSPACE)))

        if len(files) >= 100:
            break

    return files


def read_file(path):
    target = safe_path(path)

    if not target.is_file():
        return "File does not exist."

    if target.stat().st_size > MAX_FILE_SIZE:
        return "File too large to read."

    return target.read_text(encoding="utf-8")


VSCODE_LIVE_URL = "http://127.0.0.1:8765"


def vscode_live_request(endpoint, payload=None, timeout=30):
    data = json.dumps(payload or {}).encode("utf-8")
    request = urllib.request.Request(
        f"{VSCODE_LIVE_URL}{endpoint}",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        result = json.loads(response.read().decode("utf-8"))
    if not result.get("success", False):
        raise RuntimeError(result.get("error", "VS Code live editor failed."))
    return result


def send_to_vscode(relative_path, content):
    try:
        payload = json.dumps({
            "path": relative_path,
            "content": content
        }).encode("utf-8")
        request = urllib.request.Request(
            f"{VSCODE_LIVE_URL}/stream",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(request, timeout=300) as response:
            result = json.loads(response.read().decode("utf-8"))
        if not result.get("success", False):
            raise RuntimeError(result.get("error", "Unknown VS Code error"))
        return f"VS Code updated: {relative_path}"
    except Exception as e:
        return f"VS Code unavailable; file saved locally. Details: {e}"


def write_file(path, content, vscode_already_streamed=False):
    target = safe_path(path)

    if not isinstance(content, str):
        raise ValueError("File content must be text.")

    if len(content.encode("utf-8")) > MAX_FILE_SIZE:
        raise ValueError("File exceeds maximum allowed size.")

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    log("WRITE", path)

    if vscode_already_streamed:
        vscode_result = f"VS Code live stream completed: {path}"
    else:
        vscode_result = send_to_vscode(path, content)

    log("VSCODE_UPDATE", vscode_result)
    return f"Successfully wrote {path}\n{vscode_result}"


class JsonStringStreamDecoder:
    """Incrementally decode the JSON string value of the top-level content key."""

    def __init__(self):
        self.raw = ""
        self.started = False
        self.ended = False
        self.position = 0
        self.escaped = False
        self.unicode_digits = ""
        self.pending_high_surrogate = None

    def feed(self, fragment):
        self.raw += fragment
        output = []

        if not self.started:
            match = re.search(r'"content"\s*:\s*"', self.raw)
            if not match:
                return ""
            self.started = True
            self.position = match.end()

        while self.position < len(self.raw) and not self.ended:
            char = self.raw[self.position]
            self.position += 1

            if self.unicode_digits:
                self.unicode_digits += char
                if len(self.unicode_digits) == 5:
                    codepoint = int(self.unicode_digits[1:], 16)
                    if 0xD800 <= codepoint <= 0xDBFF:
                        self.pending_high_surrogate = codepoint
                    elif 0xDC00 <= codepoint <= 0xDFFF and self.pending_high_surrogate is not None:
                        combined = 0x10000 + ((self.pending_high_surrogate - 0xD800) << 10) + (codepoint - 0xDC00)
                        output.append(chr(combined))
                        self.pending_high_surrogate = None
                    else:
                        if self.pending_high_surrogate is not None:
                            output.append("\uFFFD")
                            self.pending_high_surrogate = None
                        output.append(chr(codepoint))
                    self.unicode_digits = ""
                continue

            if self.escaped:
                self.escaped = False
                if char == "u":
                    self.unicode_digits = "u"
                else:
                    output.append({
                        '"': '"', "\\": "\\", "/": "/", "b": "\b",
                        "f": "\f", "n": "\n", "r": "\r", "t": "\t"
                    }.get(char, char))
                continue

            if char == "\\":
                self.escaped = True
            elif char == '"':
                self.ended = True
                if self.pending_high_surrogate is not None:
                    output.append("\uFFFD")
                    self.pending_high_surrogate = None
            else:
                output.append(char)

        return "".join(output)


def complete_json_field(raw, field):
    match = re.search(r'"' + re.escape(field) + r'"\s*:\s*', raw)
    if not match:
        return None
    try:
        value, _ = json.JSONDecoder().raw_decode(raw[match.end():])
        return value
    except json.JSONDecodeError:
        return None


# ==========================================
# GIT CHECKPOINTS
# ==========================================


def git_checkpoint(message):
    """Create a Git checkpoint using the verified Git executable."""

    try:
        if not Path(GIT_EXE).is_file():
            return f"Git executable not found: {GIT_EXE}"

        def run_git(args, timeout=30):
            return subprocess.run(
                [GIT_EXE, *args],
                cwd=WORKSPACE,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=True
            )

        # Initialize Git only if the workspace is not already a repository.
        check_repo = subprocess.run(
            [GIT_EXE, "rev-parse", "--is-inside-work-tree"],
            cwd=WORKSPACE,
            capture_output=True,
            text=True,
            timeout=15
        )

        if check_repo.returncode != 0:
            run_git(["init"])

        # Ensure local Git author identity is configured.
        run_git(["config", "--local", "user.name", "ACA Agent"])
        run_git(["config", "--local", "user.email", "aca@localhost"])

        # Stage project changes.
        run_git(["add", "-A"])

        # Check whether there are staged changes.
        result = subprocess.run(
            [GIT_EXE, "diff", "--cached", "--quiet"],
            cwd=WORKSPACE,
            capture_output=True,
            timeout=20
        )

        if result.returncode == 0:
            return "No changes to checkpoint."

        if result.returncode != 1:
            error = result.stderr.decode(errors="replace")
            raise RuntimeError(error or "Git diff failed.")

        # Commit the staged changes.
        commit = run_git(
            ["commit", "-m", message[:100]],
            timeout=60
        )

        log(
            "GIT_CHECKPOINT_SUCCESS",
            commit.stdout.strip()
        )

        return "Git checkpoint saved successfully."

    except subprocess.TimeoutExpired:
        error = "Git checkpoint timed out."
        log("GIT_CHECKPOINT_ERROR", error)
        return error

    except subprocess.CalledProcessError as e:
        error = (
            e.stderr.strip()
            if isinstance(e.stderr, str)
            else str(e.stderr)
        )
        message = f"Git command failed: {error or e}"
        log("GIT_CHECKPOINT_ERROR", message)
        return message

    except Exception as e:
        message = f"Git checkpoint unavailable: {type(e).__name__}: {e}"
        log("GIT_CHECKPOINT_ERROR", message)
        return message

# ==========================================
# APPROVED TEST EXECUTION
# ==========================================

def run_tests():
    """
    Run automated tests when test files exist.
    Otherwise, compile Python files to catch syntax errors without executing them.
    """
    excluded = {".git", "__pycache__", ".venv", "venv"}
    python_files = [
        path for path in WORKSPACE.rglob("*.py")
        if path.is_file() and not any(part in excluded for part in path.parts)
    ]
    test_files = [
        path for path in python_files
        if path.name.startswith("test_") or path.name.endswith("_test.py")
    ]

    try:
        if test_files:
            try:
                import importlib.util
                pytest_available = importlib.util.find_spec("pytest") is not None
            except (ImportError, ValueError):
                pytest_available = False

            command = (
                [sys.executable, "-m", "pytest", "-q"]
                if pytest_available else
                [sys.executable, "-m", "unittest", "discover",
                 "-s", ".", "-p", "test*.py", "-v"]
            )
            result = subprocess.run(
                command, cwd=WORKSPACE, capture_output=True,
                text=True, timeout=180, shell=False
            )
            output = (result.stdout + "\n" + result.stderr).strip()
            return {
                "success": result.returncode == 0,
                "mode": "automated_tests",
                "tests_found": len(test_files),
                "command": " ".join(command),
                "output": output[-12000:] or "Tests completed with no output."
            }

        if not python_files:
            return {
                "success": False, "mode": "syntax_check", "tests_found": 0,
                "output": "No Python files were found to validate."
            }

        failures = []
        for path in python_files:
            result = subprocess.run(
                [sys.executable, "-m", "py_compile", str(path)],
                cwd=WORKSPACE, capture_output=True, text=True,
                timeout=30, shell=False
            )
            if result.returncode != 0:
                details = (result.stdout + "\n" + result.stderr).strip()
                failures.append(
                    f"{path.relative_to(WORKSPACE)}: {details}"
                )

        return {
            "success": not failures,
            "mode": "syntax_check",
            "tests_found": 0,
            "files_checked": len(python_files),
            "output": (
                "Python syntax validation passed for all files."
                if not failures else "\n".join(failures)[-12000:]
            )
        }

    except subprocess.TimeoutExpired:
        return {
            "success": False, "mode": "test_execution",
            "output": "Tests exceeded the 180-second limit."
        }
    except Exception as e:
        return {
            "success": False, "mode": "test_execution",
            "output": f"{type(e).__name__}: {e}"
        }


# ==========================================
# AI MODEL CONNECTION
# ==========================================

SYSTEM_PROMPT = """
You are ACA, an autonomous software development agent.

Your responsibility is to build software from the user's objective.

You must work independently and systematically.

RULES:

1. Inspect existing project files before editing.
2. Divide complex objectives into manageable tasks.
3. Write complete, functional code.
4. Do not overwrite files unnecessarily.
5. Test your work.
6. Analyse errors and correct them.
7. Keep track of completed and remaining tasks.
8. Never claim a feature works without testing it.
9. Work only within the supplied workspace.
10. Do not request unrestricted shell access.
11. Save durable project facts using the remember action.
12. Store important requirements, architecture decisions, completed milestones,
    outstanding tasks, and debugging lessons as separate concise memories.
13. Before repeating work, use recall to inspect saved project memory.
14. Update an existing memory key when its information changes; do not create
    duplicate keys for the same fact.
15. Do not store secrets, passwords, API keys, or personal sensitive data.
16. Do not inspect the same file or list the same files repeatedly.
17. For a small, clear objective, inspect the workspace once, then start implementing.
18. After at most two inspection actions without a code change, choose write_file.
19. Prefer creating a working minimum version over repeatedly planning or inspecting.
20. After writing files, run tests, fix failures, and verify the final file list.
21. If the previous result says an action was repeated, you MUST choose a different action.
22. Do not use list_files or read_file as a substitute for implementation.
23. If you have enough information to proceed, make a reasonable implementation decision
    and record important decisions with remember.
24. After implementation, call run_tests before finishing.
25. If run_tests reports success=false, analyse its output, fix the relevant files,
    and call run_tests again while time remains.
26. If no test files exist, run_tests performs Python syntax validation. Create
    automated tests when the project's behavior warrants them.
27. Do not finish merely because files were written; verify the requested behavior.
28. When tests pass, save a concise milestone memory describing what was verified.
29. If current_context includes interrupted_write, prioritize that draft and write a complete file to its saved path. Return the whole corrected file, not only the missing suffix. If the excerpt is marked truncated, reconstruct the complete file from the objective and available project context.

DEVELOPMENT SEQUENCE:
- Inspect existing files once.
- Create the application files with write_file.
- Run tests and correct errors.
- Check the resulting files.
- Finish only when the requested implementation is present and verified.

AVAILABLE ACTIONS:

list_files
read_file
write_file
run_tests
remember
recall
finish

Return exactly one JSON object per response.

JSON FORMAT:

{
  "action": "list_files",
  "path": "",
  "content": "",
  "summary": "Inspect project"
}

For write_file, provide the complete file contents. IMPORTANT: Keep JSON keys in this order: action, path, content, summary. This order allows live code streaming into VS Code.

For remember, provide:
{"action":"remember","category":"decision","key":"database","content":"Use SQLite for local persistence","summary":"Save database decision"}

For recall, provide:
{"action":"recall","summary":"Review saved project memory"}

Use categories such as requirement, decision, milestone, todo, bugfix, and note.
For finish, provide a summary of completed work.

Do not wrap JSON in Markdown code fences.
"""



def ask_ai(objective, context, retries=AI_RETRIES):

    try:
        recovery_pending = bool(json.loads(context).get("interrupted_write"))
    except (TypeError, json.JSONDecodeError):
        recovery_pending = False

    payload = {
        "model": MODEL,
        "stream": True,
        "format": "json",
        "keep_alive": "10m",
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"PROJECT OBJECTIVE:\n{objective}\n\n"
                    f"CURRENT CONTEXT:\n{context}\n\n"
                    "Use project_memory to avoid repeating work. Save important "
                    "new facts with remember. Follow agent_guidance strictly. "
                    "For a clear, small application request, do not spend more than "
                    "two actions inspecting files: create the working implementation "
                    "with write_file, then test it. If told an action was repeated, "
                    "you must choose a different action. For write_file responses, "
                    "emit JSON keys in this exact order: action, path, content, summary."
                )
            }
        ],
        "options": {
            "temperature": 0.2,
            "num_ctx": 8192 if recovery_pending else 4096
        }
    }

    for attempt in range(1, retries + 1):
        live_started = False
        live_start_attempted = False
        live_path = None
        live_error = None
        pending_text = ""
        partial_content = ""
        last_recovery_checkpoint = 0

        try:
            data = json.dumps(payload).encode("utf-8")
            request = urllib.request.Request(
                OLLAMA_URL, data=data,
                headers={"Content-Type": "application/json"}
            )

            decoder = JsonStringStreamDecoder()

            with urllib.request.urlopen(
                request, timeout=AI_REQUEST_TIMEOUT
            ) as response:
                for line in response:
                    if not line.strip():
                        continue

                    packet = json.loads(line.decode("utf-8"))
                    fragment = packet.get("message", {}).get("content", "")
                    if not fragment:
                        continue

                    delta = decoder.feed(fragment)

                    if not live_start_attempted and decoder.started:
                        action_name = complete_json_field(decoder.raw, "action")
                        candidate_path = complete_json_field(decoder.raw, "path")

                        if action_name == "write_file" and isinstance(candidate_path, str) and candidate_path:
                            # Validate path before opening an editor document.
                            safe_path(candidate_path)
                            live_path = candidate_path
                            live_start_attempted = True
                            try:
                                vscode_live_request("/live/start", {"path": live_path})
                                live_started = True
                                print(f"[LIVE] Streaming {live_path} into VS Code...")
                            except Exception as stream_exc:
                                live_error = str(stream_exc)
                                log("VSCODE_LIVE_ERROR", live_error)
                                try:
                                    vscode_live_request("/live/cancel", {}, timeout=10)
                                except Exception:
                                    pass

                    if delta and live_path:
                        partial_content += delta
                        if (
                            len(partial_content) - last_recovery_checkpoint
                            >= RECOVERY_CHECKPOINT_INTERVAL
                        ):
                            if save_interrupted_write(
                                live_path, partial_content, log_event=False
                            ):
                                last_recovery_checkpoint = len(partial_content)

                    if delta and live_started and not live_error:
                        pending_text += delta
                        if len(pending_text) >= 128:
                            try:
                                vscode_live_request(
                                    "/live/chunk",
                                    {"content": pending_text},
                                    timeout=30
                                )
                                pending_text = ""
                            except Exception as stream_exc:
                                live_error = str(stream_exc)
                                log("VSCODE_LIVE_ERROR", live_error)

                    if packet.get("done"):
                        break

            action = json.loads(decoder.raw)

            if not isinstance(action, dict):
                raise ValueError("AI returned an invalid action.")
            if "action" not in action:
                raise ValueError("AI response has no action field.")

            allowed_actions = {
                "list_files", "read_file", "write_file", "run_tests",
                "remember", "recall", "finish"
            }
            if action["action"] not in allowed_actions:
                raise ValueError(
                    f"Unsupported action returned by AI: {action['action']}"
                )

            if live_started:
                try:
                    if pending_text and not live_error:
                        vscode_live_request(
                            "/live/chunk", {"content": pending_text}, timeout=30
                        )
                    if live_error:
                        vscode_live_request("/live/cancel", {}, timeout=10)
                    else:
                        vscode_live_request("/live/finish", {}, timeout=600)
                        action["_vscode_streamed"] = True
                except Exception as stream_exc:
                    live_error = str(stream_exc)
                    try:
                        vscode_live_request("/live/cancel", {}, timeout=10)
                    except Exception:
                        pass
                    log("VSCODE_LIVE_ERROR", live_error)

            if live_error:
                action["_vscode_stream_error"] = live_error

            return action

        except KeyboardInterrupt:
            if live_path and partial_content:
                save_interrupted_write(live_path, partial_content)
            if live_started or live_start_attempted:
                try:
                    vscode_live_request("/live/cancel", {}, timeout=10)
                except Exception:
                    pass
            raise

        except Exception as e:
            if live_path and partial_content:
                save_interrupted_write(live_path, partial_content)
            if live_started:
                try:
                    vscode_live_request("/live/cancel", {}, timeout=10)
                except Exception:
                    pass

            log("OLLAMA_RETRY", f"Attempt {attempt}/{retries}: {e}")
            if attempt == retries:
                raise

            delay = min(10 * attempt, 30)
            print(f"Ollama request failed. Retrying in {delay} seconds...")
            time.sleep(delay)


# ==========================================
# ACTION CONTROLLER
# ==========================================

def execute_action(action):

    name = action.get("action")

    if name == "list_files":
        return list_files()

    elif name == "read_file":
        return read_file(action.get("path", ""))

    elif name == "write_file":
        path = action.get("path", "")
        content = action.get("content", "")

        if not path or not isinstance(content, str):
            return "Invalid file path or content."

        streamed = bool(action.pop("_vscode_streamed", False))
        stream_error = action.pop("_vscode_stream_error", "")
        result = write_file(path, content, vscode_already_streamed=streamed)
        if get_state("interrupted_write_path", "") == path:
            clear_interrupted_write()
        if stream_error:
            result += f"\nLive editor stream failed; full file fallback used. Details: {stream_error}"

        checkpoint = git_checkpoint(
            action.get("summary", "Code update")
        )

        return result + "\n" + checkpoint

    elif name == "run_tests":
        result = run_tests()
        save_state("last_test_success", result.get("success", False))
        save_state("last_test_mode", result.get("mode", "unknown"))
        save_state("last_test_output", result.get("output", "")[-12000:])
        log("TEST", result)
        return json.dumps(result)

    elif name == "remember":
        return save_memory(
            action.get("category", "note"),
            action.get("key", ""),
            action.get("content", "")
        )

    elif name == "recall":
        return get_project_memory()

    elif name == "finish":
        return None

    else:
        return f"Unknown or unauthorized action: {name}"


# ==========================================
# AUTONOMOUS DEVELOPMENT LOOP
# ==========================================

def run_agent(objective, hours, resume=False):

    start = time.monotonic()
    deadline = start + hours * 3600
    interrupted_write = get_interrupted_write_context() if resume else None

    if interrupted_write:
        print(
            f"Resuming interrupted file {interrupted_write['path']} "
            f"({interrupted_write['saved_characters']} saved characters)."
        )
        try:
            vscode_live_request("/live/cancel", {}, timeout=10)
        except Exception as error:
            log("VSCODE_LIVE_ERROR", f"Resume cleanup: {error}")

    # Restore progress only when the user explicitly requested --resume.
    if resume:
        previous_result = get_state("last_result", "") or ""
        previous_step = int(get_state("last_step", "0") or "0")
    else:
        clear_interrupted_write()
        previous_result = ""
        previous_step = 0
        save_state("last_result", "")
        save_state("last_step", "0")
        save_state("last_error", "")
    repeated_actions = 0
    previous_signature = ""
    repeat_recoveries = 0
    inspection_streak = 0
    last_action_name = ""
    consecutive_model_failures = 0

    print("\n================================")
    print(" ACA AUTONOMOUS CODING AGENT")
    print("================================")
    print(f"Model: {MODEL}")
    print(f"Runtime: {hours} hours")
    print(f"Starting from step: {previous_step + 1}")
    print(f"Objective: {objective}")

    save_state("objective", objective)
    save_state("status", "RUNNING")

    for loop_step in range(MAX_STEPS):
        step = previous_step + loop_step + 1

        # The dashboard creates this file to request a safe stop.
        if STOP_FILE.exists():
            STOP_FILE.unlink(missing_ok=True)
            save_state("status", "STOPPED")
            log("SYSTEM", "Agent stopped by dashboard.")
            print("Agent stopped safely.")
            break

        if time.monotonic() >= deadline:
            print("Execution time limit reached.")
            break

        context = {
            "step": step,
            "files": list_files(),
            "previous_result": previous_result[-6000:],
            "project_memory": get_project_memory(),
            "last_test_result": {
                "success": get_state("last_test_success", "not_run"),
                "mode": get_state("last_test_mode", "not_run"),
                "output": get_state("last_test_output", "")[-5000:]
            },
            "remaining_minutes": round(
                (deadline - time.monotonic()) / 60, 1
            ),
            "recent_history": db.execute(
                "SELECT action, details FROM history "
                "ORDER BY id DESC LIMIT 8"
            ).fetchall(),
            "agent_guidance": {
                "last_action": last_action_name,
                "inspection_actions_without_write": inspection_streak,
                "repeat_recoveries_used": repeat_recoveries,
                "required_behavior": (
                    "Choose write_file now and implement the minimum working "
                    "version; do not inspect again."
                    if inspection_streak >= MAX_INSPECTION_ACTIONS_WITHOUT_WRITE
                    else "Choose a useful next development action. Avoid repeating "
                         "the previous action without a clear reason."
                )
            }
        }

        interrupted_write = get_interrupted_write_context()
        if interrupted_write:
            context["interrupted_write"] = interrupted_write
            context["agent_guidance"]["required_behavior"] = (
                "Complete the interrupted draft now. Use write_file for "
                f"{interrupted_write['path']} with the complete file contents."
            )

        print(f"\n[STEP {step}] Asking AI...")

        # Retry model/network failures separately from code execution failures.
        try:
            action = ask_ai(objective, json.dumps(context))
            consecutive_model_failures = 0
            save_state("status", "RUNNING")
            save_state("last_error", "")
        except KeyboardInterrupt:
            print("\nInterrupted. Progress has been saved.")
            save_state("status", "PAUSED")
            break
        except Exception as e:
            consecutive_model_failures += 1
            previous_result = f"Model request failed: {type(e).__name__}: {e}"
            log("OLLAMA_ERROR", previous_result)
            print(previous_result)
            save_state("last_error", previous_result)
            save_state("status", "WAITING_FOR_MODEL")

            if consecutive_model_failures >= MAX_CONSECUTIVE_MODEL_FAILURES:
                print("Model failed repeatedly. Session paused for safety.")
                save_state("status", "INTERRUPTED")
                break

            delay = min(30 * (2 ** (consecutive_model_failures - 1)), 120)
            print(f"Waiting {delay} seconds before retrying...")
            # Check for a dashboard stop request while waiting.
            for _ in range(delay):
                if STOP_FILE.exists():
                    break
                time.sleep(1)
            continue

        signature = json.dumps(action, sort_keys=True)
        if signature == previous_signature:
            repeated_actions += 1
        else:
            repeated_actions = 0

        if repeated_actions >= MAX_IDENTICAL_ACTIONS:
            repeat_recoveries += 1
            repeated_name = action.get("action", "unknown")
            feedback = (
                f"LOOP PREVENTION: You repeated the exact action "
                f"'{repeated_name}' without making progress. Do NOT repeat it. "
                f"Choose a different useful action. For this objective, if the "
                f"workspace has been inspected, use write_file to create the "
                f"application now."
            )
            print("Repeated action detected. Sending corrective feedback.")
            log("LOOP_RECOVERY", feedback)
            previous_result = feedback
            save_state("last_result", previous_result)
            save_state("last_error", feedback)

            if repeat_recoveries > MAX_REPEAT_RECOVERIES:
                print("The model did not recover from repeated actions. Pausing.")
                save_state("status", "PAUSED")
                break

            # Clear the signature so the next response is judged independently.
            previous_signature = ""
            repeated_actions = 0
            continue

        previous_signature = signature

        print("Action:", action.get("action"))
        print("Summary:", action.get("summary", ""))

        try:
            result = execute_action(action)
        except KeyboardInterrupt:
            print("\nInterrupted. Progress has been saved.")
            save_state("status", "PAUSED")
            break
        except Exception as e:
            # Keep the session alive and feed the error back to the model so it
            # can diagnose and correct the failed operation on its next turn.
            previous_result = f"Action execution failed: {type(e).__name__}: {e}"
            log("ACTION_ERROR", previous_result)
            print(previous_result)
            save_state("last_error", previous_result)
            save_state("last_result", previous_result)
            continue

        if action.get("action") == "finish":
            print("\nAI reports project completion.")
            print(action.get("summary", ""))
            save_state("status", "FINISHED")
            save_state("last_step", step)
            log("FINISH", action.get("summary", ""))
            break

        previous_result = str(result)
        save_state("last_result", previous_result)
        save_state("last_step", step)
        save_state("last_error", "")

        last_action_name = action.get("action", "")
        if last_action_name in {"list_files", "read_file"}:
            inspection_streak += 1
        elif last_action_name == "write_file":
            inspection_streak = 0
            repeat_recoveries = 0

        log(action.get("action"), previous_result)
        print("Result:", previous_result[:1000])

    else:
        print("Maximum development steps reached.")
        save_state("status", "PAUSED")

    # Save a final source-control checkpoint when possible.
    checkpoint_result = git_checkpoint("ACA session checkpoint")
    log("CHECKPOINT", checkpoint_result)

    if get_state("status") in {"RUNNING", "WAITING_FOR_MODEL"}:
        save_state("status", "PAUSED")

    elapsed = (time.monotonic() - start) / 3600

    print("\n================================")
    print(" SESSION SUMMARY")
    print("================================")
    print(f"Elapsed hours: {elapsed:.2f}")
    print(f"Status: {get_state('status')}")
    print(f"Last completed step: {get_state('last_step', '0')}")
    print(f"Files: {len(list_files())}")
    print("Progress saved in agent.db")


# ==========================================
# PROGRAM ENTRY POINT
# ==========================================

if __name__ == "__main__":

    parser = argparse.ArgumentParser(
        description="ACA Autonomous Coding Agent"
    )

    parser.add_argument(
        "objective",
        nargs="?",
        help="Software development objective"
    )

    parser.add_argument(
        "--hours",
        type=float,
        default=10
    )

    parser.add_argument(
        "--resume",
        action="store_true"
    )

    args = parser.parse_args()

    if args.hours <= 0 or args.hours > 24:
        parser.error("Hours must be greater than 0 and at most 24.")

    objective = args.objective

    if args.resume:
        objective = get_state("objective")

    if not objective:
        parser.error("Provide a project objective or use --resume.")

    run_agent(objective, args.hours, resume=args.resume)

    db.close()
