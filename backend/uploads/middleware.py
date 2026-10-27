class MethodOverrideMiddleware:
    """tus clients behind proxies that block PATCH/DELETE send POST with X-HTTP-Method-Override."""

    ALLOWED = {"PATCH", "DELETE", "HEAD"}

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        override = request.headers.get("X-HTTP-Method-Override", "").upper()
        if request.method == "POST" and override in self.ALLOWED:
            request.method = override
        return self.get_response(request)
