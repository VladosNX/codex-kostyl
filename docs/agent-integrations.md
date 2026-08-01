# Agent integration packages

Codex Kostyl uses its native Codex driver for Codex and one built-in ACP v1
driver for installable integrations. Third-party Python modules are never
imported into the GUI process.

An integration is installed from the latest stable GitHub Release. The release
must contain exactly one asset named `codex-kostyl-agent.json`. The application
does not clone the repository and does not run installer scripts.

## Declarative ACP command

Use `acp-command` when the agent already supports ACP over stdio:

```json
{
  "$schema": "https://github.com/VladosNX/codex-kostyl/blob/main/src/codex_gui/assets/agent-package.schema.json",
  "schemaVersion": 1,
  "id": "opencode",
  "name": "OpenCode",
  "version": "1.0.0",
  "description": "Run an existing OpenCode installation through ACP",
  "kind": "acp-command",
  "homepage": "https://opencode.ai",
  "runtime": {
    "any": {
      "command": { "system": ["opencode"] },
      "args": ["acp"],
      "env": {}
    }
  },
  "installHelp": {
    "url": "https://opencode.ai/docs",
    "message": "Install OpenCode CLI and retry detection."
  }
}
```

`system` is an ordered list of executable names searched in `PATH`. It cannot
contain paths. Users can select an absolute executable path in the application.
Arguments are passed directly to `QProcess`; shell strings and interpolation are
not supported.

## Isolated ACP adapter

Use `acp-adapter` only when a separate process must translate another protocol
to ACP. Every supported platform has its own ZIP asset and SHA-256:

```json
{
  "schemaVersion": 1,
  "id": "example-adapter",
  "name": "Example Adapter",
  "version": "1.0.0",
  "description": "ACP bridge for Example CLI",
  "kind": "acp-adapter",
  "runtime": {
    "linux-x86_64": {
      "artifact": {
        "asset": "example-adapter-linux-x86_64.zip",
        "sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
        "entrypoint": "bin/example-adapter"
      },
      "command": { "artifact": "bin/example-adapter" },
      "requirements": [
        {
          "id": "example-cli",
          "commands": ["example"],
          "exportAs": "EXAMPLE_CLI_BIN",
          "helpUrl": "https://example.com/install"
        }
      ]
    }
  }
}
```

Supported targets are `linux-x86_64`, `linux-aarch64`, `darwin-x86_64`,
`darwin-aarch64`, `windows-x86_64`, and `windows-aarch64`. An adapter runs as a
subprocess with the user's permissions. Process isolation is not an operating
system sandbox.

The application rejects archive traversal, symlinks, oversized packages, unsafe
process-loader environment variables, mismatched checksums, and unknown schema
versions. Updates are checked and installed only when requested by the user.
