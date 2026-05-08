"""
MSA Legacy Endpoints
Kept for backward compatibility - new code should use msa_stock.py instead
"""
from fastapi import APIRouter

router = APIRouter(prefix="/msa-legacy", tags=["MSA Legacy"])

# This file is reserved for legacy MSA endpoints
# All new MSA functionality is in msa_stock.py
# Routes are registered in router.py with the /msa-legacy prefix
