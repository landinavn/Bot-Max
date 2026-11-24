"""Voice choice and explicit lyric approval; never submits a music generation."""
from uuid import uuid4

from maxapi.types import CallbackButton
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder

VOICES={'male':('👨 Мужской','male solo vocals'),
        'female':('👩 Женский','female solo vocals'),
        'mixed':('👥 Мужской и женский','duet, male and female vocals')}


def approved(data,details):
    return bool(data.get('song_review_approved') and data.get('song_review_details')==details
                and data.get('song_review_lyrics') and data.get('song_review_voice') in VOICES)


def arguments(data,details,fallback):
    if approved(data,details):
        return data['song_review_notes'],data['song_review_tags'],data['song_review_title']
    return fallback


async def show_voice(event,context):
    from handlers import _reply, navigation_keyboard
    d=await context.get_data()
    buttons=[(label,f"song_review:voice:{key}:{d['song_review_token']}") for key,(label,_) in VOICES.items()]
    keyboard=navigation_keyboard((*buttons,('🏠 Главный экран','back_to_main')),columns=2)
    await _reply(event,'🎙 Каким голосом исполнить песню?\n\nСмешанный — мужской и женский вместе. После выбора покажу текст для проверки.',attachments=[keyboard])


async def prepare(event,context,*,flow,details,label,audio_format,style=None,writer=None,admin=None):
    from handlers import PosterStates
    from personal_song import build_request as build_song_request
    from mothers_day import MOTHER_LABELS
    if label in MOTHER_LABELS or flow in ('video','teacher'):
        audio_format='gift'  # Review the full approved song; the cartoon uses an excerpt.
    d=await context.get_data()
    source=[flow,details,label,audio_format,style]
    if approved(d,details) and d.get('song_review_source')==source:return False
    notes,tags,title=build_song_request(details,label,audio_format,style)
    if style:tags=style+', Russian vocals, '+('short song, 35 seconds, vocals from the start' if audio_format=='fun' else 'full song, 2-3 minutes')
    await context.update_data(song_review_source=source,song_review_flow=flow,song_review_details=details,song_review_user=event.get_ids()[1],song_review_admin=admin is not None and event.get_ids()[1]==admin,
        song_review_label=label,song_review_format=audio_format,song_review_style=style,
        song_review_notes=notes,song_review_base_tags=tags,song_review_title=title,
        song_review_lyrics=None,song_review_approved=False,song_review_voice=None,
        song_review_step='voice',song_review_token=uuid4().hex,song_review_no_preview=True)
    await context.set_state(PosterStates.waiting_for_song_review)
    if d.get('song_setup_voice') in VOICES and d.get('song_setup_flow')==flow:
        await context.update_data(song_review_voice=d['song_setup_voice'])
        await write_text(event,context,writer)
    else:await show_voice(event,context)
    return True


