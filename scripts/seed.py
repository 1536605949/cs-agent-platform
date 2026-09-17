#!/usr/bin/env python
"""Seed the database with demo accounts, orders, the FAQ knowledge base and
optionally a set of realistic conversations so the back office has data to show.

Usage::

    python scripts/seed.py                        # accounts + orders + FAQ
    python scripts/seed.py --with-demo-chats 12   # also generate sample chats
    python scripts/seed.py --reset                # drop and recreate everything
"""

from __future__ import annotations

import argparse
import asyncio
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.core.factory import get_engine  # noqa: E402
from app.database import Base, SessionLocal, engine, init_db  # noqa: E402
from app.models import Conversation, Message, MessageRole, Order, utcnow  # noqa: E402
from app.tools.business import seed_demo_data  # noqa: E402

DEMO_SCRIPTS: list[tuple[str, list[str]]] = [
    ("user001", ["你好", "帮我查一下订单 ORD-1001 的物流", "那大概什么时候能到？", "好的谢谢"]),
    ("user001", ["订单 ORD-1002 我想申请退款", "为什么还要审核？", "多久能到账"]),
    ("user002", ["App 一直闪退打不开怎么办", "试过了还是不行", "那帮我转人工吧"]),
    ("user002", ["怎么开发票？", "电子发票可以吗"]),
    ("user003", ["我的订单到哪了", "ORD-3001", "好的"]),
    ("user003", ["收不到验证码", "换了个手机号也不行"]),
    ("user001", ["退款多久能到账", "7 天还没到怎么办"]),
    ("user002", ["物流信息好几天没更新了", "能帮我催一下吗"]),
    ("user003", ["优惠券怎么用", "可以叠加吗"]),
    ("user001", ["我想投诉", "订单 ORD-1003 收到的时候是坏的"]),
    ("user002", ["登录提示密码错误", "重置了还是不行", "转人工"]),
    ("user003", ["支持哪些支付方式", "可以分期吗"]),
]

COMMENTS = {
    5: ["问题解决得很快，感谢！", "客服态度很好", "回复很专业"],
    4: ["整体不错，速度可以再快一点", "解决了我的问题"],
    3: ["还行吧，等了一会儿", "基本解决了"],
    2: ["回复有点慢", "没有完全解决我的问题"],
    1: ["转人工等太久了", "问题一直没解决"],
}


def reset_database() -> None:
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    print("· 数据库已重置")


async def generate_demo_chats(count: int) -> int:
    """Replay scripted conversations through the real engine."""
    engine_instance = get_engine()
    created = 0

    with SessionLocal() as db:
        for index in range(count):
            user_id, script = DEMO_SCRIPTS[index % len(DEMO_SCRIPTS)]
            conversation_id = ""

            for utterance in script:
                outcome = await engine_instance.handle(
                    db,
                    user_id=user_id,
                    message=utterance,
                    conversation_id=conversation_id,
                )
                conversation_id = outcome.conversation_id

            conversation = db.execute(
                select(Conversation).where(Conversation.conversation_id == conversation_id)
            ).scalars().first()
            if conversation is None:
                continue

            # Most sessions are rated; leave a few unrated so the
            # "评价率" metric is not a misleading 100%.
            if random.random() < 0.75:
                score = random.choices([5, 4, 3, 2, 1], weights=[42, 30, 15, 8, 5])[0]
                conversation.satisfaction_score = score
                conversation.satisfaction_comment = random.choice(COMMENTS[score])
                conversation.rated_at = utcnow()

            created += 1

        db.commit()

    return created


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed demo data for the platform")
    parser.add_argument("--with-demo-chats", type=int, default=0, metavar="N",
                        help="generate N sample conversations with satisfaction ratings")
    parser.add_argument("--reset", action="store_true", help="drop all tables first")
    parser.add_argument("--no-faq", action="store_true", help="skip the FAQ knowledge base")
    parser.add_argument("--seed", type=int, default=20260917, help="random seed for reproducibility")
    args = parser.parse_args()

    random.seed(args.seed)

    if args.reset:
        reset_database()
    else:
        init_db()

    with SessionLocal() as db:
        created = seed_demo_data(db, with_faq=not args.no_faq)
        print(f"· 新增用户 {created['users']} 个 / 订单 {created['orders']} 个 / FAQ {created['faqs']} 条")

        faq_total = db.execute(select(Conversation)).scalars().all()
        print(f"· 当前会话数 {len(faq_total)}")

    if args.with_demo_chats > 0:
        made = asyncio.run(generate_demo_chats(args.with_demo_chats))
        print(f"· 生成演示会话 {made} 个（含满意度评分）")

    print("完成。启动服务：python -m uvicorn app.main:app --reload")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
