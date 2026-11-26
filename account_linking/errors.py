class LinkingError(Exception):
    code = 'link_denied'
    status = 400

    def __init__(self):
        # Constant diagnostics only: no codes, identities, tokens or accounts.
        super().__init__(self.code)


class InvalidLink(LinkingError):
    pass


class FreshAuthRequired(LinkingError):
    code = 'fresh_auth_required'
    status = 403


class LinkingLimited(LinkingError):
    code = 'link_rate_limited'
    status = 429


class LinkingUnavailable(LinkingError):
    code = 'link_unavailable'
    status = 503
