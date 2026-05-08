from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from .models import AuditLog
from .schemas import APIResponse

router = APIRouter()

@router.get("/batch/{batch_id}", response_model=APIResponse)
async def get_batch_details(
    batch_id: str,
    db: Session = Depends(get_db),
):
    logs = db.query(AuditLog).filter(AuditLog.batch_id == batch_id).all()
    return APIResponse(data=[log.to_dict() for log in logs])