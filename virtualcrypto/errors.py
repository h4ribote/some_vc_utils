class VirtualCryptoException(Exception):
    pass


class MissingScope(VirtualCryptoException):
    pass


class HTTPException(VirtualCryptoException):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class BadRequest(HTTPException):
    pass


class NotFound(HTTPException):
    pass
