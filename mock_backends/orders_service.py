"""
Mock Orders Backend Service
============================
A minimal FastAPI service that simulates a real orders microservice.
"""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import uvicorn, os

PORT = int(os.getenv("PORT", 9002))
INSTANCE = os.getenv("INSTANCE", "orders-1")

app = FastAPI(title=f"Mock Orders Service ({INSTANCE})")

ORDERS = {
    "101": {"id": "101", "user_id": "1", "item": "Widget A", "total": 9.99},
    "102": {"id": "102", "user_id": "2", "item": "Gadget B", "total": 49.99},
}


@app.get("/health")
async def health():
    return {"status": "ok", "service": "orders", "instance": INSTANCE}


@app.get("/orders")
async def list_orders():
    return {"orders": list(ORDERS.values()), "instance": INSTANCE}


@app.get("/orders/{order_id}")
async def get_order(order_id: str):
    if order_id not in ORDERS:
        return JSONResponse(status_code=404, content={"error": "Order not found"})
    return {**ORDERS[order_id], "instance": INSTANCE}


@app.post("/orders")
async def create_order(request: Request):
    body = await request.json()
    new_id = str(100 + len(ORDERS) + 1)
    ORDERS[new_id] = {"id": new_id, **body}
    return JSONResponse(status_code=201, content={**ORDERS[new_id], "instance": INSTANCE})


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
