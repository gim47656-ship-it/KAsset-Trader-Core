"""FastAPI installation for the Android compatibility facade."""

from fastapi import FastAPI, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.extensions.kasset.api.errors import MobileApiError
from app.extensions.kasset.api.paths import is_android_compat_path
from app.extensions.kasset.api.router import public_router, router


def install_android_compat_api(app: FastAPI) -> None:
    """Install the isolated Android contract without changing existing APIs."""

    app.include_router(public_router)
    app.include_router(router)

    @app.exception_handler(MobileApiError)
    async def mobile_api_error_handler(
        request: Request, exc: MobileApiError
    ) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=exc.response_body())

    @app.exception_handler(RequestValidationError)
    async def mobile_validation_error_handler(
        request: Request, exc: RequestValidationError
    ):
        if not is_android_compat_path(request.url.path):
            return await request_validation_exception_handler(request, exc)
        errors = [
            {
                "field": ".".join(str(part) for part in item["loc"] if part != "body"),
                "message": item["msg"],
            }
            for item in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "VALIDATION_ERROR",
                    "message": "입력값을 확인해 주세요.",
                    "details": {"errors": errors},
                }
            },
        )
