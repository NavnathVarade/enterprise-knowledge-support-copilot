# Disaster recovery (Phase 40)

Scope: losing data, a database, a zone, the cluster, a region, or trust in the data. For routine
availability (pod/node failure, dependency outages) see `docs/resilience/PHASE_34_RESILIENCE.md`.

## 1. What was actually verified

| Item | Status |
|---|---|
| `scripts/dr_backup.py` backup, restore and verification | **run for real** on PostgreSQL 16 + pgvector 0.8 |
| Restore drill on the seeded synthetic DB (`results/drill_ekc_load.json`) | **passed**: 23 tables, 1,077 rows, 956 audit events re-verified, dump 168 KiB, backup 0.51 s, restore 0.17 s. **These times say nothing about production size.** |
| Detection of: corrupt dump, wrong confirmation, non-empty target, extra rows, edited audit row, deleted audit row, non-scratch drill target | **tested** (`tests/integration/test_dr_backup.py`, 11 tests) |
| RDS point-in-time restore, Multi-AZ failover, snapshot restore | **NOT RUN** (no AWS access). Commands below are reviewed, not executed. |
| S3 version restore, Kafka rebuild, cluster rebuild from Terraform, region failover | **NOT RUN** |
| Measured RPO / RTO for any real failure | **NOT YET MEASURED** |

## 2. Targets (proposed; the business must ratify them)

These are targets to design against, **not measurements**. Where a number comes from AWS it is the
provider's documented behaviour, not something observed here.

| Failure | Target RPO | Target RTO | Mechanism | Status |
|---|---|---|---|---|
| Pod / node loss | 0 | minutes | Kubernetes, PDBs, anti-affinity | designed; chaos-tested only for the API |
| DB instance or AZ loss | ~0 | ~1-2 min (AWS Multi-AZ claim) | RDS Multi-AZ (production) | provisioned in Terraform, never applied |
| Bad migration / accidental delete / corruption | up to ~5 min (RDS PITR claim) | hours (restore + cutover) | RDS PITR to a NEW instance, 14-day retention | provisioned, never exercised |
| Tamper with audit history | n/a (detection) | n/a | hash chain, `verify-audit` | **tested** |
| S3 object deleted/overwritten | 0 | minutes | bucket versioning, 90-day noncurrent retention | provisioned, never exercised |
| Region loss | **not covered** | **not covered** | none | **gap** (section 6) |

## 3. What protects what

| Data | Source of truth? | Protection | Loss means |
|---|---|---|---|
| PostgreSQL (tenants, documents, chunks, embeddings, audit, usage, outbox) | **yes** | RDS automated backups + PITR (14 d prod, 7 d staging), Multi-AZ, deletion protection + final snapshot, logical dumps via `dr_backup` | everything; see runbooks |
| S3 documents bucket | yes (original files) | versioning, SSE-KMS, public access blocked, TLS-only policy | cannot re-ingest |
| Kafka (MSK) | **no**: transport only | 3x replication, `min.insync.replicas=2`, no unclean election | in-flight events; recoverable from the outbox (below) |
| Redis | **no**: login throttle, rate limits, breakers | replica in production | counters reset; protections fail open per design |
| Secrets Manager (RDS master, Redis token, Kafka SCRAM) | yes | KMS-encrypted; Terraform can regenerate Redis/Kafka credentials | rotate and re-sync |
| Terraform state | yes (infra) | encrypted/versioned bucket (you create it) | cannot manage infra |
| Embeddings / chunks | **derived**: re-computable from documents | n/a | cost + time to re-ingest; ledger shows spend |

## 4. Runbooks

Rule for all of them: **restore into a new instance/database, verify, then cut over.** Never restore
over the damaged one (you lose the evidence and your way back).

### R1. Bad migration, accidental delete, corruption (PITR)
1. Stop the damage: scale writers to zero (`kubectl -n ekc scale deploy/ekc-ingestion-worker deploy/ekc-outbox-relay --replicas=0`; set the API read-only or scale it down).
2. Pick the restore time: the last moment known good (UTC). Note it in the incident log.
3. Restore to a new instance (reviewed, not executed):

       aws rds restore-db-instance-to-point-in-time \
         --source-db-instance-identifier ekc-production \
         --target-db-instance-identifier ekc-production-restored \
         --restore-time 2026-01-01T12:00:00Z \
         --db-subnet-group-name ekc-production \
         --vpc-security-group-ids <rds security group id> \
         --no-publicly-accessible --deletion-protection

   Then re-create the pgvector extension if needed and re-apply the role SQL (`infrastructure/sql/`);
   restores keep data but grants to cluster roles may need re-running.
4. Verify against the restored instance (read-only): `DATABASE_URL=<restored> python -m scripts.dr_backup verify-audit`
   must report `ok`; compare `alembic_version` with the release you expect; spot-check row counts.
5. Cut over: update the `DATABASE_URL` secret, restart workloads (`kubectl rollout restart`). Keep the
   damaged instance (renamed, deletion-protected) until the post-mortem finishes.
6. Reconcile (below, "After any restore"). Scale writers back up.

### R2. DB instance or AZ failure
RDS Multi-AZ fails over automatically (DNS endpoint unchanged). Connections drop; the API returns
503 with `Retry-After` during the gap and recovers (verified for a Postgres outage in Phase 34, not
for a managed failover). Check the alerts in `monitoring/prometheus/alerts.yaml` and the readiness probe, then confirm writers are healthy. No restore needed.

