"""Approved episode 4: preparation only; every paid request needs confirmation."""
from uuid import uuid4

from . import repository as repo
from .episode_two import SPEEDS,_insert
from .rules import normalize
from .timeline import ordered

TITLE='А если мечтать по-крупному?'
SCENES=[
    ('Одна мечта — мало!','Аня','Море — это здорово… А чего ещё я хочу?','establishing',
     'Знакомая уютная кухня-гостиная вечером. Снег за большим окном, диван и новогодние огоньки. Аня в кремовом свитере стоит у окна с телефоном; на экране её готовая фотография на пляже из третьей серии.',
     'Общий план гостиной с одной Аней у окна. Лицо в три четверти и рот хорошо различимы; не превращать общий план в портрет.',
     'Медленный плавный наезд с общего плана. Аня смотрит на телефон, слегка поднимает взгляд и улыбается.'),
    ('Список желаний','Аня','Так… отпуск, новая кухня… Что ещё?','medium_side',
     'Аня сидит за прежним деревянным столом. Перед ней блокнот «Мои мечты», рядом телефон и чашка чая. Ручка в руке, она начинает список и задумывается.',
     'Средний план за столом, слегка сбоку. Видны лицо, рот, руки с ручкой, блокнот и чашка.',
     'Небольшое движение руки с ручкой, плеч и головы. Один непрерывный ракурс.'),
    ('Не только покупки','Дима','Ань, почему у тебя одни покупки?','medium_wide',
     'Дима на привычном диване в тёмно-синем свитшоте и серых домашних брюках. Телефон в руках. На фоне прежний торшер, огоньки и окно. Он опускает телефон и смотрит к Ане за кадром.',
     'Средне-общий план гостиной. Только Дима; его лицо и рот отчётливо видны, диван и интерьер остаются в кадре.',
     'Дима спокойно опускает телефон, слегка поворачивает голову к Ане за кадром и улыбается.'),
    ('А как ещё?','Аня','А разве путешествия и покупки — не мечты?','medium_side',
     'Аня за прежним столом. Видны лицо, руки, блокнот и чашка. Она смотрит к Диме за кадром, удивлённо приподняв брови.',
     'Средний боковой план со стороны гостиной, угол 30–45 градусов. Лицо в три четверти; рот не скрыт.',
     'Небольшой поворот головы к Диме за кадром и лёгкий жест открытой ладонью.'),
    ('Важный вопрос','Дима','А кем ты сама всегда хотела стать?','knee_up',
     'Дима стоит возле прежнего дивана, немного повернувшись к кухне. Тёмно-синий свитшот и серые брюки. Взгляд серьёзный и добрый.',
     'Средний план по колено. Только Дима, лицо хорошо различимо; видны корпус, руки и часть гостиной.',
     'Дима слегка наклоняет голову, мягко жестикулирует и тепло улыбается.'),
    ('Давняя мечта','Аня','Я ведь давно мечтала о своём деле…','medium_wide',
     'Аня стоит возле прежнего большого окна. Снег и вечерние городские огни за стеклом. Тёплый комнатный свет освещает лицо и кремовый свитер.',
     'Средне-общий план у окна. Лицо Ани уже видно в три четверти в исходном кадре; не снимать со спины и не прятать рот.',
     'Аня смотрит на снег, мягко поворачивает голову к камере и мечтательно улыбается. Небольшое приближение камеры.'),
    ('Моя собственная студия',None,'','establishing',
     'Воображаемое будущее Ани: новая светлая творческая студия с панорамными окнами, рабочим столом, ноутбуком, растениями и зоной отдыха. Аня с прежним лицом и причёской, в той же одежде, уверенно работает над проектом. Других людей нет.',
     'Широкий общий план новой творческой студии. Виден интерьер целиком, Аня в центре пространства, без крупного портрета.',
     'Плавное движение камеры через помещение. Аня рассматривает работу и улыбается с закрытыми губами. Без речи.'),
    ('Дима в своём стиле','Дима','Главное, чтобы начальник выходные давал!','medium',
     'Возвращение в прежнюю гостиную. Дима расслабленно сидит в кресле, закинув ногу на ногу. Прежняя одежда, хитрая улыбка, бровь слегка приподнята.',
     'Средний план только Димы в кресле. Видны лицо, рот, руки и часть кресла, без крупного портрета.',
     'Дима приподнимает бровь, слегка жестикулирует рукой, затем небольшая комическая пауза в пределах пяти секунд.'),
    ('Карта желаний','Аня','Соберу-ка я все мечты в одну карту!','over_shoulder',
     'Аня за прежним столом смотрит на телефон и блокнот. На телефоне только готовые изображения её моря из третьей серии и собственной студии из сцены 7. Не выдумывать интерфейс приложения.',
     'Средний ракурс через край плеча самой Ани, под углом. Её лицо и рот в три четверти отчётливо видны; телефон не заслоняет лицо. Не добавлять другого человека перед камерой.',
     'Аня переводит взгляд с телефона на блокнот, берёт ручку и улыбается. Мягкое движение руки.'),
    ('Всё только начинается',None,'','object_topdown',
     'Предметный план прежнего деревянного стола: блокнот «Мои мечты», телефон с готовым изображением Ани у моря, изображение её творческой студии, чашка чая и небольшие новогодние украшения. Без людей. Место для русских титров, которые добавятся при монтаже.',
     'Красивый предметный план сверху с небольшим наклоном. Только стол и перечисленные предметы; не добавлять лица или портретные панели.',
     'Небольшое волшебное свечение, мягкое движение камеры и праздничные искорки. Изображения мечтаний плавно складываются в единую карту. Без речи и генерируемых надписей.'),
]


