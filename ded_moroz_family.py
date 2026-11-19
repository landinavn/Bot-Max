"""Paid family greetings; deterministic tagged text, no extra model calls."""
import json
import re
import logging
from uuid import uuid4

from maxapi.types import CallbackButton,LinkButton,InputMedia
from maxapi.enums import UploadType
from config import BASE_DIR
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder

VARIANT='family'
STUB='🎅 Поздравление Деда Мороза для нескольких детей уже в разработке! Скоро можно будет заказать одно персональное видео для всей семьи.'
logger=logging.getLogger(__name__)
EXAMPLE=BASE_DIR/'examples/ded_moroz_kids.mp4'
MAX_CHILDREN=6
MAX_WORDS=130
PHOTO_PROMPT=('📸 Отправьте одно общее фото детей — оно появится в поздравлении в новогодней рамке. '
              'Если общего фото нет, нажмите «Продолжить без фото».')


def is_family(details):
    if isinstance(details,str):details=json.loads(details)
    return details.get('variant')==VARIANT


def count(details):
    n=details.get('children_count')
    if isinstance(n,bool) or not isinstance(n,int) or not 2<=n<=MAX_CHILDREN:
        raise ValueError(f'Укажите количество детей от 2 до {MAX_CHILDREN}.')
    return n


def labels(details):
    result={}
    for n in range(1,count(details)+1):
        name=details.get(f'child_{n}_name',f'ребёнок {n}')
        for field,label in (('name','Имя'),('age','Возраст'),('gender','Пол'),('fact','Факт'),('wish','Пожелание')):
            result[f'child_{n}_{field}']=f'{n}. {name} — {label.lower()}'
    result['common']='Общий факт о детях'
    return result


def edit_buttons(builder,data,prefix):
    fields=labels(data)
    for n in range(1,count(data)+1):
        for parts in (('name',),('age','gender'),('fact','wish')):
            builder.row(*(CallbackButton(text='✏️ '+fields[f'child_{n}_{part}'],payload=prefix+f'child_{n}_{part}') for part in parts))
    builder.row(CallbackButton(text='✏️ Общий факт о детях',payload=prefix+'common'))
    return builder


def limit(field,details):
    if field=='common':return (45,7) if count(details)<4 else (30,4)
    part=field.rsplit('_',1)[-1]
    if part=='name':return 24,2
    if part in ('age','gender'):return 10,1
    return (45,6) if count(details)<4 else (28,3)


def value(field,text,details):
    if field not in labels(details):raise ValueError('Выберите имя или факт кнопкой ниже.')
    if not isinstance(text,str):raise ValueError('Введите ответ обычным текстом.')
    text=' '.join(text.split())
    if field.endswith('_fact') and text.casefold()=='пропустить':text='очень стараешься'
    chars,words=limit(field,details)
    if not text or len(text)>chars or len(text.split())>words or any(c in text for c in '[]{}<>'):
        raise ValueError(f'Введите ответ без тегов: до {chars} символов и {words} слов.')
    if field.endswith('_age'):
        if not text.isascii() or not text.isdigit() or not 0<=int(text)<=17:
            raise ValueError('Укажите возраст ребёнка числом от 0 до 17.')
        text=str(int(text))
    if field.endswith('_gender'):
        options={'мальчик':'мальчик','м':'мальчик','девочка':'девочка','д':'девочка'}
        text=options.get(text.casefold())
        if not text:raise ValueError('Напишите «мальчик» или «девочка».')
    if field.endswith('_name') and not re.fullmatch(r'[^\W\d_]+(?:[ -][^\W\d_]+)*',text):
        raise ValueError('Напишите только имя, как зовут ребёнка дома, например «Маша» или «Сашенька».')
    return text


