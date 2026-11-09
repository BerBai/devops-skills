#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  ./install_offline_linux.sh [--prefix DIR] [--python-mode user|venv] [--plugins LIST]

Install devops-skills from an offline bundle on Linux.

The bundle carries the plugin tree plus wheels for the optional runtime
dependencies; the repository itself has no [build-system] and is installed by
copying, so there is no devops-skills wheel to install.

Options:
  --prefix DIR        Install location. Default: $HOME/.local/share/devops-skills
  --python-mode MODE  user: pip install --user so /usr/bin/env python3 can import deps.
                      venv: install into $PREFIX/.venv and print activation hint.
                      Default: user
  --plugins LIST      Comma-separated plugin names to print install commands for.
                      Default: all
  --skip-python       Do not install Python wheels.
EOF
}

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
bundle_root="$script_dir"
project_src="$bundle_root/devops-skills"
wheelhouse="$bundle_root/wheelhouse"
target_python=""
if [[ -f "$bundle_root/TARGET_PYTHON" ]]; then
  target_python="$(tr -d '[:space:]' < "$bundle_root/TARGET_PYTHON")"
fi

prefix="${HOME}/.local/share/devops-skills"
python_mode="user"
plugins="all"
skip_python=0
required_python="3.10"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --prefix)
      [[ $# -ge 2 ]] || { echo "missing value for --prefix" >&2; exit 2; }
      prefix="$2"
      shift 2
      ;;
    --python-mode)
      [[ $# -ge 2 ]] || { echo "missing value for --python-mode" >&2; exit 2; }
      python_mode="$2"
      shift 2
      ;;
    --plugins)
      [[ $# -ge 2 ]] || { echo "missing value for --plugins" >&2; exit 2; }
      plugins="$2"
      shift 2
      ;;
    --skip-python)
      skip_python=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ "$(uname -s)" != "Linux" ]]; then
  echo "This installer targets Linux. Current platform: $(uname -s)" >&2
  exit 1
fi

case "$python_mode" in
  user|venv) ;;
  *) echo "--python-mode must be user or venv" >&2; exit 2 ;;
esac

if [[ ! -d "$project_src/.claude-plugin" || ! -d "$project_src/plugins" ]]; then
  echo "bundle is missing devops-skills project files: $project_src" >&2
  exit 1
fi

python_meets_requirement() {
  local candidate="$1"
  command -v "$candidate" >/dev/null 2>&1 || return 1
  "$candidate" - "$required_python" "$target_python" <<'PY'
import sys

required = tuple(int(part) for part in sys.argv[1].split("."))
target = sys.argv[2]
if sys.version_info[:2] < required:
    raise SystemExit(1)
if target and f"{sys.version_info.major}.{sys.version_info.minor}" != target:
    raise SystemExit(1)
raise SystemExit(0)
PY
}

python_version() {
  "$1" - <<'PY'
import sys

print(f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")
PY
}

select_python() {
  if [[ -n "${PYTHON:-}" ]]; then
    if ! command -v "$PYTHON" >/dev/null 2>&1; then
      echo "python not found: $PYTHON" >&2
      exit 1
    fi
    if ! python_meets_requirement "$PYTHON"; then
      if [[ -n "$target_python" ]]; then
        echo "Python $target_python is required by this offline bundle; PYTHON=$PYTHON is $(python_version "$PYTHON")." >&2
      else
        echo "Python $required_python or newer is required; PYTHON=$PYTHON is $(python_version "$PYTHON")." >&2
      fi
      exit 1
    fi
    printf '%s\n' "$PYTHON"
    return
  fi

  local candidate
  if [[ -n "$target_python" ]]; then
    candidate="python${target_python}"
    if python_meets_requirement "$candidate"; then
      printf '%s\n' "$candidate"
      return
    fi
  fi
  for candidate in python3 python3.13 python3.12 python3.11 python3.10; do
    if python_meets_requirement "$candidate"; then
      printf '%s\n' "$candidate"
      return
    fi
  done

  local default_version="not found"
  if command -v python3 >/dev/null 2>&1; then
    default_version="$(python_version python3)"
  fi
  if [[ -n "$target_python" ]]; then
    echo "Python $target_python is required by this offline bundle; python3 is $default_version." >&2
    echo "Install Python $target_python, run with PYTHON=/path/to/python${target_python}, or pass --skip-python." >&2
  else
    echo "Python $required_python or newer is required to install Python wheels; python3 is $default_version." >&2
    echo "Install Python $required_python+, run with PYTHON=/path/to/python3.10+, or pass --skip-python." >&2
  fi
  exit 1
}

python_bin=""
if [[ "$skip_python" -eq 0 ]]; then
  python_bin="$(select_python)"
  echo "Using Python: $python_bin ($(python_version "$python_bin"))"
fi

repo_dst="$prefix/repo"
mkdir -p "$prefix"
rm -rf "$repo_dst"
mkdir -p "$repo_dst"

if command -v rsync >/dev/null 2>&1; then
  rsync -a "$project_src/" "$repo_dst/"
else
  (cd "$project_src" && tar -cf - .) | (cd "$repo_dst" && tar -xf -)
fi

if [[ "$skip_python" -eq 0 ]]; then
  if [[ ! -d "$wheelhouse" ]]; then
    echo "wheelhouse not found: $wheelhouse" >&2
    exit 1
  fi

  # The bundle carries wheels for the optional runtime dependencies, not for
  # this repository: it is a plugin marketplace with no [build-system], so
  # there is nothing to pip-install. An empty wheelhouse is a valid bundle.
  wheels=("$wheelhouse"/*.whl)
  if [[ "${wheels[0]}" == "$wheelhouse/*.whl" ]]; then
    echo "No dependency wheels in $wheelhouse; installing the plugin tree only." >&2
    wheels=()
  fi

  if [[ ${#wheels[@]} -gt 0 ]]; then
    if [[ "$python_mode" == "user" ]]; then
      "$python_bin" -m pip install --user --no-index --find-links "$wheelhouse" "${wheels[@]}"
    else
      venv_dir="$prefix/.venv"
      "$python_bin" -m venv "$venv_dir"
      "$venv_dir/bin/python" -m pip install --no-index --find-links "$wheelhouse" "${wheels[@]}"
    fi
  fi
fi

all_plugins=(
  ssh-core
  ssh-guarded
  remote-debug
  k8s-debug
  docker-quick
  log-aggregator
  iac-state
  cicd-debug
)

if [[ "$plugins" == "all" ]]; then
  selected_plugins=("${all_plugins[@]}")
else
  IFS=',' read -r -a selected_plugins <<< "$plugins"
fi

commands_file="$prefix/claude-plugin-install.txt"
{
  echo "/plugin marketplace add $repo_dst"
  for plugin in "${selected_plugins[@]}"; do
    plugin="$(echo "$plugin" | xargs)"
    [[ -n "$plugin" ]] || continue
    echo "/plugin install ${plugin}@devops-skills"
  done
} > "$commands_file"

cat <<EOF
Installed devops-skills offline files to:
  $repo_dst

Claude Code plugin commands were written to:
  $commands_file

Run these inside Claude Code:
EOF
cat "$commands_file"

if [[ "$python_mode" == "venv" && "$skip_python" -eq 0 ]]; then
  cat <<EOF

Python dependencies were installed into:
  $prefix/.venv

Start Claude Code from a shell with this venv activated if plugin scripts need
packages from the venv:
  source "$prefix/.venv/bin/activate"
EOF
fi
