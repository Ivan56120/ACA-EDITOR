const vscode = require("vscode");
const http = require("http");
const path = require("path");
const fs = require("fs");

let server;
let outputChannel;
let activeTyping = false;
let liveSession = null;

const PORT = 8765;
const MAX_BODY_SIZE = 2_000_000;
const MAX_LIVE_BUFFER = 250_000;

const TYPING = {
    minCharacters: 1,
    maxCharacters: 3,
    characterDelayMin: 35,
    characterDelayMax: 100,
    wordDelayMin: 100,
    wordDelayMax: 220,
    lineDelayMin: 300,
    lineDelayMax: 700,
    blankLineDelayMin: 500,
    blankLineDelayMax: 1000,
    blockDelayMin: 1000,
    blockDelayMax: 2000,
    thinkingChance: 0.025,
    thinkingDelayMin: 2000,
    thinkingDelayMax: 4000
};

function activate(context) {
    outputChannel = vscode.window.createOutputChannel("ACA Live Editor");
    context.subscriptions.push(outputChannel);
    outputChannel.appendLine("ACA Live Editor activated.");
    console.log("ACA Live Editor activated.");
    context.subscriptions.push(
        vscode.commands.registerCommand("aca.startServer", () =>
            startServer().catch(reportServerStartError)
        ),
        vscode.commands.registerCommand("aca.stopServer", () => stopServer())
    );
    startServer(true).catch(reportServerStartError);
}

function reportServerStartError(error) {
    const message = `ACA Live Editor failed to start: ${error.message || error}`;
    outputChannel?.appendLine(message);
    console.error(message);
    vscode.window.showErrorMessage(message);
}

function sendJson(res, status, data) {
    if (res.headersSent || res.destroyed) return;
    res.writeHead(status, { "Content-Type": "application/json" });
    res.end(JSON.stringify(data));
}

function randomDelay(min, max) {
    return Math.floor(Math.random() * (max - min + 1) + min);
}

function sleep(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
}

function followTyping(editor) {
    const document = editor.document;
    const end = document.positionAt(document.getText().length);
    const cursor = new vscode.Selection(end, end);
    editor.selection = cursor;
    editor.revealRange(
        new vscode.Range(end, end),
        vscode.TextEditorRevealType.Default
    );
}

function wakeSession(session) {
    const waiters = session.waiters.splice(0);
    for (const wake of waiters) wake();
}

function validateTarget(relativePath) {
    if (typeof relativePath !== "string" || !relativePath.trim()) {
        throw new Error("A valid relative file path is required.");
    }

    const workspace = vscode.workspace.workspaceFolders?.[0];
    if (!workspace) throw new Error("Open the ACA workspace in VS Code first.");

    const root = path.resolve(workspace.uri.fsPath);
    const target = path.resolve(root, relativePath);
    const relative = path.relative(root, target);

    if (
        relative === ".." ||
        relative.startsWith(".." + path.sep) ||
        path.isAbsolute(relative)
    ) {
        throw new Error("Path outside workspace denied.");
    }

    if (fs.existsSync(target)) {
        const realTarget = fs.realpathSync(target);
        const realRelative = path.relative(root, realTarget);
        if (
            realRelative === ".." ||
            realRelative.startsWith(".." + path.sep) ||
            path.isAbsolute(realRelative)
        ) {
            throw new Error("Target resolves outside workspace.");
        }
    }

    return { target, relativePath };
}

async function openAndClear(relativePath) {
    const { target } = validateTarget(relativePath);
    const uri = vscode.Uri.file(target);

    await vscode.workspace.fs.createDirectory(
        vscode.Uri.file(path.dirname(target))
    );

    if (!fs.existsSync(target)) {
        await vscode.workspace.fs.writeFile(uri, Buffer.from("", "utf8"));
    }

    const document = await vscode.workspace.openTextDocument(uri);
    if (document.isDirty) {
        throw new Error("Target file has unsaved editor changes.");
    }

    const editor = await vscode.window.showTextDocument(document, {
        preview: false,
        viewColumn: vscode.ViewColumn.Active
    });

    const oldLength = document.getText().length;
    if (oldLength > 0) {
        const range = new vscode.Range(
            document.positionAt(0),
            document.positionAt(oldLength)
        );
        const cleared = await editor.edit(edit => edit.delete(range));
        if (!cleared) throw new Error("Could not clear previous document.");
    }

    return editor;
}

