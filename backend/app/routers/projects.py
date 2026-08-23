from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import get_current_user
from app.models import OrgMembership, Project, User
from app.schemas import ProjectCreate, ProjectOut

router = APIRouter(prefix="/api/v1/projects", tags=["projects"])


@router.get("", response_model=list[ProjectOut])
async def list_projects(db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    org_ids = (await db.execute(select(OrgMembership.organization_id).where(OrgMembership.user_id == user.id))).scalars().all()
    if not org_ids:
        return []
    projects = (await db.execute(select(Project).where(Project.organization_id.in_(org_ids)))).scalars().all()
    return projects


@router.post("", response_model=ProjectOut, status_code=201)
async def create_project(data: ProjectCreate, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    # a user created via /auth/register always owns exactly one org as OWNER;
    # for simplicity new projects are created under that first org membership.
    membership = (await db.execute(select(OrgMembership).where(OrgMembership.user_id == user.id))).scalars().first()
    project = Project(organization_id=membership.organization_id, name=data.name, description=data.description)
    db.add(project)
    await db.commit()
    await db.refresh(project)
    return project