### R3. Logical restore (portability, corruption escape, or no PITR available)
Uses a dump taken by `dr_backup backup` (section 5).

    export DR_TARGET_DATABASE_URL='postgresql+asyncpg://<admin>@<host>:5432/ekc_restored'   # EMPTY database, pgvector available
    python -m scripts.dr_backup restore --manifest ekc-<stamp>.manifest.json --confirm-target-database ekc_restored

It refuses a non-empty target, a checksum mismatch, or a wrong confirmation name; after restoring it
checks Alembic head, every table's row count, pgvector, invalid indexes and every audit chain.
Grants are not in the dump (`--no-privileges`): re-run the role SQL. Restore time grows with data
and with HNSW index builds: **measure it on your real data before relying on it for an RTO.**

### R4. Audit-trail integrity incident
`python -m scripts.dr_backup verify-audit` (any database, live or restored) re-computes every
tenant's chain and exits 1 on a break, naming the first bad sequence. A break means an actor with
database-owner privilege edited or deleted history (the append-only trigger stops everyone else).
Compare with the last good backup manifest's `audit_tails` to bound the damage. Treat as a security
incident (Phase 32): rotate database credentials, review who had owner access.

### R5. S3 object deleted or overwritten
Versioning keeps prior versions: list versions, copy the good version back as the current one
(`aws s3api list-object-versions`, `aws s3api copy-object --copy-source ...?versionId=...`). A
delete creates a delete marker: remove the marker. After the 90-day noncurrent expiry this is no longer possible.

### R6. Kafka cluster lost or topics damaged
Kafka is not the source of truth. Re-run the topics job (`ekc-topics-init`) against the new cluster.
Events that were never published are still in the `outbox_events` table (`published_at IS NULL`)
and the outbox relay publishes them on start. Events already consumed are not re-processed (consumers
are idempotent). To force re-ingestion of a document, re-submit it through the API.
Messages that were published but not yet consumed when the cluster died are lost: re-submit the affected documents.

### R7. Redis lost
No durable data. Login-failure counters and rate-limit windows reset (a short window where throttles
are empty). The breakers keep the API serving if Redis is down (Phase 34). Replace the node and the
auth token (`terraform taint random_password.redis` in the data module, apply, re-sync the secret).

### R8. Cluster lost
`terraform apply` recreates network/EKS (data stores are separate resources and survive). Re-install
the cluster add-ons (ingress controller, External Secrets, Prometheus Operator, a CNI that enforces
NetworkPolicy), then run the deploy workflow for the current digest. Time depends on the add-ons: **not measured**.

### R9. Credential or key compromise
Rotate in this order: database master (RDS-managed), Redis token, Kafka SCRAM, LLM provider keys
(secret manager, never chat), JWT signing key. **JWT key rotation is not implemented** (single key,
no `kid`); rotating it logs everyone out and needs a deploy with the new key pair. Revoking
individual tokens is not supported (no revocation list): known gap from Phase 32.

## 5. Logical backups (`scripts/dr_backup.py`)

    export DATABASE_URL=...                        # a role that can read all rows (owner/BYPASSRLS): row security is forced OFF
    python -m scripts.dr_backup backup --out-dir /backups

- One consistent `pg_dump -Fc` using an exported snapshot; the manifest's row counts and audit-chain
  tails come from the *same* snapshot, so they describe exactly the dumped state.
- Dump and manifest are written mode 0600; the manifest contains no credentials.
- Connection strings are read from environment variables, never argv.
- **Scheduling and off-site copy are NOT automated.** The runtime image has no `pg_dump`. Run it from
  an operations host/job inside the VPC, then copy dump + manifest to a bucket in a **different
  region** with SSE-KMS (that is the only region-loss protection available today).
- `drill` restores into a scratch database (name must contain `scratch`/`drill`/`restore`) and verifies; run it
  from the same host after each backup, and alert on a non-zero exit.

## 6. Known gaps (honest list)

- **No region-loss protection.** No cross-region snapshot copy, no replica, no replicated bucket.
  Recommended next step: `aws_db_instance_automated_backups_replication` (needs a KMS key and provider alias in the DR region), S3 cross-region replication, and an off-region copy of the dumps.
- No automated schedule for logical backups; no retention pruning for them.
- RPO/RTO in section 2 are targets or provider claims, never measured by us. A scheduled game day is required.
- The restore drill used a 10 MB synthetic database. Restore time at scale, HNSW rebuild time, and
  PITR duration are **NOT YET MEASURED**.
- After a PITR restore, S3 objects uploaded after the restore point are orphaned (no DB row) and
  Kafka messages for documents that no longer exist go to the DLQ without retry (`DocumentNotFound` and
  `ObjectNotFound` are non-retryable, verified in code); an orphan reconciliation job is **not built**.
- No document deletion or retention enforcement exists (Phase 32 note), so "restore deleted data" and "delete on request" interact: re-applying deletions after a restore is a manual step.
- JWT key rotation and token revocation are not implemented (R9).
- Terraform state holds generated Redis/Kafka credentials (Phase 38 note): protect and back up the state bucket.

## 7. After any restore (checklist)

1. `verify-audit` reports ok. 2. `alembic current` equals the deployed release. 3. Smoke test
(`scripts/smoke_test.py`). 4. Check DLQ depth and outbox backlog metrics for 30 minutes.
5. Compare tenant document counts with the last backup manifest. 6. Record the real RPO (data lost)
and RTO (time to recovery) in the incident log: these are the first real measurements.

## 8. Drill cadence

Monthly: `dr_backup drill` (automatable). Quarterly: a PITR restore into a new instance in staging and a
cutover rehearsal, timed. Yearly: cluster rebuild from Terraform. Record every result under
`docs/disaster-recovery/results/`.
