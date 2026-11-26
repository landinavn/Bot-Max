"""Manual-code MAX flow; ONLY attached to the verified-TLS long-polling dispatcher.

No public webhook, deep-link secret, user registration, credits or account creation.
The SDK event's message sender / callback user is the sole source of the MAX ID.
"""
from maxapi import F, Router
from maxapi.filters.command import Command
from maxapi.context import MemoryContext
from maxapi.context.state_machine import State, StatesGroup
from maxapi.types import MessageCreated, MessageCallback
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder
from maxapi.types import CallbackButton
from accounts.errors import AccountError
from .errors import LinkingError


class LinkingStates(StatesGroup):
    waiting_code = State()


def actor_from_event(event):
    if not isinstance(event,(MessageCreated,MessageCallback)):
        return None
    user=event.callback.user if isinstance(event,MessageCallback) else event.message.sender
    recipient=event.message.recipient if event.message else None
    if (not user or user.is_bot or not recipient or recipient.chat_type!='dialog'
            or type(user.user_id) is not int or user.user_id<=0 or event.get_ids()[1]!=user.user_id):
        return None
    return user.user_id


def create_router(service):
    router=Router()

    async def tell(event,text,buttons=()):
        from handlers import _reply
        builder=InlineKeyboardBuilder()
        for label,payload in buttons:
            builder.row(CallbackButton(text=label,payload=payload))
        await _reply(event,text,**({'attachments':[builder.as_markup()]} if buttons else {}))

    async def restore(context):
        data=await context.get_data()
        if await context.get_state()==LinkingStates.waiting_code:
            await context.set_state(data.get('web_link_previous_state'))
        await context.update_data(web_link_previous_state=None)

    async def safe_actor(event):
        actor=actor_from_event(event)
        if actor is None:
            return None
        if not await service.registered_max(actor):
            await tell(event,'Сначала откройте обычное меню бота командой /start, затем вернитесь к привязке.')
            return None
        return actor

    @router.message_created(Command('link_account'))
    async def link_account(event: MessageCreated,context: MemoryContext):
        try:
            if await safe_actor(event) is None:
                return
            previous=await context.get_state()
            if previous!=LinkingStates.waiting_code:
                await context.update_data(web_link_previous_state=previous)
            await context.set_state(LinkingStates.waiting_code)
            await tell(event,'🔗 Привязка аккаунта сайта к вашему MAX.\n\nВведите одноразовый код, показанный в вашем личном кабинете. Не вводите чужие коды и не пересылайте свой код другим. После подтверждения здесь потребуется подтверждение на сайте. Балансы и заказы не объединяются.\n\nДля выхода: /cancel_link')
        except (LinkingError,AccountError):
            await tell(event,'Привязка сейчас недоступна. Попробуйте позже; текущие заказы не изменены.')

    @router.message_created(Command('cancel_link'))
    async def cancel_input(event: MessageCreated,context: MemoryContext):
        if actor_from_event(event) is not None:
            await restore(context)
            await tell(event,'Ввод кода закрыт. Для отмены созданной операции используйте личный кабинет.', [('Главный экран','open_main_menu')])

    @router.message_created(LinkingStates.waiting_code, ~F.message.body.text.startswith('/'))
    async def link_code(event: MessageCreated,context: MemoryContext):
        try:
            actor=await safe_actor(event)
            if actor is None:
                return
            code=event.message.body.text if event.message.body else ''
            operation=await service.claim_from_max(actor,code)
            await restore(context)
            await tell(event,'Аккаунт сайта: '+operation.web_hint+'\n\nПодтвердить, что вы хотите связать этот профиль MAX с аккаунтом сайта, на котором получили код?\n\nДелайте это только если сами начали привязку. Чужой код использовать нельзя. Это не объединит ваши балансы или заказы.',
                       [('Подтвердить в MAX','web_link:confirm:'+operation.operation_id),
                        ('Отменить','web_link:cancel:'+operation.operation_id)])
        except (LinkingError,AccountError):
            await tell(event,'Код не принят: он неверный, истёк, уже использован или превышен лимит. Проверьте код в своём кабинете. Для выхода: /cancel_link')

    @router.message_callback(F.callback.payload.startswith('web_link:'))
    async def link_callback(event: MessageCallback,context: MemoryContext):
        await event.ack(notification='Проверяю привязку')
        try:
            actor=await safe_actor(event)
            if actor is None:
                return
            parts=event.callback.payload.split(':')
            if len(parts)!=3 or parts[1] not in ('confirm','cancel'):
                return
            operation=await (service.confirm_from_max(actor,parts[2]) if parts[1]=='confirm' else service.cancel_from_max(actor,parts[2]))
            messages={
                'max_confirmed':'✅ В MAX подтверждено. Вернитесь на сайт и окончательно подтвердите привязку там. До этого аккаунты не связаны.',
                'completed':'✅ MAX уже привязан к этому аккаунту сайта.',
                'cancelled':'Привязка отменена. Данные и заказы не изменены.',
                'conflict':'Привязка невозможна: уже существует другая связь. Автоматическое объединение и замена аккаунтов не выполняются.',
            }
            await tell(event,messages.get(operation.status,'Операция недоступна. Начните новую привязку на сайте.'),[('Главный экран','open_main_menu')])
        except (LinkingError,AccountError):
            await tell(event,'Операция недоступна или истекла. Начните новую привязку в своём кабинете.')
    return router