async def prepare(project_id,actor):
    async with repo.connect() as db:
        await db.execute('BEGIN IMMEDIATE')
        project=await repo.one(db,'SELECT * FROM cartoon_projects WHERE id=?',(project_id,))
        if not project or project['owner_id']!=actor or project['seed_key']!='anya-s1-v1':
            raise ValueError('Нужен существующий проект Ани и его администратор.')
        cast={c['name']:c['id'] for c in await repo.rows(db,'SELECT * FROM cartoon_characters WHERE project_id=?',(project_id,))}
        if not {'Аня','Дима'}<=cast.keys():raise ValueError('Не найдены сохранённые Аня и Дима; новые персонажи не создаются.')
        previous=repo.unpack(await repo.one(db,'SELECT * FROM cartoon_episodes WHERE project_id=? AND number=3',(project_id,)))
        if not previous:raise ValueError('Сначала нужна третья серия.')
        earlier=await repo.rows(db,'SELECT id,number FROM cartoon_scenes WHERE episode_id=?',(previous['id'],))
        earlier=ordered(earlier,previous['spec'])
        if not earlier:raise ValueError('В третьей серии нет последней сцены для референса квартиры.')
        anchor=earlier[-1]['id']
        beach=await repo.one(db,'SELECT id FROM cartoon_scenes WHERE episode_id=? AND number=5',(previous['id'],))
        if not beach:raise ValueError('Не найдена сохранённая сцена моря из третьей серии.')
        episode=await repo.one(db,'SELECT * FROM cartoon_episodes WHERE project_id=? AND number=4',(project_id,))
        from .episode_four_revision import TITLE as REVISED_TITLE
        if episode and episode['title']==REVISED_TITLE:return episode['id']
        if episode and episode['title']!=TITLE:raise ValueError('Номер серии 4 занят; существующая серия не изменена.')
        if not episode:
            key=uuid4().hex
            await _insert(db,'episode',key,dict(approved=True,target_seconds=50,
                ending=['Мечтать можно по-крупному!','Продолжение следует…'],
                production=dict(audio_first=True,strict_five_seconds=True,adult_speakers_only=True,
                                varied_framing=True,quality_review_required=True),
                source='Утверждённый сценарий владельца: серия 4.'),project_id=project_id,title=TITLE,number=4)
            episode={'id':key}
        existing={s['number']:s['id'] for s in await repo.rows(db,'SELECT id,number FROM cartoon_scenes WHERE episode_id=?',(episode['id'],))}
        ids={n:existing.get(n) or uuid4().hex for n in range(1,11)}
        for number,(title,speaker,text,framing,visual,camera,motion) in enumerate(SCENES,1):
            if number in existing:continue
            names=[speaker] if speaker else ['Аня'] if number==7 else []
            spec=normalize(dict(title=title,approved=True,duration=5,framing=framing,camera_direction=camera,
                visual=visual,motion=motion,characters=[cast[n] for n in names],
                speech_speeds={cast[speaker]:SPEEDS[speaker]} if speaker else {},
                dialogue=[dict(character_id=cast[speaker],text=text,direction='Естественно и эмоционально, без ускорения сверх сохранённого профиля.')] if speaker else [],
                camera=camera,sound='Только общая музыка, без речи.' if not speaker else 'Тёплая музыка, приглушённая во время реплики.'))
            if number==7:spec['new_location']=True
            else:spec['location_reference_scene_id']=anchor
            if number in (1,9,10):spec['prop_reference_scene_ids']=[beach['id']]+([ids[7]] if number in (9,10) else [])
            await _insert(db,'scene',ids[number],spec,episode_id=episode['id'],number=number)
        await db.commit()
    return episode['id']
