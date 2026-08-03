from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get("/healthz", include_in_schema=False)
async def healthz() -> dict:
    """Liveness only — deliberately does not touch Firestore, so a database
    problem shows up on the routes that need it rather than failing the
    startup probe."""
    return {"status": "ok"}
