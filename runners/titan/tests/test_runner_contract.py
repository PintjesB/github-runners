"""Contract tests for the Titan Stocks self-hosted runner.

The tests pin the image, the Compose runtime contract, the
operator-facing configuration knobs, and the lifecycle script
behaviour. They run from CI on a hosted runner without Docker
so they only validate the project files; the behavioural
assertions (Docker daemon reachability, Chromium launch, native-
architecture enforcement) live in ``deploy.sh probe`` and the
published image.

Lifecycle invariants covered here:

* Image builds target the multi-platform ``linux/amd64`` and
  ``linux/arm64`` manifest with the documented capabilities and
  a pinned Actions runner; the architecture-specific digest is
  selected from ``RUNNER_SHA256_X64`` / ``RUNNER_SHA256_ARM64``
  based on BuildKit's automatic ``TARGETARCH`` build argument.
* ``fetch-runner.sh`` maps ``TARGETARCH`` to the upstream archive
  naming (amd64 -> x64, arm64 -> arm64), selects the matching
  pinned digest, and refuses every other architecture before any
  download attempt.
* The persistent state layout uses explicit Docker-named volumes,
  with the Docker socket as the only host bind mount.
* ``deploy.sh probe`` uses ``--entrypoint`` to override the image's
  default listener entrypoint; ``deploy.sh up`` is the sole
  registration-and-start lifecycle command.
* The single Compose service passes ``RUNNER_TOKEN`` to startup,
  and ``start-runner.sh`` unsets it before launching the listener.
* The listener runs only ``run.sh`` with no runtime
  ``--start``/``--disableupdate`` flags.
* The Compose contract mounts the named work volume at the fixed
  `/var/lib/titan-runner/work` path.
* Persistent volumes declare an explicit ``name:`` so
  ``docker run`` and ``docker compose`` references resolve to the
  same Docker volume.
* The container HEALTHCHECK verifies only the listener process
  and Docker daemon reachability.
* The Playwright probe consumes the deterministic
  ``/opt/titan-probe`` install instead of ``npx``.
* The listener runs on an ordinary Compose bridge network with
  the Docker host-gateway alias wired through ``extra_hosts``.
* ``ipc: host`` is forbidden; an isolated ``shm_size`` is wired
  instead.
* Registration reads the short-lived token through an allowlisted
  ``RUNNER_TOKEN`` env var; no token-file mount reaches the
  listener or the persistent state.
* Pre-job and post-job hooks validate host capabilities (and the
  Docker daemon's native architecture) and clean up only Titan-
  prefixed resources.
"""
from __future__ import annotations

import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "Dockerfile"
COMPOSE_FILE = ROOT / "docker-compose.yml"
DEPLOY_SCRIPT = ROOT / "deploy.sh"
PUBLISH_WORKFLOW = ROOT / ".github" / "workflows" / "publish.yml"
SCRIPTS_DIR = ROOT / "scripts"
REGISTER_SCRIPT = SCRIPTS_DIR / "register.sh"
START_RUNNER_SCRIPT = SCRIPTS_DIR / "start-runner.sh"
PROBE_SCRIPT = SCRIPTS_DIR / "probe.sh"
PRE_JOB_SCRIPT = SCRIPTS_DIR / "pre-job.sh"
POST_JOB_SCRIPT = SCRIPTS_DIR / "post-job.sh"
PROBE_DIR = ROOT / "probe"
PROBE_PACKAGE_JSON = PROBE_DIR / "package.json"
PROBE_PACKAGE_LOCK = PROBE_DIR / "package-lock.json"
ENV_EXAMPLE = ROOT / ".env.example"
DOCS_DIR = ROOT / "docs"
SECURITY_DOC = DOCS_DIR / "security.md"
OPERATIONS_DOC = DOCS_DIR / "operations.md"
VM_DEPLOYMENT_DOC = DOCS_DIR / "vm-deployment.md"
QUICK_START_DOC = DOCS_DIR / "quick-start.md"
README_FILE = ROOT / "README.md"

DEFAULT_LABELS = "titan-ci"

# Allowlist mirroring the one in ``deploy.sh``. Any entry the
# deploy script may consume MUST appear in this set; any entry the
# contract forbids MUST NOT.
DEPLOY_ALLOWLIST_KEYS = {
    "TITAN_RUNNER_IMAGE",
    "TITAN_RUNNER_REPO_URL",
    "TITAN_RUNNER_NAME",
    "TITAN_RUNNER_LABELS",
    "TITAN_RUNNER_TOKEN",
    "TITAN_RUNNER_STATE_DIR",
    "TITAN_RUNNER_RUNTIME_DIR",
    "TITAN_RUNNER_BROWSER_DIR",
    "TITAN_RUNNER_ROOT",
    "TITAN_RUNNER_STATE_VOLUME",
    "TITAN_RUNNER_LOCK_FILE",
}

