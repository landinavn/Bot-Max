"""Optional bot wiring. Disabled path imports no adapter and opens no database."""


def configure(dispatcher,config):
    if not config.max_linking_enabled:
        return None
    import os
    from database import DATABASE_PATH
    from .service import LinkingService
    from .settings import LinkingSettings
    from .max_adapter import create_router
    service=LinkingService(DATABASE_PATH,LinkingSettings(secret=os.environ['ACCOUNT_LINKING_SECRET'].encode()))
    dispatcher.include_routers(create_router(service))
    return service