async def show_text(event,context):
    from handlers import _reply
    d=await context.get_data();token=d['song_review_token'];b=InlineKeyboardBuilder()
    if d['song_review_flow'] in ('fun','gift'):
        b.row(CallbackButton(text='🎵 Сделать песню',payload='song_review:approve:'+token))
        b.row(CallbackButton(text='🎬 Сделать мульт-открытку',payload='song_review:cartoon:'+token))
        b.row(CallbackButton(text='🎵 Сделать музыкальную открытку',payload='song_review:music:'+token))
    else:
        b.row(CallbackButton(text='✅ Текст подходит, продолжить',payload='song_review:approve:'+token))
        if d.get('card_song_precheckout'):
            b.row(CallbackButton(text='🎵 Сделать только песню',payload='song_review:audio:'+token))
            if d.get('card_song_kind')!='cartoon':
                b.row(CallbackButton(text='🎬 Песня с мульт-открыткой',payload='song_review:cartoon:'+token))
    if d.get('song_review_admin'):
        b.row(CallbackButton(text='✍️ Переписать текст песни',payload='song_review:rewrite:'+token))
        b.row(CallbackButton(text='📝 Вставить свой текст',payload='song_review:manual:'+token))
    if d.get('song_review_no_preview') and not d.get('song_review_admin'):
        b.row(CallbackButton(text='📝 Изменить текст песни',payload='song_review:text_edit:'+token))
    b.row(CallbackButton(text='✏️ Изменить мои слова',payload='song_review:edit:'+token))
    b.row(CallbackButton(text='🎙 Изменить голос',payload='song_review:voice_menu:'+token))
    b.row(CallbackButton(text='🏠 Главный экран',payload='back_to_main'))
    text=d['song_review_lyrics']
    from mothers_day import MOTHER_LABELS, minute_excerpt
    if (not d.get('song_review_admin') and d.get('song_review_label') in MOTHER_LABELS
            and (d.get('song_review_flow')=='cartoon' or d.get('song_review_flow')=='product:cartoon')):
        text=minute_excerpt(text)
    for a,z in (('[Verse 1]','Куплет 1'),('[Verse 2]','Куплет 2'),('[Chorus]','Припев'),('[Bridge]','Переход')):text=text.replace(a,z)
    # Complete text may exceed a single MAX message. Only the final part carries actions.
    parts=[text[i:i+3000] for i in range(0,len(text),3000)]
    await _reply(event,'📝 Проверьте текст песни: имя, вашу мысль и поздравление.\n🎙 Голос: '+VOICES[d['song_review_voice']][0]+'\n\nМузыка ещё не создавалась.')
    for i,part in enumerate(parts):await _reply(event,part,attachments=[b.as_markup()] if i==len(parts)-1 else [])


async def write_text(event,context,writer):
    from handlers import _reply
    from audio_service import validate_song_arguments
    d=await context.get_data();token=d['song_review_token']
    await context.update_data(song_review_step='writing')
    try:
        tags=d['song_review_base_tags']+', '+VOICES[d['song_review_voice']][1]
        text=d.get('song_review_lyrics')
        if not text:
            await _reply(event,'✍️ Подготовлю слова песни. После проверки вы сможете их подтвердить или изменить.')
            text=await writer(d['song_review_notes'],tags,d['song_review_title'])
        validate_song_arguments(text,tags,d['song_review_title'])
        if (await context.get_data()).get('song_review_token')!=token:return
        await context.update_data(song_review_lyrics=text,song_review_tags=tags,song_review_step='text')
        await show_text(event,context)
    except Exception:
        if (await context.get_data()).get('song_review_token')==token:
            await context.update_data(song_review_step='voice')
            await show_voice(event,context)
        raise


async def action(event,context,writer):
    from handlers import PosterStates,_reply
    if await context.get_state()!=PosterStates.waiting_for_song_review:return None
    d=await context.get_data();parts=event.callback.payload.split(':');action=parts[1]
    if parts[-1]!=d.get('song_review_token'):return None
    if action=='voice' and len(parts)==4 and parts[2] in VOICES and d.get('song_review_step')=='voice':
        import database
        await database.increment_counter('song_voice_'+parts[2],user_id=event.get_ids()[1])
        await context.update_data(song_review_voice=parts[2],song_setup_voice=parts[2]);await write_text(event,context,writer)
    elif action=='voice_menu' and d.get('song_review_step')=='text':
        await context.update_data(song_review_step='voice');await show_voice(event,context)
    elif action in ('rewrite','manual') and d.get('song_review_step')=='text' and d.get('song_review_admin'):
        if action=='manual':
            await context.update_data(song_review_step='manual')
            await _reply(event,'📝 Отправьте полный текст песни. Картинка и выбранный голос сохранятся. До 5000 символов. После этого покажу текст для подтверждения.')
        else:
            await context.update_data(song_review_lyrics=None,song_review_approved=False,song_review_rewrite_pending=True,song_review_token=uuid4().hex)
            await _reply(event,'✍️ Перепишу только слова. Картинка, аватар и готовые сцены сохранены. Новая музыка будет создана после подтверждения текста.')
            await write_text(event,context,writer)
    elif action=='text_edit' and d.get('song_review_step')=='text' and d.get('song_review_no_preview'):
        import database
        await database.increment_counter('song_lyrics_edit',user_id=event.get_ids()[1])
        await context.update_data(song_review_step='manual')
        await _reply(event,'📝 Скопируйте показанный текст песни, внесите правки и пришлите его одним сообщением. Сохраните заголовки куплетов и припева. До 5000 символов. Затем снова покажу текст для подтверждения; музыка ещё не создавалась.')
    elif action=='edit' and d.get('song_review_step')=='text':
        import database
        await database.increment_counter('song_lyrics_edit',user_id=event.get_ids()[1])
        await context.update_data(song_review_step='input')
        from personal_song import DETAILS_PROMPT
        await _reply(event,DETAILS_PROMPT+'\n\nСейчас: '+d['song_review_details']+'\n\nОбновлённый текст снова покажем для подтверждения.')
    elif action in ('approve','cartoon','music','audio') and d.get('song_review_step')=='text' and d.get('song_review_lyrics'):
        if action!='approve' and d['song_review_flow'] not in ('fun','gift') and not d.get('card_song_precheckout'):return None
        if action=='audio' and not d.get('card_song_precheckout'):return None
        import database
        await database.increment_counter('song_lyrics_approved',user_id=event.get_ids()[1])
        product=(d.get('card_song_kind') or 'audio') if action=='approve' else action
        await database.increment_counter('song_product_'+product,user_id=event.get_ids()[1])
        await context.update_data(song_review_approved=True,song_review_step='used')
        return 'product:'+action if action!='approve' else d['song_review_flow']
    return None