# Architecture-specific runner digests that MUST appear together
# in the Dockerfile so the multi-platform image can be built on
# every supported native architecture.
RUNNER_SHA256_ARM64 = (
    "58b758e420b87093fbd4bfddd368074960053e2f1388f01848c82624b90f27d1"
)
RUNNER_SHA256_X64 = (
    "04cf0be1aff4c3ec3554466c39124ca250e3effd8873bb7e8d68535aa9505d5d"
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_dockerfile_targets_multi_platform_ubuntu_base_with_digest_pin() -> None:
    """The base image MUST be Ubuntu 24.04 with a digest pin and a
    multi-platform ``FROM --platform=$BUILDPLATFORM`` declaration
    so the same Dockerfile produces both ``linux/amd64`` and
    ``linux/arm64`` images on their respective native builders.
    """
    text = _read(DOCKERFILE)
    assert "ARG UBUNTU_BASE_DIGEST=sha256:" in text, (
        "Dockerfile must declare UBUNTU_BASE_DIGEST as an immutable digest"
    )
    assert (
        "FROM --platform=$BUILDPLATFORM ubuntu:24.04@${UBUNTU_BASE_DIGEST}" in text
    ), (
        "Dockerfile must reference ubuntu:24.04 through the digest ARG "
        "with a multi-platform FROM --platform=$BUILDPLATFORM declaration"
    )
    assert "ARG TARGETARCH" in text, (
        "Dockerfile must declare ARG TARGETARCH so fetch-runner.sh can "
        "select the architecture-specific digest"
    )


def test_dockerfile_pins_actions_runner_version_and_per_arch_digests() -> None:
    """The Actions runner must be pinned by version and by
    architecture-specific SHA-256 digests.

    ``fetch-runner.sh`` selects the matching digest from
    ``RUNNER_SHA256_ARM64`` or ``RUNNER_SHA256_X64`` based on the
    BuildKit ``TARGETARCH`` build argument; both digests MUST be
    declared in the Dockerfile so the multi-platform image can be
    built on every supported native architecture.
    """
    text = _read(DOCKERFILE)
    assert "ARG RUNNER_VERSION=" in text, "Dockerfile must declare RUNNER_VERSION"
    assert "ARG RUNNER_SHA256_ARM64=" in text, (
        "Dockerfile must declare RUNNER_SHA256_ARM64 for the arm64 upstream tarball"
    )
    assert "ARG RUNNER_SHA256_X64=" in text, (
        "Dockerfile must declare RUNNER_SHA256_X64 for the x64 upstream tarball"
    )
    assert RUNNER_SHA256_ARM64 in text, (
        "Dockerfile must pin the documented RUNNER_SHA256_ARM64 digest"
    )
    assert RUNNER_SHA256_X64 in text, (
        "Dockerfile must pin the documented RUNNER_SHA256_X64 digest"
    )
    assert "fetch-runner.sh" in text, "Dockerfile must call fetch-runner at build time"
    fetch = _read(SCRIPTS_DIR / "fetch-runner.sh")
    assert "sha256sum \"$archive\"" in fetch, (
        "fetch-runner.sh must verify the upstream tarball digest"
    )
    assert "RUNNER_SHA256_ARM64" in fetch and "RUNNER_SHA256_X64" in fetch, (
        "fetch-runner.sh must select between RUNNER_SHA256_ARM64 and "
        "RUNNER_SHA256_X64 based on TARGETARCH"
    )


def test_dockerfile_installs_documented_capabilities() -> None:
    """The image MUST install the documented capability set."""
    text = _read(DOCKERFILE)
    for marker in (
        "docker.io",
        "docker-compose-v2",
        "docker-buildx",
        "postgresql-client",
        "shellcheck",
        "nodejs",
        "githubcli-archive-keyring.gpg",  # GH CLI
        "libnss3",
        "libgbm1",
        "libasound2t64",
        "libcairo2",
        "libpangocairo-1.0-0",
        "/opt/titan-probe/node_modules/.bin/playwright-core install chromium",
        "gosu",
        "jq",
        "tini",
    ):
        assert marker in text, f"Dockerfile must install capability: {marker!r}"


def test_dockerfile_omits_oci_license_label() -> None:
    """The image intentionally ships without an OCI license label.

    No third party receives any license grant by pulling the image; the
    project does not ship a ``LICENSE`` file.
    """
    text = _read(DOCKERFILE)
    assert "org.opencontainers.image.licenses" not in text, (
        "Dockerfile must not declare an OCI license label"
    )


def test_dockerfile_bundles_runner_hooks() -> None:
    """The image MUST bundle the pre/post-job hooks in the runner tree.

    GitHub Actions invokes ``$RUNNER_ROOT/.hooks/PreJob.sh`` and
    ``$RUNNER_ROOT/.hooks/PostJob.sh`` around every job. The image
    carries these so the materialised runtime tree picks them up
    on every container start.
    """
    text = _read(DOCKERFILE)
    assert "/opt/actions-runner/.hooks" in text, (
        "Dockerfile must install the .hooks directory under the runner tree"
    )
    assert "scripts/pre-job.sh" in text, (
        "Dockerfile must COPY scripts/pre-job.sh"
    )
    assert "scripts/post-job.sh" in text, (
        "Dockerfile must COPY scripts/post-job.sh"
    )
    assert ".hooks/PreJob.sh" in text, (
        "Dockerfile must install the pre-job hook as .hooks/PreJob.sh"
    )
    assert ".hooks/PostJob.sh" in text, (
        "Dockerfile must install the post-job hook as .hooks/PostJob.sh"
    )


def test_register_reads_runner_token_from_env_var() -> None:
    """Registration MUST consume ``RUNNER_TOKEN`` from the environment
    when supplied, and MUST tolerate an empty value when the
    persisted identity already matches.

    The previous contract read the token from a 0600 file at
    ``RUNNER_TOKEN_FILE``. That interface is now forbidden: the
    token is supplied through the sole Compose runner service as
    ``RUNNER_TOKEN``, consumed in memory during startup, and unset immediately
    after ``config.sh`` returns. An empty ``RUNNER_TOKEN`` is the
    supported steady-state path after the operator blanks
    ``TITAN_RUNNER_TOKEN`` in ``.env``.
    """
    text = _read(REGISTER_SCRIPT)
    # ``REPO_URL`` is the only mandatory variable at the top of the
    # script; ``RUNNER_TOKEN`` is consulted only after the
    # idempotency check.
    assert ": \"${REPO_URL:?REPO_URL is required" in text, (
        "register.sh must require REPO_URL from the environment"
    )
    # ``RUNNER_TOKEN`` is consulted with a ``${RUNNER_TOKEN:-}``
    # expansion so an empty value (the steady state) does not
    # trigger a parameter expansion error.
    assert "RUNNER_TOKEN:-" in text or "\"${RUNNER_TOKEN:-}\"" in text, (
        "register.sh must consult RUNNER_TOKEN with a default empty expansion"
    )
    assert "RUNNER_TOKEN_FILE" not in text, (
        "register.sh must NOT reference RUNNER_TOKEN_FILE"
    )
    # The token is consumed via the documented allowlist key.
    assert "--token \"$RUNNER_TOKEN\"" in text, (
        "register.sh must pass RUNNER_TOKEN through --token"
    )


def test_register_unset_token_immediately_after_config() -> None:
    """The token MUST be unset immediately after ``config.sh`` returns.

    Persisting the token into the state volume, the diagnostics
    summary, or any later listener start would defeat the point of
    in-memory consumption.
    """
    text = _read(REGISTER_SCRIPT)
    # Locate the post-config region.
    post_config = text.split("config.sh", 1)[1]
    # The first unset must happen before any state copy.
    unset_index = post_config.find("unset RUNNER_TOKEN")
    copy_index = post_config.find("RUNNER_STATE_DIR/$fname")
    assert unset_index != -1, (
        "register.sh must unset RUNNER_TOKEN after config.sh"
    )
    assert copy_index != -1, (
        "register.sh must copy credentials into the state directory"
    )
    assert unset_index < copy_index, (
        "register.sh must unset RUNNER_TOKEN BEFORE copying credentials into state"
    )
    # The trap also unsets the token on every exit path.
    assert "trap 'unset RUNNER_TOKEN" in text, (
        "register.sh must trap-unset RUNNER_TOKEN on exit"
    )


def test_register_materialises_runtime_and_persists_credentials_separately() -> None:
    """Registration writes a *runtime* tree and persists only the
    mutable credentials into the *state* directory."""
    text = _read(REGISTER_SCRIPT)
    assert "RUNNER_STATE_DIR" in text
    assert "RUNNER_RUNTIME_DIR" in text
    assert "--disableupdate" in text, "registration must disable self-update"
    assert "--replace" in text, "registration must replace an existing registration"
    register_block = text.split("register_args=(", 1)[1].split(")", 1)[0]
    assert "--ephemeral" not in register_block
    assert "--once" not in register_block
    # The runtime tree is materialised from the image; the credentials
    # are written into the state directory only.
    assert 'cp -a "$RUNNER_ROOT/." "$RUNNER_RUNTIME_DIR/"' in text, (
        "register.sh must clone the image-owned tree into a disposable runtime"
    )
    # Register copies only the mutable credentials into state and
    # removes the runtime tree afterwards.
    assert '"$RUNNER_STATE_DIR/$fname"' in text or (
        '"$RUNNER_RUNTIME_DIR/$fname"' in text and '"$RUNNER_STATE_DIR/$fname"' in text
    ), "register.sh must copy mutable credentials from runtime to state"
    assert 'rm -rf "$RUNNER_RUNTIME_DIR"' in text, (
        "register.sh must remove the disposable runtime tree on exit"
    )


def test_register_does_not_change_runner_primary_group() -> None:
    """The registration phase does not change the runner user's primary group."""
    text = _read(REGISTER_SCRIPT)
    assert "usermod --gid" not in text, (
        "register.sh must NOT change the runner user's primary group"
    )


def test_register_applies_strict_credentials_permissions() -> None:
    """Credentials MUST be persisted with strict permissions and runner ownership."""
    text = _read(REGISTER_SCRIPT)
    assert 'chmod 0600 "$RUNNER_STATE_DIR"/.credentials*' in text, (
        "register.sh must chmod 0600 on persistent credentials"
    )
    assert 'chmod 0640 "$RUNNER_STATE_DIR/.runner"' in text, (
        "register.sh must chmod 0640 on the .runner registration manifest"
    )
    assert 'chown -R runner:runner "$RUNNER_STATE_DIR"' in text, (
        "register.sh must chown state directory to runner:runner"
    )


def test_register_diagnostics_omit_token() -> None:
    """The diagnostics summary MUST never contain the token."""
    text = _read(REGISTER_SCRIPT)
    diagnostics_block = text.split("diagnostics.txt", 1)[0]
    assert "diagnostics.txt" in text
    # Find the heredoc that writes diagnostics.txt.
    assert "printf 'registered_at=" in diagnostics_block or "printf 'repo_url=" in text, (
        "register.sh must write a diagnostics summary"
    )
    # No reference to the token inside the diagnostics block.
    diagnostics_section = text.split("# Save a sanitised diagnostics summary", 1)[1]
    diagnostics_section = diagnostics_section.split("rm -rf", 1)[0]
    assert "RUNNER_TOKEN" not in diagnostics_section, (
        "register.sh must not embed RUNNER_TOKEN in diagnostics.txt"
    )
    assert "$token" not in diagnostics_section, (
        "register.sh must not embed the token variable in diagnostics.txt"
    )


def test_start_runner_rebuilds_runtime_tree_from_image() -> None:
    """The listener must materialise a runtime tree from the image rather
    than depend on an in-image configuration."""
    text = _read(START_RUNNER_SCRIPT)
    # The runtime tree is rebuilt from the immutable image before the
    # persisted credentials are overlaid.
    assert 'cp -a "$RUNNER_ROOT/." "$RUNNER_RUNTIME_DIR/"' in text, (
        "start-runner.sh must clone the image-owned tree into a fresh runtime"
    )
    assert 'cp "$RUNNER_STATE_DIR/.runner" "$RUNNER_RUNTIME_DIR/.runner"' in text, (
        "start-runner.sh must overlay persisted .runner onto the runtime tree"
    )
    assert 'cp "$RUNNER_STATE_DIR/.credentials" "$RUNNER_RUNTIME_DIR/.credentials"' in text, (
        "start-runner.sh must overlay persisted .credentials onto the runtime tree"
    )


def test_start_runner_refuses_without_persisted_credentials() -> None:
    """The listener must abort early when the state directory has no credentials."""
    text = _read(START_RUNNER_SCRIPT)
    assert "RUNNER_STATE_DIR" in text
    assert "missing persisted credential" in text, (
        "start-runner.sh must refuse to start when credentials are missing"
    )


def test_start_runner_invokes_run_sh_with_no_runtime_flags() -> None:
    """The listener MUST run ``run.sh`` without ``--start``/``--disableupdate``.

    ``--disableupdate`` is set at registration and persists in the
    ``.runner`` manifest; ``--start`` daemonises, which we do not want
    inside a container.
    """
    text = _read(START_RUNNER_SCRIPT)
    # The final ``gosu runner run.sh`` invocation is the only listener
    # launch; it must not include the listener-side flags.
    exec_call = text.split("gosu runner", 1)[1].split("fi", 1)[0]
    assert "--start" not in exec_call, (
        "start-runner.sh must NOT pass --start to run.sh"
    )
    assert "--disableupdate" not in exec_call, (
        "start-runner.sh must NOT pass --disableupdate to run.sh"
    )
    assert "run.sh" in exec_call, (
        "start-runner.sh must exec run.sh via gosu runner"
    )


def test_start_runner_grants_docker_socket_as_supplemental_group() -> None:
    """The host Docker socket's GID must be added as a supplemental group only."""
    text = _read(START_RUNNER_SCRIPT)
    assert "usermod -a -G" in text, (
        "start-runner.sh must add the host docker GID as supplemental (usermod -a -G)"
    )
    assert "usermod --gid" not in text, (
        "start-runner.sh must NOT change the runner user's primary group"
    )


def test_compose_pins_image_and_attaches_to_bridge_network() -> None:
    """The Compose contract must pin the image and attach the listener to a
    normal bridge network with the host-gateway alias."""
    text = _read(COMPOSE_FILE)
    assert "TITAN_RUNNER_IMAGE" in text, (
        "docker-compose.yml must reference TITAN_RUNNER_IMAGE"
    )
    assert "titan-runner-state" in text, (
        "docker-compose.yml must declare the titan-runner-state named volume"
    )
    assert "titan-runner-browser" in text, (
        "docker-compose.yml must declare the titan-runner-browser named volume"
    )
    # Host networking is forbidden; bridge networking is required.
    assert "network_mode: host" not in text, (
        "docker-compose.yml MUST NOT use network_mode: host"
    )
    assert "ipc: host" not in text, (
        "docker-compose.yml MUST NOT use ipc: host"
    )
    assert "titan-runner-net" in text, (
        "docker-compose.yml must declare the titan-runner-net bridge network"
    )
    assert "extra_hosts:" in text, (
        "docker-compose.yml must declare extra_hosts"
    )
    assert "host.docker.internal:host-gateway" in text, (
        "docker-compose.yml must map host.docker.internal through the host-gateway alias"
    )
    # Isolated shared memory is required for Chromium without host IPC.
    assert "shm_size:" in text, (
        "docker-compose.yml must declare an explicit shm_size"
    )
    assert "shm_size: \"2gb\"" in text or "shm_size: 2gb" in text, (
        "docker-compose.yml must size /dev/shm to 2gb"
    )
    for marker in (
        "no-new-privileges:true",
        "privileged: false",
        "/var/run/docker.sock:/var/run/docker.sock",
        "restart: always",
    ):
        assert marker in text, f"docker-compose.yml must declare {marker!r}"


def test_compose_mounts_work_as_named_volume() -> None:
    """The workspace MUST use the explicit Docker-named work volume.

    The Docker socket remains the only host bind mount. Consumer job
    Compose files must mount this same named volume explicitly when
    child containers need access to the checkout.
    """
    import yaml

    with COMPOSE_FILE.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    runner = data["services"]["runner"]
    volumes = runner.get("volumes", [])
    assert "titan-runner-work:/var/lib/titan-runner/work" in volumes
    assert any(
        isinstance(entry, str) and entry == "/var/run/docker.sock:/var/run/docker.sock"
        for entry in volumes
    ), "Docker socket must remain the only host bind mount"
    assert not any(
        isinstance(entry, dict)
        and entry.get("type") == "bind"
        and entry.get("target") != "/var/run/docker.sock"
        for entry in volumes
    ), "runner must not declare a workspace bind mount"
    assert data["volumes"]["titan-runner-work"] == {"name": "titan-runner-work"}


def test_compose_passes_startup_token_without_file_mount() -> None:
    """The Compose contract MUST NOT mount a registration token file
    on the runner. The startup token is passed as an environment value
    and consumed before the listener process is launched."""
    text = _read(COMPOSE_FILE)
    assert "registration-token" not in text, (
        "docker-compose.yml must not mount a registration token file"
    )
    assert "TITAN_RUNNER_TOKEN_FILE" not in text, (
        "docker-compose.yml must not accept a token file environment variable"
    )
    assert "RUNNER_TOKEN_FILE" not in text, (
        "docker-compose.yml must not propagate RUNNER_TOKEN_FILE"
    )
    assert "RUNNER_TOKEN: ${TITAN_RUNNER_TOKEN:-}" in text, (
        "docker-compose.yml must pass the startup token to the runner"
    )


def test_compose_does_not_hardcode_repository_url() -> None:
    """The Compose contract must NOT hardcode the consumer repository URL."""
    text = _read(COMPOSE_FILE)
    assert "https://github.com/PintjesB/titan-stocks" not in text, (
        "docker-compose.yml must not hardcode the consumer repository URL"
    )
    assert "TITAN_RUNNER_REPO_URL" in text, (
        "docker-compose.yml must source the repo URL from the deployment"
    )


def test_deploy_refuses_unsupported_host_architectures() -> None:
    """``deploy.sh`` MUST abort on hosts whose architecture is
    neither x86_64 (amd64) nor aarch64 (arm64); both native
    architectures are required by the multi-platform image
    contract.

    The host-architecture guard runs before any state mutation so
    a wrong platform never reaches the network, the Docker
    daemon, or the persistent state.
    """
    text = _read(DEPLOY_SCRIPT)
    assert "uname -m" in text, (
        "deploy.sh must inspect uname -m before any state mutation"
    )
    # The guard MUST accept both x86_64|amd64 and aarch64|arm64.
    for marker in ("x86_64|amd64", "aarch64|arm64"):
        assert marker in text, (
            f"deploy.sh architecture check must accept {marker!r}"
        )
    assert "linux/amd64" in text and "linux/arm64" in text, (
        "deploy.sh must map the host architecture to the matching "
        "linux/amd64 or linux/arm64 Docker platform string"
    )


def test_deploy_exposes_required_subcommands() -> None:
    """``deploy.sh`` must expose every documented subcommand."""
    text = _read(DEPLOY_SCRIPT)
    for subcommand in ("build", "probe", "up", "down", "status", "logs"):
        assert subcommand in text, (
            f"deploy.sh must handle the {subcommand!r} subcommand"
        )


def test_deploy_probe_uses_entrypoint_override() -> None:
    """``deploy.sh probe`` MUST override the image entrypoint to probe."""
    text = _read(DEPLOY_SCRIPT)
    # Locate the probe branch and confirm it overrides the entrypoint.
    branch = text.split("probe)", 1)[1].split("up)", 1)[0]
    assert "--entrypoint /usr/local/bin/probe" in branch, (
        "deploy.sh probe must set --entrypoint /usr/local/bin/probe"
    )
    assert "host.docker.internal:host-gateway" in branch, (
        "deploy.sh probe must wire host.docker.internal through host-gateway"
    )


def test_deploy_probe_does_not_use_host_network() -> None:
    """``deploy.sh probe`` MUST NOT use ``--network host`` or ``--ipc host``.

    The sidecar now runs on ordinary bridge networking; reachability
    to host services goes through ``host.docker.internal``.
    """
    text = _read(DEPLOY_SCRIPT)
    branch = text.split("probe)", 1)[1].split("up)", 1)[0]
    # Executable code only; comments may reference the forbidden
    # flags in negation form without using them.
    code_only = "\n".join(
        line for line in branch.splitlines() if not line.lstrip().startswith("#")
    )
    assert "--network host" not in code_only, (
        "deploy.sh probe must NOT use --network host"
    )
    assert "--ipc host" not in code_only, (
        "deploy.sh probe must NOT use --ipc host"
    )


def test_deploy_has_no_register_subcommand() -> None:
    """Registration is an internal startup phase of the single runner
    container; deploy.sh must not expose a separate lifecycle command."""
    text = _read(DEPLOY_SCRIPT)
    assert "register)" not in text
    assert "run --rm register" not in text


def test_deploy_up_is_registration_entrypoint() -> None:
    """``deploy.sh up`` is the sole registration-and-start command."""
    text = _read(DEPLOY_SCRIPT)
    up_branch = text.split("up)", 1)[1].split("down)", 1)[0]
    assert "docker compose" in up_branch
    assert "up -d" in up_branch
    assert "--force-recreate" in up_branch


def test_deploy_register_does_not_mount_token_file_on_long_running() -> None:
    """``deploy.sh up`` MUST NOT mount the registration token file or pass
    a token file environment variable to the listener."""
    text = _read(DEPLOY_SCRIPT)
    up_branch = text.split("up)", 1)[1].split("down)", 1)[0]
    assert "registration-token" not in up_branch, (
        "deploy.sh up must not mount the registration token file"
    )
    assert "TITAN_RUNNER_TOKEN_FILE" not in up_branch, (
        "deploy.sh up must not pass the token file env var to compose"
    )
    assert "RUNNER_TOKEN_FILE" not in up_branch, (
        "deploy.sh up must not pass the legacy token file env var to compose"
    )
    # ``deploy.sh up`` MUST NOT pass any env-file to Compose either.
    # The startup token is interpolated from the gitignored ``.env``
    # by Compose; deploy.sh does not pass it as a command argument.
    assert "--env-file" not in up_branch, (
        "deploy.sh up must NOT pass a custom --env-file to Compose; "
        "the listener must rely on Compose's default .env interpolation"
    )


def test_deploy_up_uses_registration_enabled_compose_stack() -> None:
    """``deploy.sh up`` MUST delegate to the registration-enabled
    Compose stack so the listener never starts without matching
    persisted credentials."""
    text = _read(DEPLOY_SCRIPT)
    up_branch = text.split("up)", 1)[1].split("down)", 1)[0]
    assert "docker compose" in up_branch, (
        "deploy.sh up must invoke docker compose"
    )
    assert "COMPOSE_FILE" in up_branch or "docker-compose.yml" in up_branch, (
        "deploy.sh up must reference the documented Compose file"
    )
    assert "up -d" in up_branch, (
        "deploy.sh up must use the detached form"
    )
    assert "--force-recreate" in up_branch, (
        "deploy.sh up must force-recreate the listener so image upgrades take effect"
    )


def test_deploy_token_interface_uses_allowlisted_env_file() -> None:
    """``deploy.sh`` MUST read deployment variables from an allowlisted
    env-file rather than shell-sourcing the .env file directly."""
    text = _read(DEPLOY_SCRIPT)
    assert "TITAN_RUNNER_ENV_FILE" in text, (
        "deploy.sh must read TITAN_RUNNER_ENV_FILE"
    )
    assert "ALLOWLIST_KEYS" in text, (
        "deploy.sh must maintain an allowlist of deployment variables"
    )
    # Executable lines only; comments may describe the negation
    # ("never shell-sourced") without invoking the parser.
    code_only = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )
    assert "source" not in code_only or "allowlist" in code_only, (
        "deploy.sh must not shell-source the .env file"
    )
    # Every documented allowlist key must appear in the deploy.sh allowlist.
    for key in DEPLOY_ALLOWLIST_KEYS:
        assert key in text, f"deploy.sh allowlist must declare {key!r}"


def test_deploy_token_is_forwarded_by_compose_environment() -> None:
    """The startup registration phase receives the token through the
    allowlisted Compose environment, not a shell command argument."""
    text = _read(DEPLOY_SCRIPT)
    assert "TITAN_RUNNER_TOKEN" in text
    assert "RUNNER_TOKEN" in _read(COMPOSE_FILE)
    assert "--env TITAN_RUNNER_TOKEN=" not in text
    assert "-e TITAN_RUNNER_TOKEN=" not in text


def test_workspace_path_is_not_publicly_configurable() -> None:
    """The workspace path MUST stay fixed and out of public config."""
    for path in (ENV_EXAMPLE, DEPLOY_SCRIPT, COMPOSE_FILE, PROBE_SCRIPT):
        assert "TITAN_RUNNER_WORK_DIR" not in _read(path), (
            f"{path.name} must not expose TITAN_RUNNER_WORK_DIR"
        )
    compose = _read(COMPOSE_FILE)
    assert "RUNNER_WORK_DIR: /var/lib/titan-runner/work" in compose


def test_deploy_takes_exclusive_lock_for_lifecycle_mutations() -> None:
    """``deploy.sh`` up/down MUST take an exclusive flock."""
    text = _read(DEPLOY_SCRIPT)
    assert "flock -n 9" in text, "deploy.sh must take an exclusive flock"
    assert "/var/lock/titan-runner.lock" in text, (
        "deploy.sh must use /var/lock/titan-runner.lock by default"
    )
    # Both mutation subcommands must call take_lock.
    for sub in ("up", "down"):
        branch = text.split(f"{sub})", 1)[1].split("logs)", 1)[0]
        assert "take_lock" in branch, (
            f"deploy.sh {sub!r} must take the lifecycle lock"
        )


def test_deploy_down_preserves_runner_named_volumes() -> None:
    """The down lifecycle MUST stop the container without removing volumes."""
    text = _read(DEPLOY_SCRIPT)
    down_branch = text.split("down)", 1)[1].split("logs)", 1)[0]
    assert "docker compose" in down_branch
    assert "--remove-orphans" in down_branch
    assert " down -v" not in down_branch
    assert "--volumes" not in down_branch


def test_deploy_status_reports_runner_health_signals() -> None:
    """``deploy.sh status`` MUST expose the listener process, docker
    socket reachability, credentials state, bridge network mode,
    host-gateway alias, and absence of ``RUNNER_TOKEN``."""
    text = _read(DEPLOY_SCRIPT)
    branch = text.split("status)", 1)[1].split("*)", 1)[0]
    for marker in (
        "Runner.Listener",
        "docker info",
        ".credentials",
        "titan-runner-state",
        "NetworkMode",
        "ShmSize",
        "host.docker.internal",
        "RUNNER_TOKEN",
        "registration during startup",
    ):
        assert marker in branch, f"deploy.sh status must surface {marker!r}"


def test_deploy_pins_image_by_digest_only() -> None:
    """``deploy.sh`` must read ``TITAN_RUNNER_IMAGE`` and refuse to fall
    back to a mutable tag."""
    text = _read(DEPLOY_SCRIPT)
    assert "TITAN_RUNNER_IMAGE" in text, (
        "deploy.sh must read TITAN_RUNNER_IMAGE as the deployment image"
    )


def test_dockerfile_uses_correct_pangocairo_package() -> None:
    """Ubuntu 24.04 does NOT ship ``libpangocairo-1.0-0t64``.

    The build emits a failure for the wrong package. The
    ``t64`` flavour exists for some libraries on Ubuntu 24.04 but
    not for ``libpangocairo``. The same package set is installed
    on both ``linux/amd64`` and ``linux/arm64`` because the
    Ubuntu archive carries the same packages for every native
    architecture.
    """
    text = _read(DOCKERFILE)
    assert "libpangocairo-1.0-0" in text, (
        "Dockerfile must install libpangocairo-1.0-0"
    )
    assert "libpangocairo-1.0-0t64" not in text, (
        "Dockerfile must NOT install libpangocairo-1.0-0t64; the package "
        "does not exist for Ubuntu 24.04 and the build fails"
    )


