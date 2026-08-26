from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import get_current_user
from app.models import Organization, OrgMembership, OrgRole, User
from app.rate_limit import limiter
from app.schemas import Token, UserCreate, UserOut
from app.security import create_access_token, hash_password, verify_password

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

# Tighter than the global default (300/min): these are the two endpoints an
# attacker would actually want to hammer (credential stuffing on /login,
# account-creation spam on /register). Keyed by IP via rate_limit_key since
# there's no authenticated subject yet at this point in the flow.
AUTH_LIMIT = "10/minute"


@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
@limiter.limit(AUTH_LIMIT)
async def register(request: Request, data: UserCreate, db: AsyncSession = Depends(get_db)):
    existing = (await db.execute(select(User).where(User.email == data.email))).scalar_one_or_none()
    if existing:
        raise HTTPException(status.HTTP_409_CONFLICT, "Email already registered")

    user = User(email=data.email, hashed_password=hash_password(data.password), full_name=data.full_name)
    db.add(user)
    await db.flush()  # get user.id before creating dependent rows

    org = Organization(name=data.organization_name)
    db.add(org)
    await db.flush()

    db.add(OrgMembership(user_id=user.id, organization_id=org.id, role=OrgRole.OWNER))
    await db.commit()
    await db.refresh(user)
    return user


@router.post("/login", response_model=Token)
@limiter.limit(AUTH_LIMIT)
async def login(request: Request, form_data: OAuth2PasswordRequestForm = Depends(), db: AsyncSession = Depends(get_db)):
    user = (await db.execute(select(User).where(User.email == form_data.username))).scalar_one_or_none()
    if not user or not verify_password(form_data.password, user.hashed_password):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Incorrect email or password")
    token = create_access_token(subject=str(user.id))
    return Token(access_token=token)


@router.get("/me", response_model=UserOut)
async def me(current_user: User = Depends(get_current_user)):
    return current_user