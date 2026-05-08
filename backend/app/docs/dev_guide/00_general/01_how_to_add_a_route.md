# How to Add a New API Route

This is the canonical recipe for adding a new HTTP endpoint to ARS.
Everything else in this app follows the same pattern; if you can do this,
you can extend any module.

## Steps

### 1. Pick (or create) an endpoint file

Endpoint routers live in `backend/app/api/v1/endpoints/`. One file per
domain — e.g. `bdc.py` for BDC routes, `grid_builder.py` for grid routes.
If your work belongs to an existing domain, edit that file. Otherwise
create `<domain>.py` with this skeleton:

```python
from fastapi import APIRouter, Depends
from app.security.dependencies import get_current_user
from app.models.rbac import User
from app.schemas.common import APIResponse

router = APIRouter(prefix="/<domain>", tags=["<Domain>"])

@router.get("/hello", response_model=APIResponse)
def hello(current_user: User = Depends(get_current_user)):
    return APIResponse(success=True, message="ok", data={"hello": "world"})
```

### 2. Register the router

Open `backend/app/api/v1/router.py` and add:

```python
from app.api.v1.endpoints.<domain> import router as <domain>_router
api_router.include_router(<domain>_router)
```

### 3. Add a frontend API helper (optional but recommended)

Open `frontend/src/services/api.js` and append:

```js
export const <domain>API = {
  hello: () => api.get('/<domain>/hello'),
}
```

### 4. Test

```bash
curl -H "Authorization: Bearer $TOKEN" \
  http://localhost:8000/api/v1/<domain>/hello
```

## Conventions

- **Always** depend on `get_current_user` (or `RequirePermissions([...])`)
  unless the route is intentionally public.
- Wrap responses in `APIResponse` so the frontend's `api.js` interceptor
  handles errors uniformly.
- Long-running work goes through `app/services/upload_job_service.py`
  pattern (queue + worker thread + progress callback) — never block the
  event loop.
- Use `asyncio.to_thread` to wrap any synchronous DB call inside an
  `async def` route. Search `backend/app/services/file_upload_service.py`
  for examples.

## Where to look when something breaks

| Symptom | First place to check |
|---|---|
| 401 / 403 | `app/security/dependencies.py` and the user's `rbac_permissions` row |
| 500 with stack trace | Backend logs → `logs/app.log` |
| 405 / 404 | Did you register the router in `router.py`? |
| Slow request | Open browser DevTools → Network → Timing. If the wait is server-side, profile with `cProfile`. |
