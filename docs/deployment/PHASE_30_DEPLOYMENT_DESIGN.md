# Phase 30 — Deployment (image, Compose, Kubernetes, CI)

## 1. Scope

Turn the application into something that can be shipped: one hardened container image, a
Compose reference stack, Kubernetes manifests (kustomize), Kafka topic provisioning, worker
liveness, and a CI pipeline. Section 6 states exactly what was and was not verified.

## 2. Design decisions

**One image, many roles.** The command selects the role: `uvicorn` (api), `python -m
packages.workers ingestion|outbox-relay`, `alembic upgrade head` (migrate), `python -m
scripts.create_topics` (topics-init). One build means one scanned artifact and no drift
between what migrates and what serves.

**Image hardening.** Multi-stage build; build tools stay in the builder stage. Runtime has
only `libpq5` and `curl`. Dependencies install from `uv.lock` (`uv sync --frozen --no-dev`; amended Phase 49, previously `requirements/constraints.txt`) (exact pins,
regenerated with `uv lock`; hashes are recorded and verified by uv — amended Phase 49). Runs as numeric
uid/gid 10001. `.dockerignore` excludes tests, docs, `.env*`, `.git`. No secret is baked in;
a policy test fails if a `SECRET/PASSWORD/API_KEY/TOKEN` ENV/ARG is introduced.

**Worker liveness without an HTTP port.** The run loops write a heartbeat file each
iteration; the Kubernetes exec probe runs `python -m packages.workers.health --max-age 900`.
A hung event loop stops beating and the pod is restarted. 900s must exceed the longest
legitimate gap (retry backoff plus one slow handler); it is a backstop, per-call provider
timeouts are the first defence.

**Kafka provisioning.** Brokers run with auto-create off, so `scripts/create_topics.py`
creates main/retry/DLQ topics (replication 3, min ISR 2, 7d/3d/30d retention). It is
idempotent and never alters an existing topic: partition-count drift is reported only,
because changing partitions breaks per-document ordering.

**Kafka security gap found and fixed in this phase.** `KAFKA_SECURITY_PROTOCOL` defaulted to
`SASL_SSL`, but no SASL mechanism, credentials or CA path were passed to any client, so
the producer, consumer and admin client could never have authenticated to a secured broker.
`packages/kafka/client_config.py` is now the single builder for all three; `KafkaSettings`
gained `sasl_mechanism`, `sasl_username`, `sasl_password` (SecretStr) and `ssl_ca_location`.
In staging/production, settings now refuse (a) a SASL protocol without all three SASL values
and (b) `PLAINTEXT`.

**Kubernetes (kustomize, not Helm).** Helm could not be downloaded in the build sandbox and
an unvalidated chart would be unverifiable, so the manifests are plain kustomize.
- Base: Namespace (Pod Security `restricted`), ServiceAccount (token not mounted),
  ConfigMap (non-secret, cluster-DNS hosts only), api/ingestion-worker/outbox-relay
  Deployments, Service, HPA, PDB, Ingress (TLS; `/metrics` returns 404 publicly),
  default-deny NetworkPolicy plus scoped allows, migrate and topics-init Jobs,
  ServiceMonitor, PodMonitor, PrometheusRule.
- Every pod: non-root, `readOnlyRootFilesystem` with a `/tmp` emptyDir, all capabilities
  dropped, no privilege escalation, seccomp `RuntimeDefault`, CPU requests and memory limits.
  No CPU limit on purpose: CPU limits throttle latency-sensitive services.
- The outbox relay is a singleton with `Recreate` strategy. Multiple relays would be safe
  (`FOR UPDATE SKIP LOCKED`), but one keeps ordering simple until throughput demands more.
- Secrets come from an out-of-band `ekc-secrets` Secret; `examples/externalsecret.yaml`
  shows External Secrets Operator wiring. Nothing secret is in any manifest (tested).
- The PrometheusRule is generated from `monitoring/prometheus/alerts.yaml` so there is one
  source of truth; a test and a CI step fail if it is stale.
- Migration and topic Jobs carry Argo CD PreSync hook annotations. With plain `kubectl
  apply` a Job's pod template is immutable: delete the old Job first.

**Compose** is a reference environment (single-node PLAINTEXT Kafka, MinIO for S3, allowed
only because `ENVIRONMENT=development`). Order: postgres → migrate → api; kafka →
topics-init → workers.

**CI.** Adds hadolint, generated-rule freshness, kustomize + kubeconform on both overlays,
`docker compose config`, build with SBOM and max provenance (push only on `main`), and a
Trivy gate on fixable HIGH/CRITICAL.

## 3. Placeholders that MUST be replaced before first deploy

