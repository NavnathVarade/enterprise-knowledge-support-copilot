from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from packages.auth.identity import Identity
from packages.config.settings import get_settings
from packages.database.models import (
    Customer,
    Deployment,
    Incident,
    SupportTicket,
    Tenant,
    TicketEvent,
)
from packages.database.repositories.chunks import create_chunks_for_version
from packages.database.repositories.documents import (
    create_acl_entry,
    create_document,
    create_draft_version,
    publish_version,
)
from packages.database.repositories.embeddings import create_embeddings_for_chunks
from packages.database.session import get_sessionmaker
from packages.domain.errors import InvalidQuery, PermissionDenied, ResourceNotFound
from packages.domain.hashing import sha256_hex_of_text
from packages.investigation.knowledge import make_authorized_knowledge_search
from packages.investigation.workflows import analyze_incidents, investigate_customer_issue
from rag.chunking.base import Chunk
from rag.embeddings.deterministic_provider import DeterministicLocalEmbeddingProvider
from rag.reranking.lexical_overlap_reranker import LexicalOverlapReranker
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration
NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
H = timedelta(hours=1)


@pytest.fixture
async def db() -> AsyncSession:
    async with get_sessionmaker(get_settings().database)() as session:
        yield session
        await session.rollback()


def _identity(tenant: uuid.UUID, roles: frozenset[str] = frozenset({"support_agent"})) -> Identity:
    return Identity(uuid.uuid4(), tenant, "a@example.com", roles, frozenset())


async def _tenant(db: AsyncSession) -> uuid.UUID:
    t = Tenant(name="Inv", slug=f"inv-{uuid.uuid4().hex[:8]}")
    db.add(t)
    await db.flush()
    return t.id


async def _scenario(db: AsyncSession, tenant: uuid.UUID) -> dict[str, uuid.UUID]:
    """Customer with 2 payments tickets; failed deploy 3h before; overlapping SEV2 incident;
    plus unrelated billing incident and an old irrelevant deployment."""
    cust = Customer(tenant_id=tenant, plan="pro", region="eu")
    db.add(cust)
    await db.flush()
    t1 = SupportTicket(
        tenant_id=tenant,
        customer_id=cust.id,
        service_name="payments",
        status="open",
        priority="high",
        channel="email",
        error_code="ERR-4021",
        opened_at=NOW - 2 * H,
    )
    t2 = SupportTicket(
        tenant_id=tenant,
        customer_id=cust.id,
        service_name="payments",
        status="open",
        priority="high",
        channel="chat",
        error_code="ERR-4021",
        opened_at=NOW - H,
    )
    deploy = Deployment(
        tenant_id=tenant,
        service_name="payments",
        version="2.0",
        environment="prod",
        status="failed",
        deployed_at=NOW - 5 * H,
    )
    old_deploy = Deployment(
        tenant_id=tenant,
        service_name="payments",
        version="1.9",
        environment="prod",
        status="succeeded",
        deployed_at=NOW - 20 * 24 * H,
    )
    inc = Incident(
        tenant_id=tenant,
        service_name="payments",
        severity="SEV2",
        status="open",
        root_cause_category="deployment",
        affected_customer_count=50,
        started_at=NOW - 3 * H,
    )
    unrelated = Incident(
        tenant_id=tenant,
        service_name="billing",
        severity="SEV1",
        status="open",
        affected_customer_count=5,
        started_at=NOW - 3 * H,
    )
    db.add_all([t1, t2, deploy, old_deploy, inc, unrelated])
    await db.flush()
    db.add(
        TicketEvent(
            tenant_id=tenant,
            ticket_id=t1.id,
            event_type="escalated",
            occurred_at=NOW - 90 * timedelta(minutes=1),
        )
    )
    await db.commit()
    return {
        "customer": cust.id,
        "inc": inc.id,
        "deploy": deploy.id,
        "unrelated": unrelated.id,
        "old_deploy": old_deploy.id,
        "t1": t1.id,
        "t2": t2.id,
    }