async function typeChunk(session, text) {
    let offset = 0;

    while (offset < text.length) {
        if (session.cancelled) throw new Error("Live typing was cancelled.");

        const remaining = text.slice(offset);
        let size = randomDelay(TYPING.minCharacters, TYPING.maxCharacters);
        if (remaining[0] === " " || remaining[0] === "\n" || remaining[0] === "\r") {
            size = 1;
        }
        size = Math.min(size, remaining.length);
        const chunk = remaining.slice(0, size);
        const position = session.editor.document.positionAt(
            session.editor.document.getText().length
        );

        const inserted = await session.editor.edit(edit => edit.insert(position, chunk));
        if (!inserted) throw new Error("VS Code rejected a live text insertion.");
        followTyping(session.editor);

        offset += chunk.length;
        const previous = chunk[chunk.length - 1];
        let delay = randomDelay(
            TYPING.characterDelayMin,
            TYPING.characterDelayMax
        );

        if (previous === " ") {
            delay = randomDelay(TYPING.wordDelayMin, TYPING.wordDelayMax);
        } else if (previous === "\n" || previous === "\r") {
            const doc = session.editor.document;
            const line = doc.lineAt(Math.max(0, doc.lineCount - 2));
            delay = line.text.trim() === ""
                ? randomDelay(TYPING.blankLineDelayMin, TYPING.blankLineDelayMax)
                : randomDelay(TYPING.lineDelayMin, TYPING.lineDelayMax);
        } else if (previous === "{" || previous === ":") {
            delay = randomDelay(TYPING.blockDelayMin, TYPING.blockDelayMax);
        } else if (previous === ";" || previous === "." || previous === ")") {
            delay += randomDelay(100, 250);
        }

        if (Math.random() < TYPING.thinkingChance) {
            delay += randomDelay(
                TYPING.thinkingDelayMin,
                TYPING.thinkingDelayMax
            );
        }
        await sleep(delay);
    }
}

async function consumeLiveQueue(session) {
    try {
        while (true) {
            if (session.cancelled) throw new Error("Live typing was cancelled.");

            if (session.queue.length > 0) {
                const next = session.queue.shift();
                session.queuedCharacters -= next.length;
                await typeChunk(session, next);
                continue;
            }

            if (session.finished) break;

            await new Promise(resolve => session.waiters.push(resolve));
        }

        const saved = await session.editor.document.save();
        if (!saved) throw new Error("VS Code could not save the streamed file.");
        session.completed = true;
        vscode.window.setStatusBarMessage(
            `ACA completed ${session.relativePath}`, 5000
        );
        console.log(`ACA live stream completed ${session.relativePath}`);
    } catch (error) {
        session.error = error.message || String(error);
    } finally {
        activeTyping = false;
        if (liveSession === session) liveSession = null;
    }
}

function readJsonBody(req, res) {
    return new Promise((resolve, reject) => {
        let body = "";
        let rejected = false;

        req.on("data", chunk => {
            if (rejected) return;
            body += chunk.toString("utf8");
            if (Buffer.byteLength(body, "utf8") > MAX_BODY_SIZE) {
                rejected = true;
                sendJson(res, 413, {
                    success: false,
                    error: "Request exceeds the size limit."
                });
                req.destroy();
                reject(new Error("Request exceeds the size limit."));
            }
        });

        req.on("end", () => {
            if (rejected) return;
            try {
                resolve(JSON.parse(body || "{}"));
            } catch {
                reject(new Error("Invalid JSON request body."));
            }
        });

        req.on("error", reject);
    });
}

