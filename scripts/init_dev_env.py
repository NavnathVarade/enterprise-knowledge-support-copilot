"""
Create a local development `.env` from `.env.example` with a freshly generated JWT keypair.

    uv run python -m scripts.init_dev_env
    uv run python -m scripts.init_dev_env --database-password 'my local pw'

Safe by construction: it refuses to overwrite an existing `.env` (use --force), writes only
development settings, and the generated private key lives in `.env` alone (gitignored; the
secret scanner and readiness review fail the build if a `.env` is ever found in the tree).
The password is percent-encoded for you, so special characters cannot corrupt the URL.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from urllib.parse import quote

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

ROOT = Path(__file__).resolve().parent.parent
_KEY_PLACEHOLDER = '"__GENERATED_BY_INIT_DEV_ENV__"'
_PASSWORD_PLACEHOLDER = "__LOCAL_PASSWORD__"  # noqa: S105  # nosec B105 -- template token


def generate_keypair() -> tuple[str, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    public = (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    return public, private


def render_env(template: str, *, database_password: str | None) -> str:
    public, private = generate_keypair()
    out: list[str] = []
    for line in template.splitlines():
        if line.startswith("SECURITY_JWT_PUBLIC_KEY="):
            line = f'SECURITY_JWT_PUBLIC_KEY="{public.strip()}"'
        elif line.startswith("SECURITY_JWT_PRIVATE_KEY="):
            line = f'SECURITY_JWT_PRIVATE_KEY="{private.strip()}"'
        elif database_password is not None and _PASSWORD_PLACEHOLDER in line:
            line = line.replace(_PASSWORD_PLACEHOLDER, quote(database_password, safe=""))
        out.append(line)
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--output", type=Path, default=ROOT / ".env")
    parser.add_argument("--template", type=Path, default=ROOT / ".env.example")
    parser.add_argument(
        "--database-password",
        help="Password of the local `ekc_app` role. Omit to leave the placeholder to edit by hand.",
    )
    parser.add_argument("--force", action="store_true", help="Overwrite an existing file.")
    args = parser.parse_args(argv)

    if args.output.exists() and not args.force:
        print(f"{args.output} already exists; not touching it (use --force to replace).")
        return 1
    text = render_env(
        args.template.read_text(encoding="utf-8"), database_password=args.database_password
    )
    args.output.write_text(text, encoding="utf-8")
    print(f"Wrote {args.output} with a new JWT keypair.")
    if args.database_password is None:
        print(
            f"Edit DATABASE_URL: replace {_PASSWORD_PLACEHOLDER} with your local role's password."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
