"""An isolated administrative router. No customer keyboards, tariffs or billing hooks."""
import asyncio
import json
import zipfile
from uuid import uuid4

from maxapi import F, Router
from maxapi.context import MemoryContext
from maxapi.context.state_machine import State, StatesGroup
from maxapi.filters.command import Command
from maxapi.types import CallbackButton, LinkButton
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder

from handlers import _reply
from . import repository as repo
from .seed import seed
from .settings import dollars, money


class StudioStates(StatesGroup):
    input = State()


VOICES = {'alloy','nova','shimmer','echo','fable','onyx','coral','ash','sage'}


def validate_spec(entity, spec):
    if not isinstance(spec, dict) or len(repo.encoded(spec)) > 30000:
        raise ValueError('Ожидается объект JSON до 30000 символов.')
    if entity == 'character':
        if not isinstance(spec.get('description'), str) or not spec['description'].strip() or spec.get('voice') not in VOICES:
            raise ValueError('Укажите description и постоянный voice: ' + ', '.join(sorted(VOICES)))
    if entity == 'scene':
        if not isinstance(spec.get('visual'),str) or not spec['visual'].strip():
            raise ValueError('Нужно описание visual.')
        if type(spec.get('duration')) not in (int,float) or not 1 <= spec['duration'] <= 10:
            raise ValueError('Длительность сцены: 1–10 секунд; Kling генерирует 5 или 10, монтаж подгоняет длительность.')
        people = spec.get('characters')
        if not isinstance(people,list) or not 1 <= len(people) <= 2 or len(set(people)) != len(people):
            raise ValueError('Для MVP укажите 1–2 ID постоянных персонажей.')
        dialogue = spec.get('dialogue', [])
        if not isinstance(dialogue,list) or len(dialogue) > 20:
            raise ValueError('Некорректные реплики.')
        for line in dialogue:
            if not isinstance(line,dict) or line.get('character_id') not in people or not isinstance(line.get('text'),str) or not 1 <= len(line['text']) <= 2000:
                raise ValueError('Каждая реплика: character_id из characters и непустой text до 2000 символов.')
        if type(spec.get('approved',False)) is not bool:
            raise ValueError('approved — true или false.')
    if entity == 'episode':
        ending = spec.get('ending', [])
        if not isinstance(ending,list) or len(ending)>4 or any(not isinstance(s,str) or len(s)>100 for s in ending):
            raise ValueError('ending — до четырёх коротких строк финальной заставки.')
    return spec