def question(field,details):
    chars,words=limit(field,details)
    if field=='common':
        prompt=('Теперь общий факт обо всех детях. Заполните: «Я знаю, что вы …». '
                'Например: «помогаете маме», «дружите и играете вместе», «вместе занимаетесь танцами». '
                'Не перечисляйте новые имена — речь обо всех уже указанных детях.')
    else:
        _,n,part=field.split('_')
        name=details.get(f'child_{n}_name')
        heading=f'Ребёнок {n} из {count(details)}'+(' — '+name+'.' if name and part!='name' else '')+'\n\n'
        if part=='name':heading='Заполняем детей по очереди в том порядке, в котором их поздравит Дед Мороз.\n'+heading
        prompts={
            'name':'Как его или её зовут дома? Напишите одно имя, например «Машенька» или «Саша».',
            'age':'Сколько лет этому ребёнку? Напишите только число, например «7». Возраст нужен для проверки данных, в поздравлении он не перечисляется.',
            'gender':'Это мальчик или девочка? Напишите «мальчик» или «девочка», чтобы обращение звучало правильно.',
            'fact':'Один конкретный факт только об этом ребёнке. Заполните: «Я слышал, что ты …». Напишите фразу целиком после «ты», без имени. Например: «любишь рисовать», «научилась плавать», «собираешь конструкторы». Не пишите факты обо всех детях в одном сообщении. Если конкретного факта нет, напишите «пропустить» — будет нейтральное «ты очень стараешься».',
            'wish':'Одно пожелание только этому ребёнку. Заполните: «Желаю тебе …». Напишите фразу после «тебе». Например: «побед в футболе», «интересных книг», «ярких рисунков». Не начинайте с «желаю» и не добавляйте имя. Постарайтесь выбрать другое пожелание, чем для остальных детей.',
        }
        prompt=heading+prompts[part]
    if field=='common' or field.endswith(('_fact','_wish')):
        prompt+=f'\n\nДо {chars} символов и {words} слов.'
    return prompt


def joined(names):
    return ', '.join(names[:-1])+' и '+names[-1]


def tagged_text(details):
    fields={field:value(field,details.get(field),details) for field in labels(details)}
    n=count(details);names=[fields[f'child_{i}_name'] for i in range(1,n+1)]
    opening=('[deep voice] Охо-хо-хо! [raspy] Здравствуйте, мои дорогие [drawls] '+joined(names)+'! '
             '[warm] Это я, Дедушка Мороз! [slightly excited] ')
    if n==2:opening+='Из Великого Устюга я вижу двух замечательных ребят! '
    elif n==3:opening+='Я нашёл на карте волшебства ваши счастливые имена! '
    else:opening+='У меня в списке особенная семья! '
    blocks=[]
    for i,name in enumerate(names,1):
        praise=(('Умница!' if fields[f'child_{i}_gender']=='девочка' else 'Молодец!'),
                'Здорово!','Прекрасно!','Замечательно!','Отлично!','Вот это да!')[i-1]
        blocks.append(f'[raspy] {name}, '+('я слышал, что ' if n<4 else '')+
                      f'ты {fields[f"child_{i}_fact"]}. [proud] {praise} '
                      f'[warm] Желаю тебе [drawls] {fields[f"child_{i}_wish"]}. [pause] ')
    ending=('[reassuring] Я знаю, что вы '+fields['common']+'. '
            '[serious] Берегите друг друга! [warm] Пусть в доме будет много тепла и добрых чудес! '
            '[deep voice] С Новым годом, [drawls] '+joined(names)+'! [warm] До [drawls] встречи!')
    text=opening+''.join(blocks)+ending
    from heygen_tts import display_text
    if len(display_text(text).split())>MAX_WORDS:
        raise ValueError('Поздравление длиннее 130 слов. Сократите факты или пожелания перед озвучкой.')
    return text


def photo_keyboard(key):
    return InlineKeyboardBuilder().row(CallbackButton(text='Продолжить без фото',payload='dm_family_no_photo:'+key)).as_markup()


def description():
    from ded_moroz_sales import family_price_text
    return ('🎅 Одно поздравление для всей семьи!\n\n'
        '👧👦 Для 2–6 детей — каждого Дед Мороз поздравит по имени.\n'
        '📝 Заполним детей по очереди: имя, возраст, пол, один факт и личное пожелание.\n'
        '🤝 В конце добавим общий факт о детях.\n'
        '📸 Общее фото можно добавить или пропустить.\n'
        '🎧 Перед видео вы прослушаете озвучку. Можно один раз исправить данные и выбрать первый или второй вариант.\n'
        '💳 Сначала оплата, затем заполнение данных и создание поздравления.\n\n'
        +family_price_text()+'\n\n👇 Нажмите «Начать», чтобы выбрать количество детей и получить счёт. '
        'Сохранённые поздравления можно продолжить отдельными кнопками.')


