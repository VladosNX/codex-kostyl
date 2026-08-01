# Codex Kostyl

[English](README.md) · [Русский](README.ru.md)

![Platform](https://img.shields.io/badge/platform-Linux%20%7C%20macOS-1793D1)
![Python](https://img.shields.io/badge/python-3.11%2B-3776AB?logo=python&logoColor=white)
![Qt](https://img.shields.io/badge/UI-PySide6-41CD52?logo=qt&logoColor=white)
![License](https://img.shields.io/badge/license-MIT-blue)

An unofficial desktop client for local AI coding agents, built with Python and
PySide6. It bundles a native Codex CLI driver and a generic ACP v1 client for
agents that support the local stdio transport.

Codex Kostyl provides one interface for local projects, saved conversations,
streaming responses, approvals, and attachments. For Codex it talks directly to
`codex app-server`, so conversations remain in Codex's standard storage and are
also available from the CLI. For ACP, the selected agent's advertised
capabilities determine which UI controls are available.

> [!NOTE]
> This is an independent community project and is not an official OpenAI product.
> The project is currently at an MVP/early-development stage.

## Why this project exists

Codex CLI already provides a powerful terminal workflow. Codex Kostyl is intended
for Linux and macOS users who prefer a native desktop interface for longer conversations,
visual review of agent activity, permission prompts, file attachments, and
switching between multiple local projects.

The application does not implement its own agent backend. The selected CLI agent
remains responsible for authentication, model access, conversation storage, tool
execution, and sandbox enforcement.

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
- Multiple local ACP v1 agent profiles with separate executables and launch
  arguments.
- Installable ACP integrations from the official ACP Registry or the latest
  stable Release of a public GitHub repository.
- Dynamic ACP features, commands, authentication methods, models, modes, and
  settings; unsupported or temporarily disabled features remain visible with a
  reason.

## Requirements

- Linux or macOS.
- Python 3.11 or newer.
- For the built-in profile: Codex CLI 0.146.0 or newer in `PATH`, plus a
  ChatGPT/Codex account or OpenAI API key.
- For an ACP profile: a local executable that supports ACP v1 over stdio.

Verify the local tools before installing:

```bash
python3 --version
codex --version
```

## Quick start

On Linux, clone the repository and run the per-user installer:

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

### Connecting an ACP agent

Open the account menu, choose **Add ACP agent…**, and enter a display name,
executable, and optional launch arguments. The profile appears in the agent
selector and is persisted through `QSettings`. One ACP driver implementation can
serve any number of profiles; only the selected profile runs at a time.

The client uses stable ACP v1 over JSON-RPC 2.0 and local stdio. Conversation
storage, authentication, and tool execution belong to the agent. The UI enables
only advertised or observed features such as session listing, images, session
modes, config options, slash commands, and usage reporting.

The **Agents** settings page also provides the official ACP catalog and GitHub
Release installation. Declarative packages only describe how to launch an
already installed CLI. Protocol adapters are downloaded as verified ZIP assets
and run as isolated ACP subprocesses. See the
[integration package format](docs/agent-integrations.md) for authoring details.

## Other installation options

### macOS

A packaged `.app`/DMG is not available yet. Install Codex CLI and run the client
from a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
codex-kostyl
```

If the GUI process cannot find Codex CLI in `PATH`, choose
**Account → Set CLI executable…**.

### System-wide Linux installation

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
├── agents/          # Contracts, profiles, registry, controller, and ACP driver
├── integrations/    # Catalog, GitHub packages, validation, and local storage
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
                           ┌→ CodexDriver → codex app-server
PySide6 UI → AgentController
                           └→ AcpDriver   → ACP v1 agent (stdio)
```

`AgentController` owns one active driver but is not itself a driver. The registry
stores driver kinds separately from user profiles, so several profiles can share
one `AcpDriver`. A driver publishes an `AgentManifest` describing static feature
support; the controller derives the current
`FeatureState(supported, enabled, reason)`. This distinguishes “the agent cannot
compact” from “compact is supported, but no session is open yet.”

Prompts cross the boundary as `AgentPrompt`, responses become common timeline
events, and permission requests become `ClientRequest` values containing the
agent's actual choices. Codex app-server and ACP wire details do not reach the
widgets. Legacy Codex method and signal names remain temporarily as a
compatibility layer.

## Data and privacy

- Conversation messages are not copied into a separate application database.
  Codex keeps them in its standard storage; ACP session persistence is defined
  by the selected agent.
- `QSettings` stores project paths, the selected agent, per-agent executable and
  request settings, and window geometry.
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

- macOS currently supports Python/venv execution without a packaged app bundle.
- Native Windows is not supported yet.
- Codex CLI must be installed locally and accessible to the launcher.
- The interface intentionally renders only a recent bounded portion of very
  large conversations.
- Attachments depend on their original filesystem paths.
- The project is still evolving alongside the Codex app-server protocol.
- ACP support currently targets stable v1 over local stdio only. Remote
  transports and draft ACP v2 are not supported yet.
- The client does not yet advertise client-side ACP terminal, file-read, or
  file-write operations; agents must provide those on their own side.

## Contributing

Issues and pull requests are welcome. For behavior changes, please include tests
where practical and verify the application with the offscreen Qt test command
shown above.

## License

Codex Kostyl is distributed under the MIT License.
