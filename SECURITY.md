# Security policy

## Reporting a vulnerability
Do not open a public issue. Report privately to the maintainers using the contact channel the repository owner has configured for this project (set it before publishing: for example GitHub private vulnerability reporting). Include the affected version or commit, reproduction steps, and the impact you believe it has. **This repository does not publish a security contact address; the owner must add one.**

## What to expect
Acknowledge, reproduce, fix, and disclose on a timeline the owner commits to. No timeline is promised here because none has been agreed.

## Scope and known limits
The security model, controls and **documented gaps** (no token revocation/refresh/MFA/key rotation, no row-level security, in-process parsers, no document deletion API, audit chain not externally anchored, no independent penetration test) are in `docs/security/PHASE_44_SECURITY_AUDIT.md`. Reports about a documented gap are still welcome but are not new findings.

## Handling secrets
Never commit credentials. A leaked JWT signing key, database or provider credential must be rotated immediately; a leaked access token remains valid until expiry because revocation does not exist yet.
