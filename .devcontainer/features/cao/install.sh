#!/usr/bin/env bash
# Devcontainer feature install script for CLI Agent Orchestrator (CAO)
# https://github.com/awslabs/cli-agent-orchestrator
set -euo pipefail

VERSION="${VERSION:-latest}"
WEBUI="${WEBUI:-false}"
PORT="${PORT:-9889}"
AUTOSTART="${AUTOSTART:-false}"

REPO_URL="${REPO_URL:-https://github.com/awslabs/cli-agent-orchestrator.git}"
INSTALL_DIR="/usr/local/share/cao"

echo "Installing CLI Agent Orchestrator (version: ${VERSION})..."

# Install system dependencies with distro-aware package manager detection.
if command -v apt-get &>/dev/null; then
    apt-get update -y \
        && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends ca-certificates git curl kitty \
        && rm -rf /var/lib/apt/lists/*
elif command -v apk &>/dev/null; then
    apk add --no-cache ca-certificates git curl kitty
else
    echo "ERROR: Unsupported base image. Expected apt-get or apk to install dependencies." >&2
    exit 1
fi

ensure_kitty() {
    if command -v kitty &>/dev/null && command -v kitten &>/dev/null; then
        echo "kitty already installed: $(kitty --version)"
        return 0
    fi

    echo "ERROR: kitty and kitten are required, but one or both were not found on PATH." >&2
    return 1
}

# Clone repository to a fixed location so editable install keeps
# web UI asset paths correct relative to the Python package source.
mkdir -p "$INSTALL_DIR"
rm -rf "$INSTALL_DIR/repo"
if [[ "$VERSION" = "latest" ]]; then
    git clone --depth 1 "$REPO_URL" "$INSTALL_DIR/repo"
else
    # For branch/tag refs, prefer a shallow clone for faster image builds.
    if [[ ! "$VERSION" =~ ^[0-9a-fA-F]{7,40}$ ]] && git clone --depth 1 --branch "$VERSION" "$REPO_URL" "$INSTALL_DIR/repo"; then
        :
    else
        # For commit SHAs or unknown refs, try filtered clone first to reduce transfer cost.
        if ! git clone --filter=blob:none "$REPO_URL" "$INSTALL_DIR/repo"; then
            git clone "$REPO_URL" "$INSTALL_DIR/repo"
        fi
        if ! git -C "$INSTALL_DIR/repo" checkout "$VERSION"; then
            rm -rf "$INSTALL_DIR/repo"
            git clone "$REPO_URL" "$INSTALL_DIR/repo"
            if ! git -C "$INSTALL_DIR/repo" checkout "$VERSION"; then
                echo "ERROR: Version '${VERSION}' not found in repository ${REPO_URL}." >&2
                exit 1
            fi
        fi
    fi
fi

ensure_kitty || {
    echo "ERROR: Could not install kitty." >&2
    exit 1
}

pip_install_editable() {
    local target="$1"
    local -a pip_args=(--no-cache-dir)
    if python3 -m pip install --help 2>/dev/null | grep -q break-system-packages; then
        pip_args+=(--break-system-packages)
    fi
    python3 -m pip install "${pip_args[@]}" -e "$target"
}

# Editable install keeps server static asset resolution aligned with
# the checked out source layout for the selected version.
pip_install_editable "$INSTALL_DIR/repo"

# Build web UI if requested
if [[ "$WEBUI" = "true" ]]; then
    if ! command -v npm &>/dev/null; then
        echo "ERROR: npm is not available. Install the Node.js devcontainer feature before this one, or set webui=false." >&2
        exit 1
    fi
    resolve_web_project_dir() {
        local repo="$1"
        local candidate
        for candidate in "$repo/web" "$repo/frontend" "$repo/ui"; do
            if [[ -f "$candidate/package.json" ]]; then
                printf '%s\n' "$candidate"
                return 0
            fi
        done
        echo "ERROR: Could not locate web UI npm project under $repo." >&2
        echo "Supported layouts include repo/web (package.json) and built artifacts under:" >&2
        echo "  - repo/web/dist/index.html" >&2
        echo "  - repo/src/cli_agent_orchestrator/web_ui/index.html" >&2
        return 1
    }

    web_project_dir="$(resolve_web_project_dir "$INSTALL_DIR/repo")"
    echo "Building web UI in ${web_project_dir}..."
    cd "$web_project_dir"
    if [[ -f package-lock.json ]]; then
        npm ci
    else
        npm install
    fi
    npm run build
    echo "Web UI built successfully."
fi

# Create entrypoint script that optionally starts cao-server on container start
AUTOSTART_DEFAULT_LITERAL="$(printf '%q' "$AUTOSTART")"
PORT_DEFAULT_LITERAL="$(printf '%q' "$PORT")"

{
cat << EOF
#!/usr/bin/env bash
# CAO devcontainer entrypoint
AUTOSTART_DEFAULT=${AUTOSTART_DEFAULT_LITERAL}
PORT_DEFAULT=${PORT_DEFAULT_LITERAL}
EOF

cat << 'EOF'
set -euo pipefail

AUTOSTART_VALUE="${AUTOSTART:-$AUTOSTART_DEFAULT}"
PORT_VALUE="${PORT:-$PORT_DEFAULT}"

if [[ "$AUTOSTART_VALUE" = "true" ]]; then
    echo "Starting cao-server on port $PORT_VALUE..."
    exec cao-server --host 0.0.0.0 --port "$PORT_VALUE"
fi

if [[ "$#" -gt 0 ]]; then
    exec "$@"
fi

exec tail -f /dev/null
EOF
} > "$INSTALL_DIR/entrypoint.sh"
chmod +x "$INSTALL_DIR/entrypoint.sh"

echo "CLI Agent Orchestrator installed successfully."
echo "  - Run 'cao --help' to verify the CLI."
echo "  - Run 'cao-server --help' to see server options."
if [[ "$WEBUI" = "true" ]]; then
    echo "  - Web UI will be served at http://localhost:${PORT} when cao-server is running."
fi
