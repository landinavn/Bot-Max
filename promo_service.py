"""Post-delivery repeat-gift offers with durable, idempotent notification retries."""
import asyncio
import logging

from maxapi.types import CallbackButton
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder
from database import (grant_repeat_offer,pending_repeat_offers,mark_repeat_offer_sent,repeat_offers_due,claim_repeat_offer,release_repeat_offer)

logger=logging.getLogger(__name__)
REPEAT_TEXTS={
    'music_card':'🎵 Понравилась музыкальная открытка? Сделайте ещё одну для другого праздника со скидкой 20%! Цена 239 ₽ вместо 299 ₽. Скидка действует 24 часа.',
    'cartoon':'🎬 Понравилась мульт-открытка? Создайте ещё одну со скидкой 20%! Скидка действует 24 часа. Цена 472 ₽ вместо 590 ₽.',
    'photo':'📸 Вы использовали все фото из вашего пакета! Хотите сделать ещё одну фотосессию? Дарим скидку 20% на любой тариф! Скидка действует 24 часа.',
}
REPEAT_CALLBACKS={'music_card':'promo_music_card','cartoon':'promo_cartoon','photo':'promo_photo'}


async def send_repeat_offer(bot,user_id,category,delivery_key,*,admin_user_id=None):
    if user_id==admin_user_id:
        return False
    try:
        if not await grant_repeat_offer(user_id,category,delivery_key):
            return False
        for offer in await pending_repeat_offers():
            if offer['delivery_key']==delivery_key:
                return await deliver_repeat_offer(bot,offer,admin_user_id=admin_user_id)
        return True
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception('Не удалось сохранить предложение повторного подарка %s:',delivery_key)
        return False


async def deliver_repeat_offer(bot,offer,*,admin_user_id=None):
    if offer['user_id']==admin_user_id:
        return False
    try:
        if not await claim_repeat_offer(offer['delivery_key']):
            return False
        keyboard=InlineKeyboardBuilder().row(CallbackButton(text='🎁 Забрать со скидкой',
            payload=REPEAT_CALLBACKS[offer['category']])).as_markup()
        sent=await bot.send_message(user_id=offer['user_id'],text=REPEAT_TEXTS[offer['category']],attachments=[keyboard])
        if sent is None:
            raise RuntimeError('MAX не подтвердил отправку промо.')
        await mark_repeat_offer_sent(offer['delivery_key'])
        return True
    except asyncio.CancelledError:
        await asyncio.shield(release_repeat_offer(offer['delivery_key']))
        raise
    except Exception:
        logger.exception('Не удалось отправить промо %s:',offer['delivery_key'])
        await release_repeat_offer(offer['delivery_key'])
        return False


class PromoOfferWorker:
    def __init__(self,bot,*,admin_user_id=None,poll_interval=30):
        self.bot,self.admin_user_id,self.poll_interval=bot,admin_user_id,poll_interval

    async def process(self):
        for user,category,key in await repeat_offers_due(self.admin_user_id):
            if user!=self.admin_user_id:
                await grant_repeat_offer(user,category,key)
        for offer in await pending_repeat_offers():
            await deliver_repeat_offer(self.bot,offer,admin_user_id=self.admin_user_id)

    async def run(self):
        while True:
            try:
                await self.process()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception('Ошибка очереди промо повторного подарка:')
            await asyncio.sleep(self.poll_interval)
