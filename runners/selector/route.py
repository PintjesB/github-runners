"""Select the GitHub Actions runner from an independent management host."""

import argparse
import json
import os
import re
import sys
import time
from http.client import HTTPException
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen

HOSTED = "ubuntu-24.04"
SELF_HOSTED = "compose-ci"
VARIABLE = "/actions/variables/COMPOSE_CI_RUNNER"


def route(payload, runner_name):
    matching = [runner for runner in payload["runners"] if runner["name"] == runner_name]
    if len(matching) == 1:
        runner = matching[0]
        labels = {label["name"] for label in runner["labels"]}
        if runner["status"] == "online" and runner_name in labels:
            return runner_name
    return HOSTED


def reconcile(api, *, runner_name=SELF_HOSTED, dry_run=False):
    probe_error = None
    try:
        runners_path = f"/actions/runners?name={quote(runner_name, safe='')}&per_page=100"
        desired = route(api("GET", runners_path), runner_name)
    except (HTTPException, OSError, ValueError, KeyError, TypeError) as error:
        print("Runner status could not be verified; selecting GitHub-hosted.", file=sys.stderr)
        probe_error = error
        desired = HOSTED

    if not dry_run:
        current = api("GET", VARIABLE)["value"]
        if current != desired:
            api("PATCH", VARIABLE, {"value": desired})
            print(f"CI runner changed to {desired}.")
    if probe_error is not None:
        raise probe_error
    return desired


def request_api(repository, token, method, path, data=None):
    request = Request(
        f"https://api.github.com/repos/{repository}{path}",
        data=json.dumps(data).encode() if data is not None else None,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "github-runner-selector",
        },
        method=method,
    )
    with urlopen(request, timeout=8) as response:
        if response.status == 204:
            return None
        return json.load(response)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Check the runner without changing GitHub.")
    parser.add_argument("--watch", action="store_true", help="Reconcile every 30 seconds.")
    args = parser.parse_args()
    if args.watch and args.dry_run:
        parser.error("--watch and --dry-run cannot be combined")
    try:
        repository = os.environ["RUNNER_SELECTOR_REPOSITORY"]
        if not re.fullmatch(r"[A-Za-z0-9-]+/(?!\.{1,2}$)[A-Za-z0-9_.-]+", repository):
            raise ValueError("RUNNER_SELECTOR_REPOSITORY must be OWNER/REPO.")
        runner_name = os.environ.get("RUNNER_SELECTOR_NAME", SELF_HOSTED)
        if not runner_name.strip():
            raise ValueError("RUNNER_SELECTOR_NAME cannot be empty.")
        token_file = Path(os.environ.get("RUNNER_SELECTOR_TOKEN_FILE", "/run/secrets/github-token"))
        token = token_file.read_text(encoding="utf-8").strip()
        if not token:
            raise ValueError("Runner selector token is empty.")
    except (OSError, ValueError, KeyError) as error:
        print(f"Cannot configure runner selector: {error}", file=sys.stderr)
        return 1

    while True:
        try:
            selected = reconcile(
                lambda method, path, data=None: request_api(repository, token, method, path, data),
                runner_name=runner_name,
                dry_run=args.dry_run,
            )
        except (HTTPException, OSError, ValueError, KeyError, TypeError) as error:
            print(f"CI runner selection failed: {error}", file=sys.stderr)
            if not args.watch:
                return 1
        else:
            if args.dry_run:
                print(f"CI would select {selected}.")
            if not args.watch:
                return 0
        time.sleep(30)


if __name__ == "__main__":
    sys.exit(main())
