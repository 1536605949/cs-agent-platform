"""FAQ retrieval: tokenizer, BM25 ranking and cache invalidation."""

from __future__ import annotations

from app.core.faq import FaqIndex, get_index, invalidate_faq_cache, retrieve_faq, tokenize
from app.models import FaqEntry


def test_tokenizer_handles_mixed_languages():
    tokens = tokenize("退款 refund 到账时间")
    assert "退" in tokens
    assert "退款" in tokens
    assert "refund" in tokens
    assert "时间" in tokens


def test_index_ranks_exact_question_first(db):
    index = get_index(db)
    hits = index.search("怎么开发票？", top_k=3)
    assert hits
    assert "发票" in hits[0].question


def test_retrieval_is_relevant_for_refund_question(db):
    hits = retrieve_faq(db, "退款多久能到账", top_k=3, min_score=1.2)
    assert hits
    assert "退款" in hits[0].question


def test_retrieval_returns_nothing_for_unrelated_query(db):
    """Out-of-domain tokens must not retrieve anything above the threshold."""
    assert retrieve_faq(db, "zzqqxx wwvvuu qqzzxx", top_k=3, min_score=1.2) == []


def test_min_score_filters_weak_matches(db):
    permissive = retrieve_faq(db, "支付", top_k=5, min_score=0.0)
    strict = retrieve_faq(db, "支付", top_k=5, min_score=8.0)
    assert len(strict) <= len(permissive)


def test_empty_query_returns_no_hits(db):
    assert retrieve_faq(db, "", top_k=3, min_score=0.0) == []


def test_index_cache_invalidation(db):
    first = get_index(db)
    invalidate_faq_cache()
    second = get_index(db)
    assert first is not second


def test_new_faq_entry_becomes_searchable(db):
    entry = FaqEntry(
        category="general",
        question="测试专用问题：如何领取新人礼包？",
        answer="在首页点击新人专区即可领取新人礼包。",
        keywords="新人 礼包 领取",
        enabled=True,
    )
    db.add(entry)
    db.commit()
    try:
        hits = retrieve_faq(db, "新人礼包怎么领取", top_k=3, min_score=0.5)
        assert hits
        assert "新人礼包" in hits[0].question
    finally:
        db.delete(entry)
        db.commit()
        invalidate_faq_cache()


def test_disabled_entry_is_not_indexed(db):
    entry = FaqEntry(
        category="general",
        question="停用条目：独一无二的停用关键词 zzqqxx",
        answer="不应被检索到",
        keywords="zzqqxx",
        enabled=False,
    )
    db.add(entry)
    db.commit()
    try:
        invalidate_faq_cache()
        hits = retrieve_faq(db, "zzqqxx", top_k=5, min_score=0.1)
        assert all("停用条目" not in hit.question for hit in hits)
    finally:
        db.delete(entry)
        db.commit()
        invalidate_faq_cache()


def test_index_handles_empty_corpus():
    index = FaqIndex([])
    assert index.search("任何问题") == []
