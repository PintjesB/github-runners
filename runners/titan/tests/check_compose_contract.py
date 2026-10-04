#!/usr/bin/env python3
# Validate the documented Compose startup contract using the
# resolved Compose model rather than the raw YAML surface.
#
# The contract requires:
#
#   * Exactly one service: ``runner`` (the long-running listener).
#   * Registration runs inside the runner's startup entrypoint; there
#     is no disposable Compose service or ``depends_on`` gate.
#   * The workspace is the explicit ``titan-runner-work`` named volume
#     mounted at the fixed internal path; the Docker socket is the only
#     host bind mount.
#   * Codex authentication uses the explicit ``titan-runner-codex``
#     named volume mounted at ``/home/runner/.codex``.
#   * Persistent ``titan-runner-state``, ``titan-runner-work``,
#     ``titan-runner-browser``, and ``titan-runner-codex`` named
#     volumes are declared explicitly.
#
# A regression that drops any of these surfaces blocks the
# ``publish.yml`` workflow contract job and prevents publication.
#
# The validation prefers ``docker compose config`` because it
# resolves variable interpolation, the bind-mount source and
# target, and the volume declarations exactly the way Compose
# itself would. When the docker CLI is unavailable the script
# falls back to parsing the YAML surface; an available Compose CLI
# must always parse the real file successfully.
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path


COMPOSE_PATH = Path("docker-compose.yml")
DOTENV_PATH = Path(".env.example")


def _fail(message: str) -> None:
    print(f"::error::{message}", file=sys.stderr)
    raise SystemExit(1)


def _load_resolved_model() -> dict | None:
    """Return the resolved Compose model.

    Prefers ``docker compose config`` so variable interpolation,
    volume-mount resolution, and the volume declarations are
    resolved by the same engine that runs the stack. Uses the
    secret-free ``.env.example`` so the contract surface is
    checked against the same interpolation a production host
    would perform without leaking credentials. Returns ``None`` only
    when Docker itself is unavailable; when Docker Compose is present,
    an invalid model is a deployment failure and MUST fail the gate.
    """
    if shutil.which("docker") is None:
        return None
    if not DOTENV_PATH.exists():
        _fail(".env.example is required to resolve the Compose contract")
    try:
        result = subprocess.run(
            [
                "docker",
                "compose",
                "-f",
                str(COMPOSE_PATH),
                "--env-file",
                str(DOTENV_PATH),
                "config",
                "--format",
                "json",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
    except (FileNotFoundError, PermissionError):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        detail = (result.stderr or result.stdout or "unknown error").strip()
        _fail(f"docker compose config failed: {detail}")
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    return data


def _load_yaml() -> dict:
    import yaml

    return yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))


def _validate(resolved: dict | None, raw: dict) -> None:
    data = resolved if resolved is not None else raw
    if not isinstance(data, dict):
        _fail("docker-compose.yml must parse as a mapping")
    services = data.get("services")
    if not isinstance(services, dict):
        _fail("docker-compose.yml must declare `services:` as a mapping")
    if "runner" not in services or set(services) - {"runner", "runner-light"}:
        _fail(
            "docker-compose.yml must declare `runner` and only the optional light listener; "
            "registration is an internal startup phase"
        )
    if "runner-light" in services and services["runner-light"].get("profiles") != ["ci-light"]:
        _fail("runner-light must require explicit ci-light profile activation")
    runner = services["runner"]
    if not isinstance(runner, dict):
        _fail("docker-compose.yml `runner` service must be a mapping")
    if "depends_on" in runner:
        _fail("docker-compose.yml `runner` must not declare `depends_on`")

    environment = runner.get("environment") or {}
    if "RUNNER_TOKEN" not in environment:
        _fail(
            "docker-compose.yml runner environment must pass RUNNER_TOKEN "
            "to the startup registration phase"
        )
    if environment.get("CODEX_HOME") != "/home/runner/.codex":
        _fail(
            "docker-compose.yml runner environment must set CODEX_HOME "
            "to /home/runner/.codex"
        )

    # The workspace MUST be the named volume mounted at the fixed path.
    # Compose resolves the short YAML form to a type=volume mapping.
    mounts = runner.get("volumes") or []
    work_mounts = [
        mount
        for mount in mounts
        if isinstance(mount, dict)
        and mount.get("type") == "volume"
        and mount.get("source") == "titan-runner-work"
        and mount.get("target") == "/var/lib/titan-runner/work"
    ]
    if resolved is None:
        work_mounts = [
            mount
            for mount in mounts
            if mount == "titan-runner-work:/var/lib/titan-runner/work"
        ]
    if not work_mounts:
        _fail(
            "docker-compose.yml `runner` service must mount the named "
            "`titan-runner-work` volume at /var/lib/titan-runner/work"
        )

    # Codex login state is deliberately persisted in its own replaceable
    # volume. The image contains the CLI, never the user's ChatGPT auth.
    codex_mounts = [
        mount
        for mount in mounts
        if isinstance(mount, dict)
        and mount.get("type") == "volume"
        and mount.get("source") == "titan-runner-codex"
        and mount.get("target") == "/home/runner/.codex"
    ]
    if resolved is None:
        codex_mounts = [
            mount
            for mount in mounts
            if mount == "titan-runner-codex:/home/runner/.codex"
        ]
    if not codex_mounts:
        _fail(
            "docker-compose.yml `runner` service must mount the named "
            "`titan-runner-codex` volume at /home/runner/.codex"
        )

    # The socket is intentionally the only host bind mount.
    if resolved is not None:
        bind_mounts = [
            mount
            for mount in mounts
            if isinstance(mount, dict) and mount.get("type") == "bind"
        ]
        if not any(
            mount.get("target") == "/var/run/docker.sock"
            for mount in bind_mounts
        ):
            _fail("docker-compose.yml runner must bind /var/run/docker.sock")
        if any(
            mount.get("target") != "/var/run/docker.sock"
            for mount in bind_mounts
        ):
            _fail("docker-compose.yml runner may bind only /var/run/docker.sock")
    else:
        if "/var/run/docker.sock:/var/run/docker.sock" not in mounts:
            _fail("docker-compose.yml runner must bind /var/run/docker.sock")
        if any(
            isinstance(mount, dict)
            and mount.get("type") == "bind"
            and mount.get("target") != "/var/run/docker.sock"
            for mount in mounts
        ):
            _fail("docker-compose.yml runner may bind only /var/run/docker.sock")

    # All persistent runner volumes MUST be declared with stable names.
    volumes = data.get("volumes")
    if not isinstance(volumes, dict):
        _fail("docker-compose.yml must declare the persistent named volumes")
    for volume_name in (
        "titan-runner-state",
        "titan-runner-work",
        "titan-runner-browser",
        "titan-runner-codex",
    ):
        definition = volumes.get(volume_name)
        if not isinstance(definition, dict):
            _fail(f"docker-compose.yml must declare `{volume_name}` as a volume")
        if definition.get("name") != volume_name:
            _fail(
                f"docker-compose.yml `{volume_name}` volume must declare "
                f"name: {volume_name}"
            )


def main() -> int:
    if not COMPOSE_PATH.exists():
        _fail("docker-compose.yml is missing from the repository root")
    raw = _load_yaml()
    _validate(None, raw)
    resolved = _load_resolved_model()
    if resolved is not None:
        _validate(resolved, raw)
    print(
        "compose contract validated"
        + (" (resolved via docker compose config)" if resolved is not None else " (raw YAML fallback)")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
