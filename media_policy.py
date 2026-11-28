"""Immutable product-specific storage decisions. No finance or provider mutations."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass,field
import functools
import inspect
import os
from pathlib import Path
import time
from uuid import uuid4

PRODUCTS=frozenset({'postcards','vision_board','photosession','song','musical_card','animation','cartoon','ded_moroz','multstudio','custom_order'})
SUPPORTED=frozenset({'postcards','vision_board','photosession','song','musical_card','custom_order'})
_POLICY=ContextVar('media_product_policy',default=None)


@dataclass(frozen=True)
class Policy:
    product:str
    mode:str
    owner_namespace:str
    owner_id:str=field(repr=False)
    operation_id:str=field(repr=False)

    def __post_init__(self):
        from private_media import Owner,_ID,MediaAccessDenied
        Owner(self.owner_namespace,self.owner_id)
        if self.product not in PRODUCTS or self.mode not in ('private','legacy') or not _ID.fullmatch(self.operation_id):
            raise MediaAccessDenied('Invalid media policy')


def current_policy():return _POLICY.get()


@contextmanager
def policy_scope(policy):
    token=_POLICY.set(policy)
    try:yield policy
    finally:_POLICY.reset(token)


def private_enabled(config):
    policy=current_policy()
    return policy.mode=='private' if policy else getattr(config,'private_media_enabled',False)


def configured():
    from config import BASE_DIR
    registry=Path(os.environ.get('PRIVATE_MEDIA_REGISTRY',str(BASE_DIR/'private_media.sqlite3')))
    return (os.environ.get('PRIVATE_MEDIA_ENABLED','false').strip().lower()=='true'
        or bool(os.environ.get('PRIVATE_MEDIA_PRODUCTS','').strip())
        or registry.exists() or Path(str(registry)+'.required').exists())


def contains_private(value,depth=0):
    from private_media import is_private,MediaError
    import json
    if depth>32:raise MediaError('Media snapshot too deep')
    if is_private(value):return True
    if isinstance(value,dict):return any(contains_private(v,depth+1) for v in value.values())
    if isinstance(value,list):return any(contains_private(v,depth+1) for v in value)
    if isinstance(value,str) and value[:1] in ('{','['):
        try:return contains_private(json.loads(value),depth+1)
        except ValueError:return False
    return False


def private_references(value,depth=0):
    from private_media import is_private,MediaError
    import json
    if depth>32:raise MediaError('Media snapshot too deep')
    if is_private(value):return {value}
    values=value.values() if isinstance(value,dict) else value if isinstance(value,list) else ()
    refs=set()
    for v in values:refs.update(private_references(v,depth+1))
    if isinstance(value,str) and value[:1] in ('{','['):
        try:refs.update(private_references(json.loads(value),depth+1))
        except ValueError:pass
    return refs


class PolicyStore:
    def __init__(self,config):self.config=config

    def load(self,owner,operation):
        from media_registry import registry_connection
        path=Path(self.config.private_media_registry)
        if not path.exists():
            if Path(str(path)+'.required').exists():
                from private_media import MediaUnavailable
                raise MediaUnavailable('Required private registry is missing')
            return None
        with registry_connection(self.config.private_media_registry) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='media_policies'").fetchone():return None
            row=db.execute('SELECT * FROM media_policies WHERE owner_namespace=? AND owner_id=? AND operation_id=?',
                (owner.namespace,owner.identifier,operation)).fetchone()
            if row:return Policy(row['product'],row['mode'],owner.namespace,owner.identifier,operation)

    def bind(self,policy):
        from media_registry import registry_connection
        from private_media import MediaAccessDenied
        with registry_connection(self.config.private_media_registry,write=True) as db:
            db.execute('INSERT OR IGNORE INTO media_policies VALUES(?,?,?,?,?,?)',
                (policy.owner_namespace,policy.owner_id,policy.operation_id,policy.product,policy.mode,int(time.time())))
            row=db.execute('SELECT product,mode FROM media_policies WHERE owner_namespace=? AND owner_id=? AND operation_id=?',
                (policy.owner_namespace,policy.owner_id,policy.operation_id)).fetchone()
            if tuple(row)!=(policy.product,policy.mode):raise MediaAccessDenied('Immutable media policy conflict')
        return policy


def select_policy(config,owner,product,operation,*,existing=False,private_required=False):
    from private_media import MediaAccessDenied
    if product not in PRODUCTS:raise MediaAccessDenied('Unknown media product')
    saved=PolicyStore(config).load(owner,operation)
    if saved:
        if saved.product!=product:raise MediaAccessDenied('Media product mismatch')
        if private_required and saved.mode!='private':raise MediaAccessDenied('Private dependency cannot use a legacy policy')
        return saved
    parent=current_policy()
    if existing:mode='private' if private_required else 'legacy'
    elif parent and parent.product==product and (parent.owner_namespace,parent.owner_id)==(owner.namespace,owner.identifier):mode=parent.mode
    else:
        selected=getattr(config,'private_media_enabled',False) or product in getattr(config,'private_media_products',())
        pilot=getattr(config,'private_media_pilot_user_id',None)
        if pilot is not None:selected=selected and owner.namespace=='max' and owner.identifier==str(pilot)
        mode='private' if selected else 'legacy'
    return Policy(product,mode,owner.namespace,owner.identifier,operation)


async def ensure_available(policy,config,*,check_storage=True):
    from private_media import MediaProviderBlocked,MediaUnavailable,PrivateMedia
    if policy.mode!='private':return
    if policy.product not in SUPPORTED:raise MediaProviderBlocked('Создание этого подарка временно недоступно. Оплата не требуется; для оплаченного заказа обратитесь в поддержку.')
    if getattr(config,'private_media_read_only',False):raise MediaUnavailable('Создание временно приостановлено. Сохранённые материалы доступны; для оплаченного заказа обратитесь в поддержку.')
    if not getattr(config,'private_media_storage_verified',False):raise MediaUnavailable('Создание временно недоступно. Оплата не требуется; для оплаченного заказа обратитесь в поддержку.')
    if check_storage:
        import asyncio
        store=PrivateMedia(config)
        try:await asyncio.to_thread(store.preflight)
        finally:await asyncio.to_thread(store.close)


async def bind_operation(product,user_id,operation,*,existing=False,private_required=False,ready=True):
    from config import Config
    from private_media import Owner
    import asyncio
    config=Config.from_env();owner=Owner('max',str(user_id))
    if private_required and not Path(config.private_media_registry).exists():
        from private_media import MediaUnavailable
        raise MediaUnavailable('Private registry required for retained inputs')
    policy=await asyncio.to_thread(select_policy,config,owner,product,operation,existing=existing,private_required=private_required)
    if ready:await ensure_available(policy,config)
    await asyncio.to_thread(PolicyStore(config).bind,policy)
    return policy


async def restore_operation(product,user_id,operation,refs=(),*,parent=None):
    import asyncio
    from config import Config
    from private_media import Owner,is_private
    from media_registry import registry_connection
    config=Config.from_env();owner=Owner('max',str(user_id))
    def restore():
        required=any(is_private(r) for r in refs)
        if Path(config.private_media_registry).exists():
            with registry_connection(config.private_media_registry) as db:
                required=required or db.execute('SELECT 1 FROM media_orders WHERE owner_namespace=? AND owner_id=? AND order_id=?',
                    (owner.namespace,owner.identifier,operation)).fetchone() is not None
        saved=PolicyStore(config).load(owner,operation)
        if saved:return select_policy(config,owner,product,operation,existing=True,private_required=required)
        ancestor=PolicyStore(config).load(owner,parent) if parent else None
        if ancestor:
            if ancestor.product!=product:raise ValueError('Media parent product mismatch')
            if required and ancestor.mode!='private':
                from private_media import MediaAccessDenied
                raise MediaAccessDenied('Private dependency cannot inherit a legacy parent')
            policy=Policy(product,ancestor.mode,owner.namespace,owner.identifier,operation)
        else:policy=select_policy(config,owner,product,operation,existing=True,private_required=required)
        return PolicyStore(config).bind(policy)
    return await asyncio.to_thread(restore)


def checkout_policy(product,*,user='user_id',operation='order_id',getter=None):
    """Guard before business SQL; immutable external metadata, no price/state edits."""
    def decorate(function):
        signature=inspect.signature(function)
        @functools.wraps(function)
        async def wrapped(*args,**kwargs):
            if not configured():return await function(*args,**kwargs)
            values=signature.bind(*args,**kwargs).arguments
            uid=values[user];key=values.get(operation)
            chosen=product or (current_policy().product if current_policy() else 'postcards')
            old=None
            if getter and key:
                import database
                old=await getattr(database,getter)(key)
            key=key or 'operation-'+uuid4().hex
            policy=await bind_operation(chosen,uid,key,existing=bool(old),private_required=contains_private(old))
            from private_media import is_private,MediaAccessDenied
            if policy.mode=='legacy' and any(is_private(v) for v in values.values()):
                raise MediaAccessDenied('Private inputs require a private product policy')
            with policy_scope(policy):result=await function(*args,**kwargs)
            if isinstance(result,dict) and result.get('order_id') and result['order_id']!=key:
                # A reused pending invoice is pre-existing; never switch its mode.
                from config import Config
                from private_media import Owner
                import asyncio
                config=Config.from_env();owner=Owner('max',str(uid))
                saved=await asyncio.to_thread(PolicyStore(config).load,owner,result['order_id'])
                if saved is None:
                    await asyncio.to_thread(PolicyStore(config).bind,Policy(chosen,'legacy',owner.namespace,owner.identifier,result['order_id']))
            return result
        return wrapped
    return decorate


async def guard_invoice_order(key,user_id=None):
    if not configured():return
    import database
    from config import Config
    from private_media import MediaAccessDenied
    from ded_moroz_sales import payment
    routes=((('audio-',),'song',database.get_audio_order),
            (('music_','promo_music_card_'),'musical_card',database.get_musical_order),
            (('cartoon_',),'cartoon',database.get_cartoon_order),
            (('photo_','promo_photo_'),'photosession',database.get_photo_order),
            (('animate_',),'animation',database.get_animation_order),
            (('dm_',),'ded_moroz',payment),
            (('max-',),'postcards',database.get_payment_order))
    for prefixes,product,getter in routes:
        if not key or not key.startswith(prefixes):continue
        row=await getter(key)
        if row is None:raise MediaAccessDenied('Invoice order not found')
        row=dict(row)
        if user_id is not None and row['user_id']!=user_id:raise MediaAccessDenied('Invoice owner mismatch')
        # General credits may have been purchased in a vision-board session.
        from private_media import Owner
        config=Config.from_env()
        import asyncio
        saved=await asyncio.to_thread(PolicyStore(config).load,Owner('max',str(row['user_id'])),key)
        if key.startswith('max-') and saved:product=saved.product
        policy=await bind_operation(product,row['user_id'],key,existing=True)
        await ensure_available(policy,config)
        return
    if current_policy():await ensure_available(current_policy(),Config.from_env())


def invoice_guard(function):
    signature=inspect.signature(function)
    @functools.wraps(function)
    async def wrapped(*args,**kwargs):
        if configured():
            values=signature.bind(*args,**kwargs).arguments
            key=values.get('order_id')
            if key:
                if function.__name__=='create_audio_payment_link':
                    import database
                    if await database.get_audio_order(key) is None:
                        await bind_operation('song',values['user_id'],key)
                    else:await guard_invoice_order(key,values.get('user_id'))
                else:await guard_invoice_order(key,values.get('user_id'))
            elif current_policy():
                from config import Config
                await ensure_available(current_policy(),Config.from_env())
        return await function(*args,**kwargs)
    return wrapped


def generation_scope(product):
    """A root service call without a handler scope still selects its own product."""
    def decorate(function):
        @functools.wraps(function)
        async def wrapped(*args,**kwargs):
            if not configured() or current_policy() is not None:return await function(*args,**kwargs)
            import hashlib,json
            from private_media import current_access,MediaAccessDenied
            actor=current_access()
            if actor.owner is None or actor.owner.namespace!='max':raise MediaAccessDenied('Generation requires an authenticated owner')
            signature=hashlib.sha256(json.dumps([function.__name__,args,kwargs],sort_keys=True,default=str).encode()).hexdigest()
            operation=actor.order_id or 'generation:'+signature
            policy=await bind_operation(product,int(actor.owner.identifier),operation,ready=True)
            if policy.mode=='legacy' and contains_private([list(args),kwargs]):raise MediaAccessDenied('Private input cannot use a legacy product')
            with policy_scope(policy):return await function(*args,**kwargs)
        return wrapped
    return decorate


async def ded_action_policy(key,user,*,ready=True):
    from ded_moroz import get
    from private_media import MediaAccessDenied
    from config import Config
    row=await get(key)
    if row is None or row['user_id']!=user:raise MediaAccessDenied('Order owner mismatch')
    selected=await restore_operation('ded_moroz',user,'ded_moroz:'+key,private_references(row),parent=row.get('payment_order_id'))
    if ready:await ensure_available(selected,Config.from_env())
    return selected


def ded_action(function):
    @functools.wraps(function)
    async def wrapped(key,user,*args,**kwargs):
        if not configured():return await function(key,user,*args,**kwargs)
        selected=await ded_action_policy(key,user)
        with policy_scope(selected):return await function(key,user,*args,**kwargs)
    return wrapped


def studio_action(function):
    @functools.wraps(function)
    async def wrapped(key,actor,*args,**kwargs):
        if not configured():return await function(key,actor,*args,**kwargs)
        from multstudio import repository as repo
        from private_media import MediaAccessDenied
        from config import Config
        row=await repo.media_job_owner(key)
        if row is None or row['owner_id']!=actor:raise MediaAccessDenied('Studio job owner mismatch')
        selected=await restore_operation('multstudio',actor,'studio:'+key,private_references(row))
        await ensure_available(selected,Config.from_env())
        with policy_scope(selected):return await function(key,actor,*args,**kwargs)
    return wrapped


def infer_product(data,payload=''):
    if payload.startswith(('cartoon_','mother_')) or data.get('cartoon_order_id') or data.get('cartoon_theme'):return 'cartoon'
    if payload.startswith(('dm_','ded_moroz')) or data.get('dm_order'):return 'ded_moroz'
    if payload.startswith(('studio_','multstudio')):return 'multstudio'
    if payload.startswith('custom_') or data.get('custom_materials') is not None:return 'custom_order'
    if payload.startswith(('animate_','animation_')):return 'animation'
    if data.get('music_prepaid_id') or data.get('video_flow'):return 'musical_card'
    if data.get('audio_format') or payload.startswith('audio_') or data.get('song_review_flow')=='audio':return 'song'
    mode=data.get('mode','')
    if 'photo' in mode or data.get('photosession_session_id') or data.get('photo_order_id'):return 'photosession'
    if mode=='mode_vision':return 'vision_board'
    if mode:return 'postcards'
    return None


async def session_policy(context,user_id,payload=''):
    data=await context.get_data()
    product=infer_product(data,payload)
    if not product:return None
    if product=='ded_moroz' and data.get('dm_order'):
        selected=await ded_action_policy(data['dm_order'],user_id,ready=False)
        await context.update_data(media_operation_id=selected.operation_id,media_product=product,media_storage=selected.mode)
        return selected
    id_fields={'cartoon':'cartoon_order_id','musical_card':'music_prepaid_id','song':'audio_checkout_id','photosession':'photo_order_id'}
    order=data.get(id_fields.get(product,''))
    key=order or (data.get('media_operation_id') if data.get('media_product')==product else None) or 'session-'+uuid4().hex
    policy=(await restore_operation(product,user_id,key,private_references(data)) if order
            else await bind_operation(product,user_id,key,ready=False))
    await context.update_data(media_operation_id=key,media_product=product,media_storage=policy.mode)
    return policy
