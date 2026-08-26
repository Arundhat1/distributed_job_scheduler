"""
Minimal RBAC enforcement.

Every Project belongs to an Organization; every User reaches a Project
through an OrgMembership row that also carries a role. Route handlers
call `get_project_or_403` / `get_queue_or_403` instead of querying
Project/Queue directly, so access control lives in one place rather than
being re-implemented per endpoint.
"""
from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import OrgMembership, OrgRole, Project, Queue, User

WRITE_ROLES = {OrgRole.OWNER, OrgRole.ADMIN, OrgRole.MEMBER}


async def _membership(db: AsyncSession, user: User, organization_id: int) -> OrgMembership | None:
    return (
        await db.execute(
            select(OrgMembership).where(
                OrgMembership.user_id == user.id,
                OrgMembership.organization_id == organization_id,
            )
        )
    ).scalar_one_or_none()


async def get_project_or_403(db: AsyncSession, user: User, project_id: int, require_write: bool = False) -> Project:
    project = (await db.execute(select(Project).where(Project.id == project_id))).scalar_one_or_none()
    if not project:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found")
    membership = await _membership(db, user, project.organization_id)
    if not membership:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not a member of this project's organization")
    if require_write and membership.role not in WRITE_ROLES:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Viewer role cannot perform this action")
    return project


async def get_queue_or_403(db: AsyncSession, user: User, queue_id: int, require_write: bool = False) -> Queue:
    queue = (await db.execute(select(Queue).where(Queue.id == queue_id))).scalar_one_or_none()
    if not queue:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Queue not found")
    await get_project_or_403(db, user, queue.project_id, require_write=require_write)
    return queue