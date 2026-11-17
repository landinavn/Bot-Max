"""Approved episode three. Preparation creates drafts, never provider requests."""
from uuid import uuid4

from . import repository as repo
from .rules import normalize
from .episode_two import SPEEDS, _insert

TITLE = 'Мама, смотри! Ты уже на море!'
SCENES = [
    ('Я придумала!', 'Маша', 'Мам! Я придумала, как увидеть твою мечту!',
     'Крупный план Маши в бирюзовом свитере. Держит телефон и радостно обращается к маме за кадром.'),
    ('Как увидеть мечту?', 'Аня', 'Маш, как увидеть то, чего ещё нет?',
     'Средний крупный план Ани в кремовом свитере. Удивлённо приподнимает брови и заинтересованно улыбается.'),
    ('Фото и желание', 'Аня', 'Загружаешь фото, пишешь желание — и готово!',
     'Средний план взрослой Ани по пояс за кухонным столом. В кадре голова, торс, обе руки, телефон и часть стола. '
     'Аня в кремовом свитере держит телефон и показывает пальцем на экран, повёрнутый от зрителя. '
     'Спокойная улыбка, рот закрыт, зубы не видны. Маша и другие люди вне кадра. Не рисовать интерфейс бота.'),
    ('Хочу на море!', 'Аня', 'Хочу на море! И прямо сейчас!',
     'Средний план Ани. Смотрит на телефон, улыбается и решительно загадывает желание оказаться на море.'),
    ('Мечта оживает', None, '',
     'Аня на солнечном тропическом пляже у моря. Та же узнаваемая Аня по утверждённому референсу, та же причёска и одежда. Один цельный кадр, без интерфейса и коллажа.'),
    ('Это же я!', 'Аня', 'Это же я! Как будто уже на море!',
     'Возвращение в прежнюю квартиру. Крупный план счастливой удивлённой Ани. Смотрит на результат в телефоне.'),
    ('Мечта Димы', 'Дима', 'А можно мне на море, где никто не трогает?',
     'Средний план только Димы на прежнем диване. С серьёзным видом шутит, затем небольшая комическая пауза.'),
    ('Мечта ближе', 'Аня', 'Кажется, моя мечта стала немного ближе…',
     'Крупный план Ани в квартире. Смотрит на свою фотографию у моря в телефоне и мечтательно улыбается.'),
    ('Продолжение следует', None, '',
     'Спокойный кадр прежней уютной квартиры, без людей. Место для финальной заставки. Надписи добавляются только при монтаже.'),
]


async def prepare(project_id, actor):
    async with repo.connect() as db:
        await db.execute('BEGIN IMMEDIATE')
        project=await repo.one(db,'SELECT * FROM cartoon_projects WHERE id=?',(project_id,))
        if not project or project['owner_id']!=actor or project['seed_key']!='anya-s1-v1':
            raise ValueError('Нужен существующий проект «Маленькие чудеса Ани» и его администратор.')
        cast={c['name']:c['id'] for c in await repo.rows(db,'SELECT * FROM cartoon_characters WHERE project_id=?',(project_id,))}
        if not SPEEDS.keys()<=cast.keys():
            raise ValueError('Не найдены прежние Аня, Дима и Маша. Новые персонажи не создаются.')
        previous=await repo.one(db,'SELECT id FROM cartoon_episodes WHERE project_id=? AND number=2',(project_id,))
        anchor=await repo.one(db,'SELECT id FROM cartoon_scenes WHERE episode_id=? ORDER BY number DESC LIMIT 1',(previous['id'],)) if previous else None
        if not anchor: raise ValueError('Нужна последняя сцена второй серии для локации квартиры.')
        episode=await repo.one(db,'SELECT * FROM cartoon_episodes WHERE project_id=? AND number=3',(project_id,))
        if episode and episode['title']!=TITLE: raise ValueError('Номер серии 3 уже занят; существующая серия сохранена.')
        if not episode:
            key=uuid4().hex
            await _insert(db,'episode',key,dict(approved=True,target_seconds=45,
                ending=['Маленькие чудеса Ани','А о чём мечтаете вы?','Продолжение следует…'],
                production=dict(audio_first=True,strict_five_seconds=True,quality_review_required=True),
                source='Утверждённый сценарий владельца: серия 3.'),project_id=project_id,title=TITLE,number=3)
            episode={'id':key}
        for number,(title,speaker,text,visual) in enumerate(SCENES,1):
            if await repo.one(db,'SELECT id FROM cartoon_scenes WHERE episode_id=? AND number=?',(episode['id'],number)): continue
            names=[speaker] if speaker else ['Аня'] if number==5 else []
            spec=normalize(dict(title=title,approved=True,duration=5,visual=visual,
                characters=[cast[n] for n in names],
                dialogue=[dict(character_id=cast[speaker],text=text,direction='Естественно, эмоционально, без ускорения сверх профиля.')] if speaker else [],
                speech_speeds={cast[speaker]:SPEEDS[speaker]} if speaker else {},
                sound='Тёплая музыка; приглушение во время речи.'))
            if number==3: spec['framing']='medium'
            if number==5: spec['new_location']=True
            else: spec['location_reference_scene_id']=anchor['id']
            await _insert(db,'scene',uuid4().hex,spec,episode_id=episode['id'],number=number)
        await db.commit()
    return episode['id']
