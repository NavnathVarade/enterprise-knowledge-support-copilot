"""
Provision Kafka topics:  python -m scripts.create_topics [--partitions 12 --replication-factor 3]

Exit codes: 0 ok (drift is reported but not fatal), 1 any topic failed to create.
Run as a Kubernetes Job before the workers start (see infrastructure/kubernetes).
"""

from __future__ import annotations

import argparse
import sys

from packages.config.settings import get_settings
from packages.kafka.client_config import base_client_config
from packages.kafka.topic_admin import desired_topics, provision_topics


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--partitions", type=int, default=12)
    ap.add_argument("--replication-factor", type=int, default=3)
    ap.add_argument("--min-isr", type=int, default=None)
    args = ap.parse_args()

    from confluent_kafka.admin import AdminClient

    kafka = get_settings().kafka
    admin = AdminClient(base_client_config(kafka))
    result = provision_topics(
        admin,
        desired_topics(
            partitions=args.partitions,
            replication_factor=args.replication_factor,
            min_isr=args.min_isr,
        ),
    )
    print(f"created={result.created}\nexisting={result.existing}")
    for line in result.drift:
        print(f"DRIFT: {line}", file=sys.stderr)
    for name, err in result.failed.items():
        print(f"FAILED: {name}: {err}", file=sys.stderr)
    return 1 if result.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