async def test_customer_investigation_gathers_and_ranks_evidence(db: AsyncSession) -> None:
    tenant = await _tenant(db)
    ids = await _scenario(db, tenant)
    r = await investigate_customer_issue(
        db, identity=_identity(tenant), customer_id=ids["customer"], now=NOW
    )

    assert {t.id for t in r.tickets} == {ids["t1"], ids["t2"]}
    cand_ids = {c.ref_id for c in r.candidates}
    assert ids["inc"] in cand_ids and ids["deploy"] in cand_ids
    assert ids["unrelated"] not in cand_ids and ids["old_deploy"] not in cand_ids
    inc = next(c for c in r.candidates if c.ref_id == ids["inc"])
    assert inc.strength == "strong" and set(inc.related_ticket_ids) == {ids["t1"], ids["t2"]}
    assert r.candidates[0].strength == "strong"
    assert "not confirmed root causes" in r.disclaimer
    kinds = [e.kind for e in r.timeline]
    assert (
        kinds.count("ticket_opened") == 2
        and "ticket_event" in kinds
        and "incident_started" in kinds
    )
    assert [e.at for e in r.timeline] == sorted(e.at for e in r.timeline)
    assert not any(r.truncated.values())


async def test_error_code_filter_narrows_tickets(db: AsyncSession) -> None:
    tenant = await _tenant(db)
    ids = await _scenario(db, tenant)
    r = await investigate_customer_issue(
        db, identity=_identity(tenant), customer_id=ids["customer"], error_code="ERR-0000", now=NOW
    )
    assert r.tickets == [] and r.candidates == []


async def test_role_required(db: AsyncSession) -> None:
    tenant = await _tenant(db)
    ids = await _scenario(db, tenant)
    with pytest.raises(PermissionDenied):
        await investigate_customer_issue(
            db,
            identity=_identity(tenant, frozenset({"viewer"})),
            customer_id=ids["customer"],
            now=NOW,
        )
    with pytest.raises(PermissionDenied):
        await analyze_incidents(db, identity=_identity(tenant, frozenset()), now=NOW)


async def test_cross_tenant_customer_is_not_found(db: AsyncSession) -> None:
    a, b = await _tenant(db), await _tenant(db)
    ids_b = await _scenario(db, b)
    with pytest.raises(ResourceNotFound):
        await investigate_customer_issue(
            db, identity=_identity(a), customer_id=ids_b["customer"], now=NOW
        )


async def test_cross_tenant_data_never_used_as_evidence(db: AsyncSession) -> None:
    a, b = await _tenant(db), await _tenant(db)
    ids_a = await _scenario(db, a)
    await _scenario(db, b)  # b has an identical-looking incident/deployment on the same service
    r = await investigate_customer_issue(
        db, identity=_identity(a), customer_id=ids_a["customer"], now=NOW
    )
    assert {c.ref_id for c in r.candidates} <= {ids_a["inc"], ids_a["deploy"]}
    inc_report = await analyze_incidents(db, identity=_identity(a), now=NOW)
    assert {i.id for i in inc_report.incidents} == {ids_a["inc"], ids_a["unrelated"]}


@pytest.mark.parametrize(
    ("since", "until"),
    [
        (NOW, NOW - H),
        (datetime(2026, 1, 1), NOW),
        (NOW - timedelta(days=800), NOW),
    ],
)
async def test_invalid_windows_rejected(db: AsyncSession, since: datetime, until: datetime) -> None:
    tenant = await _tenant(db)
    with pytest.raises(InvalidQuery):
        await analyze_incidents(db, identity=_identity(tenant), since=since, until=until, now=NOW)


