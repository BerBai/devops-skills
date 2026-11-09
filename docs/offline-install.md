# Offline install

The marketplace normally pulls plugins from GitHub. On an air-gapped host — the
common case for a customer environment — you build a bundle on a connected
machine and carry it in.

Both scripts have lived in `scripts/` since the first release but were never
referenced from any document. This page is their contract.

## 1. Build the bundle (connected machine)

```bash
scripts/build_offline_bundle.sh --output-dir dist/
```

| Flag | Meaning |
|---|---|
| `--output-dir DIR` | Where the tarball lands (default `dist/offline`) |
| `--include-dev` | Also vendor `pytest`/`ruff`/`mypy` wheels |
| `--target-python VERSION` | Python the *target* runs (default: detected) |
| `--target-platform PLATFORM` | Wheel platform tag (default `manylinux2014_x86_64`) |

The bundle contains the marketplace files (`plugins/`, `.claude-plugin/`,
`README*.md`, `CONTRIBUTING*.md`, `pyproject.toml`, `scripts/`) plus wheels for
the optional runtime dependencies (`paramiko`, `pyyaml`, and `python-hcl2` when
the target is Python 3.9+). It does **not** contain the test suite or the
dotfiles — and no `devops-skills` wheel, because the repository is installed by
copying, not by building (there is deliberately no `[build-system]`).

## 2. Install on the target (offline)

```bash
tar xf devops-skills-*-linux-offline-*.tar.gz
cd devops-skills-offline
./install_offline_linux.sh --prefix "$HOME/.local/share/devops-skills"
```

| Flag | Meaning |
|---|---|
| `--prefix DIR` | Install root (default `$HOME/.local/share/devops-skills`) |
| `--python-mode user\|venv` | `--user` pip install, or a venv under `$PREFIX/.venv` |
| `--plugins LIST` | Comma-separated subset; default is all eight |
| `--skip-python` | Do not install the wheels; only lay down the plugin tree |

The installer copies the repository into `$PREFIX/repo/` (so the plugins stay
siblings, which is the layout `_ssh_core_locator.py` resolves), installs the
wheels if any are present, and writes `$PREFIX/claude-plugin-install.txt` with
the `/plugin marketplace add` + `/plugin install` lines to paste into Claude Code.

## Where the plugins expect ssh-core

Nothing in the bundle hardcodes a sibling layout. After installing, either keep
the eight plugins under one directory or export:

```bash
export DEVOPS_SSH_CORE_DIR="$PREFIX/repo/plugins/ssh-core/skills/ssh-core"
```

That variable is read by every diagnostic plugin's `_ssh_core_locator.py`
(see the repository `README.md`, "Where ssh-core lives").

## Verifying the result

```bash
"$PREFIX/repo/plugins/ssh-core/skills/ssh-core/scripts/run.sh" selfcheck.py
"$PREFIX/repo/plugins/ssh-core/skills/ssh-core/scripts/run.sh" doctor.py --host <alias>
```

`selfcheck.py` lists which tools exist; `doctor.py` actually exercises them and
exits 9 when a `--require`d capability regressed. If `python3` is missing on the
target, the launcher looks for `$DSH_PYTHON` and then the harness payload path.