async def edit(event,context,writer):
    from handlers import _reply
    from personal_song import build_request as build_song_request
    d=await context.get_data()
    if d.get('song_review_step')=='manual' and (d.get('song_review_admin') or d.get('song_review_no_preview')):
        from audio_service import validate_song_arguments
        text=(getattr(event.message.body,'text',None) or '').strip()
        for source,target in (('Куплет 1','[Verse 1]'),('Куплет 2','[Verse 2]'),('Припев','[Chorus]'),('Переход','[Bridge]')):
            text='\n'.join(target if line.strip()==source else line for line in text.splitlines())
        validate_song_arguments(text,d['song_review_tags'],d['song_review_title'])
        import re
        from mothers_day import MOTHER_LABELS
        if d.get('song_review_label') in MOTHER_LABELS and d.get('song_review_flow')=='cartoon' and not d.get('song_review_admin'):
            parts=re.findall(r'\[([^\]]+)\]\s*\n(.*?)(?=\n\[|$)',text,re.S)
            if [tag for tag,_ in parts]!=['Verse 1','Chorus']:
                raise ValueError('Пришлите показанный куплет и припев с заголовками.')
            verse,chorus=(body.strip() for _,body in parts)
            full=d['song_review_lyrics']
            text=re.sub(r'\[Verse 1\]\s*\n.*?(?=\n\[|$)',lambda _: '[Verse 1]\n'+verse,full,flags=re.S)
            text=re.sub(r'\[Chorus\]\s*\n.*?(?=\n\[|$)',lambda _: '[Chorus]\n'+chorus,text,flags=re.S)
        if not re.findall(r'\[Chorus\]',text):raise ValueError('Сохраните заголовки куплетов и припева.')
        validate_song_arguments(text,d['song_review_tags'],d['song_review_title'])
        await context.update_data(song_review_lyrics=text,song_review_approved=False,song_review_rewrite_pending=True,
            song_review_step='text',song_review_token=uuid4().hex)
        await show_text(event,context)
        return
    if d.get('song_review_step')!='input':
        await _reply(event,'Используйте кнопки под текстом песни.');return
    text=(getattr(event.message.body,'text',None) or '').strip()
    if not text or len(text)>1000:
        await _reply(event,'Напишите имя и свои слова одним сообщением, до 1000 символов.');return
    notes,tags,title=build_song_request(text,d['song_review_label'],d['song_review_format'],d['song_review_style'])
    if d['song_review_style']:tags=d['song_review_style']+', Russian vocals, '+('short song, 35 seconds, vocals from the start' if d['song_review_format']=='fun' else 'full song, 2-3 minutes')
    flow=d['song_review_flow']
    changes={'fun':{'song_details':text,'song_chorus_phrase':text},
             'gift':{'gift_details':text,'gift_chorus_phrase':text},
             'cartoon':{'chosen_song_phrase':text,'video_song_phrase':text,'chosen_song_url':None},
             'video':{'song_custom_text':text,'video_song_phrase':text},
             'teacher':{'video_teacher_details':text,'video_song_phrase':text}}[flow]
    source=[flow,text,d['song_review_label'],d['song_review_format'],d['song_review_style']]
    await context.update_data(**changes,song_review_source=source,song_review_details=text,
        song_review_notes=notes,song_review_base_tags=tags,song_review_title=title,
        song_review_lyrics=None,song_review_approved=False,song_review_token=uuid4().hex,
        audio_lyrics=None,audio_checkout_id=None,audio_checkout_link=None)
    await write_text(event,context,writer)