async def test_incident_analysis_unresolved_recurring_and_preceding_changes(
    db: AsyncSession,
) -> None:
    tenant = await _tenant(db)
    db.add_all(
        [
            Incident(
                tenant_id=tenant,
                service_name="payments",
                severity="SEV2",
                status="open",
                root_cause_category="deployment",
                affected_customer_count=1,
                started_at=NOW - 3 * H,
            ),
            Incident(
                tenant_id=tenant,
                service_name="payments",
                severity="SEV3",
                status="resolved",
                root_cause_category="deployment",
                affected_customer_count=1,
                started_at=NOW - 10 * 24 * H,
                ended_at=NOW - 10 * 24 * H + H,
            ),
            Incident(
                tenant_id=tenant,
                service_name="search",
                severity="SEV3",
                status="resolved",
                root_cause_category="capacity",
                affected_customer_count=1,
                started_at=NOW - 5 * 24 * H,
                ended_at=NOW - 5 * 24 * H + H,
            ),
            Deployment(
                tenant_id=tenant,
                service_name="payments",
                version="3.1",
                environment="prod",
                status="succeeded",
                deployed_at=NOW - 4 * H,
            ),
        ]
    )
    await db.commit()
    r = await analyze_incidents(db, identity=_identity(tenant), now=NOW)
    assert len(r.incidents) == 3 and len(r.unresolved) == 1
    assert [(p.service_name, p.root_cause_category, len(p.incident_ids)) for p in r.recurring] == [
        ("payments", "deployment", 2)
    ]
    open_inc = r.unresolved[0]
    assert [d.version for d in r.preceding_deployments[open_inc.id]] == ["3.1"]
    only_search = await analyze_incidents(
        db, identity=_identity(tenant), service_name="search", now=NOW
    )
    assert [i.service_name for i in only_search.incidents] == ["search"]


async def _publish_doc(
    db: AsyncSession, tenant: uuid.UUID, title: str, text: str, *, public: bool
) -> uuid.UUID:
    provider = DeterministicLocalEmbeddingProvider(dimensions=1024)
    doc = await create_document(
        db,
        tenant_id=tenant,
        title=title,
        source_type="upload",
        mime_type="text/plain",
        content_hash=sha256_hex_of_text(title),
    )
    if public:
        await create_acl_entry(
            db, tenant_id=tenant, document_id=doc.id, visibility="public_within_tenant"
        )
    v = await create_draft_version(
        db, tenant_id=tenant, document_id=doc.id, content_hash=sha256_hex_of_text("v")
    )
    v = await publish_version(db, tenant_id=tenant, document_id=doc.id, version_number=v.version)
    rows = await create_chunks_for_version(
        db,
        tenant_id=tenant,
        document_version_id=v.id,
        chunks=[Chunk(0, text, max(1, len(text) // 4), None, None, 0, len(text))],
    )
    await create_embeddings_for_chunks(
        db,
        tenant_id=tenant,
        chunk_ids=[r.id for r in rows],
        vectors=await provider.embed_batch([text]),
        embedding_model=provider.model_name,
        embedding_version="v1",
        provider="deterministic-local",
    )
    await db.commit()
    return doc.id


async def test_knowledge_references_are_acl_enforced(db: AsyncSession) -> None:
    tenant = await _tenant(db)
    ids = await _scenario(db, tenant)
    open_doc = await _publish_doc(
        db,
        tenant,
        "ERR-4021 Error Guide",
        "ERR-4021 means the payment gateway timed out.",
        public=True,
    )
    secret_doc = await _publish_doc(
        db,
        tenant,
        "Secret ERR-4021 Memo",
        "ERR-4021 internal exec memo about payment gateway.",
        public=False,
    )
    identity = _identity(tenant)
    search = make_authorized_knowledge_search(
        db, identity, DeterministicLocalEmbeddingProvider(dimensions=1024), LexicalOverlapReranker()
    )
    r = await investigate_customer_issue(
        db, identity=identity, customer_id=ids["customer"], knowledge_search=search, now=NOW
    )
    doc_ids = {k.document_id for k in r.knowledge}
    assert open_doc in doc_ids and secret_doc not in doc_ids
    assert {k.kind for k in r.knowledge} >= {"error_documentation"}