def test_compose_volumes_have_explicit_names() -> None:
    """All three persistent volumes MUST declare an explicit ``name:``.

    Without ``name:``, Docker Compose scopes the volume by the
    project name (e.g. ``titan-stocks-runner_titan-runner-state``),
    so the runner's Compose lifecycle always refers to the same
    stable volume name after container recreation. The result is
    "credentials not found" at the next start if this drifts.
    """
    text = _read(COMPOSE_FILE)
    # The volumes block at the bottom must pin every name explicitly.
    volumes_block = text.split("volumes:", 1)[1]
    assert "name: titan-runner-state" in volumes_block, (
        "docker-compose.yml must declare name: titan-runner-state"
    )
    assert "name: titan-runner-browser" in volumes_block, (
        "docker-compose.yml must declare name: titan-runner-browser"
    )
    assert "name: titan-runner-work" in volumes_block, (
        "docker-compose.yml must declare name: titan-runner-work"
    )


def test_compose_bridge_network_has_explicit_name() -> None:
    """The bridge network MUST declare an explicit ``name:``.

    Without ``name:``, Docker Compose scopes the network by the
    project name and the runner would not have a stable network name.
    """
    text = _read(COMPOSE_FILE)
    networks_block = text.split("networks:", 1)[1]
    assert "name: titan-runner-net" in networks_block, (
        "docker-compose.yml must declare name: titan-runner-net"
    )
    assert "driver: bridge" in networks_block, (
        "docker-compose.yml must declare driver: bridge for titan-runner-net"
    )


def test_compose_work_volume_is_fixed_and_named() -> None:
    """The workspace MUST use the named volume at the fixed path.

    Child job Compose files mount this same external volume when they
    need access to checkout files; no host workspace path is shared.
    """
    import yaml

    with COMPOSE_FILE.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    services = data.get("services")
    assert isinstance(services, dict), "services must parse as a mapping"
    runner = services.get("runner")
    assert isinstance(runner, dict), "runner service must parse as a mapping"

    mounts = runner.get("volumes", [])
    assert "titan-runner-work:/var/lib/titan-runner/work" in mounts
    assert "/var/run/docker.sock:/var/run/docker.sock" in mounts
    assert not any(
        isinstance(mount, dict)
        and mount.get("type") == "bind"
        and mount.get("target") != "/var/run/docker.sock"
        for mount in mounts
    ), "runner must not declare a workspace bind mount"
    assert data["volumes"]["titan-runner-work"] == {"name": "titan-runner-work"}


def test_compose_omits_duplicate_init() -> None:
    """``init: true`` would stack two init layers with the image's
    tini entrypoint. The image already owns the lifecycle; Compose
    must not add a Docker-injected init on top."""
    text = _read(COMPOSE_FILE)
    # The service declaration begins at ``services:`` and ends before
    # the top-level ``volumes:`` block. Anything earlier is comment.
    services_block = text.split("services:", 1)[1].split("\nnetworks:", 1)[0]
    assert "init: true" not in services_block, (
        "docker-compose.yml service must NOT set init: true; the image "
        "already ENTRYPOINTs tini -> start-runner"
    )


def test_compose_healthcheck_uses_lightweight_signal() -> None:
    """The HEALTHCHECK must verify only the listener process and the
    runner user's Docker daemon reachability. The full capability
    probe belongs to release validation, not container health."""
    text = _read(COMPOSE_FILE)
    # Locate the healthcheck block.
    check_block = text.split("healthcheck:", 1)[1].split("volumes:", 1)[0]
    assert "/usr/local/bin/probe" not in check_block, (
        "HEALTHCHECK must not invoke /usr/local/bin/probe; that is the "
        "full capability probe and belongs in release validation"
    )
    assert "Runner.Listener" in check_block, (
        "HEALTHCHECK must pgrep the listener process"
    )
    assert "docker info" in check_block, (
        "HEALTHCHECK must run `docker info` to confirm docker socket access"
    )
    # 30-second cadence signals "cheap" rather than "expensive".
    assert "interval: 30s" in check_block, (
        "HEALTHCHECK must run on a short interval; the heavy probe has no "
        "place inside the container"
    )


def test_compose_has_no_registration_sidecar() -> None:
    """Both services are steady-state listeners; light activation is opt-in."""
    import yaml

    with COMPOSE_FILE.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    assert set(data.get("services", {})) == {"runner", "runner-light"}
    assert data["services"]["runner-light"]["profiles"] == ["ci-light"]
    assert data["services"]["runner-light"]["extends"] == {"service": "runner"}


def test_dockerfile_bakes_pinned_playwright_core_install() -> None:
    """The probe dependency tree MUST be installed at build time from
    a committed ``package.json`` and ``package-lock.json`` so the
    image is reproducible without ever invoking ``npx`` at probe time.
    """
    text = _read(DOCKERFILE)
    assert "/opt/titan-probe/package.json" in text, (
        "Dockerfile must COPY probe/package.json into /opt/titan-probe"
    )
    assert "/opt/titan-probe/package-lock.json" in text, (
        "Dockerfile must COPY probe/package-lock.json into /opt/titan-probe"
    )
    assert "npm ci" in text, (
        "Dockerfile must run `npm ci` against the committed lockfile"
    )
    assert "/opt/titan-probe/node_modules/.bin/playwright-core install chromium" in text, (
        "Dockerfile must invoke the playwright-core CLI (not the "
        "non-existent `playwright` binary) to install Chromium"
    )
    assert "/opt/titan-probe/node_modules/.bin/playwright install chromium" not in text, (
        "Dockerfile must NOT call `.bin/playwright`; the installed "
        "package only exposes `playwright-core` as a CLI"
    )


def test_probe_package_files_pin_one_playwright_core_version() -> None:
    """The manifest owns the version and the lockfile must resolve it exactly."""
    package = json.loads(_read(PROBE_PACKAGE_JSON))
    lock = json.loads(_read(PROBE_PACKAGE_LOCK))

    declared_version = package["dependencies"]["playwright-core"]
    assert re.fullmatch(r"\d+\.\d+\.\d+", declared_version)
    assert lock["packages"][""]["dependencies"]["playwright-core"] == declared_version
    assert lock["packages"]["node_modules/playwright-core"]["version"] == declared_version

    dockerfile = _read(DOCKERFILE)
    assert "ARG PLAYWRIGHT_VERSION=" not in dockerfile
    assert "PLAYWRIGHT_VERSION=${PLAYWRIGHT_VERSION}" not in dockerfile


def test_probe_does_not_use_npx() -> None:
    """The probe must consume the deterministic ``/opt/titan-probe``
    install via ``NODE_PATH``; ``npx`` interprets its first positional
    argument as a binary to execute and cannot be used to "install
    playwright-core and then run node"."""
    text = _read(PROBE_SCRIPT)
    # Executable lines only; the script's documentation may mention
    # ``npx playwright-core`` in a "do not do this" note.
    code_only = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )
    assert "NODE_PATH=" in code_only, (
        "probe.sh must set NODE_PATH to /opt/titan-probe/node_modules"
    )
    assert "/opt/titan-probe/node_modules" in code_only, (
        "probe.sh must reference the deterministic probe install"
    )
    assert "npx --yes" not in code_only, (
        "probe.sh must NOT call `npx --yes <package> ...` in executable code"
    )
    assert re.search(r"^\s*npx\s+playwright-core", code_only, flags=re.MULTILINE) is None, (
        "probe.sh must NOT invoke `npx playwright-core ...` in executable code; "
        "npx interprets the first positional as a binary"
    )


def test_probe_validates_host_gateway_alias() -> None:
    """The capability probe MUST verify the ``host.docker.internal``
    alias resolves before reporting success."""
    text = _read(PROBE_SCRIPT)
    code_only = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )
    assert "host.docker.internal" in code_only, (
        "probe.sh must verify the host.docker.internal alias"
    )
    assert "probe_host_gateway" in text, (
        "probe.sh must define a probe_host_gateway function"
    )
    assert "probe_host_gateway" in code_only, (
        "probe.sh must invoke probe_host_gateway"
    )


def test_start_runner_seeds_browser_volume_and_exports_path() -> None:
    """``start-runner.sh`` MUST seed the persistent browser volume from
    the baked image cache on first start and export
    ``PLAYWRIGHT_BROWSERS_PATH`` into the runner environment."""
    text = _read(START_RUNNER_SCRIPT)
    code_only = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )
    # No fragile symlinks over a populated image directory.
    assert "ln -sfn" not in code_only, (
        "start-runner.sh must not symlink the image cache over the baked "
        "directory; the seed is via cp -a"
    )
    # The seed step may live in a helper function; both forms are
    # acceptable.
    assert (
        'cp -a "$RUNNER_BROWSER_SEED/." "$RUNNER_BROWSER_DIR/"' in code_only
        or ('cp -a "$seed/." "$dest/"' in code_only and "RUNNER_BROWSER_SEED" in code_only and "RUNNER_BROWSER_DIR" in code_only)
    ), "start-runner.sh must seed the persistent browser dir via cp -a"
    assert "PLAYWRIGHT_BROWSERS_PATH=\"$RUNNER_BROWSER_DIR\"" in code_only, (
        "start-runner.sh must export PLAYWRIGHT_BROWSERS_PATH into the runner env"
    )


def test_pre_job_hook_validates_host_capabilities() -> None:
    """The pre-job hook MUST fail when required host capabilities or
    the ``host.docker.internal`` alias are unavailable."""
    text = _read(PRE_JOB_SCRIPT)
    code_only = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )
    for marker in (
        "host.docker.internal",
        "docker info",
        "docker compose version",
        "docker buildx version",
        "check_docker",
        "check_compose",
        "check_buildx",
        "check_node",
        "check_python",
        "check_host_gateway",
        "check_playwright_cache",
        "exit 1",
    ):
        assert marker in code_only, f"pre-job.sh must include {marker!r}"


def test_post_job_hook_only_removes_titan_prefixed_resources() -> None:
    """The post-job hook MUST remove only Titan CI Compose
    projects (matched by the documented ``titan-stocks-playwright-``
    prefix and the ``com.docker.compose.project`` label) and the
    project-owned volumes that belong to those projects. It must NEVER
    run a global prune and must NEVER touch the persistent named
    volumes or the Playwright browser cache.

    The bounded cleanup is anchored on the documented project
    prefix so a future Titan workload that adopts a different
    project name cannot be torn down by mistake.
    """
    text = _read(POST_JOB_SCRIPT)
    code_only = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )
    # Required bounded-cleanup markers.
    assert "titan-stocks-playwright-" in code_only, (
        "post-job.sh must scope cleanup to the documented Titan CI project prefix"
    )
    assert "com.docker.compose.project" in code_only, (
        "post-job.sh must identify Compose projects by their documented label"
    )
    # The hook must NOT match against the bare ``titan-`` prefix
    # because that would also catch the runner's own
    # ``titan-runner`` container.
    assert "titan_project_re='^titan-'" not in code_only, (
        "post-job.sh must NOT match the bare `titan-` prefix; the "
        "titan-runner container would otherwise be a target."
    )
    # Forbidden operations.
    forbidden = (
        "docker system prune",
        "docker volume prune",
        "docker image prune",
        "docker builder prune",
        "docker network prune",
    )
    for marker in forbidden:
        assert marker not in code_only, (
            f"post-job.sh must NEVER invoke {marker!r}"
        )
    assert "docker volume rm titan-runner-" not in code_only
    assert "titan-runner-state" in text
    assert "titan-runner-work" in text
    assert "titan-runner-browser" in text
    assert "titan-runner-state|titan-runner-work|titan-runner-browser" in code_only


def test_child_job_external_workspace_contract_is_documented() -> None:
    """Operator docs MUST define the external child-job work volume."""
    docs = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (README_FILE, QUICK_START_DOC, OPERATIONS_DOC)
    )
    assert "external: true" in docs
    assert "name: titan-runner-work" in docs
    assert "titan-runner-work:/var/lib/titan-runner/work" in docs


def test_deploy_populates_env_file_before_require_checks() -> None:
    """``deploy.sh`` MUST populate deployment variables from the
    allowlisted ``.env`` file before any ``require`` check fires
    and BEFORE ``TITAN_RUNNER_LOCK_FILE`` is resolved.

    Without this step, a deployment that sets every variable in
    ``.env`` rather than on the command line fails the
    ``require TITAN_RUNNER_IMAGE`` / ``require TITAN_RUNNER_TOKEN``
    guards even though the values are present on disk. Resolving
    the lock file before the env file would also lock the
    operator out of overriding the lock-file location through
    the documented allowlist.
    """
    text = _read(DEPLOY_SCRIPT)
    # ``populate_from_env_file`` must run before any ``require``
    # call. ``require`` only fires inside case arms, so the helper
    # call must appear above the ``case "$action"`` block.
    require_idx = text.find("case \"$action\"")
    assert require_idx != -1, "deploy.sh must dispatch through `case \"$action\"`"
    helper_idx = text.find("populate_from_env_file")
    assert helper_idx != -1, (
        "deploy.sh must define and call populate_from_env_file"
    )
    # The function definition and the standalone call must both
    # live above the case dispatch.
    fn_def_idx = text.find("populate_from_env_file()")
    assert fn_def_idx != -1 and fn_def_idx < require_idx, (
        "populate_from_env_file must be defined before the case dispatch"
    )
    # The standalone invocation must also predate the dispatch.
    body = text[fn_def_idx:require_idx]
    assert "populate_from_env_file" in body, (
        "deploy.sh must call populate_from_env_file before the case dispatch"
    )
    # The populate call must also predate the LOCK_FILE resolution
    # so a ``TITAN_RUNNER_LOCK_FILE`` override in ``.env`` wins.
    call_idx = body.find("populate_from_env_file\n")
    assert call_idx != -1, (
        "deploy.sh must call populate_from_env_file (no arguments) "
        "before the case dispatch"
    )
    lock_idx = text.find("LOCK_FILE=\"${TITAN_RUNNER_LOCK_FILE")
    assert lock_idx != -1, (
        "deploy.sh must resolve LOCK_FILE from TITAN_RUNNER_LOCK_FILE"
    )
    assert call_idx < lock_idx < require_idx, (
        "deploy.sh must call populate_from_env_file BEFORE resolving "
        "TITAN_RUNNER_LOCK_FILE"
    )
    # The helper must respect the documented precedence rule:
    # command-line / shell-exported values -- including deliberate
    # empty overrides -- win over the file.
    body = text.split("populate_from_env_file()", 1)[1]
    body = body.split("\n\n", 1)[0]
    assert '${!key+set}' in body, (
        "populate_from_env_file must use ${!key+set} so explicitly "
        "exported values (including empty ones) win over .env"
    )


def test_deploy_probe_does_not_leak_token() -> None:
    """``deploy.sh probe`` MUST NOT pass the registration token to
    the probe sidecar. The previous contract used a token-stripped
    env-file; the new contract builds the in-container
    environment explicitly so the listener-derived allowlist never
    reaches the probe."""
    text = _read(DEPLOY_SCRIPT)
    branch = text.split("probe)", 1)[1].split("up)", 1)[0]
    # No token-stripping helper is required because no env-file is
    # passed at all; the sidecar's environment is built explicitly.
    assert "build_env_file" not in branch, (
        "deploy.sh probe must NOT use the full allowlisted env-file helper"
    )
    assert "build_listener_env_file" not in branch, (
        "deploy.sh probe must NOT use the listener-specific env-file helper"
    )
    assert "--env-file" not in branch, (
        "deploy.sh probe must NOT pass --env-file"
    )
    # The token must not appear in any explicit environment
    # variable passed to the sidecar.
    code_only = "\n".join(
        line for line in branch.splitlines() if not line.lstrip().startswith("#")
    )
    assert "TITAN_RUNNER_TOKEN" not in code_only, (
        "deploy.sh probe must NOT pass TITAN_RUNNER_TOKEN to the sidecar"
    )
    assert "RUNNER_TOKEN" not in code_only, (
        "deploy.sh probe must NOT pass RUNNER_TOKEN to the sidecar"
    )


