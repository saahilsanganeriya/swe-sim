"""Deployment pin guard: keep `infra/image.auto.tfvars.json` (the digest a plain `make apply` rolls the workloads to) in
step with the digest the Cloud Run workloads actually run (handoff finding F4). Standard library only; `make` runs it
with the system python3.

Contract
- `check` (the default) compares the pin file with the image every `swesim-*` worker pool and the console currently
  run and exits 0 (apply may proceed) or 1 (apply is refused) with one `pin-guard: <code>: <message>` line:
    no-workloads  nothing is deployed yet (bootstrap)                                 -> 0
    unpinned      no pin file and the workloads run a tag, not a digest (bootstrap)   -> 0
    match         the pin equals the digest every workload runs                       -> 0
    pin-missing   no pin file but the workloads run a digest: apply would roll them to :latest -> 1 (`make pull-pin`)
    mismatch      the pin differs from the running digest (a stale laptop file after a CI deploy, or a new build) -> 1
    mixed         the workloads disagree with each other (a half-rolled stack)        -> 1
    unknown       gcloud could not list the workloads                                 -> 1
  `--force` (Makefile `FORCE_PIN=1`, set by `make deploy` and `make cloud-bootstrap` for the apply that follows their own
  `pin-image`) turns every refusal into a recorded `forced` pass. Refusals name the fix.
- `write --image <ref>` writes the pin file for a digest reference (`...@sha256:<64 hex>`); anything else is refused
  (exit 2) so a failed build can never pin a tag.
- Never prints a secret: only image references and workload names.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

DIGEST_RE = re.compile(r"@sha256:[0-9a-f]{64}$")
DEFAULT_PIN_FILE = Path("infra/image.auto.tfvars.json")
Runner = Callable[[list[str]], subprocess.CompletedProcess]

UNKNOWN = "<unknown>"


@dataclass(frozen=True)
class Decision:
    ok: bool
    code: str
    message: str

    def line(self) -> str:
        return f"pin-guard: {self.code}: {self.message}"


def read_pin(path: Path) -> str | None:
    """The pinned image reference, or None when there is no pin file. A malformed file is an error, not "no pin"."""
    if not path.is_file():
        return None
    doc = json.loads(path.read_text())
    image = doc.get("image") if isinstance(doc, dict) else None
    if not isinstance(image, str) or not image:
        raise ValueError(f"{path} has no \"image\" string")
    return image


def write_pin(path: Path, image: str) -> None:
    if not DIGEST_RE.search(image):
        raise ValueError(f"refusing to pin {image!r}: not a digest reference (...@sha256:<64 hex>)")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"image": image}) + "\n")


def _image_of(resource: Mapping) -> str:
    try:
        return resource["spec"]["template"]["spec"]["containers"][0]["image"] or UNKNOWN
    except (KeyError, IndexError, TypeError):
        return UNKNOWN


def _name_of(resource: Mapping) -> str:
    try:
        return resource["metadata"]["name"]
    except (KeyError, TypeError):
        return UNKNOWN


def parse_workloads(pools_json: str, services_json: str, *, console: str, prefix: str = "swesim-") -> dict[str, str]:
    """`gcloud run worker-pools list --format=json` + `gcloud run services list --format=json` -> {name: image}."""
    out: dict[str, str] = {}
    for item in json.loads(pools_json or "[]"):
        name = _name_of(item)
        if name.startswith(prefix):
            out[f"pool/{name}"] = _image_of(item)
    for item in json.loads(services_json or "[]"):
        if _name_of(item) == console:
            out[f"service/{console}"] = _image_of(item)
    return out


def running_images(project: str, region: str, *, console: str, run: Runner | None = None) -> dict[str, str] | None:
    """{workload: image} for the deployed pools and the console; None when gcloud could not answer (unknown, not empty)."""
    run = run or _run
    base = ["gcloud", "run"]
    tail = ["--project", project, "--region", region, "--format=json", "--quiet"]
    pools = run([*base, "worker-pools", "list", *tail])
    services = run([*base, "services", "list", *tail])
    if pools.returncode != 0 or services.returncode != 0:
        return None
    try:
        return parse_workloads(pools.stdout, services.stdout, console=console)
    except (json.JSONDecodeError, TypeError):
        return None


def decide(pin: str | None, running: Mapping[str, str] | None, *, force: bool = False, pin_file: Path = DEFAULT_PIN_FILE) -> Decision:
    """Pure: may `terraform apply` with this pin file proceed against these running workloads?"""
    def refuse(code: str, message: str) -> Decision:
        if force:
            return Decision(True, "forced", f"FORCE_PIN=1 acknowledged ({code}): {message}")
        return Decision(False, code, message)

    if running is None:
        return refuse("unknown", "gcloud could not list the Cloud Run workloads; fix the gcloud auth/project first")
    if not running:
        return Decision(True, "no-workloads", "no swesim worker pools or console are deployed yet; nothing to protect")
    images = sorted(set(running.values()))
    if len(images) > 1:
        detail = ", ".join(f"{name}={image}" for name, image in sorted(running.items()))
        return refuse("mixed", f"the workloads run different images ({detail}); finish or roll back that deploy before a plain apply")
    current = images[0]
    if current == UNKNOWN:
        return refuse("unknown", "gcloud listed the workloads without an image; re-run with a newer gcloud")
    if pin is None:
        if DIGEST_RE.search(current):
            return refuse("pin-missing", f"{pin_file} is absent but the workloads run {current}; a plain apply would roll them to :latest. "
                                         f"Run `make pull-pin` to adopt the deployed pin (or `make deploy` to build a new one)")
        return Decision(True, "unpinned", f"no pin file; the workloads run the tag {current} (bootstrap state)")
    if pin == current:
        return Decision(True, "match", f"{pin_file} pins the image the workloads run ({current})")
    return refuse("mismatch", f"{pin_file} pins {pin} but the workloads run {current}. `make pull-pin` adopts the deployed digest; "
                              f"`make deploy SHA=<sha>` builds and rolls a new one; FORCE_PIN=1 rolls to the pinned digest anyway")


def _run(args: list[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(args, capture_output=True, text=True, check=False)
    except FileNotFoundError:
        return subprocess.CompletedProcess(args=args, returncode=127, stdout="", stderr="gcloud not found")


def main(argv: list[str] | None = None, run: Runner | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pin-file", type=Path, default=DEFAULT_PIN_FILE)
    sub = parser.add_subparsers(dest="command")
    check = sub.add_parser("check", help="refuse an apply whose pin disagrees with the running workloads (default)")
    check.add_argument("--project", required=True)
    check.add_argument("--region", required=True)
    check.add_argument("--console", default="swesim-console")
    check.add_argument("--force", action="store_true", help="record the disagreement and allow the apply (FORCE_PIN=1)")
    write = sub.add_parser("write", help="write the pin file for a digest reference")
    write.add_argument("--image", required=True)
    args = parser.parse_args(argv)

    if args.command == "write":
        try:
            write_pin(args.pin_file, args.image)
        except ValueError as e:
            print(f"pin-guard: {e}", file=sys.stderr)
            return 2
        print(f"pin-guard: wrote {args.pin_file} = {args.image}")
        return 0
    if args.command != "check":
        parser.print_usage(sys.stderr)
        return 2
    try:
        pin = read_pin(args.pin_file)
    except (ValueError, json.JSONDecodeError) as e:
        print(f"pin-guard: malformed pin file: {e}", file=sys.stderr)
        return 2
    decision = decide(pin, running_images(args.project, args.region, console=args.console, run=run), force=args.force, pin_file=args.pin_file)
    print(decision.line(), file=sys.stdout if decision.ok else sys.stderr)
    return 0 if decision.ok else 1


if __name__ == "__main__":
    sys.exit(main())
