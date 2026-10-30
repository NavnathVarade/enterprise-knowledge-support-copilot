# Kubernetes (Phase 36)

Kustomize base + `staging` / `production` overlays in `infrastructure/kubernetes/`. Phase 30
created the base; this phase audited it and closed the gaps below.

## What is deployed

| Object | Purpose |
|---|---|
| Namespace `ekc` | Pod Security Admission `restricted` (enforce/audit/warn) |
| `ekc-api` Deployment + Service + HPA + PDB | HPA owns replica count (3-12 prod, 2-4 staging), PDB `minAvailable` < HPA min |
| `ekc-ingestion-worker` Deployment + PDB | 2 replicas, `maxUnavailable: 1`; Kafka redelivers in-flight work |
| `ekc-outbox-relay` Deployment | Singleton, `Recreate` strategy (no two relays) |
| `ekc-migrate`, `ekc-topics-init` Jobs | Argo CD PreSync hooks |
| NetworkPolicies | default-deny, then explicit ingress/metrics/egress allows |
| LimitRange + ResourceQuota | defaults/caps per container; namespace ceiling (new) |
| Ingress, ServiceMonitor, PodMonitor, PrometheusRule | edge TLS, scraping, alerts (generated from `monitoring/prometheus/alerts.yaml`) |

## Changes in this phase

- **Worker startup probes.** Same heartbeat check as liveness, 5 s x 36 (3 min) window; the old
  fixed `initialDelaySeconds: 60` is gone. A missing heartbeat file fails the check, so the pod is
  "started" only once the run loop has beaten.
- **Anti-affinity** (preferred, per host) on all three Deployments. Preferred, not required, so a
  small cluster still schedules.
- **Ephemeral storage** requests/limits on every container, each >= its `emptyDir` size limit
  (otherwise the scratch volume could evict the pod). Enforced by a test.
- **PDB for the ingestion worker.**
- **HPA owns replicas.** Removed `spec.replicas` from the API Deployment and the staging replica
  patch; a re-apply no longer fights the autoscaler. Consequence: a brand-new Deployment starts at
  1 replica until the HPA raises it to `minReplicas`.
- **LimitRange + ResourceQuota** (`base/quota.yaml`). Pod ceiling covers HPA max + workers + a
  surge pod + both Jobs (tested). Values are **starting points, NOT MEASURED** capacity.

## Decisions (deliberate, not omissions)

- **No Role / RoleBinding.** The pods never call the Kubernetes API, the ServiceAccount does not
  automount a token, so granting RBAC would only add attack surface. A test fails if any
  Role/ClusterRole/binding appears, forcing a conscious review. (The master spec lists RBAC
  objects; this is a documented deviation.)
- **No CPU limits** (requests only). CPU limits throttle latency-sensitive request paths;
  memory limits stay. kube-score flags this as CRITICAL by default; it is accepted.
- **`imagePullPolicy: IfNotPresent`** with an immutable, CI-produced tag/digest (`:latest` is
  rejected by a test). kube-score flags this too; accepted for the same reason.
- **No PriorityClass.** It is cluster-scoped and shared with other tenants of the cluster; add one
  at the platform level if needed.
- **Kafka port 9093** in the NetworkPolicy/ConfigMap is the SASL_SSL listener of an *external*
  managed or operator-run Kafka (namespace `data`). It differs from the Compose reference (9092
  plaintext, internal only). Adjust to your broker.

## Verification status

| Check | Result |
|---|---|
| `kustomize build` both overlays | run, passes |
| `kubeconform -strict`, k8s 1.29 | run: 22 resources, 0 invalid (3 CRD kinds skipped: no schemas) |
| kube-score on production render | run: remaining findings are only the 2 accepted ones above |
| Manifest policy tests (`test_deploy_assets.py`, 27) | run, pass |
| Applied to a real cluster | **NOT RUN** (no cluster in the build environment) |
| Probe behaviour under a slow Kafka join, PDB during a node drain, quota rejection, HPA scale-out | **NOT RUN / NOT YET MEASURED** |
| NetworkPolicy enforcement (needs a CNI that implements it) | **NOT RUN** |

## Operating notes

- Fill `REPLACE_ME_OWNER/REPLACE_ME_TAG` through the CI image step, never by hand.
- Secrets `ekc-secrets` and `ekc-kafka-ca` come from your secret manager (see
  `examples/externalsecret.yaml`); they are never in git.
- Plain `kubectl apply`: delete completed Jobs first (immutable pod templates):
  `kubectl delete job -n ekc ekc-migrate ekc-topics-init --ignore-not-found`.