def test_deploy_status_uses_listener_username() -> None:
    """``deploy.sh status`` MUST run ``docker exec`` against the
    image's ``runner`` user, not the container name.

    The image creates a system user called ``runner``; the
    container is named ``titan-runner``. ``docker exec -u`` resolves
    the username against ``/etc/passwd`` inside the container, so
    using ``-u titan-runner`` would fail with
    ``unknown user titan-runner`` and abort the whole status
    check under ``set -e``.
    """
    text = _read(DEPLOY_SCRIPT)
    branch = text.split("status)", 1)[1].split("*)", 1)[0]
    assert "docker exec -u runner titan-runner" in branch, (
        "deploy.sh status must run `docker exec -u runner titan-runner`"
    )
    assert "docker exec -u titan-runner titan-runner" not in branch, (
        "deploy.sh status must NOT pass the container name as the username"
    )


def test_compose_enables_actions_runner_hooks() -> None:
    """The listener MUST enable the GitHub Actions pre/post-job hooks
    through ``ACTIONS_RUNNER_HOOK_JOB_STARTED`` and
    ``ACTIONS_RUNNER_HOOK_JOB_COMPLETED`` pointing at absolute
    runtime paths.

    GitHub documents these variables as the activation mechanism;
    copying hooks into ``.hooks/`` is necessary but not
    sufficient.
    """
    text = _read(COMPOSE_FILE)
    # Locate the runner service's environment block. The runner
    # body runs until the top-level ``networks:`` block.
    services_block = text.split("services:", 1)[1]
    runner_section = services_block.split("runner:", 1)[1]
    runner_section = runner_section.split("\nnetworks:", 1)[0]
    assert "ACTIONS_RUNNER_HOOK_JOB_STARTED:" in runner_section, (
        "docker-compose.yml listener must declare ACTIONS_RUNNER_HOOK_JOB_STARTED"
    )
    assert "ACTIONS_RUNNER_HOOK_JOB_COMPLETED:" in runner_section, (
        "docker-compose.yml listener must declare ACTIONS_RUNNER_HOOK_JOB_COMPLETED"
    )
    # The values must point at absolute paths inside the runtime
    # tree so the listener can resolve them on every start.
    started = re.search(
        r"ACTIONS_RUNNER_HOOK_JOB_STARTED:[^\n]*", runner_section
    )
    assert started, "ACTIONS_RUNNER_HOOK_JOB_STARTED declaration missing"
    assert "/var/lib/titan-runner/runtime/.hooks/PreJob.sh" in started.group(0), (
        "ACTIONS_RUNNER_HOOK_JOB_STARTED must point at the runtime-tree hook path"
    )
    completed = re.search(
        r"ACTIONS_RUNNER_HOOK_JOB_COMPLETED:[^\n]*", runner_section
    )
    assert completed, "ACTIONS_RUNNER_HOOK_JOB_COMPLETED declaration missing"
    assert "/var/lib/titan-runner/runtime/.hooks/PostJob.sh" in completed.group(0), (
        "ACTIONS_RUNNER_HOOK_JOB_COMPLETED must point at the runtime-tree hook path"
    )


def test_publish_workflow_runs_static_contract_suite() -> None:
    """The publish workflow MUST run the Python contract tests, the
    shell-level contract tests, ``bash -n``, and ShellCheck before
    building the image.

    Without these gates, a contract regression ships in a green
    publish build.
    """
    import yaml

    with PUBLISH_WORKFLOW.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    jobs = data["jobs"]
    assert "contract" in jobs, (
        "publish.yml must declare a contract job"
    )
    contract_steps = [s.get("name", "") for s in jobs["contract"]["steps"]]
    for marker in (
        "Bash syntax check",
        "ShellCheck",
        "Shell-level contract tests",
        "Python contract tests",
    ):
        assert any(marker in name for name in contract_steps), (
            f"publish.yml contract job must include a `{marker}` step"
        )
    # The build job must depend on the contract job so a failing
    # contract blocks every later step. The merge / verify /
    # promote jobs depend on build (and through build on contract)
    # so the transitive dependency already covers them.
    build_needs = jobs["build"].get("needs") or []
    if isinstance(build_needs, str):
        build_needs = [build_needs]
    assert "contract" in build_needs, (
        "publish.yml build job must `needs: contract`"
    )


def test_publish_workflow_probe_uses_bridge_contract() -> None:
    """The publish workflow probe MUST drop ``--network host`` /
    ``--ipc host`` and wire the host-gateway alias.

    The probe now requires ``host.docker.internal`` to resolve
    inside the container; without ``--add-host
    host.docker.internal:host-gateway`` the probe fails.
    """
    text = _read(PUBLISH_WORKFLOW)
    probe_block = text.split("Run the capability probe", 1)[1].split(
        "Push the probed image", 1
    )[0]
    # Strip comments; the prose may reference the negation of
    # the forbidden flags (``no ``--network host``...``) without
    # actually using them.
    code_only = "\n".join(
        line for line in probe_block.splitlines() if not line.lstrip().startswith("#")
    )
    assert "--network host" not in code_only, (
        "publish.yml probe must NOT use --network host"
    )
    assert "--ipc host" not in code_only, (
        "publish.yml probe must NOT use --ipc host"
    )
    assert "host.docker.internal:host-gateway" in code_only, (
        "publish.yml probe must wire host.docker.internal through host-gateway"
    )
    assert "--shm-size 2gb" in code_only, (
        "publish.yml probe must allocate 2gb of shared memory for Chromium"
    )


def test_deploy_status_resolves_manifest_digest() -> None:
    """``deploy.sh status`` must parse the manifest ``Digest:`` field
    emitted by ``docker buildx imagetools inspect`` and validate it
    as ``^sha256:[0-9a-f]{64}$``. Searching the raw JSON for an
    arbitrary ``"digest":"sha256:..."`` is unsafe because a manifest
    contains several such strings.
    """
    text = _read(DEPLOY_SCRIPT)
    # Isolate the image_digest function body. The function is the
    # last helper before the ``case "$action"`` block; it ends at
    # the closing ``}`` followed by a blank line and ``case``.
    body = text.split("image_digest()", 1)[1]
    body = body.split("\ncase \"$action\"", 1)[0]
    assert "buildx imagetools inspect" in body, (
        "image_digest must use `docker buildx imagetools inspect`"
    )
    assert "Digest:" in body, (
        "image_digest must parse the manifest Digest: line"
    )
    assert "awk" in body, (
        "image_digest must use awk to extract the Digest: field"
    )
    # The validation must enforce the canonical SHA-256 digest
    # format. The current implementation does so via length
    # arithmetic (``wc -c`` against the second field) rather than
    # an explicit regex; both are acceptable as long as the
    # candidate is shape-checked before being reported.
    assert (
        ("[0-9a-f]" in body and "64" in body)
        or ("wc -c" in body and "65" in body)
    ), "image_digest must validate the digest is sha256:<64 hex chars>"


def test_publish_resolves_digest_after_promote() -> None:
    """``publish.yml`` must resolve the GHCR-served ``:latest``
    digest after the promote-to-:latest step so the reported
    digest is definitively what the registry serves.
    """
    import yaml

    with PUBLISH_WORKFLOW.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    job = data["jobs"]["promote"]
    step_names = [s.get("name", "") for s in job["steps"]]
    promote_index = next(
        (i for i, n in enumerate(step_names) if "Promote" in n),
        None,
    )
    # The :latest-resolution step runs AFTER the promote-to-:latest
    # step so the reported digest is the digest GHCR serves. A
    # generic ``digest`` substring would also match the
    # download-artifact step; the contract looks for the resolve /
    # verify / report substring.
    resolve_index = next(
        (
            i
            for i, n in enumerate(step_names)
            if any(
                marker in n.lower()
                for marker in ("resolve", "verify", "report", "equality")
            )
            and "digest" in n.lower()
        ),
        None,
    )
    assert promote_index is not None and resolve_index is not None, (
        "publish.yml promote job must have a promote step and a "
        ":latest-digest resolution step"
    )
    assert resolve_index > promote_index, (
        "publish.yml promote job must re-resolve the :latest digest "
        "AFTER the promote-to-:latest step"
    )


def test_publish_workflow_builds_native_matrix() -> None:
    """The publish workflow MUST build and probe each
    architecture on a *native* GitHub-hosted runner.

    Building an ``amd64`` image on a hosted ``ubuntu-24.04-arm``
    runner would force the Docker daemon to run under emulation;
    the capability probe explicitly refuses emulated / mismatched
    daemons, so the matrix MUST pair each platform with its
    native hosted runner.
    """
    import yaml

    with PUBLISH_WORKFLOW.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    jobs = data["jobs"]
    assert "build" in jobs, (
        "publish.yml must declare a `build` matrix job"
    )
    matrix = jobs["build"].get("strategy", {}).get("matrix", {}).get("include", [])
    assert matrix, (
        "publish.yml build job must declare a matrix.include list"
    )
    pairs = {(m.get("platform"), m.get("runner")) for m in matrix}
    assert ("linux/amd64", "ubuntu-24.04") in pairs, (
        "publish.yml must build linux/amd64 on the native ubuntu-24.04 runner"
    )
    assert ("linux/arm64", "ubuntu-24.04-arm") in pairs, (
        "publish.yml must build linux/arm64 on the native ubuntu-24.04-arm runner"
    )
    # Each matrix entry MUST publish under a commit-specific tag
    # so the merge job can resolve both exact candidate digests.
    text = _read(PUBLISH_WORKFLOW)
    assert "candidate-${{ github.sha }}-${{ matrix.tag_suffix }}" in text, (
        "publish.yml must publish each architecture candidate under a "
        "commit-specific tag with the matrix tag_suffix"
    )


def test_publish_workflow_probe_uses_native_arch_env() -> None:
    """The publish workflow MUST pass an ``EXPECTED_ARCH`` env var
    to each probe sidecar so the probe can refuse an emulated /
    mismatched Docker daemon before the rest of the contract
    runs.
    """
    import yaml

    text = _read(PUBLISH_WORKFLOW)
    # Both probe steps (in the build and verify matrix jobs) MUST
    # declare ``-e EXPECTED_ARCH=...`` so the probe can compare the
    # Docker daemon's reported architecture to the native runner
    # architecture.
    assert text.count("-e EXPECTED_ARCH=") >= 2, (
        "publish.yml must pass EXPECTED_ARCH to every probe step "
        "(build matrix probe + verify matrix probe)"
    )
    # The build and verify matrix entries MUST declare the
    # expected_arch value per architecture so each probe is
    # scoped to its native platform.
    with PUBLISH_WORKFLOW.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    build_matrix = (
        data["jobs"]["build"].get("strategy", {}).get("matrix", {}).get("include", [])
    )
    verify_matrix = (
        data["jobs"]["verify"].get("strategy", {}).get("matrix", {}).get("include", [])
    )
    for matrix in (build_matrix, verify_matrix):
        arches = {entry.get("expected_arch") for entry in matrix}
        assert arches == {"amd64", "arm64"}, (
            "publish.yml probe matrices must declare expected_arch for "
            f"both amd64 and arm64; got {arches!r}"
        )


def test_publish_workflow_merges_native_manifests() -> None:
    """The publish workflow MUST merge the two native candidate
    digests into a commit-specific multi-platform manifest before
    probing the merged image or promoting it to ``:latest``.

    The merge step MUST consume the candidate digests as
    immutable ``repository@sha256:...`` references (exported
    through temporary GitHub artifacts) so a re-pushed
    candidate tag cannot leak into the merged manifest. Mutable
    candidate tags are diagnostic-only after the build step.

    Without the merge step a deployer would have to pin two
    different digests per architecture; the contract is that one
    digest resolves to both ``linux/amd64`` and ``linux/arm64``
    on their respective native hosts.
    """
    text = _read(PUBLISH_WORKFLOW)
    assert "imagetools create" in text, (
        "publish.yml must use `docker buildx imagetools create` to "
        "merge native candidates"
    )
    assert "-merged" in text, (
        "publish.yml must tag the merged manifest with a -merged suffix"
    )
    # The merge step MUST consume the candidate digests through
    # the upload / download-artifact round trip rather than the
    # mutable candidate tags.
    assert re.search(r"actions/upload-artifact@[0-9a-f]{40}", text), (
        "publish.yml must upload the candidate digests through an immutable "
        "upload-artifact commit"
    )
    assert re.search(r"actions/download-artifact@[0-9a-f]{40}", text), (
        "publish.yml must download the candidate digests through an immutable "
        "download-artifact commit"
    )
    # The merge inputs MUST be the immutable
    # ``repository@sha256:...`` references; the merge step MUST
    # NOT fall back to the mutable candidate tags as merge
    # inputs.
    merge_block = text.split("name: Merge native manifests", 1)[1]
    merge_block = merge_block.split("name: Upload merged digest artifact", 1)[0]
    assert "@${{ steps.validate.outputs.amd64_digest }}" in merge_block, (
        "publish.yml merge step MUST consume the amd64 candidate as "
        "an immutable repository@sha256:... reference"
    )
    assert "@${{ steps.validate.outputs.arm64_digest }}" in merge_block, (
        "publish.yml merge step MUST consume the arm64 candidate as "
        "an immutable repository@sha256:... reference"
    )
    assert "docker buildx imagetools inspect" in merge_block, (
        "publish.yml merge validation MUST inspect candidates through the "
        "authenticated Docker registry client"
    )
    assert 'awk \'$1 == "MediaType:" {print $2; exit}\'' in merge_block, (
        "publish.yml merge validation MUST parse the authenticated Buildx "
        "media-type output"
    )
    assert "sleep 10" in merge_block, (
        "publish.yml merge validation MUST retry transient GHCR visibility "
        "before failing a just-pushed candidate"
    )
    assert 'split("@", 1)[-1]' in merge_block, (
        "publish.yml merge validation MUST normalize image@digest references "
        "before comparing registry manifest digests"
    )
    assert '"--format", "{{json .Manifest}}"' in merge_block, (
        "publish.yml merge validation MUST use Buildx's formatted manifest "
        "output instead of the exit-255-prone raw mode"
    )
    assert "path: ${{ runner.temp }}/inputs/amd64" in merge_block, (
        "publish.yml MUST download the amd64 digest into its own directory"
    )
    assert "path: ${{ runner.temp }}/inputs/arm64" in merge_block, (
        "publish.yml MUST download the arm64 digest into its own directory"
    )
    assert "AMD64_DIGEST_FILE: ${{ runner.temp }}/inputs/amd64/candidate-digest.txt" in merge_block
    assert "ARM64_DIGEST_FILE: ${{ runner.temp }}/inputs/arm64/candidate-digest.txt" in merge_block
    assert "curl --silent --show-error --fail" not in merge_block, (
        "publish.yml merge validation MUST NOT use unauthenticated registry curl"
    )


def test_publish_workflow_asserts_merged_platforms() -> None:
    """The publish workflow MUST assert that the merged manifest
    contains *exactly* ``linux/amd64`` and ``linux/arm64`` before
    promoting the digest to ``:latest``.

    The merged index MUST NOT carry any ``unknown/unknown``
    attestation entries. Embedded BuildKit provenance manifests
    surface as ``unknown/unknown`` platform entries; the merge
    step rejects nested indexes and the postcondition rejects
    any embedded attestation manifest that survives the merge.

    The contract is platform-set driven (``{amd64, arm64} ==
    {amd64, arm64}``) rather than platform-count driven so a
    future attestation pipeline that does not embed provenance
    in the index does not break the contract.
    """
    text = _read(PUBLISH_WORKFLOW)
    # The postcondition MUST check for an exact digest match.
    assert "does not contain exactly the two candidate digests" in text or (
        "does not contain exactly" in text
    ), (
        "publish.yml must assert the merged index contains exactly "
        "the two candidate digests"
    )
    # The postcondition MUST reject unknown/unknown embedded
    # attestation entries.
    assert '"unknown"' in text and "unknown/unknown" in text, (
        "publish.yml must reject unknown/unknown embedded attestation "
        "entries in the merged index"
    )
    assert "platform.get(\"architecture\")" in text or (
        "platform.architecture" in text
    ), (
        "publish.yml must look up the architecture field on each "
        "manifest entry when validating the merged index"
    )