def create_router(admin_id, settings, assets):
    router = Router()

    async def allowed(event):
        actor = event.get_ids()[1]
        if actor is None or actor != admin_id or not settings.enabled or getattr(getattr(event,'message',None),'sender',None) and getattr(event.message.sender,'is_bot',False):
            return False
        return True

    async def tell(event,text,buttons=(),links=()):
        builder = InlineKeyboardBuilder()
        for title,payload in buttons:
            builder.row(CallbackButton(text=title,payload='studio:'+payload))
        for title,url in links:
            builder.row(LinkButton(text=title,url=url))
        for offset in range(0,max(1,len(text)),3500):
            last=offset+3500>=len(text)
            await _reply(event,text[offset:offset+3500],**({'attachments':[builder.as_markup()]} if last and (buttons or links) else {}))

    async def owner(entity,key):
        row = await repo.get(entity,key)
        if not row:
            raise ValueError('Запись не найдена.')
        if entity=='scene':
            episode = await repo.get('episode',row['episode_id'])
            project = await repo.get('project',episode['project_id'])
        else:
            project = row if entity=='project' else await repo.get('project',row['project_id'])
        if not project or project['owner_id'] != admin_id:
            raise ValueError('Нет доступа к проекту.')
        return row,project

    async def home(event,context):
        await context.clear()
        project = await seed(admin_id)
        await repo.prepare_drafts(project,settings)
        projects = [p for p in await repo.children('project') if p['owner_id']==admin_id]
        await tell(event,'🎬 Мультстудия — только для администратора. Все платные действия требуют отдельного подтверждения.\n'
                   'Расценки — оценки в USD, бюджет по умолчанию 0. /start — выйти в обычное меню.',
                   [(p['title']+f" / сезон {p['season']}",'p:'+p['id']) for p in projects]
                   +[('➕ Создать мультсериал','new:project:root')])

    async def show(event,entity,key):
        row,project = await owner(entity,key)
        if entity=='project':
            state = await repo.budget(key)
            buttons = [(c['name'],'c:'+c['id']) for c in await repo.children('character',key)]
            buttons += [(e['title'],'e:'+e['id']) for e in await repo.children('episode',key)]
            buttons += [('➕ Персонаж','new:character:'+key),('➕ Серия','new:episode:'+key),
                        ('✏️ Локация / стиль','edit:project:'+key),('💰 Бюджет','budget:'+key),
                        ('📋 Задания и расходы','jobs:'+key),('📦 Архив исходников','export:'+key),('⬅️ Проекты','home')]
            await tell(event,row['title']+f" / сезон {row['season']}\nБюджет: {dollars(state['limit'])}; расход + резервы: {dollars(state['committed'])}",buttons)
        elif entity=='character':
            await tell(event,row['name']+'\nID: '+key+'\n'+json.dumps(row['spec'],ensure_ascii=False,indent=2),[
                ('✏️ Изменить персонажа / голос','edit:character:'+key),('📷 Загрузить референс','reference:'+key),
                ('🎨 Создать / перегенерировать эталон','make:image:character:'+key),
                ('👁 Эталон / утвердить','view:character:'+key),('⬅️ Проект','p:'+project['id'])])
        elif entity=='episode':
            buttons=[(f"{s['number']}. {s['spec'].get('title','Сцена')}",'s:'+s['id']) for s in await repo.children('scene',key)]
            buttons += [('➕ Сцена','new:scene:'+key),('✏️ Серия / финальная заставка','edit:episode:'+key),
                        ('🎵 Загрузить фоновую музыку','music:'+key),('🎞 Собрать серию','make:assemble:episode:'+key),
                        ('📥 Получить MP4','view:episode:'+key),('⬅️ Проект','p:'+project['id'])]
            await tell(event,row['title']+'\nЕстественная речь без ускорения; целевая длительность 40 секунд, допустимо 42–45.\n'
                       'Звуковые эффекты и отдельная музыка сцен в MVP сохраняются как режиссёрские указания; можно загрузить общий музыкальный трек.',buttons)
        else:
            await tell(event,f"Сцена {row['number']} · версия {row['revision']}\n"+json.dumps(row['spec'],ensure_ascii=False,indent=2),[
                ('✏️ Изменить сцену','edit:scene:'+key),('🎨 Изображение / перегенерировать','make:image:scene:'+key),
                ('🎥 Оживить','make:video:scene:'+key),('🎙 Озвучить','make:speech:scene:'+key),
                ('👁 Результаты / утвердить изображение','view:scene:'+key),('⬅️ Серия','e:'+row['episode_id'])])

    @router.message_created(Command('multstudio'))
    async def studio_start(event,context:MemoryContext):
        if await allowed(event):
            await home(event,context)

    @router.message_callback(F.callback.payload.startswith('studio:'))
    async def studio_callback(event,context:MemoryContext):
        if not await allowed(event):
            await event.ack(notification='Раздел доступен только администратору.')
            return
        await event.ack(notification='Мультстудия')
        if event.message is None:
            return
        parts=event.callback.payload.split(':')[1:]
        action=parts[0]
        try:
            if action=='home':
                await home(event,context)
            elif action in ('p','c','e','s'):
                await show(event,{'p':'project','c':'character','e':'episode','s':'scene'}[action],parts[1])
            elif action in ('new','edit'):
                entity,key=parts[1:3]
                if action=='edit':
                    row,_=await owner(entity,key)
                    sample=row['spec']
                else:
                    if entity!='project':
                        await owner('episode' if entity=='scene' else 'project',key)
                    sample={'project':{'title':'Новый мультсериал','season':1,'style':'Семейная 3D-анимация','location':'Описание локации'},
                            'character':{'name':'Имя','description':'Возраст, лицо, причёска, одежда, характер','voice':'nova','voice_direction':'Описание голоса'},
                            'episode':{'title':'Новая серия','ending':['Продолжение следует…']},
                            'scene':{'title':'Новая сцена','duration':5,'visual':'Действие и локация','camera':'Средний план',
                                     'characters':[],'dialogue':[],'approved':False}}[entity]
                await context.update_data(studio_input=dict(action=action,entity=entity,key=key))
                await context.set_state(StudioStates.input)
                await tell(event,'Пришлите JSON. Для новой сцены подставьте ID персонажей из карточек; approved=true утверждает сценарий, но не запускает генерацию.\n'
                           +json.dumps(sample,ensure_ascii=False,indent=2),[('Отмена','home')])
            elif action in ('budget','cost','reference','music'):
                key=parts[1]
                if action=='cost':
                    item=await repo.job(key)
                    if not item: raise ValueError('Задание не найдено.')
                    await owner('project',item['project_id'])
                else:
                    await owner('character' if action=='reference' else 'episode' if action=='music' else 'project',key)
                await context.update_data(studio_input=dict(action=action,key=key))
                await context.set_state(StudioStates.input)
                await tell(event, 'Пришлите публичную HTTPS-ссылку на файл (для референса можно отправить фотографию).'
                           if action in ('reference','music') else 'Введите сумму в USD. Фактический расход берите из кабинета провайдера; оценку нельзя выдавать за факт.',[('Отмена','home')])
            elif action=='make':
                kind,entity,key=parts[1:4]
                await owner(entity,key)
                if (entity,kind) not in {('character','image'),('scene','image'),('scene','video'),('scene','speech'),('episode','assemble')}:
                    raise ValueError('Недопустимое действие.')
                item=await repo.quote(entity,key,kind,settings)
                snap=json.loads(item['snapshot']); q=snap['quote']
                await tell(event, f"Подтверждение задания {item['id']}\nМодель: {q['model']}\nРазрешение: {q['resolution']}\n"
                    f"Длительность: {q['duration']} сек (озвучка — по естественной речи)\nОценка: {dollars(q['estimate'])}\n"
                    'Это оценка, не подтверждённый расход. Если провайдер не вернёт цену, сверка будет ручной, резерв сохранится. '
                    'При перегенерации предыдущий результат останется в архиве.',
                    [('✅ Подтвердить запуск','confirm:'+item['id']),('Отмена','cancel:'+item['id'])])
            elif action=='cancel':
                await repo.cancel_quote(parts[1],admin_id)
                await tell(event,'Оценка отменена, запуск по её старой кнопке недоступен.',[('Проекты','home')])
            elif action=='confirm':
                item=await repo.confirm(parts[1],admin_id)
                await tell(event,'Статус задания: '+item['status']+'. ID: '+item['id'],[('Проект','p:'+item['project_id'])])
            elif action=='approve':
                await repo.approve_asset(parts[1],admin_id)
                await tell(event,'Изображение утверждено. Генерация видео не запускается автоматически.',[('Проекты','home')])
            elif action=='view':
                entity,key=parts[1:3]
                await owner(entity,key)
                snap=await repo.snapshot(entity,key)
                links,buttons=[],[]
                for kind,asset in snap['assets'].items():
                    if kind not in ('image','video','speech','assemble'): continue
                    links.append((kind+' · скачать / посмотреть',await assets.asset_url(asset)))
                    if kind=='image' and not asset['approved']:
                        buttons.append(('✅ Утвердить изображение','approve:'+asset['id']))
                if entity=='episode' and snap['assets'].get('assemble'):
                    from media_service import upload_video_for_max
                    video=await upload_video_for_max(event.bot,await assets.asset_url(snap['assets']['assemble']))
                    await _reply(event,'Готовая серия MP4',attachments=[video])
                await tell(event,'Результаты текущей версии.' if links else 'У этой версии пока нет результатов.',buttons+[('Проект','p:'+snap['project_id'])],links)
            elif action=='jobs':
                await owner('project',parts[1])
                jobs=await repo.job_list(parts[1])
                text='\n'.join(f"{j['id']} · {j['kind']} · {j['status']} · оценка {dollars(j['estimate'])} · факт "
                     +(dollars(j['actual']) if j['actual'] is not None else 'не сверено') for j in jobs)
                await tell(event,text or 'Нет заданий.',[(j['kind']+' · '+j['status']+' · '+j['id'][:6],'job:'+j['id']) for j in jobs]+[('Проект','p:'+parts[1])])
            elif action in ('job','poll'):
                item=await repo.job(parts[1])
                if not item: raise ValueError('Задание не найдено.')
                await owner('project',item['project_id'])
                if action=='poll': await repo.retry_poll(item['id'],admin_id)
                buttons=[('💰 Сверить фактический расход','cost:'+item['id'])] if item['status'] in ('done','manual','failed') else []
                if item['status']=='manual' and item['provider_id']:
                    buttons.append(('Повторить только опрос Kling','poll:'+item['id']))
                if item['status']=='manual' and item['kind']=='video':
                    buttons.append(('Завершить ручную сверку Kling','resolve:'+item['id']))
                await tell(event,json.dumps({k:item[k] for k in ('id','kind','status','provider_id','error','estimate','actual','attempts')},ensure_ascii=False,indent=2)
                           +'\nПри неизвестном результате платного запроса сначала сверьте расход в кабинете. Новый запрос — только с новым подтверждением.',buttons+[('Проект','p:'+item['project_id'])])
            elif action=='resolve':
                item=await repo.job(parts[1])
                if not item: raise ValueError('Задание не найдено.')
                await owner('project',item['project_id'])
                await tell(event,'Подтвердите, что проверили кабинет PiAPI и эта задача больше НЕ выполняется. '
                           'Это разблокирует следующую задачу очереди. Перед подтверждением сверьте фактический расход.',
                           [('✅ Задача больше не выполняется','resolved:'+item['id']),('Отмена','job:'+item['id'])])
            elif action=='resolved':
                await repo.resolve_video(parts[1],admin_id)
                await tell(event,'Ручная сверка завершена. Повторный POST не отправлен.',[('Проекты','home')])
            elif action=='export':
                await owner('project',parts[1])
                url=await export_project(parts[1],assets)
                await tell(event,'Архив: сценарий, версии, задания, расходы и сохранённые исходники/результаты.',links=[('📦 Скачать исходный проект ZIP',url)])
        except (ValueError,KeyError,IndexError,TypeError) as exc:
            await tell(event,str(exc) if isinstance(exc,ValueError) else 'Неверные данные действия.',[('Проекты','home')])

    @router.message_created(StudioStates.input, ~F.message.body.text.startswith('/'))
    async def studio_input(event,context:MemoryContext):
        if not await allowed(event): return
        data=await context.get_data(); task=data.get('studio_input',{}); action=task.get('action')
        text=(event.message.body.text or '').strip()
        try:
            key=task.get('key')
            if action=='budget':
                await owner('project',key); await repo.budget(key,money(text))
            elif action=='cost':
                await repo.reconcile(key,admin_id,money(text))
            elif action in ('reference','music'):
                entity='character' if action=='reference' else 'episode'
                row,project=await owner(entity,key)
                if action=='reference':
                    for attachment in event.message.body.attachments or []:
                        if attachment.type=='image' and getattr(attachment.payload,'url',None):
                            text=attachment.payload.url; break
                snap=await repo.snapshot(entity,key)
                asset_id=await assets.retain(snap,'image' if action=='reference' else 'music',url=text,
                                             suffix='png' if action=='reference' else 'mp3')
                if action=='reference':
                    await repo.edit('character',key,dict(row['spec'],reference=asset_id))
            elif action in ('new','edit'):
                entity=task['entity']; spec=validate_spec(entity,json.loads(text))
                if action=='edit':
                    _,project=await owner(entity,key)
                    if entity=='scene':
                        for character in spec['characters']:
                            _,cp=await owner('character',character)
                            if cp['id']!=project['id']: raise ValueError('Персонаж из другого проекта.')
                    await repo.edit(entity,key,spec)
                elif entity=='project':
                    await repo.create(entity,spec,owner_id=admin_id,title=spec['title'],season=int(spec.get('season',1)))
                elif entity=='character':
                    await owner('project',key)
                    await repo.create(entity,spec,project_id=key,name=spec['name'])
                elif entity=='episode':
                    await owner('project',key)
                    await repo.create(entity,spec,project_id=key,title=spec['title'],number=1+len(await repo.children(entity,key)))
                elif entity=='scene':
                    _,project=await owner('episode',key)
                    for character in spec['characters']:
                        _,cp=await owner('character',character)
                        if cp['id']!=project['id']: raise ValueError('Персонаж из другого проекта.')
                    await repo.create(entity,spec,episode_id=key,number=1+len(await repo.children(entity,key)))
            else:
                raise ValueError('Откройте действие заново.')
            await context.clear()
            await tell(event,'Сохранено. Платные задания автоматически не запускаются.',[('Проекты','home')])
        except (ValueError,KeyError,TypeError) as exc:
            await tell(event,str(exc) if isinstance(exc,ValueError) else 'Проверьте JSON и обязательные поля.')
    return router


