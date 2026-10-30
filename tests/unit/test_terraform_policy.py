"""Static policy checks over the Terraform code.

NOT a plan or an apply: no cloud provider could be reached from the build environment, so
`terraform validate/plan` have never run. What does run: `tofu/terraform fmt -check` (which also
proves the HCL parses; skipped when no binary is installed) and the text-level security policy
below, which fails on the regressions that matter most (public data stores, unencrypted storage,
wide-open ingress, embedded credentials).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TF = ROOT / "infrastructure" / "terraform"
FILES = sorted(TF.rglob("*.tf"))
ENVS = ["staging", "production"]


def _read(rel: str) -> str:
    return (TF / rel).read_text()


def _all() -> str:
    return "\n".join(f.read_text() for f in FILES)


def _binary() -> str | None:
    return os.environ.get("TOFU_BIN") or shutil.which("tofu") or shutil.which("terraform")


def test_terraform_files_are_formatted_and_parse() -> None:
    binary = _binary()
    if binary is None:
        pytest.skip("neither tofu nor terraform installed")
    out = subprocess.run(  # noqa: S603  # nosec B603
        [binary, "fmt", "-check", "-recursive", str(TF)], capture_output=True, text=True
    )
    assert out.returncode == 0, out.stdout + out.stderr


def test_no_credentials_or_account_ids_in_code() -> None:
    text = _all()
    assert not re.search(r"AKIA[0-9A-Z]{16}", text)
    assert not re.search(r"(?i)(secret_key|access_key|password)\s*=\s*\"[^\"$]+\"", text)
    assert not re.search(r"\b\d{12}\b", text), "hard-coded AWS account id"


def test_every_module_and_env_pins_terraform_and_providers() -> None:
    for f in FILES:
        if f.name in {"main.tf", "versions.tf"} and "required_version" not in f.read_text():
            # env main.tf files rely on versions.tf; modules declare their own
            assert f.parent.parent.name == "envs", f
    for env in ENVS:
        v = _read(f"envs/{env}/versions.tf")
        assert 'required_version = ">= 1.6.0"' in v
        assert re.search(r'version\s*=\s*"~> \d+\.\d+"', v)
        assert 'backend "s3"' in v and "encrypt = true" in v
        assert not re.search(r'bucket\s*=\s*"', v), "backend bucket must come from backend.hcl"


def test_data_stores_are_private_encrypted_and_tls_only() -> None:
    d = _read("modules/data/main.tf")
    assert "publicly_accessible    = false" in d
    assert "storage_encrypted     = true" in d
    assert "manage_master_user_password   = true" in d  # no DB password in state
    assert '"rds.force_ssl"' in d
    assert "at_rest_encryption_enabled = true" in d
    assert "transit_encryption_enabled = true" in d
    assert 'client_broker = "TLS"' in d and "unauthenticated = false" in d
    assert "auto.create.topics.enable=false" in d
    assert "unclean.leader.election.enable=false" in d


def test_data_security_groups_only_admit_the_client_group() -> None:
    d = _read("modules/data/main.tf")
    rules = re.findall(r'resource "aws_vpc_security_group_ingress_rule" "\w+" \{(.*?)\n\}', d, re.S)
    assert len(rules) == 3
    for r in rules:
        assert "referenced_security_group_id = var.client_security_group_id" in r
        assert "cidr_ipv4" not in r and "cidr_ipv6" not in r


def test_no_ingress_from_the_internet_anywhere() -> None:
    for f in FILES:
        for block in re.findall(
            r'resource "aws_(?:vpc_security_group_ingress_rule|security_group)".*?\n\}',
            f.read_text(),
            re.S,
        ):
            assert "0.0.0.0/0" not in block or "egress" in block, f


def test_bucket_is_private_versioned_and_kms_encrypted() -> None:
    s = _read("modules/storage/main.tf")
    for flag in (
        "block_public_acls       = true",
        "block_public_policy     = true",
        "ignore_public_acls      = true",
        "restrict_public_buckets = true",
    ):
        assert flag in s
    assert 'status = "Enabled"' in s
    assert 'sse_algorithm     = "aws:kms"' in s
    assert "aws:SecureTransport" in s
    assert "enable_key_rotation     = true" in s


def test_eks_posture() -> None:
    e = _read("modules/eks/main.tf")
    assert 'resources = ["secrets"]' in e  # envelope-encrypted Kubernetes secrets
    assert 'http_tokens                 = "required"' in e  # IMDSv2
    assert "http_put_response_hop_limit = 1" in e
    assert "encrypted   = true" in e
    assert "endpoint_private_access = true" in e
    assert '"audit"' in e
    # IRSA trust is pinned to one namespace/service account
    assert ":sub" in e and "system:serviceaccount:" in e


def test_production_is_hardened_and_staging_is_the_cheap_one() -> None:
    prod = _read("envs/production/main.tf")
    stg = _read("envs/staging/main.tf")
    assert "deletion_protection      = true" in prod and "force_destroy = false" in prod
    assert "db_multi_az              = true" in prod and "single_nat_gateway = false" in prod
    assert "az_count           = 3" in prod
    assert "deletion_protection         = false" in stg and "force_destroy = true" in stg


def test_public_k8s_api_cannot_be_opened_to_the_world() -> None:
    for env in ENVS:
        assert "!contains(var.public_api_cidrs" in _read(f"envs/{env}/variables.tf")
    assert "default     = []" in _read("modules/eks/main.tf")


@pytest.mark.parametrize("env", ENVS)
def test_env_examples_hold_only_placeholders(env: str) -> None:
    for name in ("backend.hcl.example", "terraform.tfvars.example"):
        text = _read(f"envs/{env}/{name}")
        assert "REPLACE" in text
    assert not list((TF / "envs" / env).glob("*.tfvars"))  # real tfvars/backend.hcl stay local
