"""
Mock Users Backend Service
===========================
A minimal FastAPI service that simulates a real users microservice.
Used in docker-compose for integration testing of the gateway.
"""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import uvicorn, os

PORT = int(os.getenv("PORT", 9001))
INSTANCE = os.getenv("INSTANCE", "users-1")

app = FastAPI(title=f"Mock Users Service ({INSTANCE})")

USERS = {
    "1": {"id": "1", "name": "Alice", "email": "alice@example.com"},
    "2": {"id": "2", "name": "Bob",   "email": "bob@example.com"},
}


@app.get("/health")
async def health():
    return {"status": "ok", "service": "users", "instance": INSTANCE}


@app.get("/users")
async def list_users():
    return {"users": list(USERS.values()), "instance": INSTANCE}


@app.get("/users/{user_id}")
async def get_user(user_id: str):
    if user_id not in USERS:
        return JSONResponse(status_code=404, content={"error": "User not found"})
    return {**USERS[user_id], "instance": INSTANCE}


@app.post("/users")
async def create_user(request: Request):
    body = await request.json()
    new_id = str(len(USERS) + 1)
    USERS[new_id] = {"id": new_id, **body}
    return JSONResponse(status_code=201, content={**USERS[new_id], "instance": INSTANCE})


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