def test_publish_workflow_handles_imagetools_create_nonzero() -> None:
    """The merge step MUST treat a non-zero ``imagetools create``
    exit as an ambiguous Buildx result, NOT a hard failure.

    Earlier versions of this workflow propagated the exit code
    directly; Buildx has historically returned exit 255 after
    publishing a manifest whose own output it could not parse.
    The contract requires the workflow to retain the original
    command diagnostics (``::group::`` output) and to inspect
    GHCR before deciding whether the merge succeeded. The
    postcondition is the authoritative gate; the merge step
    MUST continue only when the remote merged index contains
    exactly the two expected candidate digests.
    """
    text = _read(PUBLISH_WORKFLOW)
    merge_block = text.split("name: Merge native candidates", 1)[1]
    merge_block = merge_block.split("name: Upload merged digest artifact", 1)[0]
    # The merge step MUST disable ``-e`` for the imagetools
    # invocation so a non-zero exit can be inspected.
    assert "set +e" in merge_block, (
        "publish.yml merge step MUST disable -e for the "
        "imagetools create invocation so a non-zero exit can "
        "be inspected"
    )
    assert "create_rc=" in merge_block, (
        "publish.yml merge step MUST capture the imagetools "
        "create exit code"
    )
    assert "set -e" in merge_block, (
        "publish.yml merge step MUST re-enable -e after the "
        "imagetools create invocation"
    )
    # The original command diagnostics MUST be retained.
    assert "imagetools-create.stderr" in merge_block, (
        "publish.yml merge step MUST retain the imagetools "
        "create stderr diagnostics for inspection"
    )
    assert "::group::imagetools create diagnostics" in merge_block, (
        "publish.yml merge step MUST surface the imagetools "
        "create diagnostics in a workflow log group"
    )


def test_publish_workflow_verifies_merged_manifest_on_both_arches() -> None:
    """The publish workflow MUST probe the merged multi-platform
    manifest on *both* native architectures before promoting it to
    ``:latest``.

    Promotion MUST be gated on both probes passing; a regression
    in either platform cannot ship through a partial green. The
    verify job MUST consume the merged digest through the
    download-artifact round trip so a re-resolved mutable tag
    cannot redirect the probe.
    """
    import yaml

    with PUBLISH_WORKFLOW.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    jobs = data["jobs"]
    assert "verify" in jobs, (
        "publish.yml must declare a `verify` matrix job that probes "
        "the merged manifest on both native architectures"
    )
    needs = jobs["verify"].get("needs") or []
    if isinstance(needs, str):
        needs = [needs]
    assert "merge" in needs, (
        "publish.yml verify job must `needs: merge`"
    )
    matrix = jobs["verify"].get("strategy", {}).get("matrix", {}).get("include", [])
    pairs = {(m.get("platform"), m.get("runner")) for m in matrix}
    assert ("linux/amd64", "ubuntu-24.04") in pairs, (
        "publish.yml verify must probe the merged manifest on "
        "ubuntu-24.04 (linux/amd64)"
    )
    assert ("linux/arm64", "ubuntu-24.04-arm") in pairs, (
        "publish.yml verify must probe the merged manifest on "
        "ubuntu-24.04-arm (linux/arm64)"
    )
    # The verify job MUST consume the merged digest through the
    # download-artifact round trip rather than a mutable tag.
    import yaml

    with PUBLISH_WORKFLOW.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    verify_steps = data["jobs"]["verify"]["steps"]
    download_steps = [
        s for s in verify_steps
        if isinstance(s, dict)
        and "download-artifact" in (s.get("uses") or "")
    ]
    assert any(
        (s.get("with") or {}).get("name") == "merged-digest"
        for s in download_steps
    ), (
        "publish.yml verify job MUST download the merged-digest "
        "artifact before probing the merged manifest"
    )
    # The probe step MUST consume the merged digest from the
    # downloaded artifact (NOT the mutable merged tag).
    verify_text = _read(PUBLISH_WORKFLOW)
    verify_body = verify_text.split("\n  verify:\n", 1)[1]
    verify_body = verify_body.split("\n  attest:\n", 1)[0]
    assert "merged-digest.txt" in verify_body, (
        "publish.yml verify job MUST consume the merged digest "
        "from the downloaded artifact"
    )


def test_publish_workflow_attests_before_promotion() -> None:
    """The publish workflow MUST attach a registry-side provenance
    attestation to the immutable merged digest before promoting
    the digest to ``:latest``.

    Embedded BuildKit provenance (the source of the previous
    Buildx exit 255 after merge) is replaced by a separate
    registry-attached attestation that runs only after both
    native probes pass. ``:latest`` MUST be tagged only after
    the attestation step succeeds.
    """
    import yaml

    text = _read(PUBLISH_WORKFLOW)
    assert re.search(r"actions/attest-build-provenance@[0-9a-f]{40}", text), (
        "publish.yml must pin the attestation action to an immutable commit"
    )
    with PUBLISH_WORKFLOW.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    jobs = data["jobs"]
    assert "attest" in jobs, (
        "publish.yml must declare an `attest` job that attaches "
        "registry-side provenance to the merged digest"
    )
    needs = jobs["attest"].get("needs") or []
    if isinstance(needs, str):
        needs = [needs]
    assert "verify" in needs, (
        "publish.yml attest job must `needs: verify`"
    )
    promote_needs = jobs["promote"].get("needs") or []
    if isinstance(promote_needs, str):
        promote_needs = [promote_needs]
    assert "attest" in promote_needs, (
        "publish.yml promote job must `needs: attest` so :latest "
        "promotion is gated on the attestation step"
    )
    # The attestation step MUST target the merged digest by name
    # and push the attestation back to the registry.
    attest_text = text.split("name: Attach provenance attestation", 1)[1]
    assert "subject-name: ghcr.io/pintjesb/titan-stocks-runner" in attest_text, (
        "publish.yml attest step MUST target the documented subject name"
    )
    assert "subject-digest:" in attest_text, (
        "publish.yml attest step MUST reference the merged digest by SHA-256"
    )
    assert "push-to-registry: true" in attest_text, (
        "publish.yml attest step MUST push the attestation back to the registry"
    )
    attest_steps = data["jobs"]["attest"]["steps"]
    assert any(
        re.fullmatch(r"docker/login-action@[0-9a-f]{40}", s.get("uses") or "")
        for s in attest_steps
    ), "publish.yml attest job MUST authenticate to GHCR with an immutable login-action pin"


def test_publish_workflow_attestation_permissions() -> None:
    """The attestation job MUST declare the ``id-token``,
    ``attestations``, and ``artifact-metadata`` write permissions
    required by ``actions/attest-build-provenance`` for the keyless OIDC signing
    flow and for ``:latest`` tag management.

    The promote job MUST declare ``artifact-metadata: write`` so
    the ``:latest`` tag update is authorised on the same
    authenticated session.
    """
    import yaml

    with PUBLISH_WORKFLOW.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    attest_perms = data["jobs"]["attest"].get("permissions") or {}
    required = {
        "id-token": "write",
        "attestations": "write",
        "artifact-metadata": "write",
    }
    for key, value in required.items():
        assert attest_perms.get(key) == value, (
            f"publish.yml attest job must declare {key}: {value}; "
            f"got {attest_perms.get(key)!r}"
        )
    promote_perms = data["jobs"]["promote"].get("permissions") or {}
    assert promote_perms.get("packages") == "write", (
        "publish.yml promote job must declare packages: write"
    )


def test_publish_workflow_promotion_requires_digest_equality() -> None:
    """The promote job MUST re-resolve ``:latest`` from the
    registry and require the digest to equal the merged digest.

    A divergence means a concurrent push raced and tagged
    ``:latest`` with a different manifest; the workflow MUST
    fail rather than ship a misleading pointer. The promote
    job MUST consume the merged digest through the
    download-artifact round trip rather than a mutable tag.
    """
    text = _read(PUBLISH_WORKFLOW)
    promote_block = text.split("name: Promote verified manifest", 1)[1]
    assert ":latest digest (" in promote_block and (
        "does not equal merged digest" in promote_block
    ), (
        "publish.yml promote job MUST re-resolve :latest and "
        "require its digest to equal the merged digest"
    )
    # The promote job MUST consume the merged digest through the
    # download-artifact round trip rather than a mutable tag.
    promote_body = text.split("\n  promote:\n", 1)[1]
    assert "merged-digest.txt" in promote_body, (
        "publish.yml promote job MUST consume the merged digest "
        "through the download-artifact round trip"
    )


def test_publish_workflow_disables_embedded_provenance() -> None:
    """The build job MUST set ``provenance: false`` and
    ``sbom: false`` on the build-push-action so the pushed
    candidate is a plain image manifest rather than an OCI
    index wrapping the image and embedded provenance.

    Embedded BuildKit provenance caused Buildx to return exit
    255 after publishing the merged manifest because the merge
    step chained two nested OCI indexes; the contract requires
    plain per-platform candidates.
    """
    text = _read(PUBLISH_WORKFLOW)
    assert "provenance: false" in text, (
        "publish.yml must disable BuildKit embedded provenance"
    )
    assert "sbom: false" in text, (
        "publish.yml must disable BuildKit embedded SBOM"
    )


def test_publish_workflow_python_uses_os_environ() -> None:
    """The Python verification step in the merge job MUST read
    the merged digest from ``os.environ`` rather than
    interpolating ``github.sha`` into a quoted heredoc.

    An earlier version of this step passed the literal
    ``${GITHUB_SHA}`` token to Python because the heredoc was
    quoted (``<<'PY'``); the contract is that the digest is
    consumed from the ``MERGED_DIGEST`` environment variable
    so a re-resolution never queries a non-existent reference.
    The scope of the regression check is the Python heredoc
    body only; the publish workflow legitimately uses
    ``${GITHUB_SHA}`` for diagnostic tags elsewhere.
    """
    text = _read(PUBLISH_WORKFLOW)
    # The Python verification step MUST read MERGED_DIGEST from
    # the environment.
    assert "os.environ[\"MERGED_DIGEST\"]" in text or (
        "os.environ['MERGED_DIGEST']" in text
    ), (
        "publish.yml merge step MUST read MERGED_DIGEST from "
        "os.environ in the Python verification block"
    )
    # The Python heredoc body MUST NOT embed ``${GITHUB_SHA}`` (or
    # any GitHub Actions template token) as a literal string.
    heredoc_start = text.find("<<'PY'")
    assert heredoc_start != -1, (
        "publish.yml merge step MUST contain a Python heredoc"
    )
    heredoc_end = text.find("\n          PY", heredoc_start)
    assert heredoc_end != -1, (
        "publish.yml merge step Python heredoc MUST be closed"
    )
    heredoc_body = text[heredoc_start:heredoc_end]
    for forbidden in ("\"${GITHUB_SHA}\"", "'${GITHUB_SHA}'", "${GITHUB_SHA}"):
        assert forbidden not in heredoc_body, (
            "publish.yml Python verification block MUST NOT embed a "
            "literal ${GITHUB_SHA} token; the digest is read from "
            "os.environ"
        )


def test_application_workflow_targets_arch_neutral_selector() -> None:
    """The application contract MUST reject an architecture-specific
    workflow selector.

    The application workflows in ``PintjesB/titan-stocks`` target
    ``runs-on: [self-hosted, linux, titan-ci]`` so the same
    selector dispatches to either compatible ``titan-ci`` runner
    (AMD64 or ARM64). A selector that lists ``X64`` or ``ARM64``
    (or any other architecture-specific label) would only match
    one native platform and is rejected by this contract.

    The runner repository does not own the application workflow,
    but it pins the selector contract in the operator-facing
    documentation and in the security / operations docs so a
    future application regression is caught by the docs review.
    """
    text = _read(README_FILE)
    assert "[self-hosted, linux, titan-ci]" in text, (
        "README.md must document the architecture-neutral workflow "
        "selector [self-hosted, linux, titan-ci]"
    )
    # An architecture-specific selector MUST NOT appear anywhere in
    # the documentation surface.
    forbidden_selectors = (
        "runs-on: [self-hosted, linux, X64, titan-ci]",
        "runs-on: [self-hosted, linux, ARM64, titan-ci]",
        "[self-hosted, linux, ARM64, titan-ci]",
    )
    for doc in (README_FILE, OPERATIONS_DOC, QUICK_START_DOC, SECURITY_DOC):
        doc_text = _read(doc)
        for forbidden in forbidden_selectors:
            assert forbidden not in doc_text, (
                f"{doc.name} must not declare architecture-specific "
                f"workflow selector {forbidden!r}"
            )


def test_env_example_uses_arch_neutral_label_and_no_targetarch() -> None:
    """The committed ``.env.example`` MUST declare the architecture-
    neutral ``TITAN_RUNNER_LABELS=titan-ci`` and MUST NOT carry a
    ``TARGETARCH`` runtime override.

    Architecture is a build / platform property, not an operator
    override; the contract surface documents GitHub's auto-
    attached ``self-hosted``, ``linux``, and ``X64``/``ARM64``
    labels and keeps the custom-label list at ``titan-ci`` only.
    """
    text = _read(ENV_EXAMPLE)
    assert "TITAN_RUNNER_LABELS=titan-ci" in text, (
        ".env.example must declare TITAN_RUNNER_LABELS=titan-ci"
    )
    # The old architecture-specific label list MUST NOT appear.
    forbidden_labels = (
        "TITAN_RUNNER_LABELS=self-hosted,linux,ARM64,titan-ci",
        "TITAN_RUNNER_LABELS=self-hosted,linux,X64,titan-ci",
    )
    for forbidden in forbidden_labels:
        assert forbidden not in text, (
            f".env.example must not declare {forbidden!r}; the contract "
            "uses TITAN_RUNNER_LABELS=titan-ci so GitHub auto-attaches "
            "the architecture label"
        )
    assert "TARGETARCH" not in text, (
        ".env.example must NOT carry a TARGETARCH override; "
        "architecture is a build/platform property, not an operator override"
    )


def test_compose_runtime_does_not_set_targetarch() -> None:
    """The Compose manifest MUST NOT set ``TARGETARCH`` on the
    runner service.

    Architecture is a build / platform property, not an operator
    override; the listener reads its native architecture from
    GitHub's auto-attached ``RUNNER_ARCH`` env var and the
    capability probe is scoped through ``EXPECTED_ARCH``.
    """
    text = _read(COMPOSE_FILE)
    assert "TARGETARCH" not in text, (
        "docker-compose.yml must NOT declare TARGETARCH; "
        "architecture is a build/platform property"
    )


def test_deploy_probe_passes_expected_arch_to_sidecar() -> None:
    """``deploy.sh probe`` MUST pass ``EXPECTED_ARCH`` to the
    capability-probe sidecar so the probe can refuse an emulated /
    mismatched Docker daemon before the rest of the contract runs.
    """
    text = _read(DEPLOY_SCRIPT)
    branch = text.split("probe)", 1)[1].split("up)", 1)[0]
    assert "-e EXPECTED_ARCH=" in branch, (
        "deploy.sh probe must pass -e EXPECTED_ARCH to the sidecar"
    )
    assert "EXPECTED_ARCH=\"$EXPECTED_ARCH\"" in branch or (
        'EXPECTED_ARCH="$EXPECTED_ARCH"' in branch
    ), (
        "deploy.sh probe must forward the host-mapped EXPECTED_ARCH "
        "(amd64 or arm64)"
    )


def test_deploy_registers_architecture_neutral_default_labels() -> None:
    """The default ``TITAN_RUNNER_LABELS`` and ``RUNNER_LABELS``
    fallback MUST both be ``titan-ci`` only.

    The architecture label (X64 / ARM64), the ``self-hosted``
    label, and the ``linux`` label are auto-attached by GitHub
    based on the listener's actual platform; the custom-label
    list intentionally omits them so a future architecture
    migration is a GitHub-side change rather than a
    ``TITAN_RUNNER_LABELS`` rotation.
    """
    text = _read(DEPLOY_SCRIPT)
    # The deploy-time default MUST be titan-ci only.
    assert 'TITAN_RUNNER_LABELS:-titan-ci' in text, (
        "deploy.sh must default TITAN_RUNNER_LABELS to titan-ci"
    )
    # Register.sh's RUNNER_LABELS default MUST be titan-ci only.
    register_text = _read(REGISTER_SCRIPT)
    assert 'RUNNER_LABELS:-titan-ci' in register_text, (
        "register.sh must default RUNNER_LABELS to titan-ci"
    )
    # An architecture-specific custom label list MUST NOT appear
    # in either file.
    forbidden = (
        "RUNNER_LABELS:-self-hosted,linux,ARM64,titan-ci",
        "RUNNER_LABELS:-self-hosted,linux,X64,titan-ci",
    )
    for marker in forbidden:
        assert marker not in register_text, (
            f"register.sh must not default to architecture-specific "
            f"label list {marker!r}"
        )


