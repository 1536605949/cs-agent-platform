"""Refund state machine: propose -> human review -> execute exactly once."""

from __future__ import annotations

from app.tools import business


def test_proposal_requires_an_owned_order(db):
    proposal, error = business.create_refund_proposal(db, "user002", "ORD-1001", "test")
    assert proposal is None
    assert error == "order_not_found"


def test_proposal_created_as_pending_review(db):
    proposal, error = business.create_refund_proposal(db, "user001", "ORD-1001", "不想要了")
    assert error is None
    assert proposal.status == "pending_review"


def test_duplicate_proposal_is_reused(db):
    first, _ = business.create_refund_proposal(db, "user001", "ORD-1001", "第一次")
    second, _ = business.create_refund_proposal(db, "user001", "ORD-1001", "第二次")
    assert first.proposal_id == second.proposal_id


def test_execute_without_approval_is_refused(db):
    business.create_refund_proposal(db, "user001", "ORD-1001", "test")
    executed, error = business.execute_approved_refund(db, "ORD-1001")
    assert executed is None
    assert error == "approval_required", "an unapproved refund must never execute"


def test_review_then_execute_consumes_approval_once(db):
    business.create_refund_proposal(db, "user003", "ORD-3001", "质量问题")
    reviewed, error = business.approve_or_reject_refund(db, "ORD-3001", "approve", "admin", "核实通过")
    assert error is None
    assert reviewed.status == "approved"

    executed, error = business.execute_approved_refund(db, "ORD-3001")
    assert error is None
    assert executed.status == "executed"

    replay, replay_error = business.execute_approved_refund(db, "ORD-3001")
    assert replay is None
    assert replay_error in ("approval_required", "refund_already_processed", "approval_already_consumed")


def test_rejected_proposal_cannot_be_executed(db):
    business.create_refund_proposal(db, "user002", "ORD-2002", "test")
    reviewed, _ = business.approve_or_reject_refund(db, "ORD-2002", "reject", "admin", "不符合条件")
    assert reviewed.status == "rejected"

    executed, error = business.execute_approved_refund(db, "ORD-2002")
    assert executed is None
    assert error == "approval_required"


def test_invalid_decision_is_rejected(db):
    business.create_refund_proposal(db, "user002", "ORD-2001", "test")
    proposal, error = business.approve_or_reject_refund(db, "ORD-2001", "maybe", "admin")
    assert proposal is None
    assert error == "invalid_decision"


def test_review_without_pending_proposal_returns_error(db):
    proposal, error = business.approve_or_reject_refund(db, "ORD-9999", "approve", "admin")
    assert proposal is None
    assert error == "pending_proposal_not_found"


def test_refund_marks_order_as_not_refundable(db):
    business.create_refund_proposal(db, "user002", "ORD-2002", "test")
    business.approve_or_reject_refund(db, "ORD-2002", "approve", "admin")
    business.execute_approved_refund(db, "ORD-2002")

    order = business.get_order(db, "ORD-2002")
    assert order.refund_available is False
    assert order.status == "refund_processing"


def test_audit_trail_is_written(db):
    from sqlalchemy import select

    from app.models import AuditEvent, Order, User

    # Use a dedicated order so the assertion is independent of test ordering.
    if db.get(User, "audit_user") is None:
        db.add(User(user_id="audit_user", display_name="审计测试", level="STANDARD"))
    if db.get(Order, "ORD-9001") is None:
        db.add(Order(order_id="ORD-9001", user_id="audit_user", product_name="审计测试商品", amount_cents=1000))
    db.commit()

    before = len(db.execute(select(AuditEvent).where(AuditEvent.event_type == "refund_proposal_created")).scalars().all())
    proposal, error = business.create_refund_proposal(db, "audit_user", "ORD-9001", "audit test")
    assert error is None
    after = db.execute(select(AuditEvent).where(AuditEvent.event_type == "refund_proposal_created")).scalars().all()

    assert len(after) == before + 1, "creating a proposal must write exactly one audit event"
    assert any(row.resource_id == proposal.proposal_id for row in after)


def test_repeated_proposal_does_not_duplicate_audit_events(db):
    from sqlalchemy import select

    from app.models import AuditEvent

    business.create_refund_proposal(db, "audit_user", "ORD-9001", "again")
    first_count = len(db.execute(select(AuditEvent).where(AuditEvent.event_type == "refund_proposal_created")).scalars().all())
    business.create_refund_proposal(db, "audit_user", "ORD-9001", "again and again")
    second_count = len(db.execute(select(AuditEvent).where(AuditEvent.event_type == "refund_proposal_created")).scalars().all())

    assert first_count == second_count, "an idempotent proposal must not write a second audit event"