GENRES={'pop':('🎶 Поп','warm pop'),'rap':('🎤 Рэп / хип-хоп','melodic hip hop'),
        'rock':('🎸 Рок','melodic rock'),'classic':('🎻 Классика','orchestral ballad'),
        'electro':('⚡ Электро','electronic pop'),'jazz':('🎷 Джаз','soft jazz'),
        'chanson':('🎼 Шансон','lyrical chanson')}


async def start_questions(event,context,*,flow,prompt,state):
    from handlers import PosterStates,_reply
    token=uuid4().hex
    await context.update_data(song_setup_flow=flow,song_setup_prompt=prompt,song_setup_state=str(state),
        song_setup_step='genre',song_setup_token=token,song_setup_voice=None)
    await context.set_state(PosterStates.waiting_for_song_setup)
    b=InlineKeyboardBuilder()
    items=list(GENRES.items())
    for i in range(0,len(items),2):b.row(*(CallbackButton(text=v[0],payload=f'song_setup:genre:{key}:{token}') for key,v in items[i:i+2]))
    b.row(CallbackButton(text='✨ Свой стиль',payload='song_setup:genre:custom:'+token))
    b.row(CallbackButton(text='🏠 Главный экран',payload='back_to_main'))
    await _reply(event,'🎼 Теперь выберите жанр песни:',attachments=[b.as_markup()])
    return True


async def setup_voice(event,context):
    from handlers import _reply, navigation_keyboard
    d=await context.get_data()
    buttons=[(label,f"song_setup:voice:{key}:{d['song_setup_token']}") for key,(label,_) in VOICES.items()]
    keyboard=navigation_keyboard((*buttons,('🏠 Главный экран','back_to_main')),columns=2)
    await _reply(event,'🎙 Кто исполнит вашу песню? Смешанный вариант — мужской и женский вместе.',attachments=[keyboard])


async def setup_action(event,context):
    from handlers import PosterStates,_reply
    import database
    if await context.get_state()!=PosterStates.waiting_for_song_setup:return
    d=await context.get_data();_,action,key,token=event.callback.payload.split(':')
    if token!=d.get('song_setup_token'):return
    if action=='genre' and d.get('song_setup_step')=='genre':
        if key=='custom':
            await context.update_data(song_setup_step='custom')
            await _reply(event,'Напишите желаемый стиль музыки, например «нежная акустическая баллада с пианино». До 100 символов.');return
        if key not in GENRES:return
        await database.increment_counter('song_genre_'+key,user_id=event.get_ids()[1])
        await context.update_data(song_style=GENRES[key][1],song_setup_step='voice');await setup_voice(event,context)
    elif action=='voice' and d.get('song_setup_step')=='voice' and key in VOICES:
        await context.update_data(song_setup_voice=key,song_setup_step='details')
        await database.increment_counter('song_voice_'+key,user_id=event.get_ids()[1])
        await context.set_state(d['song_setup_state'])
        await _reply(event,d['song_setup_prompt'])


async def custom_style(event,context):
    from handlers import _reply
    d=await context.get_data()
    if d.get('song_setup_step')!='custom':return
    text=(getattr(event.message.body,'text',None) or '').strip()
    if not text or len(text)>100:
        await _reply(event,'Опишите стиль музыки, до 100 символов.');return
    import database
    await database.increment_counter('song_genre_custom',user_id=event.get_ids()[1])
    await context.update_data(song_style=text,song_setup_step='voice');await setup_voice(event,context)
