from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Application, Customer
from app.schemas import ApplicationCreate, ApplicationOut, ApplicationUpdate
from app.services.deletion import clear_dependents, commit_or_conflict

router = APIRouter(prefix="/applications", tags=["applications"])


@router.get("", response_model=list[ApplicationOut])
async def list_applications(
    customer_id: int | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
) -> list[Application]:
    query = select(Application).order_by(Application.name)
    if customer_id is not None:
        query = query.where(Application.customer_id == customer_id)
    result = await db.execute(query)
    return list(result.scalars().all())


@router.post("", response_model=ApplicationOut, status_code=status.HTTP_201_CREATED)
async def create_application(payload: ApplicationCreate, db: AsyncSession = Depends(get_db)) -> Application:
    customer = await db.get(Customer, payload.customer_id)
    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found")
    existing = await db.execute(
        select(Application).where(
            Application.customer_id == payload.customer_id, Application.name == payload.name
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Application name already exists for this customer")
    application = Application(**payload.model_dump())
    db.add(application)
    await db.commit()
    await db.refresh(application)
    return application


@router.get("/{application_id}", response_model=ApplicationOut)
async def get_application(application_id: int, db: AsyncSession = Depends(get_db)) -> Application:
    application = await db.get(Application, application_id)
    if not application:
        raise HTTPException(status_code=404, detail="Application not found")
    return application


@router.patch("/{application_id}", response_model=ApplicationOut)
async def update_application(
    application_id: int, payload: ApplicationUpdate, db: AsyncSession = Depends(get_db)
) -> Application:
    application = await db.get(Application, application_id)
    if not application:
        raise HTTPException(status_code=404, detail="Application not found")
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(application, key, value)
    await db.commit()
    await db.refresh(application)
    return application


@router.delete("/{application_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_application(application_id: int, db: AsyncSession = Depends(get_db)) -> None:
    application = await db.get(Application, application_id)
    if not application:
        raise HTTPException(status_code=404, detail="Uygulama bulunamadı")

    # Faz 23: alt kayıtlar ÖNCE temizleniyor. ORM ilişkileri zincirin yalnızca bir kısmını
    # kapsıyordu (ör. müşteri → uygulama → grup → düğüm kapsanıyor ama `servers` kapsanmıyor,
    # grup silmede `group_health_snapshots` kapsanmıyor); kapsanmayan bir tablo foreign key
    # ihlaline ve 500'e yol açıyordu. `clear_dependents` listeyi model metadata'sından
    # türetiyor, yani yeni bir tablo eklendiğinde burayı güncellemek gerekmiyor.
    await clear_dependents(db, "applications", application_id)
    await db.delete(application)
    await commit_or_conflict(db, "applications", application_id, "Uygulama")
