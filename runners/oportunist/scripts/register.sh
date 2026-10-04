#!/usr/bin/env bash
set -euo pipefail

log() { printf '[register] %s\n' "$*"; }
fail() { log "ERROR: $*"; exit "${2:-1}"; }

[ "$(id -u)" -eq 0 ] || fail "register must run as root"
: "${REPO_URL:?REPO_URL is required}"

RUNNER_NAME="${RUNNER_NAME:-oportunist-ci-$(hostname)}"
RUNNER_LABELS="${RUNNER_LABELS:-oportunist-ci,codex}"
RUNNER_STATE_DIR="${RUNNER_STATE_DIR:-/var/lib/oportunist-runner/state}"
RUNNER_RUNTIME_DIR="${RUNNER_RUNTIME_DIR:-/var/lib/oportunist-runner/runtime}"
RUNNER_WORK_DIR="${RUNNER_WORK_DIR:-/var/lib/oportunist-runner/work}"
RUNNER_ROOT="${RUNNER_ROOT:-/opt/actions-runner}"

install -d -m 0750 -o runner -g runner "$RUNNER_STATE_DIR" "$RUNNER_RUNTIME_DIR" "$RUNNER_WORK_DIR"

if [ -s "$RUNNER_STATE_DIR/.runner" ] \
   && [ -s "$RUNNER_STATE_DIR/.credentials" ] \
   && [ -s "$RUNNER_STATE_DIR/diagnostics.txt" ]; then
    existing_repo="$(sed -n 's/^repo_url=//p' "$RUNNER_STATE_DIR/diagnostics.txt")"
    existing_name="$(sed -n 's/^runner_name=//p' "$RUNNER_STATE_DIR/diagnostics.txt")"
    existing_labels="$(sed -n 's/^runner_labels=//p' "$RUNNER_STATE_DIR/diagnostics.txt")"
    if ! jq -e '(.Ephemeral // false) == false' "$RUNNER_STATE_DIR/.runner" >/dev/null 2>&1; then
        [ -n "${RUNNER_TOKEN:-}" ] || fail "persisted runner registration is ephemeral/one-shot; set a fresh OPORTUNIST_RUNNER_TOKEN and recreate the listener once" 2
    elif [ "$existing_repo" = "$REPO_URL" ] \
       && [ "$existing_name" = "$RUNNER_NAME" ] \
       && [ "$existing_labels" = "$RUNNER_LABELS" ]; then
        log "existing persistent registration matches requested identity"
        exit 0
    fi
fi

[ -n "${RUNNER_TOKEN:-}" ] || fail "RUNNER_TOKEN is required for initial registration or identity change" 2

rm -rf "$RUNNER_RUNTIME_DIR"
mkdir -p "$RUNNER_RUNTIME_DIR"
cp -a "$RUNNER_ROOT/." "$RUNNER_RUNTIME_DIR/"
chown -R runner:runner "$RUNNER_RUNTIME_DIR" "$RUNNER_WORK_DIR"

set +e
gosu runner env HOME="$RUNNER_RUNTIME_DIR" \
    "$RUNNER_RUNTIME_DIR/config.sh" \
    --unattended \
    --replace \
    --disableupdate \
    --url "$REPO_URL" \
    --token "$RUNNER_TOKEN" \
    --name "$RUNNER_NAME" \
    --labels "$RUNNER_LABELS" \
    --work "$RUNNER_WORK_DIR"
rc=$?
set -e
unset RUNNER_TOKEN
[ "$rc" -eq 0 ] || fail "GitHub runner configuration failed" 3

for file in .runner .credentials .credentials_rsaparams; do
    if [ -f "$RUNNER_RUNTIME_DIR/$file" ]; then
        cp "$RUNNER_RUNTIME_DIR/$file" "$RUNNER_STATE_DIR/$file"
    fi
done

cat > "$RUNNER_STATE_DIR/diagnostics.txt" <<EOF
repo_url=$REPO_URL
runner_name=$RUNNER_NAME
runner_labels=$RUNNER_LABELS
EOF

chown -R runner:runner "$RUNNER_STATE_DIR"
chmod 0640 "$RUNNER_STATE_DIR/.runner"
chmod 0600 "$RUNNER_STATE_DIR"/.credentials* 2>/dev/null || true
rm -rf "$RUNNER_RUNTIME_DIR"
log "registration complete"
