from baileys.foundation.node_runtime import JSError, JSObject

STATUS_TEXT = JSObject({
    400: 'Bad Request',
    401: 'Unauthorized',
    403: 'Forbidden',
    404: 'Not Found',
    408: 'Request Time-out',
    409: 'Conflict',
    411: 'Length Required',
    428: 'Precondition Required',
    440: 'Login Timeout',
    500: 'Internal Server Error',
    501: 'Not Implemented',
    503: 'Service Unavailable',
    515: 'Stream Error Code'
})


class Boom(JSError):
    def __init__(self, message=None, options=None):
        if options is None:
            options = JSObject({})
        statusCode = options.get('statusCode') or 500
        super().__init__(message or STATUS_TEXT[statusCode] or 'Internal Server Error')
        self.name = 'Boom'

        self.isBoom = True
        self.isServer = statusCode >= 500
        self.data = options.get('data') if options.get('data') is not None else None
        self.output = JSObject({
            'statusCode': statusCode,
            'payload': JSObject({
                'statusCode': statusCode,
                'error': STATUS_TEXT[statusCode] or 'Error',
                'message': self.message
            }),
            'headers': JSObject({})
        })


def isBoom(err):
    return isinstance(err, Exception) and bool(getattr(err, 'isBoom', False))
