"""数据库初始化: 创建所有表 (开发期使用, 生产用 alembic 迁移).

运行: python -m scripts.init_db
"""
import asyncio

from app.common.db import engine
from app.common.logger import logger
from app.common.models import Base


async def main():
    logger.info("开始创建数据库表...")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("数据库表创建完成")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