async def check_example():
    from media_service import audio_duration
    if not EXAMPLE.is_file():raise ValueError('Добавьте examples/ded_moroz_kids.mp4 — видео-пример семейного поздравления.')
    seconds=await audio_duration(EXAMPLE)
    if not 0<seconds<=70 or EXAMPLE.stat().st_size>100*1024*1024:
        raise ValueError('Пример ded_moroz_kids.mp4 должен быть MP4 до 70 секунд и 100 МБ.')


async def example_attachment(bot):
    await check_example()
    result=await bot.upload_media(InputMedia(path=str(EXAMPLE),type=UploadType.VIDEO))
    if getattr(result,'type',None)!='video':raise ValueError('MAX не вернул видео-пример.')
    return result


async def count_buttons(context):
    from handlers import PosterStates
    await context.clear();await context.set_state(PosterStates.waiting_for_mode)
    b=InlineKeyboardBuilder();token=uuid4().hex
    for label,n in (('2 ребёнка','2'),('3 ребёнка','3'),('4 и больше','more')):
        b.row(CallbackButton(text=label,payload='dm_family_count:'+n+':'+token))
    return b


async def entry(event,context,admin):
    from ded_moroz import read,tell
    from ded_moroz_sales import allowed,enabled,family_price_at
    from handlers import PosterStates
    user=event.get_ids()[1]
    await context.clear();await context.set_state(PosterStates.waiting_for_mode)
    try:
        attachment=await example_attachment(event.bot)
        await tell(event,'🎬 Пример поздравления для нескольких детей:',attachments=[attachment])
    except Exception as exc:
        logger.warning('Не удалось показать семейный пример: %s',type(exc).__name__)
        await tell(event,'🎬 Пример семейного поздравления скоро появится.')
    b=await count_buttons(context) if admin is not None and user==admin else InlineKeyboardBuilder()
    if user!=admin and enabled() and family_price_at() is not None:b.row(CallbackButton(text='🎅 Начать',payload='dm_family_start'))
    active=await read("SELECT * FROM ded_moroz_orders WHERE user_id=? AND json_extract(details,'$.variant')='family' AND status NOT IN ('done','cancelled') ORDER BY rowid DESC LIMIT 5",(user,),all_rows=True)
    for row in active:
        if await allowed(row,admin):b.row(CallbackButton(text='Продолжить семейное · '+row['id'][:8],payload='dm_open:'+row['id']))
    b.row(CallbackButton(text='🎬 Готовые поздравления',payload='dm_last'))
    b.row(CallbackButton(text='🏠 Главный экран',payload='back_to_main'))
    text=description()
    if admin is not None and user==admin:
        text+='\n\n🛠 Для администратора: выбор количества открывает тестовый заказ без оплаты.'
    if not enabled() and user!=admin:text='Семейное поздравление временно недоступно для новых покупок.'
    await tell(event,text,attachments=[b.as_markup()])


