"""An isolated administrative router. No customer keyboards, tariffs or billing hooks."""
import asyncio
import json
import logging
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
from .tts import VOICES, speech_settings, MASHA_PROFILE


class StudioStates(StatesGroup):
    input = State()


logger = logging.getLogger(__name__)
DESCRIPTION_PROMPT = 'Пожалуйста, введите описание обычным текстом (без кавычек и фигурных скобок)'


def validate_spec(entity, spec):
    if not isinstance(spec, dict) or len(repo.encoded(spec)) > 30000:
        raise ValueError('Ожидается объект JSON до 30000 символов.')
    if entity == 'character':
        if not isinstance(spec.get('description'), str) or not spec['description'].strip():
            raise ValueError('Укажите description и постоянный voice: ' + ', '.join(sorted(VOICES)))
        speech_settings(spec)
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
        # In MessageCallback, message.sender is the BOT that posted the keyboard.
        # The actor is callback.user; only incoming messages use message.sender.
        callback = getattr(event,'callback',None)
        sender = (getattr(callback,'user',None) if callback is not None else
                  getattr(getattr(event,'message',None),'sender',None))
        if actor is None or actor != admin_id or not settings.enabled or getattr(sender,'is_bot',False):
            return False
        return True

    async def tell(event,text,buttons=(),links=(),*,replace=False):
        builder = InlineKeyboardBuilder()
        for title,payload in buttons:
            builder.row(CallbackButton(text=title,payload='studio:'+payload))
        for title,url in links:
            builder.row(LinkButton(text=title,url=url))
        editor=getattr(event.bot,'edit_message',None)
        body=getattr(getattr(event,'message',None),'body',None)
        if replace and len(text)<=3500 and callable(editor) and getattr(body,'mid',None):
            try:
                edited=await editor(message_id=body.mid,text=text,
                    attachments=[builder.as_markup()] if buttons or links else [],notify=False)
                if edited is not None:
                    return
            except Exception as exc:
                logger.warning('Не удалось обновить карточку студии (%s); отправляется новая.',type(exc).__name__)
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

    async def result_for_job(item, *, force=False):
        try:
            return await assets.result_for_job(item,force=force)
        except ValueError:
            raise
        except Exception as exc:
            logger.warning('Не удалось опубликовать результат задания %s (%s).',item['id'],type(exc).__name__)
            raise ValueError('Файл сохранён, но публикация временно недоступна. Повторите просмотр; генерация не перезапускается.') from None

    async def home(event,context):
        await context.clear()
        project = await seed(admin_id)
        await repo.prepare_drafts(project,settings)
        projects = [p for p in await repo.children('project') if p['owner_id']==admin_id]
        await tell(event,'🎬 Мультстудия — только для администратора. Все платные действия требуют отдельного подтверждения.\n'
                   'Расценки — оценки в USD, бюджет по умолчанию 0. /start — выйти в обычное меню.',
                   [(p['title']+f" / сезон {p['season']}",'project:'+p['id']) for p in projects]
                   +[('➕ Создать мультсериал','new:project:root')])

    async def show(event,entity,key,*,notice='',replace=False):
        if entity=='project':
            logger.info('Открываю проект %s', key)
        row,project = await owner(entity,key)
        if entity=='project':
            state = await repo.budget(key)
            buttons = [('💰 Установить бюджет','budget:'+key),('👥 Персонажи','characters:'+key),
                       ('🎬 Сцены','scenes:'+key),('⬅️ Назад','home')]
            amount = dollars(state['limit']).removesuffix(' USD')
            await tell(event,f"📁 Проект: {row['title']}\nБюджет: {amount}$\n"
                       f"Расход + резервы: {dollars(state['committed'])}",buttons)
        elif entity=='character':
            profile=await repo.character_speech(key)
            await tell(event,row['name']+'\nID: '+key+f"\nSpeechKit {'v3' if profile['voice']=='masha' else 'v1'}: {profile['voice']}, emotion={profile['emotion']}, speed={profile['speed']}"
                       + (f", pitch_shift={profile['pitch_shift']} Гц" if 'pitch_shift' in profile else '')+'\n'
                       +json.dumps(dict(row['spec'],**profile),ensure_ascii=False,indent=2),[
                ('✏️ Изменить описание','edit:character:'+key),
                *([('🧒 Юный голос Маши','masha_voice:'+key)] if row['name']=='Маша' else []),
                ('🎙 Настройки голоса (JSON)','character_settings:'+key),('📷 Загрузить референс','reference:'+key),
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
            snap=await repo.snapshot('scene',key)
            editing=await repo.scene_editing(key)
            links=[]
            for kind,title in (('image','🖼️ Открыть картинку'),('video','🎬 Открыть видео')):
                if snap['assets'].get(kind):
                    try:
                        links.append((title,await assets.asset_url(snap['assets'][kind])))
                    except Exception as exc:
                        logger.warning('Не удалось получить ссылку сцены %s kind=%s (%s)',key,kind,type(exc).__name__)
            await tell(event,(notice+'\n' if notice else '')+f"Сцена {row['number']} · версия {row['revision']}\nНачало речи: {editing['speech_start']:g} с\n"+json.dumps(row['spec'],ensure_ascii=False,indent=2),[
                ('✏️ Изменить сцену','edit:scene:'+key),('⏱ Начало речи','speech_start:'+key),('🎨 Изображение / перегенерировать','make:image:scene:'+key),
                ('♻️ Восстановить картинку и видео','restore_assets:'+key),
                ('📤 Загрузить своё изображение','scene_upload:'+key),
                ('⚙️ Параметры сцены (JSON)','scene_settings:'+key),
                ('🎥 Оживить','make:video:scene:'+key),('🎙 Озвучить','make:speech:scene:'+key),
                ('👁 Результаты / утвердить изображение','view:scene:'+key),('⬅️ Серия','e:'+row['episode_id'])],links,replace=replace)

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
            elif action=='project':
                await show(event,'project',parts[1])
            elif action in ('p','c','e','s'):
                await show(event,{'p':'project','c':'character','e':'episode','s':'scene'}[action],parts[1])
            elif action=='restore_assets':
                key=parts[1]
                await owner('scene',key)
                result=await repo.restore_scene_assets(key,actor=admin_id,assets=assets)
                restored=', '.join('картинка' if kind=='image' else 'видео' for kind in result['restored'])
                missing=(' Не найдено в архиве: '+', '.join('картинка' if k=='image' else 'видео' for k in result['missing'])+'.') if result['missing'] else ''
                await show(event,'scene',key,notice='♻️ Восстановлено из архива: '+restored+'.'+missing,replace=True)
            elif action=='characters':
                key=parts[1]
                await owner('project',key)
                buttons=[(c['name'],'c:'+c['id']) for c in await repo.children('character',key)]
                await tell(event,'👥 Персонажи проекта',buttons+[
                    ('➕ Добавить персонажа','new:character:'+key),('⬅️ Назад','project:'+key)])
            elif action=='scenes':
                key=parts[1]
                await owner('project',key)
                buttons=[(f"Серия {e['number']}: {e['title']}",'e:'+e['id']) for e in await repo.children('episode',key)]
                await tell(event,'🎬 Сцены и серии проекта',buttons+[
                    ('➕ Создать серию','new:episode:'+key),('✏️ Локация / стиль','edit:project:'+key),
                    ('📋 Задания и расходы','jobs:'+key),('📦 Архив исходников','export:'+key),
                    ('⬅️ Назад','project:'+key)])
            elif action=='edit' and len(parts)>2 and parts[1]=='character':
                key=parts[2]
                row,_=await owner('character',key)
                await context.update_data(studio_input=dict(action='description',entity='character',key=key))
                await context.set_state(StudioStates.input)
                await tell(event,DESCRIPTION_PROMPT+'\n\nТекущее описание:\n'+row['spec'].get('description',''),
                           [('Отмена','home')])
            elif action=='edit' and len(parts)>2 and parts[1]=='scene':
                key=parts[2]
                row,_=await owner('scene',key)
                await context.update_data(studio_input=dict(action='scene_visual',entity='scene',key=key))
                await context.set_state(StudioStates.input)
                await tell(event,'Опишите словами, что изменить в сцене (например: Аня сидит на диване)'
                           +'\nТекст заменит визуальное описание целиком.\n\nТекущее описание:\n'
                           +row['spec'].get('visual',''),[('Отмена','home')])
            elif action=='scene_upload':
                key=parts[1]
                await owner('scene',key)
                await context.update_data(studio_input=dict(action='scene_upload',entity='scene',key=key))
                await context.set_state(StudioStates.input)
                await tell(event,'Отправьте изображение для этой сцены (JPG/PNG). Оно будет использовано вместо генерации.\n'
                           'Выберите «Файл» в MAX, чтобы сохранить исходный формат JPG/JPEG или PNG: '
                           'при отправке как фото MAX может преобразовать изображение в WEBP.',
                           [('Отмена','home')])
            elif action=='speech_start':
                key=parts[1]
                await owner('scene',key)
                value=(await repo.scene_editing(key))['speech_start']
                await context.update_data(studio_input=dict(action='speech_start',entity='scene',key=key))
                await context.set_state(StudioStates.input)
                await tell(event,f'Сейчас речь начинается на {value:g} секунде сцены. '
                           'Пришлите новое время от 0 до 30 секунд. Например: 3. '
                           'Изменится только монтаж; готовые картинки, видео и озвучка сохраняются.',[('Отмена','home')])
            elif action=='scene_settings':
                key=parts[1]
                row,_=await owner('scene',key)
                await context.update_data(studio_input=dict(action='scene_settings',entity='scene',key=key))
                await context.set_state(StudioStates.input)
                await tell(event,'Расширенные параметры сцены: пришлите JSON. Для изменения только описания '
                           'используйте «Изменить сцену».\n'+json.dumps(row['spec'],ensure_ascii=False,indent=2),[('Отмена','home')])
            elif action=='masha_voice':
                key=parts[1]
                row,_=await owner('character',key)
                if row['name']!='Маша': raise ValueError('Этот профиль предназначен для Маши.')
                await repo.set_character_speech(key,admin_id,MASHA_PROFILE)
                await tell(event,'Сохранён вариант юного голоса Маши: SpeechKit v3 / masha, темп 1.0, тон +80 Гц. '
                           'Точный возраст голосом не задаётся: оцените звучание после подтверждения озвучки. '
                           'Картинки и видео сохранены; платная озвучка не запускалась.',[('👤 Персонаж','c:'+key)])
            elif action=='character_settings':
                key=parts[1]
                row,_=await owner('character',key)
                await context.update_data(studio_input=dict(action='character_settings',entity='character',key=key))
                await context.set_state(StudioStates.input)
                profile=await repo.character_speech(key)
                await tell(event,'Настройки озвучки: пришлите JSON, voice='+', '.join(sorted(VOICES))
                           +'; speed=0.1–3.0. Для masha (v3): emotion=good/friendly/strict, pitch_shift=-1000…1000 Гц. '
                           'Для остальных (v1): emotion=neutral/good/evil. Эти настройки не меняют картинки и видео.\n'
                           +json.dumps(profile,ensure_ascii=False,indent=2),[('Отмена','home')])
            elif action in ('new','edit'):
                entity,key=parts[1:3]
                if action=='edit':
                    row,_=await owner(entity,key)
                    sample=row['spec']
                else:
                    if entity!='project':
                        await owner('episode' if entity=='scene' else 'project',key)
                    sample={'project':{'title':'Новый мультсериал','season':1,'style':'Семейная 3D-анимация','location':'Описание локации'},
                            'character':{'name':'Имя','description':'Возраст, лицо, причёска, одежда, характер','voice':'alena','emotion':'neutral','speed':1.0,'voice_direction':'Описание голоса'},
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
                speech_info=('Голоса:\n'+'\n'.join(
                    f"{p.get('name','Персонаж')}: {p['voice']}, emotion={p['emotion']}, speed={p['speed']}"
                    +(f", pitch_shift={p['pitch_shift']} Гц; SpeechKit v3" if p['voice']=='masha' else '')
                    for p in q.get('speech_profiles',{}).values())+'\n') if kind=='speech' else ''
                await tell(event, f"Подтверждение задания {item['id']}\nМодель: {q['model']}\nРазрешение: {q['resolution']}\n"
                    f"Длительность: {q['duration']} сек (озвучка — по естественной речи)\nОценка: {dollars(q['estimate'])}\n"
                    +speech_info+
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
            elif action=='approve_video':
                await repo.approve_asset(parts[1],admin_id,expected_kind='video')
                await tell(event,'✅ Видео утверждено. Новая генерация не запускается.',[('Проекты','home')])
            elif action=='resend_result':
                item=await repo.job(parts[1])
                if not item: raise ValueError('Задание не найдено.')
                await owner('project',item['project_id'])
                if item['status']!='done' or not repo.is_media_job(item):
                    raise ValueError('В задании пока нет готового медиафайла.')
                await repo.update_job(item['id'],notified=0)
                await tell(event,'Готовый результат будет повторно отправлен в этот чат. '
                           'Новая генерация и платный запрос не запускаются.',[('Задание','job:'+item['id'])])
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
                links=[]
                if item['status']=='done' and repo.is_image_job(item):
                    _,url=await result_for_job(item)
                    item=await repo.job(item['id'])
                    links.append(('🔗 Открыть изображение',url))
                    buttons.append(('Показать картинку','show_image:'+item['id']))
                    buttons.append(('Отправить результат в чат','resend_result:'+item['id']))
                    if item['target']=='character':
                        buttons += [('✅ Утвердить эталон','approve_reference:'+item['id']),
                                    ('🔄 Перегенерировать','make:image:character:'+item['target_id'])]
                if item['status']=='done' and item['kind']=='video':
                    asset,url=await result_for_job(item)
                    item=await repo.job(item['id'])
                    links.append(('🔗 Открыть видео',url))
                    buttons += [('Отправить видео в чат','resend_result:'+item['id']),
                                ('✅ Утвердить видео','approve_video:'+asset['id']),
                                ('🔄 Перегенерировать','make:video:scene:'+item['target_id'])]
                if item['status']=='done' and item['kind'] in ('speech','assemble','episode_assembly'):
                    _,url=await result_for_job(item)
                    item=await repo.job(item['id'])
                    links.append(('📥 Скачать результат',url))
                    buttons.append(('Отправить результат в чат','resend_result:'+item['id']))
                if item['status']=='manual' and await repo.job_result(item['id']):
                    buttons.append(('Опубликовать сохранённый результат','publish_result:'+item['id']))
                if item['status']=='manual' and item['provider_id']:
                    buttons.append(('Повторить только опрос Kling','poll:'+item['id']))
                if item['status']=='manual' and item['kind']=='video':
                    buttons.append(('Завершить ручную сверку Kling','resolve:'+item['id']))
                await tell(event,json.dumps({k:item[k] for k in ('id','kind','status','provider_id','result_url','error','estimate','actual','attempts')},ensure_ascii=False,indent=2)
                           + ('\nРезультат: '+item['result_url'] if links else '')
                           +'\nПри неизвестном результате платного запроса сначала сверьте расход в кабинете. Новый запрос — только с новым подтверждением.',
                           buttons+[('Проект','p:'+item['project_id'])],links)
            elif action=='show_image':
                item=await repo.job(parts[1])
                if not item: raise ValueError('Задание не найдено.')
                await owner('project',item['project_id'])
                if item['status']!='done' or not repo.is_image_job(item):
                    raise ValueError('В этом задании пока нет готового изображения.')
                _,url=await result_for_job(item,force=True)
                from image_service import upload_image_for_max
                try:
                    image=await upload_image_for_max(event.bot,url)
                except Exception as exc:
                    logger.warning('MAX не принял изображение задания %s (%s).',item['id'],type(exc).__name__)
                    raise ValueError('Изображение сохранено в S3, но MAX временно не принял файл. Повторите показ; новая генерация не запускается.') from None
                await _reply(event,'🖼️ Изображение задания '+item['id'],attachments=[image])
            elif action=='approve_reference':
                asset_id=await repo.approve_character_result(parts[1],admin_id)
                await tell(event,'✅ Эталон утверждён и выбран для персонажа. ID: '+asset_id,
                           [('Задание','job:'+parts[1])])
            elif action=='publish_result':
                item=await repo.job(parts[1])
                if not item: raise ValueError('Задание не найдено.')
                await owner('project',item['project_id'])
                if item['status']!='manual' or not await repo.job_result(item['id']):
                    raise ValueError('Нет сохранённого результата для повторной публикации.')
                await repo.update_job(item['id'],status='publishing',attempts=0,lease_until=0,next_attempt=0,error=None)
                await tell(event,'Повторяется только публикация сохранённого файла, без нового запроса генерации.',
                           [('Задание','job:'+item['id'])])
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
        if text.startswith('/'): return
        try:
            key=task.get('key')
            if action=='description' or (action=='edit' and task.get('entity')=='character'):
                if not text:
                    raise ValueError(DESCRIPTION_PROMPT)
                if len(text)>10000:
                    raise ValueError('Описание слишком длинное: не более 10000 символов.\n'+DESCRIPTION_PROMPT)
                await repo.update_character_description(key,admin_id,text)
                logger.info('Обновлено описание персонажа %s: %s',key,text)
                await context.clear()
                await tell(event,'Описание персонажа сохранено.',[('👤 Персонаж','c:'+key)])
                return
            elif action=='scene_upload':
                await owner('scene',key)
                attachments=getattr(event.message.body,'attachments',None) or []
                photos=[a for a in attachments if a.type in ('image','file') and getattr(a.payload,'url',None)]
                logger.info('Мультстудия: вложения сцены scene=%s types=%s candidates=%s',
                            key,[str(a.type) for a in attachments],len(photos))
                if len(photos)!=1:
                    raise ValueError('Отправьте один JPG/JPEG или PNG через «Файл» для этой сцены.')
                from .uploads import upload_scene_image
                await upload_scene_image(assets,key,admin_id,photos[0].payload.url)
                await context.clear()
                await tell(event,'✅ Изображение загружено и утверждено. Перейти к оживлению?',
                           [('🎬 Оживить сцену','make:video:scene:'+key),('🎬 Сцена','s:'+key)])
                return
            elif action=='scene_visual' or (action=='edit' and task.get('entity')=='scene'):
                await repo.update_scene_visual(key,admin_id,text)
                logger.info('Обновлено визуальное описание сцены %s',key)
                await context.clear()
                await tell(event,'Описание сцены сохранено. Платные задания автоматически не запускаются.',
                           [('🎬 Сцена','s:'+key)])
                return
            elif action=='budget':
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
            elif action=='speech_start':
                value=await repo.set_scene_speech_start(key,admin_id,text)
                await context.clear()
                await tell(event,f'Начало речи: {value:g} с. Нажмите «Собрать серию», чтобы применить к готовым файлам.',
                           [('🎬 Сцена','s:'+key)])
                return
            elif action=='character_settings':
                profile=json.loads(text)
                if not isinstance(profile,dict) or set(profile)-{'voice','emotion','speed','pitch_shift'}:
                    raise ValueError('Пришлите только voice, emotion, speed и pitch_shift (для masha).')
                await repo.set_character_speech(key,admin_id,profile)
            elif action in ('new','edit','scene_settings'):
                entity=task['entity']; spec=validate_spec(entity,json.loads(text))
                if action in ('edit','character_settings','scene_settings'):
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
        data['scene_editing']=await repo.rows(db,'''SELECT t.* FROM cartoon_scene_editing t
            JOIN cartoon_scenes s ON s.id=t.scene_id JOIN cartoon_episodes e ON e.id=s.episode_id
            WHERE e.project_id=?''',(project,))
        data['speech_profiles']=await repo.rows(db,'''SELECT p.* FROM cartoon_character_speech_profiles p
            JOIN cartoon_characters c ON c.id=p.character_id WHERE c.project_id=?''',(project,))
        data['episodes']=await repo.rows(db,'SELECT * FROM cartoon_episodes WHERE project_id=?',(project,))
        data['scenes']=await repo.rows(db,'SELECT s.* FROM cartoon_scenes s JOIN cartoon_episodes e ON e.id=s.episode_id WHERE e.project_id=?',(project,))
        data['jobs']=await repo.rows(db,'SELECT * FROM cartoon_jobs WHERE project_id=?',(project,))
        data['assets']=await repo.rows(db,'SELECT * FROM cartoon_assets WHERE project_id=?',(project,))
        data['scene_image_selections']=await repo.rows(db,'''SELECT s.* FROM cartoon_scene_image_selections s
            JOIN cartoon_scenes c ON c.id=s.scene_id JOIN cartoon_episodes e ON e.id=c.episode_id
            WHERE e.project_id=?''',(project,))
        data['scene_asset_restorations']=await repo.rows(db,'''SELECT r.* FROM cartoon_scene_asset_restorations r
            JOIN cartoon_scenes s ON s.id=r.scene_id JOIN cartoon_episodes e ON e.id=s.episode_id
            WHERE e.project_id=?''',(project,))
        ids=[project]+[r['id'] for kind in ('characters','episodes','scenes') for r in data[kind]]
        data['versions']=await repo.rows(db,'SELECT * FROM cartoon_versions WHERE entity_id IN ('+','.join('?' for _ in ids)+')',ids)
        data['events']=await repo.rows(db,'SELECT v.* FROM cartoon_job_events v JOIN cartoon_jobs j ON j.id=v.job_id WHERE j.project_id=?',(project,))
    archive=assets.path(uuid4().hex+'.zip')
    def pack():
        with zipfile.ZipFile(archive,'w',compression=zipfile.ZIP_DEFLATED) as output:
            output.writestr('project.json',json.dumps(data,ensure_ascii=False,indent=2))
            packed=set()
            for asset in data['assets']:
                path=assets.path(asset['path'])
                relative='assets/'+path.relative_to(assets.root).as_posix()
                if path.is_file() and relative not in packed:
                    output.write(path,relative)
                    packed.add(relative)
    await asyncio.to_thread(pack)
    return await assets.publish(archive.name)