| Where | Placeholder |
|---|---|
| `base/kustomization.yaml` `images` | `ghcr.io/REPLACE_ME_OWNER/ekc` and `REPLACE_ME_TAG` (set per release by CI) |
| `base/ingress.yaml` | host `ekc.example.invalid` and the TLS secret `ekc-api-tls` |
| `base/configmap.yaml` | `*.data.svc.cluster.local` hosts are assumed names; set the real Redis/Kafka services (Postgres URL is in the secret) |
| `examples/externalsecret.yaml` | `ClusterSecretStore` name and remote key paths |
| Out-of-band Secrets | `ekc-secrets`, `ekc-kafka-ca` (`ca.crt`), `ekc-metrics-token` (`metrics-token`) |
| NetworkPolicy | namespace names `ingress-nginx`, `monitoring`, `data`, `observability` |

## 4. Rollout and rollback

1. CI builds and scans the image, tagged by commit SHA.
2. Set the tag in the overlay and apply. PreSync jobs run `alembic upgrade head` and topic
   provisioning first; if either fails, the Deployments do not roll.
3. API rolls with `maxUnavailable: 0`; readiness gates on database and Redis.
4. Rollback = redeploy the previous tag. **Migrations are not rolled back automatically**;
   every migration here has a tested `downgrade`, but run it deliberately, and only for
   migrations that carry no data you need.

## 5. Tests added

- `tests/unit/test_worker_health.py` — missing/fresh/stale heartbeat, env path.
- `tests/unit/test_topic_admin.py` — topic shape, ISR rules, idempotence, drift reported not
  changed, failures surfaced, create races tolerated (fake admin client).
- `tests/unit/test_kafka_client_config.py` — SASL keys reach clients, password not in repr,
  production rejects PLAINTEXT and SASL without credentials.
- `tests/unit/test_deploy_assets.py` — Dockerfile policy (non-root, pinned, no secrets),
  per-pod hardening, probes, no localhost, no inline secrets, default-deny NetworkPolicy,
  PDB < HPA minimum, singleton relay, generated-rule sync. The kustomize-dependent tests skip
  when the binary is absent (CI installs it). I broke the manifests on purpose (writable
  rootfs, a localhost host) and confirmed these tests fail.

Full suite at the end of this phase: **659 passed**, ruff and mypy strict clean, migrations
upgrade → downgrade → upgrade on a fresh database.

## 6. What was verified, and what was not

| Item | Status |
|---|---|
| hadolint on the Dockerfile | **Run, clean** |
| `docker build` | **Run, succeeded — against a substitute base** (Ubuntu 24.04 debootstrap with Python 3.12 and the sandbox proxy CA), because Docker Hub is unreachable. Behaviour on `python:3.12-slim-bookworm` is **not verified** (glibc/libpq versions differ) |
| Image runtime | **Run:** uid 10001; app imports; no dev packages; `alembic current` reported `0006_audit_events (head)` against the real database; API served live/ready (database + Redis ok) on a read-only rootfs with all capabilities dropped and `no-new-privileges`, container healthcheck reached healthy, SIGTERM gave exit 0 and a `service_shutdown` log; `/metrics` returns 404 without a token |
| `kustomize build` + `kubeconform -strict` (k8s 1.29) | **Run, pass** for both overlays; 16 core resources validated, **3 operator CRDs skipped** (ServiceMonitor, PodMonitor, PrometheusRule have no schemas available) |
| `docker compose config` | **Run, valid** |
| Compose stack end to end | **Not run** (postgres/redis/kafka/minio images not pullable here) |
| Manifests applied to a cluster | **Not run** — no cluster. Probe timings, resource sizes, NetworkPolicy reachability and PodSecurity admission are unproven |
| Kafka SASL against a real broker | **Not run** — config construction is tested, the handshake is not |
| CI workflow | **Not run on GitHub.** Action versions and release URLs were written from memory of their conventions and not fetched |
| Trivy / SBOM / provenance | **Not run** |
| Resource requests/limits, HPA and PDB numbers | **Untuned guesses** — NOT YET MEASURED under load |
| Helm chart, Terraform | **Not built** (binaries unreachable, so nothing could be validated) |

## 7. Known limitations

- ~~No pip `--require-hashes`~~ Closed in the uv migration: `uv.lock` records sha256 for every artifact and uv verifies them on install.
- No image signing (cosign) or admission policy verifying signatures.
- Single Ingress class assumed (`nginx`).
- Egress to the internet is allowed on 443 to any public address (LLM provider and S3 have
  no fixed IPs); tighten with an egress proxy or FQDN policy if the CNI supports it.
- Worker metrics are served only when `OTEL_METRICS_BEARER_TOKEN` is set.
- The `.retry` topics are provisioned but nothing consumes them yet.
- Consumer lag reporting exists but is not scheduled inside the worker process.
