"""Mother's Day deliveries are counted once by retained source, excluding admin in reports."""
import hashlib
import aiosqlite
import database

EVENTS={'mother_music_ready','mother_cartoon_ready','mother_audio_ready','mother_video_song_ready','mother_song_pick_1','mother_song_pick_2'}


async def record(user,event,source):
    if event not in EVENTS or not isinstance(source,str) or not source:raise ValueError('Некорректное событие Дня матери.')
    key=hashlib.sha256(source.encode()).hexdigest()
    async with aiosqlite.connect(database.DATABASE_PATH,timeout=30) as db:
        await db.execute('BEGIN IMMEDIATE')
        cur=await db.execute('INSERT OR IGNORE INTO mother_events(user_id,event_name,source) VALUES (?,?,?)',(user,event,key))
        if cur.rowcount:await database.increment_counter(event,user_id=user,connection=db)
        await db.commit()
        return bool(cur.rowcount)


async def sales(admin=None):
    result={}
    async with aiosqlite.connect(database.DATABASE_PATH,timeout=30) as db:
        for key,table,where in (
            ('cartoon','cartoon_orders',"theme='theme_mother' AND status IN ('paid','completed','manual')"),
            ('music','musical_orders',"mode='mode_mother' AND status='paid'"),
            ('audio','audio_orders',"scenario='mother' AND status='paid'")):
            async with db.execute('SELECT COUNT(*),COALESCE(SUM(amount_kopecks),0) FROM '+table+' WHERE '+where+' AND (? IS NULL OR user_id!=?)',(admin,admin)) as cur:
                count,revenue=await cur.fetchone()
            result[key+'_paid']=count;result[key+'_revenue']=revenue
    return result


def text(c,all_c,paid):
    keys=('click_mother_card','click_mother_music','theme_mother_click','audio_scen_mother','audio_gift_mother',
          'gen_mother','mother_music_ready','mother_cartoon_ready','mother_audio_ready','mother_video_song_ready','mother_song_pick_1','mother_song_pick_2')
    return ('🤱 День матери (клиенты):\n'
        f"- Выбрали: открытка {c('click_mother_card')}, музыкальная {c('click_mother_music')}, мульт {c('theme_mother_click')}, короткая песня {c('audio_scen_mother')}, полная {c('audio_gift_mother')}\n"
        f"- Готово: открыток {c('gen_mother')}, музыкальных видео {c('mother_music_ready')}, мультфильмов {c('mother_cartoon_ready')}, аудиозаказов {c('mother_audio_ready')}\n"
        f"- Песен для видео (заданий): {c('mother_video_song_ready')}\n"
        f"- Выбрали исполнение: спокойное {c('mother_song_pick_1')}, чуть веселее {c('mother_song_pick_2')}\n"
        f"- Оплаты: мульт {paid['cartoon_paid']} / {paid['cartoon_revenue']//100} ₽, музыкальная {paid['music_paid']} / {paid['music_revenue']//100} ₽, песни {paid['audio_paid']} / {paid['audio_revenue']//100} ₽\n"
        f"- Покупки мультфильма: 590 ₽ — {c('buy_cartoon_mother_590')}, прежние 790 ₽ — {c('buy_cartoon_790')}\n"
        f"- Рассылка: доставлено {c('mother_broadcast_delivered')}, ошибок {c('mother_broadcast_errors')}, нажали Старт {c('mother_broadcast_start')}\n"
        '- Администратор — '+', '.join(f'{label}: {all_c.get(key,0)-c(key)}' for key,label in zip(keys,('выбор открытки','выбор музыкальной','выбор мультфильма','выбор короткой песни','выбор полной песни','готовые открытки','готовые музыкальные','готовые мультфильмы','готовые аудиозаказы','песни для видео','спокойное исполнение','весёлое исполнение')))+'\n\n')


async def send_report(event,text):
    """MAX requires fewer than 4000 characters per message, including long traffic lists."""
    from handlers import _reply
    chunk=''
    for line in text.splitlines(keepends=True):
        while len(line)>3500:
            if chunk:await _reply(event,chunk);chunk=''
            await _reply(event,line[:3500]);line=line[3500:]
        if len(chunk)+len(line)>3500:
            await _reply(event,chunk);chunk=''
        chunk+=line
    if chunk:await _reply(event,chunk)
