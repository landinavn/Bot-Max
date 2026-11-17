"""Admin pilot for family greetings; deterministic tagged text, no extra model calls."""
import json
import re

from maxapi.types import CallbackButton
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder

VARIANT='family'
STUB='🎅 Поздравление Деда Мороза для нескольких детей уже в разработке! Скоро можно будет заказать одно персональное видео для всей семьи.'
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
        name=details.get(f'child_{n}_name','пока без имени')
        heading=f'Ребёнок {n} из {count(details)} — {name}.\n\n'
        prompts={
            'name':'Как его или её зовут дома? Напишите одно имя, например «Машенька» или «Саша». Заполняем детей по очереди в том порядке, в котором их поздравит Дед Мороз.',
            'age':'Сколько лет этому ребёнку? Напишите только число, например «7». Возраст нужен для проверки данных, в поздравлении он не перечисляется.',
            'gender':'Это мальчик или девочка? Напишите «мальчик» или «девочка», чтобы обращение звучало правильно.',
            'fact':'Один конкретный факт только об этом ребёнке. Заполните: «Я слышал, что ты …». Напишите фразу целиком после «ты», без имени. Например: «любишь рисовать», «научилась плавать», «собираешь конструкторы». Не пишите факты обо всех детях в одном сообщении. Если конкретного факта нет, напишите «пропустить» — будет нейтральное «ты очень стараешься».',
            'wish':'Одно пожелание только этому ребёнку. Заполните: «Желаю тебе …». Напишите фразу после «тебе». Например: «побед в футболе», «интересных книг», «ярких рисунков». Не начинайте с «желаю» и не добавляйте имя. Постарайтесь выбрать другое пожелание, чем для остальных детей.',
        }
        prompt=heading+prompts[part]
    return prompt+f'\n\nДо {chars} символов и {words} слов. Для 4–6 детей фразы короче, чтобы каждому хватило времени.'


def joined(names):
    return ', '.join(names[:-1])+' и '+names[-1]


def tagged_text(details):
    fields={field:value(field,details.get(field),details) for field in labels(details)}
    n=count(details);names=[fields[f'child_{i}_name'] for i in range(1,n+1)]
    opening=('[deep voice] Охо-хо-хо! [raspy] Здравствуйте, мои дорогие! '
             '[warm] Это я, Дедушка Мороз! [slightly excited] [drawls] '+joined(names)+'! ')
    if n==2:opening+='Из Великого Устюга я вижу двух замечательных ребят! '
    elif n==3:opening+='Я нашёл на карте волшебства ваши счастливые имена! '
    else:opening+='У меня в списке особенная семья! '
    blocks=[]
    for i,name in enumerate(names,1):
        praise='Умница!' if fields[f'child_{i}_gender']=='девочка' else 'Молодец!'
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


async def entry(event,context,admin):
    from ded_moroz import read,tell,enter
    user=event.get_ids()[1]
    if admin is None or user!=admin:
        from handlers import PosterStates
        await context.clear();await context.set_state(PosterStates.waiting_for_mode)
        b=InlineKeyboardBuilder().row(CallbackButton(text='⬅️ Главный экран',payload='back_to_main'))
        await tell(event,STUB,attachments=[b.as_markup()]);return
    prior=await read("SELECT * FROM ded_moroz_orders WHERE user_id=? AND status NOT IN ('done','cancelled')",(user,))
    if prior:
        if not is_family(prior['details']):
            await tell(event,'Сначала завершите текущее поздравление для одного ребёнка. Его данные и файлы сохранены.',attachments=[InlineKeyboardBuilder().row(CallbackButton(text='Продолжить заказ',payload='dm_continue')).as_markup()]);return
        await enter(event,context,admin);return
    from handlers import PosterStates
    await context.clear();await context.set_state(PosterStates.waiting_for_mode)
    b=InlineKeyboardBuilder()
    for label,n in (('2 ребёнка','2'),('3 ребёнка','3'),('4 и больше','more')):
        b.row(CallbackButton(text=label,payload='dm_family_count:'+n))
    b.row(CallbackButton(text='⬅️ Главный экран',payload='back_to_main'))
    await tell(event,'🎅 Одно поздравление для всей семьи!\n\nСначала выберите количество детей. Затем заполним каждого по очереди: имя, возраст, пол, один факт и пожелание. В конце — общий факт и необязательное общее фото. Перед озвучкой всё можно проверить и исправить. Пока доступно для 2–6 детей.',attachments=[b.as_markup()])


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


async def callback(event,context,admin):
    from ded_moroz import begin,enter,get,tell,update,Settings,AUDIO_GENERATING
    from config import Config
    from handlers import PosterStates
    import aiosqlite
    import database
    user=event.get_ids()[1]
    if admin is None or user!=admin:
        await entry(event,context,admin);return
    action,key,*extra=event.callback.payload.split(':')
    if action=='dm_family_count':
        if extra:raise ValueError('Выберите количество детей кнопкой.')
        if key=='more':
            b=InlineKeyboardBuilder()
            for n in range(4,MAX_CHILDREN+1):b.row(CallbackButton(text=str(n)+' детей',payload='dm_family_count:'+str(n)))
            await tell(event,'Сколько детей поздравим? Для каждого будет короткий факт и отдельное пожелание.',attachments=[b.as_markup()]);return
        if key not in tuple(map(str,range(2,MAX_CHILDREN+1))):raise ValueError('Выберите количество детей кнопкой.')
        row=await begin(user,details={'variant':VARIANT,'children_count':int(key)})
        if not is_family(row['details']):
            await entry(event,context,admin);return
        await enter(event,context,admin);return
    row=await get(key)
    if not row or row['user_id']!=user or not is_family(row['details']) or row['status']!='input':
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