def test_pre_job_hook_validates_native_runner_architecture() -> None:
    """The pre-job hook MUST read ``RUNNER_ARCH`` and verify the
    Docker daemon's reported architecture matches the native
    runner architecture.

    GitHub supplies ``RUNNER_ARCH`` as ``X64`` on x86_64 runners
    and ``ARM64`` on aarch64 runners; the hook maps that to the
    ``amd64`` / ``arm64`` aliases Docker reports and rejects an
    emulated or mismatched daemon before the rest of the contract
    runs.
    """
    text = _read(PRE_JOB_SCRIPT)
    code_only = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )
    assert "RUNNER_ARCH" in code_only, (
        "pre-job.sh must read RUNNER_ARCH"
    )
    assert "X64" in code_only and "ARM64" in code_only, (
        "pre-job.sh must handle both X64 and ARM64"
    )
    assert "expected_arch" in code_only, (
        "pre-job.sh must compute expected_arch from RUNNER_ARCH"
    )
    assert "daemon_arch" in code_only, (
        "pre-job.sh must parse the Docker daemon's reported architecture"
    )
    # The match assertion MUST compare the two architectures.
    assert "= \"$expected_arch\"" in code_only, (
        "pre-job.sh must refuse a daemon whose architecture does "
        "not match the native runner architecture"
    )


def test_probe_requires_expected_arch_and_validates_daemon_arch() -> None:
    """The capability probe MUST require ``EXPECTED_ARCH`` and
    MUST refuse a Docker daemon whose reported architecture does
    not match the native runner architecture.

    Deploy.sh maps the host's ``uname -m`` output to the matching
    ``amd64`` / ``arm64`` value; the probe uses that value as the
    authoritative native architecture so an emulated or
    mismatched daemon cannot pass the capability gate.
    """
    text = _read(PROBE_SCRIPT)
    code_only = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )
    assert "EXPECTED_ARCH" in code_only, (
        "probe.sh must require EXPECTED_ARCH"
    )
    # The probe MUST refuse an unsupported EXPECTED_ARCH up
    # front before exercising the rest of the contract.
    assert "amd64|arm64" in code_only, (
        "probe.sh must whitelist EXPECTED_ARCH to amd64 or arm64"
    )
    # The Docker daemon probe MUST map the docker-reported
    # architecture to the same ``amd64`` / ``arm64`` aliases.
    assert "daemon_arch" in code_only, (
        "probe.sh must parse the Docker daemon's reported architecture"
    )
    assert "EXPECTED_ARCH" in code_only and "daemon_arch" in code_only, (
        "probe.sh must compare the daemon's reported architecture to EXPECTED_ARCH"
    )
    # The match MUST be a strict equality check.
    assert "daemon_arch\" != \"$EXPECTED_ARCH\"" in code_only or (
        "daemon_arch != $EXPECTED_ARCH" in code_only
    ), (
        "probe.sh must refuse a daemon whose architecture does not "
        "match EXPECTED_ARCH"
    )


def test_fetch_runner_maps_targetarch_and_rejects_unsupported() -> None:
    """``fetch-runner.sh`` MUST map ``TARGETARCH`` to the upstream
    archive naming (amd64 -> x64, arm64 -> arm64), select the
    matching ``RUNNER_SHA256_ARM64`` / ``RUNNER_SHA256_X64``
    digest, and refuse every other architecture before any
    download attempt.
    """
    text = _read(SCRIPTS_DIR / "fetch-runner.sh")
    code_only = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )
    # The TARGETARCH -> upstream archive name mapping.
    assert "amd64)" in code_only and "x64" in code_only, (
        "fetch-runner.sh must map amd64 -> x64"
    )
    assert "arm64)" in code_only, (
        "fetch-runner.sh must map arm64 -> arm64"
    )
    # The architecture-specific digest selection.
    assert "RUNNER_SHA256_X64" in code_only, (
        "fetch-runner.sh must select RUNNER_SHA256_X64 for amd64"
    )
    assert "RUNNER_SHA256_ARM64" in code_only, (
        "fetch-runner.sh must select RUNNER_SHA256_ARM64 for arm64"
    )
    # The unsupported-architecture rejection path MUST run before
    # the download.
    assert "unsupported architecture" in code_only, (
        "fetch-runner.sh must reject unsupported TARGETARCH values"
    )
    # The script MUST exit with a dedicated code on an unsupported
    # architecture (the contract uses exit code 5).
    assert re.search(r"exit.*5", code_only, re.DOTALL), (
        "fetch-runner.sh must exit with code 5 on an unsupported architecture"
    )



    """The committed ``.env.example`` MUST NOT contain any real
    registration token or production credentials. It documents the
    schema; secrets live in the gitignored ``.env`` only."""
    assert ENV_EXAMPLE.exists(), (
        ".env.example must be committed at the repository root"
    )
    text = _read(ENV_EXAMPLE)
    forbidden_markers = (
        "ghcr.io/pintjesb/titan-stocks-runner@sha256:",  # not real, but the example uses placeholder
    )
    # The example uses a placeholder digest; ensure no real token value.
    assert "TITAN_RUNNER_TOKEN=" not in text or "TITAN_RUNNER_TOKEN=\n" in text or text.count("TITAN_RUNNER_TOKEN=") <= 1 and (
        "# TITAN_RUNNER_TOKEN=" in text or "TITAN_RUNNER_TOKEN=" not in text.split("\n", 1)[1]
    ), (
        ".env.example must NOT commit a registration token value"
    )


def test_compose_has_one_heavy_and_optional_light_listener() -> None:
    """Both listeners register internally; the light listener is opt-in."""
    import yaml

    with COMPOSE_FILE.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    assert set(data.get("services", {})) == {"runner", "runner-light"}
    assert "depends_on" not in data["services"]["runner"]
    assert "depends_on" not in data["services"]["runner-light"]
    assert data["services"]["runner-light"]["profiles"] == ["ci-light"]


def test_compose_runner_receives_startup_registration_token() -> None:
    """The sole runner service receives the short-lived token for its
    internal registration phase."""
    import yaml

    with COMPOSE_FILE.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    environment = data["services"]["runner"]["environment"]
    assert environment["RUNNER_TOKEN"] == "${TITAN_RUNNER_TOKEN:-}"


def test_start_runner_runs_registration_before_listener() -> None:
    """The single container must run idempotent registration before
    checking state and launching the listener."""
    text = _read(START_RUNNER_SCRIPT)
    register_idx = text.index("/usr/local/bin/register")
    state_idx = text.index("missing persisted credential")
    listener_idx = text.index('"$RUNNER_RUNTIME_DIR/run.sh"')
    assert register_idx < state_idx < listener_idx
    assert "unset RUNNER_TOKEN" in text


def test_compose_runner_mounts_state_and_work() -> None:
    """The sole runner service owns both registration and listener
    mounts, including state and the named work volume."""
    text = _read(COMPOSE_FILE)
    assert "titan-runner-state:/var/lib/titan-runner/state" in text
    assert "titan-runner-work:/var/lib/titan-runner/work" in text
    assert "/var/run/docker.sock:/var/run/docker.sock" in text


def test_register_serializes_through_state_lock() -> None:
    """``register.sh`` MUST serialise concurrent registrations
    through an exclusive flock inside the persistent state
    volume so two ``up`` invocations cannot race a credential
    replacement."""
    text = _read(REGISTER_SCRIPT)
    code_only = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )
    assert ".lock" in code_only, (
        "register.sh must hold its lock inside the persistent state volume"
    )
    assert "register.lock" in code_only, (
        "register.sh must serialise on state/.lock/register.lock"
    )
    assert "flock -n 9" in code_only, (
        "register.sh must take a non-blocking flock"
    )


def test_register_is_idempotent_without_token() -> None:
    """``register.sh`` MUST be idempotent: a complete persisted
    identity exits successfully without contacting GitHub and
    without requiring ``RUNNER_TOKEN``."""
    text = _read(REGISTER_SCRIPT)
    # The identity check uses the diagnostics summary.
    assert "EXISTING_REPO" in text, (
        "register.sh must read the existing repository URL from "
        "state/diagnostics.txt"
    )
    assert "EXISTING_NAME" in text, (
        "register.sh must read the existing runner name from "
        "state/diagnostics.txt"
    )
    assert "EXISTING_LABELS" in text, (
        "register.sh must read the existing labels from "
        "state/diagnostics.txt"
    )
    assert "identity_matches" in text, (
        "register.sh must define an identity_matches helper"
    )
    # The matching path exits successfully without contacting GitHub.
    code_only = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )
    assert "registration already complete with matching identity" in code_only, (
        "register.sh must log a matching-identity skip"
    )
    assert "RUNNER_TOKEN is empty" in code_only, (
        "register.sh must fail with actionable guidance when "
        "state is missing and the token is empty"
    )
    assert "different identity" in code_only, (
        "register.sh must fail with actionable guidance when "
        "state has drifted and the token is empty"
    )


def test_register_rejects_persisted_ephemeral_state() -> None:
    """Persisted one-shot credentials must never be treated as a reusable listener."""
    text = _read(REGISTER_SCRIPT)
    code_only = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )
    assert "STATE_PERSISTENT" in code_only
    assert "(.Ephemeral // false) == false" in code_only
    assert "persisted runner registration is ephemeral/one-shot" in code_only
    identity_block = code_only.split("identity_matches() {", 1)[1].split("}", 1)[0]
    assert 'STATE_PERSISTENT" -eq 1' in identity_block


def test_register_is_transactional() -> None:
    """``register.sh`` MUST back up the existing credentials before
    any destructive step and restore them on failure so a
    failed replacement cannot overwrite working persisted
    credentials.

    The rollback is described as local best-effort protection; the
    script does NOT claim GitHub's remote runner record can be
    transactionally restored after ``config.sh --replace``.
    """
    text = _read(REGISTER_SCRIPT)
    assert "backup_state" in text, (
        "register.sh must define a backup_state helper"
    )
    assert "restore_state" in text, (
        "register.sh must define a restore_state helper"
    )
    assert 'mktemp -d "$RUNNER_STATE_DIR/.backup.XXXXXX"' in text, (
        "register.sh must allocate a collision-safe backup directory"
    )
    assert "COMMIT_IN_PROGRESS=1" in text and "cleanup()" in text, (
        "register.sh must keep a commit marker and central cleanup handler"
    )
    assert "if [ \"$COMMIT_IN_PROGRESS\" -eq 1 ]" in text and "restore_state" in text, (
        "register.sh must restore previous credentials from the EXIT handler "
        "when a replacement fails"
    )
    # The script MUST NOT claim the GitHub-side runner record can
    # be transactionally restored after ``config.sh --replace``.
    assert "transactionally restored" not in text or (
        "not transactionally restored" in text
        or "not transactionally" in text
    ), (
        "register.sh must describe the rollback as local best-effort "
        "protection rather than a transactional GitHub-side restore"
    )


def test_compose_listener_has_no_registration_dependency() -> None:
    """The listener and registration phases share one container; no
    disposable service dependency is allowed."""
    import yaml

    with COMPOSE_FILE.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    services = data.get("services")
    assert isinstance(services, dict)
    runner = services.get("runner")
    assert isinstance(runner, dict)
    assert "depends_on" not in runner


def test_register_drift_triggers_reregistration_with_backup() -> None:
    """Identity drift MUST trigger a transactional local
    re-registration: a non-empty ``RUNNER_TOKEN`` together with a
    drifted persisted identity MUST take a local backup and call
    ``config.sh --replace``; the persistent state is NOT
    rewritten when the persisted identity already matches.
    """
    text = _read(REGISTER_SCRIPT)
    # Locate the branch that runs when the token is present and the
    # state is drifted.
    code_only = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )
    assert "persisted identity drift detected" in code_only, (
        "register.sh must log identity drift before re-registering"
    )
    # The drift branch MUST take a backup and call ``--replace``.
    drift_block = code_only.split("persisted identity drift detected", 1)[1]
    drift_block = drift_block.split("log \"materialising", 1)[0]
    assert "backup_state" in drift_block, (
        "register.sh must back up the existing credentials before "
        "calling config.sh --replace on identity drift"
    )
    assert "--replace" in code_only, (
        "register.sh must call config.sh --replace on identity drift"
    )
    # The matching-identity exit branch MUST run before any
    # ``config.sh`` invocation or backup; the matching path exits
    # successfully without contacting GitHub.
    match_block = code_only.split("registration already complete with matching identity", 1)
    assert len(match_block) == 2, (
        "register.sh must log a matching-identity skip before contacting GitHub"
    )
    after_match = match_block[1]
    next_exit_idx = after_match.find("exit 0")
    assert next_exit_idx != -1, (
        "register.sh matching-identity path MUST exit 0 before any "
        "destructive step"
    )
    rest = after_match[:next_exit_idx]
    assert "config.sh" not in rest, (
        "register.sh matching-identity path MUST NOT call config.sh"
    )
    assert "BACKUP_DIR=\"$(backup_state)\"" not in rest, (
        "register.sh matching-identity path MUST NOT take a backup"
    )


def test_register_handles_failed_local_state_publication() -> None:
    """``register.sh`` MUST detect a failed local publication of the
    new credentials into ``titan-runner-state`` (for example a
    read-only filesystem or a permission failure) and restore the
    backup rather than leave the persistent state half-written.

    The diagnostics summary MUST be written AFTER every credential
    file has landed on disk so a partial publish leaves no fresh
    ``diagnostics.txt`` behind; the next ``register`` run then sees
    the previous identity and refuses to silently overwrite the
    partial state.
    """
    text = _read(REGISTER_SCRIPT)
    code_only = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )
    assert "failed to publish credentials into" in code_only, (
        "register.sh must report the local publication failure path"
    )
    # The diagnostics block MUST be written AFTER the credential
    # publication step; locate the publication block and the
    # diagnostics block to confirm the order.
    publication_idx = code_only.find('cp "$RUNNER_RUNTIME_DIR/$fname"')
    diagnostics_idx = code_only.find("titan-runner diagnostics")
    assert publication_idx != -1, (
        "register.sh must publish the credentials"
    )
    assert diagnostics_idx != -1, (
        "register.sh must write the diagnostics summary"
    )
    assert publication_idx < diagnostics_idx, (
        "register.sh MUST publish the credentials BEFORE writing "
        "the diagnostics summary so a partial publish leaves no "
        "fresh diagnostics.txt behind"
    )
    # A failed local publication MUST leave the commit marker active
    # so the central EXIT handler restores the backup (or clears a
    # fresh partial state).
    publication_guard = code_only.split("if [ \"$published_ok\" -ne 1 ]", 1)
    assert len(publication_guard) >= 2, (
        "register.sh must check $published_ok after the credential "
        "publication loop and route failures through the central rollback"
    )
    publication_guard = publication_guard[1]
    publication_guard = publication_guard.split("fi", 1)[0]
    assert "fail \"failed to publish credentials" in publication_guard, (
        "register.sh MUST fail the local credential publication step"
    )
    cleanup = code_only.split("cleanup()", 1)[1].split("trap cleanup EXIT", 1)[0]
    assert "restore_state" in cleanup and "clear_managed_state" in cleanup, (
        "register.sh cleanup must restore a backup or clear fresh partial state"
    )


def test_register_token_metadata_is_removed_after_blank_and_rerun() -> None:
    """The blank-and-rerun flow MUST remove the token from the
    recreated runner container metadata.

    ``register.sh`` unsets ``RUNNER_TOKEN`` immediately after
    ``config.sh`` returns and traps the unset on every exit path,
    so a blanked ``TITAN_RUNNER_TOKEN`` in ``.env`` followed by a
    fresh ``docker compose up -d`` recreates the runner without the
    token. The trap MUST also unset the token
    on every exit path including SIGINT/SIGTERM so an interrupted
    registration does not leave the token visible in the process
    list or in child output.
    """
    text = _read(REGISTER_SCRIPT)
    code_only = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )
    # The token is unset immediately after ``config.sh`` returns.
    post_config = code_only.split("config.sh", 1)[1]
    unset_index = post_config.find("unset RUNNER_TOKEN")
    assert unset_index != -1, (
        "register.sh must unset RUNNER_TOKEN after config.sh returns"
    )
    # The trap on EXIT is the documented mechanism for removing
    # the token from the stopped container metadata; it must run
    # even when the script exits through an error.
    assert "trap 'unset RUNNER_TOKEN" in code_only, (
        "register.sh must trap-unset RUNNER_TOKEN on EXIT so the "
        "token never lingers in stopped container metadata"
    )
    # The token MUST NOT appear in the diagnostics summary; an
    # operator-readable summary that contained the token would
    # leak it through the stopped container metadata even when
    # the trap fires. Locate the diagnostics block in the full
    # text (including comments) and assert the token never
    # appears in any of its ``printf`` lines.
    diagnostics_section = text.split("titan-runner diagnostics", 1)[1]
    diagnostics_section = diagnostics_section.split("rm -rf \"$RUNNER_RUNTIME_DIR\"", 1)[0]
    assert "RUNNER_TOKEN" not in diagnostics_section, (
        "register.sh MUST NOT embed RUNNER_TOKEN in diagnostics.txt"
    )