async def export_project(project,assets):
    async with repo.connect() as db:
        data={'project':await repo.one(db,'SELECT * FROM cartoon_projects WHERE id=?',(project,))}
        data['characters']=await repo.rows(db,'SELECT * FROM cartoon_characters WHERE project_id=?',(project,))
        data['episodes']=await repo.rows(db,'SELECT * FROM cartoon_episodes WHERE project_id=?',(project,))
        data['scenes']=await repo.rows(db,'SELECT s.* FROM cartoon_scenes s JOIN cartoon_episodes e ON e.id=s.episode_id WHERE e.project_id=?',(project,))
        data['jobs']=await repo.rows(db,'SELECT * FROM cartoon_jobs WHERE project_id=?',(project,))
        data['assets']=await repo.rows(db,'SELECT * FROM cartoon_assets WHERE project_id=?',(project,))
        ids=[project]+[r['id'] for kind in ('characters','episodes','scenes') for r in data[kind]]
        data['versions']=await repo.rows(db,'SELECT * FROM cartoon_versions WHERE entity_id IN ('+','.join('?' for _ in ids)+')',ids)
        data['events']=await repo.rows(db,'SELECT v.* FROM cartoon_job_events v JOIN cartoon_jobs j ON j.id=v.job_id WHERE j.project_id=?',(project,))
    archive=assets.path(uuid4().hex+'.zip')
    def pack():
        with zipfile.ZipFile(archive,'w',compression=zipfile.ZIP_DEFLATED) as output:
            output.writestr('project.json',json.dumps(data,ensure_ascii=False,indent=2))
            for asset in data['assets']:
                path=assets.path(asset['path'])
                if path.is_file(): output.write(path,'assets/'+path.name)
    await asyncio.to_thread(pack)
    return await assets.publish(archive.name)