async def checkout(event,context,admin,number,payments):
    from ded_moroz import tell,Settings,read
    from ded_moroz_sales import CHECKOUT_LOCKS,checkout as make_invoice,enabled
    import asyncio
    if admin is None or not enabled():raise ValueError('Покупка семейного поздравления временно недоступна.')
    if payments is None:raise ValueError('Оплата временно недоступна. Попробуйте позже.')
    settings=Settings.from_config(payments.config)
    avatar=await read('SELECT look_id FROM ded_moroz_avatar WHERE singleton=1')
    try:
        settings.ready(payments.config,stage='audio')
        settings.ready(payments.config,stage='video',avatar_known=bool(avatar and avatar['look_id']))
    except ValueError:
        raise ValueError('Мастерская временно недоступна. Счёт не создан, попробуйте позже.') from None
    user=event.get_ids()[1]
    lock=CHECKOUT_LOCKS.setdefault(user,asyncio.Lock())
    async with lock:
        invoice=await make_invoice(user,variant='family',children_count=number)
        link=await payments.create_payment_link(user,order_id=invoice['order_id'],amount=invoice['amount_kopecks']//100,description=invoice['product_name'])
    b=InlineKeyboardBuilder().row(LinkButton(text=f"💳 Оплатить {invoice['amount_kopecks']//100} ₽",url=link))
    b.row(CallbackButton(text='✅ Я оплатил(а), продолжить',payload='dm_family_paid:'+invoice['order_id']))
    b.row(CallbackButton(text='🏠 Главный экран',payload='back_to_main'))
    await tell(event,f"🎅 Семейное поздравление для {number} детей — {invoice['amount_kopecks']//100} ₽.\n\nПосле оплаты вернитесь в бот: начнём с имени первого ребёнка.",attachments=[b.as_markup()])


async def review(event,context,row):
    from ded_moroz import tell
    from handlers import PosterStates
    data=json.loads(row['details']);b=InlineKeyboardBuilder()
    lines=['Проверьте данные перед озвучкой. Дети будут поздравлены именно в этом порядке:']
    for i in range(1,count(data)+1):
        lines.append(f'\nРебёнок {i} — {data[f"child_{i}_name"]}, {data[f"child_{i}_age"]} лет, {data[f"child_{i}_gender"]}\nФакт: {data[f"child_{i}_fact"]}\nПожелание: {data[f"child_{i}_wish"]}')
    lines.append('\nОбщий факт: '+data['common'])
    wishes={}
    for i in range(1,count(data)+1):
        wishes.setdefault(data[f'child_{i}_wish'].strip().casefold(),[]).append(data[f'child_{i}_name'])
    for names in wishes.values():
        if len(names)>1:lines.append('\nПожелание повторяется: '+joined(names)+'. Можно сделать его индивидуальным кнопками ниже.')
    edit_buttons(b,data,f'dm_family_field:{row["id"]}:')
    b.row(CallbackButton(text='✅ Всё верно, далее',payload='dm_family_confirm:'+row['id']))
    b.row(CallbackButton(text='⬅️ Главный экран',payload='back_to_main'))
    await context.clear();await context.update_data(dm_order=row['id']);await context.set_state(PosterStates.ded_moroz_input)
    await tell(event,'\n'.join(lines)+'\n\nНажмите на поле, чтобы исправить его. Озвучка ещё не запускается.',attachments=[b.as_markup()])


async def resume_input(event,context,row):
    from ded_moroz import tell
    from handlers import PosterStates
    data=json.loads(row['details'])
    await context.clear();await context.update_data(dm_order=row['id'])
    field=row.get('edit_field') or next((f for f in labels(data) if f not in data),None)
    if field:
        await context.set_state(PosterStates.ded_moroz_input)
        await tell(event,question(field,data))
    elif not data.get('family_confirmed'):
        await review(event,context,row)
    else:
        await context.set_state(PosterStates.ded_moroz_photo)
        await tell(event,PHOTO_PROMPT,attachments=[photo_keyboard(row['id'])])


async def collect_input(event,context,row):
    import aiosqlite
    import database
    from ded_moroz import get,tell
    data=json.loads(row['details']);field=row.get('edit_field') or next((f for f in labels(data) if f not in data),None)
    if not field:
        await review(event,context,row);return
    data[field]=value(field,event.message.body.text,data);data.pop('family_confirmed',None)
    async with aiosqlite.connect(database.DATABASE_PATH,timeout=30) as db:
        cur=await db.execute("UPDATE ded_moroz_orders SET details=?,edit_field=NULL WHERE id=? AND user_id=? AND status='input' AND details=? AND edit_field IS ?",
                             (json.dumps(data,ensure_ascii=False),row['id'],row['user_id'],row['details'],row['edit_field']))
        await db.commit()
    if cur.rowcount!=1:
        await tell(event,'Данные уже изменились. Откройте семейное поздравление снова.');return
    await resume_input(event,context,await get(row['id']))


async def queue_audio(row,*,photo_path=None,photo_url=None):
    import aiosqlite
    import database
    data=json.loads(row['details'])
    if not data.get('family_confirmed'):raise ValueError('Сначала проверьте и подтвердите данные всех детей.')
    text=tagged_text(data)
    async with aiosqlite.connect(database.DATABASE_PATH,timeout=30) as db:
        cur=await db.execute("UPDATE ded_moroz_orders SET photo_path=?,photo_url=?,text=?,status='audio_queued' WHERE id=? AND user_id=? AND status='input' AND details=? AND edit_field IS NULL",
                             (photo_path,photo_url,text,row['id'],row['user_id'],row['details']))
        await db.commit();return cur.rowcount==1


async def callback(event,context,admin,payments=None):
    from ded_moroz import begin,enter,get,tell,update,Settings,AUDIO_GENERATING
    from config import Config
    from handlers import PosterStates
    import aiosqlite
    import database
    user=event.get_ids()[1]
    from ded_moroz_sales import allowed,enabled
    action,key,*extra=event.callback.payload.split(':')
    if action=='dm_family_count':
        if len(extra)>1 or (extra and not re.fullmatch(r'[a-f0-9]{32}',extra[0])):raise ValueError('Откройте семейное поздравление заново.')
        token=extra[0] if extra else None
        if key=='more':
            b=InlineKeyboardBuilder()
            for n in range(4,MAX_CHILDREN+1):b.row(CallbackButton(text=str(n)+' детей',payload='dm_family_count:'+str(n)+(':'+token if token else '')))
            await tell(event,'Сколько детей поздравим? Для каждого будет короткий факт и отдельное пожелание.',attachments=[b.as_markup()]);return
        if key not in tuple(map(str,range(2,MAX_CHILDREN+1))):raise ValueError('Выберите количество детей кнопкой.')
        if admin is None or user!=admin:
            await checkout(event,context,admin,int(key),payments);return
        row=await begin(user,details={'variant':VARIANT,'children_count':int(key)},new=bool(token),creation_key='family:'+token if token else None)
        if not is_family(row['details']):
            await entry(event,context,admin);return
        await enter(event,context,admin,key=row['id']);return
    if action=='dm_family_paid':
        from ded_moroz_sales import payment
        invoice=await payment(key)
        if not invoice or invoice['user_id']!=user or invoice.get('variant')!='family':
            await tell(event,'Счёт семейного поздравления не найден.');return
        await enter(event,context,admin,payment_key=key);return
    row=await get(key)
    if not row or row['user_id']!=user or not is_family(row['details']) or row['status']!='input' or not await allowed(row,admin):
        await tell(event,'Эта кнопка уже использована. Откройте семейное поздравление снова.');return
    data=json.loads(row['details'])
    if action=='dm_family_field' and len(extra)==1:
        if extra[0] not in labels(data):return
        # Persist selected field so returning from the main menu/restart resumes the question.
        async with aiosqlite.connect(database.DATABASE_PATH,timeout=30) as db:
            cur=await db.execute("UPDATE ded_moroz_orders SET edit_field=? WHERE id=? AND user_id=? AND status='input' AND details=?",(extra[0],key,user,row['details']));await db.commit()
        if cur.rowcount:await resume_input(event,context,await get(key))
    elif action=='dm_family_confirm' and not extra:
        if row['edit_field']:raise ValueError('Сначала завершите изменение выбранного поля.')
        tagged_text(data)  # Validate the entire script before accepting the data.
        data['family_confirmed']=True
        async with aiosqlite.connect(database.DATABASE_PATH,timeout=30) as db:
            cur=await db.execute("UPDATE ded_moroz_orders SET details=? WHERE id=? AND user_id=? AND status='input' AND details=? AND edit_field IS NULL",(json.dumps(data,ensure_ascii=False),key,user,row['details']));await db.commit()
        if cur.rowcount:await resume_input(event,context,await get(key))
    elif action=='dm_family_no_photo' and not extra:
        config=Config.from_env();Settings.from_config(config).ready(config,stage='audio')
        if await queue_audio(row):
            await context.clear();await tell(event,AUDIO_GENERATING)


def title_options(details):
    from ded_moroz_intro import genitive_name
    names=[details[f'child_{i}_name'] for i in range(1,count(details)+1)]
    declined=[genitive_name(name) for name in names]
    return joined(names),({'name_genitive':joined(declined)} if all(declined) and len(joined(declined))<=120 else {})
