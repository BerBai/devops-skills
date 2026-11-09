#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  scripts/build_offline_bundle.sh [--output-dir DIR] [--include-dev]
                                  [--target-python VERSION]
                                  [--target-platform PLATFORM]

Build a Linux-target offline bundle for this repository.

Run this on a machine with internet access. The generated tarball contains:
  - a copy of the plugin repository files needed by Claude Code
  - Python wheels for the optional runtime dependencies (paramiko, PyYAML,
    python-hcl2), and the dev tools with --include-dev
  - scripts/install_offline_linux.sh copied to the bundle root

The repository is a plugin marketplace, not an installable package: it has no
[build-system] on purpose, so the bundle carries no devops-skills wheel. The
installer copies the plugin tree into the prefix and prints the
`/plugin install` commands.

Options:
  --output-dir DIR            Output directory. Default: dist/offline
  --include-dev               Also cache pytest/ruff/mypy wheels
  --target-python VERSION     Target CPython minor version. Default: builder Python minor.
                              Example: 3.12
  --target-platform PLATFORM  Target pip platform tag. Default: manylinux2014_x86_64
EOF
}

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
output_dir="$repo_root/dist/offline"
include_dev=0
required_python="3.10"
target_python=""
target_platform="manylinux2014_x86_64"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --output-dir)
      [[ $# -ge 2 ]] || { echo "missing value for --output-dir" >&2; exit 2; }
      output_dir="$2"
      shift 2
      ;;
    --include-dev)
      include_dev=1
      shift
      ;;
    --target-python)
      [[ $# -ge 2 ]] || { echo "missing value for --target-python" >&2; exit 2; }
      target_python="$2"
      shift 2
      ;;
    --target-platform)
      [[ $# -ge 2 ]] || { echo "missing value for --target-platform" >&2; exit 2; }
      target_platform="$2"
      shift 2
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

python_meets_requirement() {
  local candidate="$1"
  command -v "$candidate" >/dev/null 2>&1 || return 1
  "$candidate" - "$required_python" <<'PY'
import sys

required = tuple(int(part) for part in sys.argv[1].split("."))
raise SystemExit(0 if sys.version_info[:2] >= required else 1)
PY
}

python_version() {
  "$1" - <<'PY'
import sys

print(f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")
PY
}

python_minor() {
  "$1" - <<'PY'
import sys

print(f"{sys.version_info.major}.{sys.version_info.minor}")
PY
}

select_python() {
  if [[ -n "${PYTHON:-}" ]]; then
    if ! command -v "$PYTHON" >/dev/null 2>&1; then
      echo "python not found: $PYTHON" >&2
      exit 1
    fi
    if ! python_meets_requirement "$PYTHON"; then
      echo "Python $required_python or newer is required; PYTHON=$PYTHON is $(python_version "$PYTHON")." >&2
      exit 1
    fi
    printf '%s\n' "$PYTHON"
    return
  fi

  local candidate
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
  echo "Python $required_python or newer is required to build the offline bundle; python3 is $default_version." >&2
  echo "Install Python $required_python+ or run with PYTHON=/path/to/python3.10+." >&2
  exit 1
}

python_bin="$(select_python)"
echo "Using Python: $python_bin ($(python_version "$python_bin"))"

if [[ "$output_dir" != /* ]]; then
  output_dir="$(pwd -P)/$output_dir"
fi

if [[ -z "$target_python" ]]; then
  target_python="$(python_minor "$python_bin")"
fi
if [[ ! "$target_python" =~ ^3\.[0-9]+$ ]]; then
  echo "--target-python must be a CPython minor version like 3.12; got: $target_python" >&2
  exit 2
fi
"$python_bin" - "$required_python" "$target_python" <<'PY'
import sys

required = tuple(int(part) for part in sys.argv[1].split("."))
target = tuple(int(part) for part in sys.argv[2].split("."))
if target < required:
    print(
        f"--target-python must be {sys.argv[1]} or newer; got: {sys.argv[2]}",
        file=sys.stderr,
    )
    raise SystemExit(2)
PY
target_python_tag="${target_python/./}"
echo "Target Linux wheels: CPython $target_python on $target_platform"

mkdir -p "$output_dir"
stage="$(mktemp -d "${TMPDIR:-/tmp}/devops-skills-offline.XXXXXX")"
cleanup() {
  rm -rf "$stage"
}
trap cleanup EXIT

bundle_root="$stage/devops-skills-offline"
project_copy="$bundle_root/devops-skills"
wheelhouse="$bundle_root/wheelhouse"
mkdir -p "$project_copy" "$wheelhouse"

copy_path() {
  local rel="$1"
  if [[ -e "$repo_root/$rel" ]]; then
    mkdir -p "$project_copy/$(dirname "$rel")"
    cp -a "$repo_root/$rel" "$project_copy/$rel"
  fi
}

copy_path ".claude-plugin"
copy_path "plugins"
copy_path "README.md"
copy_path "README.zh-CN.md"
copy_path "CONTRIBUTING.md"
copy_path "CONTRIBUTING.zh-CN.md"
copy_path "LICENSE"
copy_path "pyproject.toml"
copy_path "scripts"

cp "$repo_root/scripts/install_offline_linux.sh" "$bundle_root/install_offline_linux.sh"
chmod +x "$bundle_root/install_offline_linux.sh"

# The repository has no [build-system] on purpose: it is installed by copying
# plugins into an agent's skill directory, never by building a wheel. Only the
# optional runtime dependencies are downloaded here.
echo "Downloading dependency wheels..."
target_minor="${target_python#3.}"
runtime_requirements=("paramiko>=3.0,<4" "pyyaml>=6.0")
if [[ "$target_minor" -ge 9 ]]; then
  runtime_requirements+=("python-hcl2>=8.1,<9")
else
  echo "Target Python $target_python < 3.9: skipping python-hcl2 (its 8.x needs 3.9+)"
fi
if [[ "$include_dev" -eq 1 ]]; then
  runtime_requirements+=("pytest>=8.0" "pytest-cov>=5.0" "ruff>=0.4" "mypy>=1.10" "types-PyYAML>=6.0")
fi
"$python_bin" -m pip download \
  --dest "$wheelhouse" \
  --only-binary=:all: \
  --platform "$target_platform" \
  --implementation cp \
  --python-version "$target_python_tag" \
  --abi "cp${target_python_tag}" \
  "${runtime_requirements[@]}"

printf '%s\n' "$target_python" > "$bundle_root/TARGET_PYTHON"
printf '%s\n' "$target_platform" > "$bundle_root/TARGET_PLATFORM"

manifest="$bundle_root/SHA256SUMS"
(
  cd "$bundle_root"
  if command -v sha256sum >/dev/null 2>&1; then
    find . -type f ! -name SHA256SUMS -print0 | sort -z | xargs -0 sha256sum > "$manifest"
  elif command -v shasum >/dev/null 2>&1; then
    find . -type f ! -name SHA256SUMS -print0 | sort -z | xargs -0 shasum -a 256 > "$manifest"
  else
    echo "warning: sha256sum/shasum not found; skipping SHA256SUMS" >&2
    rm -f "$manifest"
  fi
)

version="$("$python_bin" - <<'PY' "$repo_root/pyproject.toml"
import pathlib
import re
import sys

text = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")
match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.M)
print(match.group(1) if match else "unknown")
PY
)"
stamp="$(date +%Y%m%d%H%M%S)"
bundle_name="devops-skills-${version}-linux-offline-${stamp}.tar.gz"
bundle_path="$output_dir/$bundle_name"

(
  cd "$stage"
  tar -czf "$bundle_path" devops-skills-offline
)

cat <<EOF
Offline bundle created:
  $bundle_path

Copy it to the Linux target, then run:
  tar -xzf $bundle_name
  cd devops-skills-offline
  ./install_offline_linux.sh
EOF
