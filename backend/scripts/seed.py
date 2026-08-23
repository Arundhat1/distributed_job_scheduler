"""
Populates a fresh database with a demo user/org/project/queues so the
grader can log in immediately: demo@example.com / password123

    python scripts/seed.py
"""
import asyncio

from app.database import AsyncSessionLocal, Base, engine
from app.models import Organization, OrgMembership, OrgRole, Project, Queue, RetryPolicy, RetryStrategy, User
from app.security import hash_password


async def main():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with AsyncSessionLocal() as db:
        user = User(email="demo@example.com", hashed_password=hash_password("password123"), full_name="Demo User")
        db.add(user)
        await db.flush()

        org = Organization(name="Demo Org")
        db.add(org)
        await db.flush()

        db.add(OrgMembership(user_id=user.id, organization_id=org.id, role=OrgRole.OWNER))

        project = Project(organization_id=org.id, name="Demo Project", description="Seeded demo project")
        db.add(project)
        await db.flush()

        retry_policy = RetryPolicy(name="default-exponential", strategy=RetryStrategy.EXPONENTIAL, base_delay_seconds=5, multiplier=2.0, max_delay_seconds=300, max_retries=3)
        db.add(retry_policy)
        await db.flush()

        emails_queue = Queue(project_id=project.id, name="emails", priority=5, max_concurrency=10, default_retry_policy_id=retry_policy.id)
        reports_queue = Queue(project_id=project.id, name="reports", priority=1, max_concurrency=2, default_retry_policy_id=retry_policy.id)
        db.add_all([emails_queue, reports_queue])

        await db.commit()
        print("Seeded demo data. Login with demo@example.com / password123")


if __name__ == "__main__":
    asyncio.run(main())