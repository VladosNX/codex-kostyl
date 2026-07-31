# Codex Kostyl

[English](README.md) · [Русский](README.ru.md)

![Platform](https://img.shields.io/badge/platform-Linux-1793D1?logo=linux&logoColor=white)
![Python](https://img.shields.io/badge/python-3.11%2B-3776AB?logo=python&logoColor=white)
![Qt](https://img.shields.io/badge/UI-PySide6-41CD52?logo=qt&logoColor=white)
![License](https://img.shields.io/badge/license-MIT-blue)

An unofficial native Linux desktop client for Codex CLI, built with Python and
PySide6.

Codex Kostyl provides a graphical interface for working with local projects,
saved Codex conversations, streaming responses, approvals, attachments, and Plan
Mode. It talks directly to the official `codex app-server`, so conversations
remain in Codex's standard storage and are also available from the CLI.

> [!NOTE]
> This is an independent community project and is not an official OpenAI product.
> The project is currently at an MVP/early-development stage.

## Why this project exists

Codex CLI already provides a powerful terminal workflow. Codex Kostyl is intended
for Linux users who prefer a native desktop interface for longer conversations,
visual review of agent activity, permission prompts, file attachments, and
switching between multiple local projects.

The application does not replace Codex or implement its own agent backend. Codex
CLI remains responsible for authentication, model access, conversation storage,
tool execution, and sandbox enforcement.

## Features

- Local projects backed by working directories.
- Create, open, continue, and fork saved Codex conversations.
- Streaming agent messages, reasoning, command output, plans, and file changes.
- Dynamic model and reasoning-effort selection from Codex.
- Read-only, workspace-write, full-access, and Plan Mode workflows.
- Inline approval prompts for commands, file changes, network access, and
  additional filesystem permissions.
- Local image and file attachments.
- Message queue for follow-up prompts while a turn is still running.
- Built-in Codex questions and plan-to-implementation confirmation.
- Context-window and weekly usage indicators when reported by Codex.
- Markdown rendering with tables, links, and syntax-highlighted code blocks.
- Copy and reuse previous messages in the composer.
- Desktop notifications and active-turn interruption.
- ChatGPT and OpenAI API-key authentication through Codex.

## Requirements

- Linux.
- Python 3.11 or newer.
- Codex CLI 0.146.0 or newer available in `PATH`.
- A ChatGPT/Codex account or an OpenAI API key.

Verify the local tools before installing:

```bash
python3 --version
codex --version
```

## Quick start

Clone the repository and run the per-user installer:

```bash
git clone https://github.com/VladosNX/codex-kostyl.git
cd codex-kostyl
./scripts/install.sh
```

No `sudo` is required. After installation, launch **Codex Kostyl** from the
desktop application menu or run:

```bash
~/.local/bin/codex-kostyl
```

The installer creates an isolated virtual environment under
`~/.local/share/codex-kostyl`, installs a launcher into `~/.local/bin`, and
registers the desktop entry and application icon.

When the app opens:

1. Sign in with ChatGPT or provide an OpenAI API key.
2. Add or select a local working directory.
3. Choose a model, reasoning effort, and access mode.
4. Enter a task and send it to Codex.

## Other installation options

Install the application system-wide for all users:

```bash
sudo ./scripts/install.sh --system
```

This installs the application under `/opt/codex-kostyl`, the launcher under
`/usr/local/bin`, and desktop integration under `/usr/local/share`.

Running the installer again updates the existing installation.

### Uninstall

Remove a per-user installation:

```bash
./scripts/uninstall.sh
```

Remove a system-wide installation:

```bash
sudo ./scripts/uninstall.sh --system
```

## Run from source

For development or a temporary local run:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
codex-kostyl
```

You can also run the package without the console-script entry point:

```bash
PYTHONPATH=src python -m codex_gui
```

## Development

Install test dependencies and run the test suite:

```bash
python -m pip install -e '.[test]'
QT_QPA_PLATFORM=offscreen python -m pytest
```

Useful additional checks:

```bash
ruff check src tests
shellcheck scripts/*.sh packaging/codex-kostyl-launcher
```

### Project structure

```text
src/codex_gui/
├── app.py           # Application startup and global Qt styling
├── main_window.py   # Main window, composer, timeline, and dialogs
├── models.py        # Application data models and access policies
├── rendering.py     # Markdown and command-output rendering
├── rpc.py           # QProcess transport and JSON-RPC client
├── service.py       # Codex app-server integration and state handling
├── settings.py      # Persistent Qt settings
└── diagnostics.py   # Rotating diagnostic log
```

The runtime flow is:

```text
PySide6 UI → CodexService → JSON-RPC → codex app-server → Codex CLI
```

## Data and privacy

- Conversation messages are not copied into a separate application database.
  They remain in the standard Codex conversation storage.
- `QSettings` stores only local project paths, the latest model and access-mode
  selections, and window geometry.
- API keys are passed to Codex and are not persisted by Codex Kostyl.
- Attachments are referenced by absolute local path and are not copied. Moving
  or deleting a source file makes the old attachment path unavailable.
- The rotating diagnostic log contains lifecycle and protocol errors, not prompt
  or response bodies. Its location is selected through Qt's `QStandardPaths`.

To keep the interface responsive, the UI renders at most the latest 40 turns and
300 items of an opened conversation. Very long messages and command output may be
visually shortened, while the original history remains managed by Codex.

## Security model

The default mode is `workspace-write`. Codex can write inside the selected
working directory, while actions that require approval are displayed in the GUI.

- **Read only** — filesystem changes are disabled.
- **Workspace write** — writes are limited to the selected project directory.
- **Full access** — removes the filesystem sandbox and uses the `never` approval
  policy. Enable it only for trusted projects and tasks.
- **Plan Mode** — uses Codex's planning mode with a read-only sandbox. After the
  plan is complete, the UI can start implementation in workspace-write mode.

Messages sent during an active turn are queued locally and run one at a time.
The queue continues after a successful turn and pauses after an error or manual
interruption. Model, reasoning effort, and access-mode changes apply to the next
queued or newly sent message, not to the turn already in progress.

## Known limitations

- Linux is the only supported desktop platform.
- Codex CLI must be installed locally and accessible to the launcher.
- The interface intentionally renders only a recent bounded portion of very
  large conversations.
- Attachments depend on their original filesystem paths.
- The project is still evolving alongside the Codex app-server protocol.

## Contributing

Issues and pull requests are welcome. For behavior changes, please include tests
where practical and verify the application with the offscreen Qt test command
shown above.

## License

Codex Kostyl is distributed under the MIT License.
