#!/usr/bin/env python3
"""Plan an atomic version + both architecture SHA256 update for general-runner.

The GitHub release API publishes immutable SHA256 digests per uploaded asset.
Never update the runner version without BOTH matching Linux tarball digests.
"""

import argparse
import json
import os
from pathlib import Path
import re
import urllib.request

DEFAULT_DOCKERFILE = Path(__file__).resolve().parents[1] / "Dockerfile"
RELEASE_URL = "https://api.github.com/repos/actions/runner/releases/latest"
ARGUMENTS = {
    "RUNNER_VERSION": re.compile(r"(?m)^ARG RUNNER_VERSION=(\d+\.\d+\.\d+)$"),
    "RUNNER_SHA256_ARM64": re.compile(r"(?m)^ARG RUNNER_SHA256_ARM64=([a-f0-9]{64})$"),
    "RUNNER_SHA256_X64": re.compile(r"(?m)^ARG RUNNER_SHA256_X64=([a-f0-9]{64})$"),
}


def pins_from_dockerfile(content):
    pins = {}
    for name, pattern in ARGUMENTS.items():
        matches = pattern.findall(content)
        if len(matches) != 1:
            raise ValueError(f"Expected exactly one pinned {name} Dockerfile ARG")
        pins[name] = matches[0]
    return pins


def plan(content, release):
    old = pins_from_dockerfile(content)
    tag = release.get("tag_name", "")
    if release.get("draft") or release.get("prerelease"):
        raise ValueError("Cannot use draft or prerelease GitHub runner releases")
    if not re.fullmatch(r"v\d+\.\d+\.\d+", tag):
        raise ValueError("Invalid upstream actions/runner release tag")
    version = tag[1:]
    incoming, current = tuple(map(int, version.split("."))), tuple(
        map(int, old["RUNNER_VERSION"].split("."))
    )
    if incoming <= current:
        return content, None
    if incoming[0] != current[0]:
        raise ValueError("Runner major version change requires separate approval")
    assets = release.get("assets", [])
    updates = {"RUNNER_VERSION": version}
    for arch, key in (("arm64", "RUNNER_SHA256_ARM64"), ("x64", "RUNNER_SHA256_X64")):
        expected_name = f"actions-runner-linux-{arch}-{version}.tar.gz"
        matches = [asset for asset in assets if asset.get("name") == expected_name]
        if len(matches) != 1:
            raise ValueError(f"Missing or duplicated official release asset {expected_name}")
        digest = matches[0].get("digest", "")
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", digest):
            raise ValueError(f"Invalid official SHA256 for {expected_name}")
        updates[key] = digest.split(":", 1)[1]
    for name, value in updates.items():
        content, count = ARGUMENTS[name].subn(f"ARG {name}={value}", content)
        if count != 1:
            raise ValueError(f"Could not replace {name}")
    assert pins_from_dockerfile(content) == updates
    return content, version


def latest_release():
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "innerflows-runner-updater"}
    token = os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(RELEASE_URL, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true", help="Replace all three pins if newer")
    parser.add_argument("--release-file", type=Path, help="Use offline JSON release fixture")
    parser.add_argument("--dockerfile", type=Path, default=DEFAULT_DOCKERFILE)
    args = parser.parse_args()
    release = json.loads(args.release_file.read_text()) if args.release_file else latest_release()
    original = args.dockerfile.read_text()
    modified, version = plan(original, release)
    if version and args.write:
        args.dockerfile.write_text(modified)
    if version:
        print(f"New supported actions/runner release: {version}")
    else:
        print("The current general-runner version is up to date")
    if path := os.getenv("GITHUB_OUTPUT"):
        with open(path, "a", encoding="utf-8") as out:
            out.write(f"updated={'true' if version else 'false'}\n")
            out.write(f"version={version or ''}\n")


if __name__ == "__main__":
    main()
