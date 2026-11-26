"""Explicit repair revision of a completed Mother film; no provider calls or automatic retries."""
import argparse
import copy
import json
import math
import os
from pathlib import Path
import sqlite3
import tempfile
import time

from mother_recovery import urls
from mother_storyboard import SHOT_SECONDS, storyboard
from song_alignment import alignment_source


VISUAL_FIELDS = ('cartoon_kling_story','cartoon_kling_source','cartoon_kling_task_id',
                 'cartoon_kling_video_url','video_merged_url','video_merged_source')


def prepare_revision(data):
    """Pure deep copy. Original successful video/IDs remain in a retained archive."""
    image,audio=urls(data)
    old=data.get('cartoon_kling_story') or {}
    lyrics=data.get('song_review_lyrics') or data.get('video_song_lyrics') or ''
    start=old.get('audio_start')
    if (data.get('cartoon_theme') not in ('theme_mother','theme_mother_bday') or not image or not audio
            or not data.get('chosen_avatar') or data['chosen_avatar']!=image or not lyrics
            or not (old.get('result_url') or data.get('video_merged_url'))):
        raise ValueError('Нужен завершённый мультфильм с сохранёнными песней и утверждённым персонажем.')
    if (old.get('alignment_source')!=alignment_source(audio,lyrics) or isinstance(start,bool)
            or not isinstance(start,(int,float)) or not math.isfinite(start) or not 0<=start<=29):
        raise ValueError('Начало песни не подтверждено для текущего исполнения и текста.')
    if any((shot.get('image_submitting') and not shot.get('image_url'))
           or (shot.get('video_submitting') and not shot.get('task_id')) for shot in old.get('shots',[])):
        raise ValueError('Есть запросы с неизвестным результатом. Новая ревизия запрещена.')
    result=copy.deepcopy(data)
    archive={key:copy.deepcopy(data[key]) for key in VISUAL_FIELDS if key in data}
    archive.update(reason='approved_identity_repair',audio_url=audio,audio_start=start)
    result.setdefault('cartoon_kling_story_archive',[]).append(archive)
    for key in VISUAL_FIELDS:result.pop(key,None)
    policy,shots,_=storyboard(data['cartoon_theme']=='theme_mother_bday')
    result['cartoon_kling_story']={'source':[policy,image],'shot_seconds':SHOT_SECONDS,
        'shots':[{} for _ in shots],'alignment_source':alignment_source(audio,lyrics),
        'audio_start':float(start),'alignment_method':old.get('alignment_method','admin')}
    result['cartoon_video_failed']=False
    return result


def prepare_order(path, order_id, *, apply=False):
    """Default read-only plan. --prepare backs up DB and stages one CAS-protected revision."""
    path=Path(path).expanduser().resolve()
    uri=path.as_uri()+('?mode=rw' if apply else '?mode=ro')
    db=sqlite3.connect(uri,uri=True,timeout=10);db.row_factory=sqlite3.Row
    try:
        row=db.execute('SELECT * FROM cartoon_orders WHERE order_id=?',(order_id,)).fetchone()
        if not row or row['status']!='completed' or not row['paid_at']:
            raise ValueError('Нужен завершённый оплаченный заказ. Повторное восстановление не выполняется.')
        original=json.loads(row['fsm_data'] or '{}')
        if original.get('cartoon_order_id')!=order_id:
            raise ValueError('Данные принадлежат другому заказу.')
        revised=prepare_revision(original)
        summary={'order_id':order_id,'user_id':row['user_id'],'shots':len(revised['cartoon_kling_story']['shots']),
                 'audio_start':revised['cartoon_kling_story']['audio_start'],'prepared':False}
        if not apply:return summary
        fd,backup=tempfile.mkstemp(prefix='database.before-mother-identity-',suffix='.sqlite3',dir=path.parent)
        os.close(fd);deadline=time.monotonic()+30
        def progress(*_):
            if time.monotonic()>deadline:raise TimeoutError('Не завершено резервное копирование; заказ не изменён.')
        destination=sqlite3.connect(backup)
        try:db.backup(destination,pages=128,progress=progress,sleep=.05)
        finally:destination.close()
        cursor=db.execute("UPDATE cartoon_orders SET status='manual',fsm_state=?,fsm_data=? "
            "WHERE order_id=? AND user_id=? AND status='completed' AND paid_at IS NOT NULL AND fsm_data=?",
            ('PosterStates:cartoon_manual_review',json.dumps(revised,ensure_ascii=False),order_id,row['user_id'],row['fsm_data']))
        if cursor.rowcount!=1:
            db.rollback();raise ValueError('Заказ изменился параллельно. Восстановление отменено.')
        db.commit()
        return {**summary,'prepared':True,'backup':str(backup)}
    finally:db.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database',type=Path,required=True)
    parser.add_argument('--order-id',required=True)
    parser.add_argument('--prepare',action='store_true',help='Back up and stage a new visual revision; no generation is started')
    args=parser.parse_args()
    try:summary=prepare_order(args.database,args.order_id,apply=args.prepare)
    except (ValueError,TimeoutError) as exc:raise SystemExit(str(exc)) from None
    print(json.dumps(summary,ensure_ascii=False,indent=2))
    if args.prepare:
        print('Песня и старые результаты сохранены. Генерации не запускались. Продолжение через старую кнопку заказа запускает новые платные сцены.')
    else:
        print('План: новая версия из 6 сцен с утверждённым персонажем. Изменений и платных запросов нет.')


if __name__=='__main__':main()
