from rest_framework import status
from rest_framework.exceptions import APIException
from rest_framework.views import exception_handler


class ApiError(APIException):
    """Domain error rendered as {"error": {"code", "message", "details"}}."""

    status_code = status.HTTP_400_BAD_REQUEST
    default_code = "bad_request"

    def __init__(self, code, message, status_code=None, details=None):
        super().__init__(detail=message, code=code)
        if status_code:
            self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details


class ConflictError(ApiError):
    def __init__(self, code, message, details=None):
        super().__init__(code, message, status.HTTP_409_CONFLICT, details)


def api_exception_handler(exc, context):
    response = exception_handler(exc, context)
    if response is None:
        return None
    if isinstance(exc, ApiError):
        response.data = {"error": {"code": exc.code, "message": exc.message, "details": exc.details}}
        return response
    data = response.data
    code = getattr(exc, "default_code", "error")
    message = "Request failed"
    details = None
    if isinstance(data, dict) and "detail" in data:
        message = str(data["detail"])
    elif isinstance(data, (dict, list)):
        message = "Validation failed" if response.status_code == 400 else message
        details = data
    response.data = {"error": {"code": code, "message": message, "details": details}}
    return response
