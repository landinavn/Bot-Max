"""Reader-compatible emergency delivery. No generation, billing, publish or signer."""
import asyncio


async def deliver_saved(bot,reference):
    from config import Config
    from private_media import PrivateMedia,current_access,user_access,upload_for_max,MediaAccessDenied,MediaUnavailable
    config=Config.from_env()
    if not config.private_media_read_only:raise MediaAccessDenied('Emergency reader requires read-only mode')
    actor=current_access()
    if actor.role=='user' and actor.owner.namespace=='max':
        destination=int(actor.owner.identifier)
        actor=await user_access(destination)
    elif actor.role=='admin' and actor.admin_id==config.admin_user_id and actor.reason in ('support','recovery','export'):
        destination=actor.admin_id
    else:raise MediaAccessDenied('An authenticated personal recipient is required')
    store=PrivateMedia(config)
    try:metadata=await asyncio.to_thread(store.stat,reference,actor)
    finally:await asyncio.to_thread(store.close)
    family=metadata['content_type'].split('/')[0]
    kind=family if family in ('image','audio','video') else 'file'
    attachment=await upload_for_max(bot,reference,actor,kind=kind,destination_user=destination)
    sent=await bot.send_message(user_id=destination,text='Ваш сохранённый файл',attachments=[attachment])
    if sent is None:raise MediaUnavailable('MAX did not confirm retained media delivery')
    return sent
