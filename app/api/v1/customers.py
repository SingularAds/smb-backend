"""Customers API Router — Firestore backed (no auth)"""

from fastapi import APIRouter, HTTPException

from app import firestore as fs

router = APIRouter()


@router.get("")
async def get_customers(
    business_id: str = "",
    limit: int = 100,
    offset: int = 0,
):
    """Get customers. Pass ?business_id=xxx to filter by business."""
    if not business_id:
        return {"error": "business_id query param required", "example": "?business_id=YOUR_BUSINESS_ID"}
    return fs.list_customers(business_id, limit=limit, offset=offset)


@router.get("/{phone}")
async def get_customer(phone: str, business_id: str = ""):
    """Get a customer by phone. Pass ?business_id=xxx to scope to a business."""
    if business_id:
        customer = fs.get_customer_by_phone(business_id, phone)
    else:
        docs = fs._db().collection("customers").where("phone", "==", phone).limit(1).stream()
        customer = None
        for doc in docs:
            customer = doc.to_dict()
            customer["id"] = doc.id
    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found")
    return customer
