"""Optional isolated setup; disabling studio leaves the existing bot untouched."""
from . import repository
from .settings import Settings
from .assets import Assets
from .adapters import Providers
from .router import create_router
from .seed import seed
from .worker import Worker


async def configure(bot, dispatcher, config):
    if not config.admin_user_id:
        return None
    settings = Settings.from_config(config)
    if not settings.enabled:
        return None
    await repository.init()
    project = await seed(config.admin_user_id)
    from .episode_two import prepare, prepare_jobs
    try:
        episode_two = await prepare(project,config.admin_user_id)
        await prepare_jobs(episode_two,settings)
    except ValueError:
        import logging
        logging.getLogger(__name__).exception('Не удалось подготовить серию 2; существующие серии сохранены:')
    await repository.prepare_drafts(project,settings)
    assets = Assets(settings.root,config)
    dispatcher.include_routers(create_router(config.admin_user_id,settings,assets))
    return Worker(bot,config.admin_user_id,settings,Providers(settings,config,assets),assets)
