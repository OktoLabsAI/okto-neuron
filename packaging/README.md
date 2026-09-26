# okto-neuron — service templates

This directory holds **unsupported, opt-in** macOS service templates for running
`okto-neuron serve` under an operating-system service manager. They are not part
of the public installer. The supported background path is the explicit
`okto-neuron serve --daemon` command; it survives the launching shell but does
not register login/reboot persistence. The package never modifies
`~/Library/LaunchAgents` or the Homebrew services registry on your behalf.

If you want a background server you install it explicitly. If you don't,
nothing here runs.

Linux `systemd` units are intentionally **out of scope** for this card
and will be tracked as a separate packaging task.

## Layout

```
packaging/
  macos/com.oktolabs.okto-neuron.plist   launchd LaunchAgent template
  brew/okto-neuron.rb                    Homebrew formula stub with `service` block
  README.md                             this file
```

---

## Option A — launchd LaunchAgent (no Homebrew required)

The plist in `macos/com.oktolabs.okto-neuron.plist` is a template with
two placeholders:

| Placeholder         | What to put there                                                                    |
| ------------------- | ------------------------------------------------------------------------------------ |
| `{{OKTO_NEURON_BIN}}`| Absolute path to the `okto-neuron` binary (output of `command -v okto-neuron`).        |
| `{{LOG_DIR}}`       | Absolute path to a writable log directory (e.g. `~/Library/Logs/okto-neuron`).        |

### Install

```sh
cp packaging/macos/com.oktolabs.okto-neuron.plist \
   ~/Library/LaunchAgents/com.oktolabs.okto-neuron.plist

# Edit the copy and replace every {{...}} placeholder:
$EDITOR ~/Library/LaunchAgents/com.oktolabs.okto-neuron.plist

mkdir -p ~/Library/Logs/okto-neuron

launchctl load  ~/Library/LaunchAgents/com.oktolabs.okto-neuron.plist
launchctl start com.oktolabs.okto-neuron
```

### Verify

```sh
launchctl list | grep com.oktolabs.okto-neuron
tail -f ~/Library/Logs/okto-neuron/okto-neuron.out.log \
        ~/Library/Logs/okto-neuron/okto-neuron.err.log
```

### Stop / uninstall

```sh
launchctl stop   com.oktolabs.okto-neuron
launchctl unload ~/Library/LaunchAgents/com.oktolabs.okto-neuron.plist
rm ~/Library/LaunchAgents/com.oktolabs.okto-neuron.plist
```

Uninstalling the plist does **not** delete vault data or logs.

---

## Option B — Homebrew formula stub

The formula in `brew/okto-neuron.rb` is a stub. It is not yet published
to a tap; the `url`/`sha256`/`version` fields are placeholders that
must be filled in at release time. The `service do` block defines the
opt-in background service shape.

Do not run this formula as an installer: the placeholder URL and checksum are
deliberately invalid. A real formula must be published to a tap with release
resources and CI before this section can become a supported install path.

### Start / stop the service (opt-in)

```sh
brew services start okto-neuron
brew services info  okto-neuron
brew services stop  okto-neuron
```

`brew install` alone never starts the service. Only
`brew services start okto-neuron` registers it with launchd via the
Homebrew wrapper. The application daemon starts without a process-global vault;
each browser tab selects or creates the vault it uses.

### Logs

```
$(brew --prefix)/var/log/okto-neuron/okto-neuron.out.log
$(brew --prefix)/var/log/okto-neuron/okto-neuron.err.log
```

---

## Hard rules

- **No auto-install hooks.** Nothing in the main `okto-neuron` Python
  package (CLI, server, install scripts) reaches into these files,
  copies the plist, or calls `launchctl`/`brew services`. The user
  always runs the registration command themselves.
- **No daemon-by-default.** `okto-neuron serve` runs in the foreground.
  `okto-neuron serve --daemon`, launchd, and `brew services` are all explicit
  opt-ins; only the built-in daemon is currently supported.
- **Linux systemd is out of scope** for this card and will land as a
  separate `packaging/linux/` task later.

## Dependency reproducibility contract

`pyproject.toml` is the compatibility contract published in source and wheel
metadata. Every direct build, runtime, optional, and development dependency has
an upper bound so a fresh installer cannot silently cross into an untested
breaking release line. Fast-moving integrations (`fastmcp` and `litellm`) stay
on the minor line exercised by the repository lock; stable libraries generally
stay below their next major release.

`uv.lock` is the exact environment tested from a source checkout. It is not
copied into the wheel and pip does not consume it. `fastembed` remains exactly
pinned for bit-stable embedding evaluations; the existing `ladybug` minor-line
policy is unchanged.

The base wheel includes Click because both published console entry points import
it directly, plus `email-validator` because EMAIL is part of the core Identifier
registry. Ladybug remains optional: importing `okto-neuron`, asking either entry
point for `--version`, and viewing command help do not load the graph backend;
running graph-backed library work without it reports `okto-neuron[ladybug]` as the
required install. JSON-LD export follows the same explicit boundary through
`okto-neuron[jsonld]`, which supplies RDFLib. The preferred public
`okto-neuron[serve]` aggregate closes the complete application server over MCP,
Ladybug, and embeddings. `okto-neuron[mcp]` is an identical compatibility alias so
the install command advertised by older releases remains functional; it is not a
smaller standalone server feature. A base-wheel server start names `[serve]`
actionably rather than exposing an import traceback.

Embedding providers never fall back silently to test vectors. The `embeddings`
extra directly supplies FastEmbed and NumPy, `sentence-transformers` supplies its
named local provider, and external providers report the `litellm` extra when it
is absent. The Bedrock extra includes both LiteLLM and Boto3 so it resolves on its
own instead of relying on a full-extras install to mask an incomplete contract.

The wheel is also a publication boundary. Its Python modules, generated UI,
packaged text resources, and metadata are scanned for private source paths,
credential shapes, and client- or person-derived identifiers. Product examples
must use fictional neutral names. The former personalized config-template helpers
were removed instead of preserved as an accidental public API; the corresponding
pilot and budget labels are now the generic `pilot` and `corpus` surfaces. The
retired GLiNER adapter remains import-compatible only long enough to warn and fail
with the configured extraction-pipeline remedy; GLiNER is absent from wheel
requirements and the packaged model manifest.

When intentionally upgrading a dependency line:

1. Change the bound in `pyproject.toml`, including both an optional extra and
   its matching dependency group when the dependency appears in both places.
2. Run `uv lock`, `uv lock --check`, and
   `uv run pytest tests/test_dependency_contract.py`.
3. Build the wheel and validate its metadata with
   `OKTO_NEURON_WHEEL=/path/to/wheel uv run pytest tests/test_dependency_contract.py`.
   This also installs the base wheel into a clean Python environment and verifies
   `import okto_neuron`, core EMAIL identifiers, both console entry points, and
   the actionable missing-Ladybug/server/embedding-provider paths. It installs
   `[serve]` separately, starts the real daemon with an isolated HOME and zero vaults,
   probes `/health` and `/version`, executes JSON-LD export through `[jsonld]`, and
   proves the retired legacy MCP constructors fail closed, checks the GLiNER and
   personalized-config retirements, and scans all packaged text and Python code.
4. Resolve each lower-level runtime extra independently in a clean environment before
   releasing it, then execute every composed public feature aggregate. A combined
   full-extras environment or raw third-party import list is not dependency evidence.

Compatible transitive releases may still move inside these direct dependency
ranges during a fresh pip install. The lockfile is the authoritative exact set
for development and CI; narrowing or pinning every transitive in wheel metadata
would turn the library package into its own package manager.