async function handleRequest(req, res) {
    if (req.method !== "POST") {
        sendJson(res, 404, { success: false, error: "Endpoint not found." });
        return;
    }

    let data;
    try {
        data = await readJsonBody(req, res);
    } catch (error) {
        if (!res.headersSent) {
            sendJson(res, 400, { success: false, error: error.message });
        }
        return;
    }

    try {
        if (req.url === "/live/start") {
            if (activeTyping || liveSession) {
                throw new Error("ACA is already typing another file.");
            }

            const editor = await openAndClear(data.path);
            const session = {
                editor,
                relativePath: data.path,
                queue: [],
                queuedCharacters: 0,
                waiters: [],
                finished: false,
                cancelled: false,
                completed: false,
                error: null
            };

            liveSession = session;
            activeTyping = true;
            session.worker = consumeLiveQueue(session);
            vscode.window.setStatusBarMessage("ACA is generating code live...");
            sendJson(res, 200, { success: true, path: data.path });
            return;
        }

        if (req.url === "/live/chunk") {
            const session = liveSession;
            if (!session || session.finished || session.cancelled) {
                throw new Error("No active ACA live-stream session.");
            }
            if (typeof data.content !== "string") {
                throw new Error("Stream chunk must be text.");
            }

            session.queuedCharacters += data.content.length;
            if (session.queuedCharacters > MAX_LIVE_BUFFER) {
                throw new Error("Live typing queue is full; slow down generation.");
            }
            if (data.content.length) session.queue.push(data.content);
            wakeSession(session);
            sendJson(res, 200, { success: true, queued: session.queuedCharacters });
            return;
        }

        if (req.url === "/live/finish") {
            const session = liveSession;
            if (!session) throw new Error("No active ACA live-stream session.");
            session.finished = true;
            wakeSession(session);
            await session.worker;
            if (session.error) throw new Error(session.error);
            sendJson(res, 200, {
                success: true,
                path: session.relativePath,
                typingMode: "human-like-live"
            });
            return;
        }

        if (req.url === "/live/cancel") {
            const session = liveSession;
            if (session) {
                session.cancelled = true;
                session.finished = true;
                wakeSession(session);
                await session.worker;
            }
            sendJson(res, 200, { success: true, cancelled: true });
            return;
        }

        if (req.url === "/stream") {
            if (activeTyping || liveSession) {
                throw new Error("ACA is already typing another file.");
            }
            if (typeof data.content !== "string") {
                throw new Error("File content must be text.");
            }

            const editor = await openAndClear(data.path);
            activeTyping = true;
            const session = {
                editor,
                relativePath: data.path,
                cancelled: false
            };

            try {
                await typeChunk(session, data.content);
                const saved = await editor.document.save();
                if (!saved) throw new Error("VS Code could not save generated file.");
            } finally {
                activeTyping = false;
            }

            vscode.window.setStatusBarMessage(`ACA completed ${data.path}`, 5000);
            sendJson(res, 200, {
                success: true,
                path: data.path,
                characters: data.content.length,
                typingMode: "human-like"
            });
            return;
        }

        sendJson(res, 404, { success: false, error: "Endpoint not found." });
    } catch (error) {
        sendJson(res, 500, {
            success: false,
            error: error.message || String(error)
        });
        console.error("ACA Live Editor error:", error);
    }
}

function startServer(silent = false) {
    if (server) {
        if (!silent) {
            vscode.window.showInformationMessage(
                "ACA Live Editor server is already running."
            );
        }
        return Promise.resolve();
    }

    server = http.createServer(handleRequest);
    const startingServer = server;
    return new Promise((resolve, reject) => {
        startingServer.once("error", error => {
            if (server === startingServer) server = undefined;
            const message = `Could not bind 127.0.0.1:${PORT}: ${error.message}`;
            outputChannel?.appendLine(message);
            reject(error);
        });
        startingServer.listen(PORT, "127.0.0.1", () => {
            startingServer.removeAllListeners("error");
            startingServer.on("error", error => {
                const message = `ACA Live Editor server error: ${error.message}`;
                outputChannel?.appendLine(message);
                console.error(message);
                if (server === startingServer) server = undefined;
            });
            outputChannel?.appendLine(`Listening on 127.0.0.1:${PORT}`);
            console.log(`ACA Live Editor listening on 127.0.0.1:${PORT}`);
            if (!silent) {
                vscode.window.showInformationMessage(
                    `ACA Live Editor running on port ${PORT}`
                );
            }
            resolve();
        });
    });
}

function stopServer() {
    if (!server) {
        vscode.window.showInformationMessage("ACA Live Editor is not running.");
        return;
    }

    if (liveSession) {
        liveSession.cancelled = true;
        liveSession.finished = true;
        wakeSession(liveSession);
    }
    activeTyping = false;

    server.close(() => {
        outputChannel?.appendLine("Server stopped.");
        server = undefined;
        vscode.window.showInformationMessage("ACA Live Editor stopped.");
    });
}

function deactivate() {
    if (liveSession) {
        liveSession.cancelled = true;
        liveSession.finished = true;
        wakeSession(liveSession);
    }
    activeTyping = false;
    if (server) {
        server.close();
        server = undefined;
    }
}

module.exports = { activate, deactivate };