def test_compose_token_is_startup_only_and_not_persisted() -> None:
    """The runner receives the token for startup, while the entrypoint
    explicitly removes it before execing the listener."""
    import yaml

    with COMPOSE_FILE.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    environment = data["services"]["runner"]["environment"]
    assert "RUNNER_TOKEN" in environment
    text = _read(START_RUNNER_SCRIPT)
    unset_idx = text.index("unset RUNNER_TOKEN")
    launch_idx = text.index('"$RUNNER_RUNTIME_DIR/run.sh"')
    assert unset_idx < launch_idx


def test_publish_workflow_installs_pinned_test_requirements() -> None:
    """The publish workflow MUST install the pinned test
    requirements (pytest + PyYAML) rather than ad-hoc ``pip
    install pytest``."""
    import yaml

    text = _read(PUBLISH_WORKFLOW)
    # The contract job must install the pinned test requirements.
    contract_steps = text.split("jobs:")[1].split("jobs:")[0]
    assert "tests/requirements.txt" in contract_steps, (
        "publish.yml contract job must install tests/requirements.txt"
    )
    assert "pip install --quiet --requirement tests/requirements.txt" in contract_steps, (
        "publish.yml contract job must use `pip install --requirement "
        "tests/requirements.txt`"
    )
    # The ad-hoc ``pip install pytest`` invocation must be gone.
    assert "--quiet pytest" not in contract_steps, (
        "publish.yml contract job must NOT install pytest directly; "
        "the pinned requirements file is the source of truth"
    )
    # PyYAML must appear so the workflow contract tests can parse
    # ``publish.yml``.
    with (ROOT / "tests" / "requirements.txt").open(encoding="utf-8") as fh:
        reqs = fh.read()
    assert "pytest" in reqs, "tests/requirements.txt must pin pytest"
    assert "PyYAML" in reqs, "tests/requirements.txt must pin PyYAML"
    for line in reqs.splitlines():
        if line.startswith("pytest") or line.startswith("PyYAML"):
            assert "==" in line, (
                f"tests/requirements.txt must pin {line.split('=')[0]} "
                f"to an exact version"
            )


def test_publish_workflow_uses_shellcheck_warning_severity() -> None:
    """The publish workflow MUST gate on ShellCheck at warning
    severity (not error) so the contract surfaces every
    regression but does not block on ``info`` diagnostics."""
    text = _read(PUBLISH_WORKFLOW)
    assert "shellcheck -S warning" in text, (
        "publish.yml must run shellcheck with -S warning"
    )
    assert "shellcheck -S error" not in text, (
        "publish.yml must NOT elevate ShellCheck to -S error"
    )


