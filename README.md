# ACA — Autonomous Coding Agent

ACA is a local coding assistant that turns a software objective into a sequence of code changes. It uses an Ollama model running on the same computer, keeps its working files in `workspace/`, and can show generated code as it is written in a VS Code editor.

The project includes a browser dashboard for starting, monitoring, stopping, and resuming an agent run. Run ACA directly from the terminal or control it from the dashboard.

## What it does

- Sends project context and development actions to a local Ollama model.
- Lists and reads files, writes code, runs available tests or syntax checks, and records progress.
- Restricts agent file operations to the `workspace/` directory.
- Saves agent state, run history, and project memories in a local SQLite database.
- Checkpoints partial streamed code so `--resume` can request a complete file after an interrupted run.
- Creates Git checkpoints in the agent workspace when Git is available.
- Provides a Flask dashboard with run controls, status, logs, and workspace file previews.
- Streams generated code into VS Code with a human-paced typing effect when the ACA Live Editor extension is running.
- Falls back to local file writing if the VS Code live editor is unavailable.

## Project layout

| Path | Purpose |
| --- | --- |
| `agent.py` | Agent loop, Ollama connection, workspace file actions, memory, tests, and checkpointing. |
| `dashboard.py` | Local Flask control dashboard. |
| `aca-vscode/` | VS Code extension and its development launch configuration. |
| `workspace/` | Files the agent inspects and creates. |
| `logs/` | Agent and dashboard logs. |
| `agent.db` | Local SQLite state, history, and project memory. |
| `requirements.txt` | Python dependency for the dashboard. |

## Requirements

- Windows with Python 3.10 or newer.
- [Ollama](https://ollama.com/) running locally at `http://127.0.0.1:11434`.
- The default model `qwen2.5-coder:3b`, or another Ollama model that supports chat responses in JSON format.
- VS Code is optional. It is only needed for live editor streaming.

## Setup

From PowerShell in the project directory:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
ollama pull qwen2.5-coder:3b
```

Start Ollama before launching the agent. You can choose a different model with the `OLLAMA_MODEL` environment variable.

## Run the dashboard

```powershell
python dashboard.py
```

Open [http://127.0.0.1:5000](http://127.0.0.1:5000). Enter an objective and runtime, then start the agent. The dashboard can also resume the last saved objective, request a safe stop, show logs, and preview workspace files.

The dashboard listens on the local loopback address only. It is intended for use on the same computer.

## Run the agent from the terminal

```powershell
python agent.py "Build a small command-line habit tracker" --hours 1
```

The runtime is optional and defaults to 10 hours; the accepted range is greater than 0 and up to 24 hours. To continue the previously saved objective:

```powershell
python agent.py --resume --hours 1
```

If a run is interrupted while a `write_file` response is streaming, ACA saves the partial draft in `agent.db`. On the next model turn or `--resume`, it supplies the saved draft and target path to the model, which is instructed to return the complete file. A successful write clears the recovery draft.

Agent-created and agent-managed project files go under `workspace/`. Keep important files backed up and review generated changes before using them in production.

## Connect the VS Code live editor

1. Open `aca-vscode/` in VS Code.
2. Choose **Run ACA Live Editor Extension** in Run and Debug and press **F5**. This opens an Extension Development Host window.
3. In that new window, open `C:\ACA\workspace` as the workspace.
4. Check the **ACA Live Editor** output channel for activation and server status. The extension listens on `127.0.0.1:8765`.
5. Start the agent from the terminal or dashboard. Generated text is inserted at the end of the selected target file, and the editor caret and view follow the insertion as the file grows.

The extension provides **ACA: Start Live Editor Server** and **ACA: Stop Live Editor Server** commands. Its local HTTP interface accepts POST requests at `/live/start`, `/live/chunk`, `/live/finish`, `/live/cancel`, and `/stream`. The agent uses relative paths inside the open VS Code workspace. Starting a live session clears the selected existing file after checking that it has no unsaved editor changes.

If the extension server is stopped or unavailable, the agent records a `VSCODE_LIVE_ERROR` and retains its local file-write fallback. This does not require the Ollama request to be repeated.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `OLLAMA_MODEL` | `qwen2.5-coder:3b` | Ollama model name. |
| `ACA_AI_TIMEOUT` | `300` seconds | Timeout for a model request. |
| `ACA_AI_RETRIES` | `2` | Attempts for Ollama request or response failures. |

The Ollama endpoint is currently `http://127.0.0.1:11434/api/chat`. The dashboard uses `http://127.0.0.1:5000`, and the VS Code extension uses `http://127.0.0.1:8765`.

## GitHub and project data

The root `.gitignore` excludes Python caches, virtual environments, local databases, logs, and the agent stop signal. Source files in `workspace/` are not ignored; review them for private data before publishing. ACA creates Git checkpoints inside `workspace/`; those checkpoints are separate from publishing the whole `C:\ACA` project. Before creating a GitHub repository for the root project, decide which generated workspace files and workspace history belong in that repository. No commit or remote is created automatically for the root project.

To publish the ACA source as a new root-level repository, create an empty GitHub repository, then run these commands from `C:\ACA` after reviewing the files to include:

```powershell
git init
git add README.md .gitignore requirements.txt agent.py dashboard.py aca-vscode
git status
git commit -m "Prepare ACA project"
git branch -M main
git remote add origin <your-GitHub-repository-URL>
git push -u origin main
```

Add selected files from `workspace/` separately only if you want those generated projects published. ACA uses that directory for its own Git checkpoints when Git is available, so keep any existing workspace checkpoint history separate from the root project repository.

## About this Markdown file

This `README.md` is written in Markdown: a plain-text format that uses markers such as `#` for headings, `-` for lists, and triple backticks for code blocks. GitHub renders those markers as formatted project documentation.
