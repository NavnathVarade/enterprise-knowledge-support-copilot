# Helm chart (Phase 37)

`infrastructure/helm/ekc/` packages the same workloads as the Kustomize base
(`docs/deployment/KUBERNETES.md`) for teams that deploy with Helm.

## STATUS: reviewed, NOT rendered

The `helm` binary could not be obtained in the build environment (get.helm.sh and the Go module
proxy are blocked). **No template has ever been rendered by Helm.** What was verified instead:

| Check | Result |
|---|---|
| `tests/unit/test_helm_chart.py` (22 structural tests) | run, pass |
| values.yaml validates against `values.schema.json` (incl. rejecting tag `latest`) | run, pass |
| Every `.Values.*` reference exists in values.yaml; every `include` has a `define`; blocks balanced | run, pass |
| Chart config and API resources equal the Kustomize base (drift guard) | run, pass |
| `helm lint`, `helm template`, kubeconform on rendered output | **NOT RUN** (CI step added, never executed) |
| Install/upgrade on a cluster, hook ordering, rollback | **NOT RUN** |

Treat the first CI run as the real test; expect to fix template whitespace/indent issues.

## Install

    helm upgrade --install ekc infrastructure/helm/ekc -n ekc --create-namespace \
      -f infrastructure/helm/ekc/values-production.yaml \
      --set image.repository=ghcr.io/<owner>/ekc --set image.tag=<ci-tag> \
      --set ingress.host=<your-host>

Required values (render fails otherwise): `image.repository`, `image.tag` (not `latest`),
`ingress.host` when ingress is enabled. Secrets `ekc-secrets`, `ekc-kafka-ca` (and
`ekc-metrics-token` with ServiceMonitor) must already exist; the chart never creates Secrets.
Label the namespace `pod-security.kubernetes.io/enforce=restricted`.

## Design notes

- **Hooks.** Migrations and Kafka topic creation are `pre-install,pre-upgrade` hook Jobs, named
  with the release revision (a Job pod template is immutable). Because hooks run before regular
  resources exist, the ConfigMap and ServiceAccount are hooks too (weight -10). Trade-off: those
  two are not deleted by `helm uninstall`. Set `migrations.enabled=false` to run migrations
  yourself (then they become normal resources).
- **Same posture as Kustomize:** hardened pod/container security, no API token, anti-affinity,
  startup/liveness/readiness probes, HPA-owned replicas, PDBs, default-deny NetworkPolicies,
  LimitRange/ResourceQuota, no Role/RoleBinding (documented decision).
- **Not in the chart:** PrometheusRule (generated from `monitoring/prometheus/alerts.yaml`;
  apply `infrastructure/kubernetes/base/prometheusrule.yaml` or add it to your platform chart).
- **Pick one path** (Kustomize or Helm) per environment; do not apply both to one namespace.
- `outboxRelay` is a singleton (`Recreate`); the chart hardcodes one replica.
