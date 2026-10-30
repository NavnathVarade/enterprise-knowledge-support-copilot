"""Production-readiness review: static, deterministic checks over the repository.

Every check reports PASS, FAIL or SKIPPED with the evidence it looked at. It does not run
the application or touch any environment, so a PASS means "the repository is configured this
way", never "production behaves this way". Exit status is 1 if any check FAILS.

Usage: python scripts/readiness_review.py [--json PATH] [--kustomize PATH]
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess  # nosec B404
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKLOADS = ("Deployment", "StatefulSet", "Job", "CronJob")


@dataclass
class Check:
    name: str
    status: str  # PASS | FAIL | SKIPPED
    detail: str


def _ok(name: str, detail: str) -> Check:
    return Check(name, "PASS", detail)


def _fail(name: str, problems: list[str]) -> Check:
    return Check(name, "FAIL", "; ".join(problems[:10]) + (" ..." if len(problems) > 10 else ""))


def _pod_spec(doc: dict[str, Any]) -> dict[str, Any]:
    spec = doc["spec"]
    if doc["kind"] == "CronJob":
        spec = spec["jobTemplate"]["spec"]
    result: dict[str, Any] = spec["template"]["spec"]
    return result


def render(overlay: str, kustomize: str) -> list[dict[str, Any]]:
    out = subprocess.run(  # noqa: S603  # nosec B603
        [kustomize, "build", str(ROOT / "infrastructure/kubernetes/overlays" / overlay)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [d for d in yaml.safe_load_all(out) if d]


def check_manifests(overlay: str, docs: list[dict[str, Any]]) -> list[Check]:
    prefix = f"k8s[{overlay}]"
    problems: dict[str, list[str]] = {
        k: [] for k in ("sec", "res", "img", "probe", "pdb", "secret")
    }
    deployments = [d for d in docs if d["kind"] == "Deployment"]
    pdb_targets = {
        json.dumps(d["spec"]["selector"].get("matchLabels", {}), sort_keys=True)
        for d in docs
        if d["kind"] == "PodDisruptionBudget"
    }
    for d in docs:
        if d["kind"] == "Secret" and (d.get("data") or d.get("stringData")):
            problems["secret"].append(f"Secret {d['metadata']['name']} carries data")
        if d["kind"] not in WORKLOADS:
            continue
        name = f"{d['kind']}/{d['metadata']['name']}"
        pod = _pod_spec(d)
        if not pod.get("securityContext", {}).get("runAsNonRoot"):
            problems["sec"].append(f"{name}: pod runAsNonRoot not set")
        for c in [*pod.get("containers", []), *pod.get("initContainers", [])]:
            sc = c.get("securityContext", {})
            if sc.get("allowPrivilegeEscalation") is not False:
                problems["sec"].append(f"{name}/{c['name']}: privilege escalation allowed")
            if not sc.get("readOnlyRootFilesystem"):
                problems["sec"].append(f"{name}/{c['name']}: writable root filesystem")
            if "ALL" not in sc.get("capabilities", {}).get("drop", []):
                problems["sec"].append(f"{name}/{c['name']}: capabilities not dropped")
            res = c.get("resources", {})
            if not (res.get("requests", {}).get("cpu") and res.get("requests", {}).get("memory")):
                problems["res"].append(f"{name}/{c['name']}: missing requests")
            if not res.get("limits", {}).get("memory"):
                problems["res"].append(f"{name}/{c['name']}: missing memory limit")
            image = c["image"]
            if (
                ":" not in image.rsplit("/", 1)[-1]
                and "@" not in image
                or image.endswith(":latest")
            ):
                problems["img"].append(f"{name}/{c['name']}: unpinned image {image}")
    for d in deployments:
        pod = _pod_spec(d)
        for c in pod["containers"]:
            for probe in ("livenessProbe", "readinessProbe"):
                if probe not in c:
                    problems["probe"].append(f"Deployment/{d['metadata']['name']}: no {probe}")
        labels = d["spec"]["selector"].get("matchLabels", {})
        if not any(
            set(json.loads(t).items()) <= set(labels.items())
            or set(labels.items()) <= set(json.loads(t).items())
            for t in pdb_targets
        ):
            problems["pdb"].append(f"Deployment/{d['metadata']['name']}: no PodDisruptionBudget")
    has_default_deny = any(
        d["kind"] == "NetworkPolicy"
        and d["spec"].get("podSelector") == {}
        and set(d["spec"].get("policyTypes", [])) >= {"Ingress", "Egress"}
        for d in docs
    )
    checks = []
    labels_ = {
        "sec": "pods are non-root, read-only rootfs, no privilege escalation, all caps dropped",
        "res": "every container has requests and a memory limit",
        "img": "images are pinned (tag or digest, never :latest)",
        "probe": "every Deployment container has liveness and readiness probes",
        "pdb": "every Deployment is covered by a PodDisruptionBudget",
        "secret": "no Secret object with data is committed in the manifests",  # nosec B105
    }
    for key, desc in labels_.items():
        checks.append(
            _ok(f"{prefix} {desc}", f"{len(deployments)} deployments inspected")
            if not problems[key]
            else _fail(f"{prefix} {desc}", problems[key])
        )
    checks.append(
        _ok(f"{prefix} default-deny NetworkPolicy (ingress+egress)", "present")
        if has_default_deny
        else _fail(f"{prefix} default-deny NetworkPolicy (ingress+egress)", ["absent"])
    )
    return checks


def check_dockerfiles() -> list[Check]:
    problems = []
    files = sorted(ROOT.glob("**/Dockerfile"))
    files = [
        f
        for f in files
        if not {"node_modules", ".venv"} & set(f.parts)
        and "build" not in f.relative_to(ROOT).parts[:1]
    ]
    for f in files:
        text = f.read_text()
        users = re.findall(r"^USER\s+(\S+)", text, re.MULTILINE)
        if not users or users[-1] in ("0", "root"):
            problems.append(f"{f.relative_to(ROOT)}: final USER is root/unset")
        args = dict(re.findall(r"^ARG\s+(\w+)=(\S+)", text, re.MULTILINE))
        for ref in re.findall(r"^FROM\s+(\S+)", text, re.MULTILINE):
            m = re.fullmatch(r"\$\{(\w+)\}", ref)
            image = args.get(m.group(1), "") if m else ref
            if (
                ref in ("scratch",)
                or image.endswith(":latest")
                or (":" not in image and "@" not in image)
            ):
                problems.append(f"{f.relative_to(ROOT)}: unpinned base image {ref}")
    name = "docker: non-root final user and pinned base images"
    return [_fail(name, problems) if problems else _ok(name, f"{len(files)} Dockerfiles")]


def check_migrations() -> list[Check]:
    versions = ROOT / "migrations/versions"
    revs: dict[str, str | None] = {}
    problems = []
    for f in sorted(versions.glob("*.py")):
        text = f.read_text()
        rev = re.search(r'^revision\s*(?::\s*\w+\s*)?=\s*"([^"]+)"', text, re.MULTILINE)
        down = re.search(r'^down_revision\s*(?::[^=]+)?=\s*(None|"([^"]+)")', text, re.MULTILINE)
        if not rev or not down:
            problems.append(f"{f.name}: cannot parse revision ids")
            continue
        revs[rev.group(1)] = down.group(2)
        body = text.split("def downgrade", 1)[-1] if "def downgrade" in text else ""
        code = [
            ln.strip()
            for ln in body.splitlines()[1:]
            if ln.strip() and not ln.strip().startswith("#")
        ]
        if not code or code == ["pass"]:
            problems.append(f"{f.name}: downgrade() is empty")
    heads = set(revs) - {d for d in revs.values() if d}
    if len(heads) != 1:
        problems.append(f"expected one alembic head, found {sorted(heads)}")
    name = "migrations: single head, every revision has a real downgrade"
    return [
        _fail(name, problems)
        if problems
        else _ok(name, f"{len(revs)} revisions, head {sorted(heads)[0]}")
    ]


def check_alert_runbooks() -> list[Check]:
    problems = []
    rules = yaml.safe_load((ROOT / "monitoring/prometheus/alerts.yaml").read_text())
    n = 0
    for group in rules["groups"]:
        for rule in group["rules"]:
            n += 1
            ref = rule.get("annotations", {}).get("runbook", "")
            path, _, anchor = ref.partition("#")
            doc = ROOT / path
            if not ref or not doc.is_file():
                problems.append(f"{rule['alert']}: runbook missing ({ref or 'none'})")
                continue
            anchors = {
                re.sub(r"[^a-z0-9 -]", "", h.lower()).replace(" ", "-")
                for h in re.findall(r"^#+\s+(.+)$", doc.read_text(), re.MULTILINE)
            }
            if anchor and anchor not in anchors:
                problems.append(f"{rule['alert']}: no heading for #{anchor} in {path}")
    name = "observability: every alert links to a runbook heading that exists"
    return [_fail(name, problems) if problems else _ok(name, f"{n} alerts")]


def check_secrets_hygiene() -> list[Check]:
    problems = []
    gi = (ROOT / ".gitignore").read_text()
    for needle in (".env", "*.tfstate", "*.tfvars", "node_modules"):
        if needle not in gi:
            problems.append(f".gitignore lacks {needle}")
    for f in ROOT.glob("**/.env"):
        if not {"node_modules", ".venv"} & set(f.parts):
            problems.append(f"{f.relative_to(ROOT)} exists in the tree (must not be shipped)")
    name = "secrets: ignore rules present, no .env file in the tree"
    return [_fail(name, problems) if problems else _ok(name, "gitleaks is a separate CI gate")]


def open_risks() -> list[Check]:
    """Counts honest 'not run / not measured' markers so the report cannot hide them."""
    pat = re.compile(
        r"NOT (?:YET )?(?:MEASURED|RUN|EXECUTED)|never (?:run|built|executed)", re.IGNORECASE
    )
    per_file = {
        str(f.relative_to(ROOT)): len(pat.findall(f.read_text(errors="ignore")))
        for f in sorted((ROOT / "docs").rglob("*.md"))
    }
    total = sum(per_file.values())
    top = sorted(((n, f) for f, n in per_file.items() if n), reverse=True)[:5]
    return [
        Check(
            "open risks: unmeasured / never-executed markers across docs",
            "SKIPPED" if total else "PASS",
            f"{total} markers; most in " + ", ".join(f"{f} ({n})" for n, f in top),
        )
    ]


def run_all(kustomize: str | None) -> list[Check]:
    checks: list[Check] = []
    for overlay in ("staging", "production"):
        if kustomize:
            checks += check_manifests(overlay, render(overlay, kustomize))
        else:
            checks.append(
                Check(f"k8s[{overlay}] manifest checks", "SKIPPED", "kustomize not found")
            )
    checks += check_dockerfiles()
    checks += check_migrations()
    checks += check_alert_runbooks()
    checks += check_secrets_hygiene()
    checks += open_risks()
    return checks


def main(argv: list[str] | None = None, emit: Callable[[str], object] = print) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", type=Path)
    ap.add_argument("--kustomize", default=shutil.which("kustomize"))
    args = ap.parse_args(argv)
    checks = run_all(args.kustomize)
    for c in checks:
        emit(f"[{c.status:7}] {c.name} — {c.detail}")
    if args.json:
        args.json.write_text(json.dumps([asdict(c) for c in checks], indent=2) + "\n")
    return 1 if any(c.status == "FAIL" for c in checks) else 0


if __name__ == "__main__":
    sys.exit(main())