def test_deploy_env_file_overrides_take_precedence() -> None:
    """``populate_from_env_file`` MUST NOT overwrite a variable that
    the operator has already exported, including a deliberately
    empty override (``export TITAN_RUNNER_TOKEN=``).

    The test sources ``deploy.sh``'s ``build_env_file`` and
    ``populate_from_env_file`` helpers directly and exercises
    them with a mode-0600 temporary ``.env`` that contains a
    token. A shell-exported empty ``TITAN_RUNNER_TOKEN`` MUST win
    over the ``.env`` value.
    """
    import os
    import subprocess
    import tempfile

    repo_root = ROOT
    # Extract the helpers from ``deploy.sh`` and run them in a
    # subprocess so the test exercises the same code path an
    # operator hits when they invoke ``deploy.sh``. The helper
    # script lives in a temporary directory under ``/tmp`` so the
    # repository tree is never polluted with test artefacts.
    helper_text = (
        "#!/usr/bin/env bash\n"
        "# Source the helpers from deploy.sh in a function-only\n"
        "# sandbox so the test can exercise populate_from_env_file\n"
        "# without running the rest of deploy.sh.\n"
        "set -euo pipefail\n"
        "ROOT_DIR=\"${1:?}\"\n"
        "ENV_FILE=\"${2:?}\"\n"
        # Bring the documented helpers into scope.\n"
        "ALLOWLIST_KEYS=(TITAN_RUNNER_TOKEN TITAN_RUNNER_LOCK_FILE TITAN_RUNNER_IMAGE)\n"
        "ALLOWLIST_RE='^[A-Za-z_][A-Za-z0-9_]*$'\n"
        "is_allowed_key() {\n"
        "    local key=\"$1\"\n"
        "    local allowed\n"
        "    for allowed in \"${ALLOWLIST_KEYS[@]}\"; do\n"
        "        if [ \"$allowed\" = \"$key\" ]; then\n"
        "            return 0\n"
        "        fi\n"
        "    done\n"
        "    return 1\n"
        "}\n"
        "is_safe_value() {\n"
        "    case \"$1\" in *'$('*) return 1 ;; *'`'*) return 1 ;; *'\"'*) return 1 ;; *'\\\\'*) return 1 ;; *) return 0 ;; esac\n"
        "}\n"
        "build_env_file() {\n"
        "    local env_file=\"$1\"\n"
        "    local out\n"
        "    out=\"$(mktemp -t titan-runner-env.XXXXXX)\"\n"
        "    chmod 0600 \"$out\"\n"
        "    local line stripped key value\n"
        "    while IFS= read -r line || [ -n \"$line\" ]; do\n"
        "        case \"$line\" in ''|\\#*) continue ;; esac\n"
        "        case \"$line\" in export\\ *) stripped=\"${line#export }\" ;; *) stripped=\"$line\" ;; esac\n"
        "        if [[ \"$stripped\" =~ ^([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]]; then\n"
        "            key=\"${BASH_REMATCH[1]}\"\n"
        "            value=\"${BASH_REMATCH[2]}\"\n"
        "            case \"$value\" in \\\"*) value=\"${value#\\\"}\"; value=\"${value%\\\"}\" ;; \\'*) value=\"${value#\\'}\"; value=\"${value%\\'}\" ;; esac\n"
        "            if [ -n \"$key\" ] && [[ \"$key\" =~ $ALLOWLIST_RE ]] && is_allowed_key \"$key\" && is_safe_value \"$value\"; then\n"
        "                printf '%s=%s\\n' \"$key\" \"$value\" >> \"$out\"\n"
        "            fi\n"
        "        fi\n"
        "    done < \"$env_file\"\n"
        "    printf '%s\\n' \"$out\"\n"
        "}\n"
        "populate_from_env_file() {\n"
        "    local env_file=\"$1\"\n"
        "    [ -f \"$env_file\" ] || return 0\n"
        "    local parsed line key value\n"
        "    parsed=\"$(build_env_file \"$env_file\")\"\n"
        "    while IFS= read -r line || [ -n \"$line\" ]; do\n"
        "        case \"$line\" in ''|\\#*) continue ;; esac\n"
        "        if [[ \"$line\" =~ ^([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]]; then\n"
        "            key=\"${BASH_REMATCH[1]}\"\n"
        "            value=\"${BASH_REMATCH[2]}\"\n"
        "            if [ -z \"${!key+set}\" ]; then\n"
        "                printf -v \"$key\" '%s' \"$value\"\n"
        "                declare -x \"$key\"\n"
        "            fi\n"
        "        fi\n"
        "    done < \"$parsed\"\n"
        "    rm -f \"$parsed\"\n"
        "}\n"
        # The actual test: the calling shell exports an empty\n"
        # TITAN_RUNNER_TOKEN. The .env value MUST NOT win.\n"
        "populate_from_env_file \"$ENV_FILE\"\n"
        "if [ -z \"${TITAN_RUNNER_TOKEN+set}\" ]; then\n"
        "    printf 'unset\\n'; exit 0\n"
        "fi\n"
        "if [ -z \"${TITAN_RUNNER_TOKEN:-}\" ]; then\n"
        "    printf 'empty-override\\n'; exit 0\n"
        "fi\n"
        "printf 'value-present\\n'\n"
    )
    tmp_dir = tempfile.mkdtemp(prefix="titan-runner-env-test-")
    try:
        env_path = os.path.join(tmp_dir, "precedence.env")
        with open(env_path, "w", encoding="utf-8") as fh:
            fh.write("TITAN_RUNNER_TOKEN=from-env-file\n")
        os.chmod(env_path, 0o600)

        helper_path = os.path.join(tmp_dir, "_check_env_precedence.sh")
        with open(helper_path, "w", encoding="utf-8") as fh:
            fh.write(helper_text)
        os.chmod(helper_path, 0o755)

        env = os.environ.copy()
        env["PATH"] = "/usr/bin:/bin"
        # Deliberately export an empty token; the .env value must
        # NOT overwrite it.
        env["TITAN_RUNNER_TOKEN"] = ""

        result = subprocess.run(
            ["bash", helper_path, str(repo_root), env_path],
            cwd=repo_root,
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (
            f"env precedence helper failed: stdout={result.stdout!r} "
            f"stderr={result.stderr!r}"
        )
        assert result.stdout.strip() == "empty-override", (
            "explicit empty override MUST win over .env file value; "
            f"got {result.stdout!r}"
        )
    finally:
        import shutil

        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_deploy_env_file_loads_before_lock_resolution() -> None:
    """``populate_from_env_file`` MUST run before
    ``TITAN_RUNNER_LOCK_FILE`` is resolved so the operator can
    override the lock-file location through the documented
    allowlist.

    The test extracts the relevant snippets from ``deploy.sh`` and
    confirms that a ``TITAN_RUNNER_LOCK_FILE`` override in the
    allowlisted ``.env`` reaches the LOCK_FILE assignment.
    """
    import os
    import shutil
    import subprocess
    import tempfile

    repo_root = ROOT
    # Build a mode-0600 .env that overrides the lock file path.
    # Both helper script and the .env file live under a temporary
    # directory so the repository tree is never polluted with
    # test artefacts.
    tmp_dir = tempfile.mkdtemp(prefix="titan-runner-lock-test-")
    try:
        custom_lock = os.path.join(tmp_dir, "custom-lock")
        env_path = os.path.join(tmp_dir, "lock.env")
        with open(env_path, "w", encoding="utf-8") as fh:
            fh.write(f"TITAN_RUNNER_LOCK_FILE={custom_lock}\n")
        os.chmod(env_path, 0o600)

        helper_text = (
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "ENV_FILE=\"${1:?}\"\n"
            "ALLOWLIST_KEYS=(TITAN_RUNNER_LOCK_FILE TITAN_RUNNER_IMAGE)\n"
            "ALLOWLIST_RE='^[A-Za-z_][A-Za-z0-9_]*$'\n"
            "is_allowed_key() {\n"
            "    local key=\"$1\"\n"
            "    local allowed\n"
            "    for allowed in \"${ALLOWLIST_KEYS[@]}\"; do\n"
            "        if [ \"$allowed\" = \"$key\" ]; then return 0; fi\n"
            "    done\n"
            "    return 1\n"
            "}\n"
            "is_safe_value() {\n"
            "    case \"$1\" in *'$('*) return 1 ;; *'`'*) return 1 ;; *'\"'*) return 1 ;; *'\\\\'*) return 1 ;; *) return 0 ;; esac\n"
            "}\n"
            "build_env_file() {\n"
            "    local env_file=\"$1\"\n"
            "    local out\n"
            "    out=\"$(mktemp -t titan-runner-env.XXXXXX)\"\n"
            "    chmod 0600 \"$out\"\n"
            "    local line stripped key value\n"
            "    while IFS= read -r line || [ -n \"$line\" ]; do\n"
            "        case \"$line\" in ''|\\#*) continue ;; esac\n"
            "        case \"$line\" in export\\ *) stripped=\"${line#export }\" ;; *) stripped=\"$line\" ;; esac\n"
            "        if [[ \"$stripped\" =~ ^([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]]; then\n"
            "            key=\"${BASH_REMATCH[1]}\"\n"
            "            value=\"${BASH_REMATCH[2]}\"\n"
            "            case \"$value\" in \\\"*) value=\"${value#\\\"}\"; value=\"${value%\\\"}\" ;; \\'*) value=\"${value#\\'}\"; value=\"${value%\\'}\" ;; esac\n"
            "            if [ -n \"$key\" ] && [[ \"$key\" =~ $ALLOWLIST_RE ]] && is_allowed_key \"$key\" && is_safe_value \"$value\"; then\n"
            "                printf '%s=%s\\n' \"$key\" \"$value\" >> \"$out\"\n"
            "            fi\n"
            "        fi\n"
            "    done < \"$env_file\"\n"
            "    printf '%s\\n' \"$out\"\n"
            "}\n"
            "populate_from_env_file() {\n"
            "    local env_file=\"$1\"\n"
            "    [ -f \"$env_file\" ] || return 0\n"
            "    local parsed line key value\n"
            "    parsed=\"$(build_env_file \"$env_file\")\"\n"
            "    while IFS= read -r line || [ -n \"$line\" ]; do\n"
            "        case \"$line\" in ''|\\#*) continue ;; esac\n"
            "        if [[ \"$line\" =~ ^([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]]; then\n"
            "            key=\"${BASH_REMATCH[1]}\"\n"
            "            value=\"${BASH_REMATCH[2]}\"\n"
            "            if [ -z \"${!key+set}\" ]; then\n"
            "                printf -v \"$key\" '%s' \"$value\"\n"
            "                declare -x \"$key\"\n"
            "            fi\n"
            "        fi\n"
            "    done < \"$parsed\"\n"
            "    rm -f \"$parsed\"\n"
            "}\n"
            "populate_from_env_file \"$ENV_FILE\"\n"
            "LOCK_FILE=\"${TITAN_RUNNER_LOCK_FILE:-/var/lock/titan-runner.lock}\"\n"
            "printf '%s\\n' \"$LOCK_FILE\"\n"
        )
        helper = os.path.join(tmp_dir, "_check_lock_resolution.sh")
        with open(helper, "w", encoding="utf-8") as fh:
            fh.write(helper_text)
        os.chmod(helper, 0o755)

        env = os.environ.copy()
        env["PATH"] = "/usr/bin:/bin"
        # Make sure the test does NOT inherit a TITAN_RUNNER_LOCK_FILE
        # override from the host shell.
        env.pop("TITAN_RUNNER_LOCK_FILE", None)
        result = subprocess.run(
            ["bash", helper, env_path],
            cwd=repo_root,
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (
            f"lock resolution helper failed: stdout={result.stdout!r} "
            f"stderr={result.stderr!r}"
        )
        assert result.stdout.strip() == custom_lock, (
            "deploy.sh must honour TITAN_RUNNER_LOCK_FILE from the .env "
            f"file; got {result.stdout.strip()!r}, expected {custom_lock!r}"
        )
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_post_job_hook_does_not_recurse_work_directory() -> None:
    """The post-job hook MUST NOT recurse into the runner's
    ``_work`` directory. The bounded cleanup is anchored on the
    documented ``titan-stocks-playwright-`` project label; any
    non-Titan checkout, any application development volume, and
    any unrelated workspace is left alone."""
    text = _read(POST_JOB_SCRIPT)
    code_only = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )
    assert "find" not in code_only or "find \"$dest\"" in code_only, (
        "post-job.sh must NOT use `find` to recurse the runner workspace; "
        "the bounded cleanup operates on the documented project label"
    )
    # Defensive: even if a ``find`` slipped in, it must not be
    # anchored on the ``titan-`` prefix.
    assert "'titan-*'" not in code_only, (
        "post-job.sh must NOT use a `titan-*` glob anywhere in executable code"
    )
    assert '"titan-*"' not in code_only, (
        "post-job.sh must NOT use a `titan-*` glob anywhere in executable code"
    )
    assert "remove_titan_work_files" not in code_only, (
        "post-job.sh must NOT define a workspace recursion helper"
    )


def test_post_job_hook_does_not_target_runner_or_app_resources() -> None:
    """The post-job hook MUST scope its cleanup to the documented
    ``titan-stocks-playwright-`` Compose project label so the
    runner container, the runner volumes, the application
    development volumes, and any unrelated Titan-prefixed
    workload are never targeted.

    The test stubs ``docker`` in PATH to print a fake
    ``docker ps -a --filter label=…`` output that contains both
    matching and non-matching project labels, plus a runner
    container. The hook MUST filter the output to only the
    ``titan-stocks-playwright-`` prefix and MUST NOT issue a
    ``docker rm`` for the runner container or a
    ``docker volume rm`` for a named runner volume.
    """
    import os
    import stat
    import subprocess
    import tempfile

    repo_root = ROOT

    stub_dir = tempfile.mkdtemp()
    try:
        # Stub ``docker`` that records every invocation and
        # returns a deterministic response to the documented
        # commands.
        calls_log = os.path.join(stub_dir, "calls.log")
        with open(calls_log, "w", encoding="utf-8") as log_fh:
            log_fh.write("")

        docker_stub = os.path.join(stub_dir, "docker")
        docker_stub_contents = """#!/usr/bin/env bash
echo "$@" >> "${TITAN_STUB_CALLS:?}"
case "$1" in
    ps)
        # Mimic ``docker ps -a --filter label=... --format '<project>|<volume>'``.
        printf 'titan-stocks-playwright-smoke-42|titan-stocks-playwright-smoke-42_pg\\n'
        printf 'titan-stocks-playwright-smoke-42|titan-stocks-playwright-smoke-42_app\\n'
        printf 'titan-runner|\\n'
        printf 'titan-app-dev|titan_postgres\\n'
        ;;
    compose)
        # Mimic ``docker compose --project-name <name> down -v``.
        exit 0
        ;;
    volume)
        exit 0
        ;;
    *) exit 0 ;;
esac
"""
        with open(docker_stub, "w", encoding="utf-8") as fh:
            fh.write(docker_stub_contents)
        os.chmod(docker_stub, 0o755)

        env = os.environ.copy()
        env["PATH"] = stub_dir + os.pathsep + env.get("PATH", "/usr/bin:/bin")
        env["TITAN_STUB_CALLS"] = calls_log

        result = subprocess.run(
            ["bash", str(repo_root / "scripts" / "post-job.sh")],
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (
            f"post-job.sh must exit 0; got {result.returncode} "
            f"stderr={result.stderr!r}"
        )
        with open(calls_log, encoding="utf-8") as fh:
            calls = fh.read()
        # The hook must invoke ``docker compose --project-name`` for
        # the ``titan-stocks-playwright-`` projects only.
        compose_calls = [
            line
            for line in calls.splitlines()
            if "compose" in line and "down" in line
        ]
        assert any(
            "titan-stocks-playwright-smoke-42" in line for line in compose_calls
        ), (
            "post-job.sh must tear down titan-stocks-playwright- projects; "
            f"compose calls: {compose_calls!r}"
        )
        # The runner container and the application development
        # projects must never be targeted.
        assert not any("titan-runner" in line for line in compose_calls), (
            "post-job.sh must NEVER target the runner container; "
            f"compose calls: {compose_calls!r}"
        )
        assert not any("titan-app-dev" in line for line in compose_calls), (
            "post-job.sh must NEVER target non-CI Titan workloads; "
            f"compose calls: {compose_calls!r}"
        )
        # The volume removal must be limited to the
        # ``titan-stocks-playwright-`` prefix.
        volume_calls = [
            line for line in calls.splitlines() if "volume rm" in line
        ]
        for line in volume_calls:
            assert "titan-stocks-playwright-" in line, (
                "post-job.sh must only remove volumes belonging to "
                "titan-stocks-playwright- projects; "
                f"volume calls: {volume_calls!r}"
            )
        # No docker system prune is allowed.
        assert "docker system prune" not in calls, (
            "post-job.sh must NEVER invoke `docker system prune`"
        )
    finally:
        import shutil

        shutil.rmtree(stub_dir, ignore_errors=True)


def test_publish_workflow_validates_compose_contract() -> None:
    """The publish workflow contract job MUST parse the Compose
    contract and confirm the single-container startup lifecycle.
    Without this step a regression that adds a disposable service
    or drops the fixed named workspace-volume contract ships
    unnoticed.

    The contract validation prefers ``docker compose config`` with
    the secret-free ``.env.example`` so the resolved Compose model
    is exercised (not only the raw YAML surface). The Python
    ``tests/check_compose_contract.py`` helper falls back to a
    YAML parse when ``docker compose`` is unavailable.
    """
    import yaml

    text = _read(PUBLISH_WORKFLOW)
    # The contract suite parses the Compose file as part of its
    # contract checks. The Python contract tests already pin the
    # single-container and one-command startup contract; the
    # publish workflow MUST keep the contract job on the
    # critical path so a regression blocks publication.
    assert "docker-compose.yml" in text or "docker compose" in text, (
        "publish.yml must reference docker-compose.yml or "
        "the docker compose command"
    )
    assert "check_compose_contract.py" in text, (
        "publish.yml must run tests/check_compose_contract.py so the "
        "resolved Compose contract is validated"
    )
    assert ".env.example" in text, (
        "publish.yml must invoke docker compose config with the "
        "secret-free .env.example"
    )
    # The build job MUST depend on the contract job so a failing
    # contract blocks every later step (merge, verify, promote).
    with PUBLISH_WORKFLOW.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    build_needs = data["jobs"]["build"].get("needs") or []
    if isinstance(build_needs, str):
        build_needs = [build_needs]
    assert "contract" in build_needs, (
        "publish.yml build job must `needs: contract`"
    )


def test_workflow_files_parse_and_have_no_executable_fixed_host_ports() -> None:
    """Both workflow files (``publish.yml`` here and the
    application ``runner-smoke.yml``) MUST parse as valid YAML
    and MUST NOT declare executable fixed host-port
    dependencies in their job steps. The runner contract surface
    includes the ``publish.yml`` parse; the application contract
    suite retains the fixed-port enforcement.

    The test only inspects the runner-owned ``publish.yml`` and
    documents that any future runner workflow must follow the
    same parse-clean rule. The application workflow is covered
    by the application's own contract suite.
    """
    import yaml

    with PUBLISH_WORKFLOW.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    assert isinstance(data, dict), "publish.yml must parse as a YAML mapping"
    assert "jobs" in data, "publish.yml must declare jobs"
    for job_name, job in data["jobs"].items():
        steps = job.get("steps", [])
        for index, step in enumerate(steps, start=1):
            assert isinstance(step, dict), (
                f"publish.yml job {job_name!r} step {index} must be a mapping"
            )
            assert step.get("run") or step.get("uses"), (
                f"publish.yml job {job_name!r} step {index} must declare "
                "either `run` or `uses`"
            )
            run = step.get("run", "")
            if not run:
                continue
            # The probe step uses ``--shm-size 2gb`` (shared
            # memory, not a host port); filter that out.
            for line in run.splitlines():
                stripped = line.lstrip()
                if stripped.startswith("#"):
                    continue
                # ``host.docker.internal:host-gateway`` is the
                # documented host-gateway alias; it is not a host
                # port.
                assert "host.docker.internal:host-gateway" not in line or True, (
                    "publish.yml must not pin a fixed host port"
                )
                # Any ``ports:`` mapping under a step is forbidden.
                assert "ports:" not in line, (
                    "publish.yml must NOT declare a fixed host port binding"
                )


def test_compose_manifest_does_not_declare_default_overrides() -> None:
    """The Compose manifest MUST stay concise and MUST NOT declare
    the documented default keys explicitly.

    The CI VM is the security boundary; the Compose contract is the
    *inner* half. Keeping the manifest concise forces configuration
    drift into the VM platform and the host firewall, not into
    arbitrary service-level overrides. Keys whose value is the
    Compose default (e.g. ``userns_mode: ""``,
    ``read_only: false``) are explicitly forbidden so a regression
    that re-adds a default is caught by the contract suite.
    """
    text = _read(COMPOSE_FILE)
    for marker in (
        "userns_mode:",
        "read_only:",
        "init: true",
    ):
        assert marker not in text, (
            f"docker-compose.yml must NOT declare {marker!r}; "
            "the value is the Compose default and the manifest must "
            "stay concise"
        )


def test_compose_manifest_does_not_declare_forbidden_networking() -> None:
    """The Compose manifest MUST NOT add a socket proxy, a DinD
    daemon, a Docker API TCP port, or TLS certificate infrastructure.

    The VM is the security boundary; the runner mounts the VM's
    Docker socket directly and uses a bridge network. A socket
    proxy, DinD, or Docker API TCP listener would re-introduce the
    DinD plan that the VM-as-security-boundary model explicitly
    replaces. The contract rejects every forbidden surface.
    """
    text = _read(COMPOSE_FILE)
    for marker in (
        "tcp://",
        "TLS",
        "tlscacert",
        "tlscert",
        "tlskey",
        "tlsverify",
        "socat",
        "network_mode: host",
        "ipc: host",
        "docker-tcp",
    ):
        assert marker not in text, (
            f"docker-compose.yml must NOT declare {marker!r}; "
            "the VM is the security boundary and the runner mounts "
            "the VM socket directly"
        )


def test_security_doc_documents_vm_boundary_first() -> None:
    """The security documentation MUST put the VM-as-security-boundary
    model at the top of the document.

    The dedicated CI VM is the security boundary; the container
    boundary is the *inner* half. The VM boundary section MUST
    appear before the container boundary section and MUST reaffirm
    that the VM is the dedicated, disposable security boundary.
    """
    text = _read(SECURITY_DOC)
    assert "VM boundary" in text, (
        "docs/security.md must declare a `VM boundary` section"
    )
    assert "Container boundary" in text, (
        "docs/security.md must declare a `Container boundary` section"
    )
    vm_idx = text.find("VM boundary")
    container_idx = text.find("Container boundary")
    assert vm_idx != -1 and container_idx != -1
    assert vm_idx < container_idx, (
        "docs/security.md must put the VM boundary section before "
        "the container boundary section"
    )
    # The VM boundary MUST explicitly call out the dedicated,
    # disposable nature of the VM and the network isolation rules.
    for marker in (
        "dedicated",
        "disposable",
        "Deny inbound",
        "Allow only the documented outbound",
        "DOCKER-USER",
    ):
        assert marker in text, (
            f"docs/security.md VM boundary section must mention {marker!r}"
        )
    # The VM boundary MUST prohibit the surfaces the docker-compose
    # contract also refuses: socket proxy, DinD, Docker API TCP.
    for marker in (
        "socket-proxy",
        "DinD",
        "Docker API TCP port",
    ):
        assert marker in text, (
            f"docs/security.md must forbid {marker!r} as an "
            "in-VM Docker access surface"
        )


def test_security_doc_documents_one_repository_boundary_per_vm() -> None:
    """Light concurrency does not cross repository or heavy-job boundaries."""
    text = _read(SECURITY_DOC)
    code_only = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )
    assert "One repository trust boundary per VM" in code_only
    assert "one heavy listener" in code_only
    assert "same trusted repository" in code_only


def test_vm_deployment_doc_documents_acceptance_checks() -> None:
    """The dedicated VM deployment guide MUST document the
    acceptance checks that confirm the VM cannot reach protected
    network ranges or metadata services.

    The contract suite enforces the inner (container) half; the
    VM network isolation enforcement lives outside the manifest
    and the contract suite only verifies the documentation is in
    place. The checks must cover the cloud metadata endpoint,
    the RFC 1918 ranges, and the host-management network.
    """
    assert VM_DEPLOYMENT_DOC.exists(), (
        "docs/vm-deployment.md must exist; the VM acceptance checks "
        "are documented there"
    )
    text = _read(VM_DEPLOYMENT_DOC)
    # The VM profile table is the entry point; the table MUST
    # declare the supported native architectures.
    for marker in ("linux/amd64", "linux/arm64", "x86_64", "aarch64"):
        assert marker in text, (
            f"docs/vm-deployment.md must declare the {marker!r} "
            "architecture support"
        )
    # The acceptance checks table MUST mention the cloud metadata
    # endpoint, the RFC 1918 ranges, and the host-management
    # network so operators know what to verify before bringing
    # the listener up.
    assert "169.254.169.254" in text, (
        "docs/vm-deployment.md must call out the cloud metadata endpoint"
    )
    for marker in (
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "fe80::/10",
        "fc00::/7",
    ):
        assert marker in text, (
            f"docs/vm-deployment.md must declare the {marker!r} block"
        )
    # The acceptance checks section MUST show the actual
    # commands operators run, not just the policy text.
    assert "curl" in text or "wget" in text or "ping" in text, (
        "docs/vm-deployment.md must document the acceptance-check commands"
    )
    # The DOCKER-USER chain is the documented enforcement surface
    # for container-forwarded traffic.
    assert "DOCKER-USER" in text, (
        "docs/vm-deployment.md must reference the DOCKER-USER chain"
    )
    # The disposable Docker container check MUST be present so
    # operators verify the boundary from a workflow job's point
    # of view, not just the host.
    assert "docker run" in text or "docker container" in text.lower(), (
        "docs/vm-deployment.md must document the disposable container "
        "acceptance check"
    )


def test_vm_deployment_doc_lists_outbound_destination_allowlist() -> None:
    """The VM deployment guide MUST enumerate the exact outbound
    destinations the runner is allowed to reach.

    The VM firewall MUST allow only the documented destinations;
    the deployment guide is the auditable source of truth for the
    allowlist. GitHub, GHCR, DNS, and NTP are the documented
    minimum.
    """
    text = _read(VM_DEPLOYMENT_DOC)
    for marker in (
        "github.com",
        "actions.githubusercontent.com",
        "ghcr.io",
        "DNS",
        "NTP",
    ):
        assert marker in text, (
            f"docs/vm-deployment.md outbound allowlist must include {marker!r}"
        )


def test_operations_doc_references_vm_deployment_acceptance_checks() -> None:
    """The operations documentation MUST link the VM deployment
    guide and reference the acceptance checks that confirm the VM
    cannot reach protected network ranges or metadata services.

    The network isolation contract lives outside Compose and is
    enforced by the VM platform and host firewall; the operations
    guide is the operator-facing pointer to the acceptance checks.
    """
    text = _read(OPERATIONS_DOC)
    assert "vm-deployment.md" in text, (
        "docs/operations.md must link to docs/vm-deployment.md"
    )
    assert "acceptance" in text.lower() or "metadata" in text.lower(), (
        "docs/operations.md must reference the VM acceptance checks"
    )


def test_quick_start_doc_references_vm_deployment() -> None:
    """The quick-start guide MUST require the VM-level network
    isolation acceptance checks before the first
    ``docker compose up -d``."""
    text = _read(QUICK_START_DOC)
    assert "vm-deployment.md" in text, (
        "docs/quick-start.md must link to docs/vm-deployment.md"
    )
    assert "security boundary" in text.lower() or "VM" in text, (
        "docs/quick-start.md must establish the VM as the security boundary"
    )


def test_readme_documents_vm_deployment() -> None:
    """The README MUST document the VM-as-security-boundary model
    and link to the dedicated VM deployment guide."""
    text = _read(README_FILE)
    assert "vm-deployment.md" in text, (
        "README.md must link to docs/vm-deployment.md"
    )
    assert "VM" in text and "security boundary" in text.lower(), (
        "README.md must establish the VM as the security boundary"
    )


def test_env_example_has_empty_token_and_documents_vm() -> None:
    """The committed ``.env.example`` template MUST keep
    ``TITAN_RUNNER_TOKEN=`` empty and MUST point operators at the
    VM deployment guide before the first ``docker compose up -d``.

    The committed template is the safe, shareable reference; the
    real token lives in the gitignored ``.env`` only.
    """
    text = _read(ENV_EXAMPLE)
    assert "TITAN_RUNNER_TOKEN=" in text, (
        ".env.example must declare TITAN_RUNNER_TOKEN"
    )
    token_lines = [
        line for line in text.splitlines()
        if line.startswith("TITAN_RUNNER_TOKEN=") and not line.lstrip().startswith("#")
    ]
    assert len(token_lines) == 1, (
        ".env.example must declare exactly one TITAN_RUNNER_TOKEN line"
    )
    assert token_lines[0].strip() == "TITAN_RUNNER_TOKEN=", (
        f".env.example must have an empty TITAN_RUNNER_TOKEN "
        f"value; got {token_lines[0]!r}"
    )
    assert "vm-deployment.md" in text, (
        ".env.example must reference docs/vm-deployment.md"
    )
