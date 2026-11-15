"""Queue free montage of a completed greeting using its saved HeyGen video and approved speech."""
import argparse
import json
import sqlite3
from datetime import datetime, timezone
from uuid import uuid4

import database
from config import Config
from ded_moroz import Settings
from multstudio.assets import Assets


def queue_rebuild(config, settings, order_id=None):
    if config.admin_user_id is None:
        raise ValueError('Настройте ADMIN_USER_ID.')
    assets = Assets(settings.root, config)
    with sqlite3.connect(f'file:{database.DATABASE_PATH}?mode=rw', uri=True) as db:
        db.row_factory = sqlite3.Row
        row = (db.execute('SELECT * FROM ded_moroz_orders WHERE id=?', (order_id,)).fetchone()
            if order_id else db.execute("SELECT * FROM ded_moroz_orders WHERE user_id=? ORDER BY rowid DESC LIMIT 1",
                                      (config.admin_user_id,)).fetchone())
        if not row or row['user_id'] != config.admin_user_id or row['status'] != 'done':
            raise ValueError('Нужен завершённый заказ этого администратора.')
        for name, path in (('видео HeyGen', row['raw_path']), ('фото', row['photo_path']),
                           ('озвучка', row['approved_audio_path'] or row['audio_path']),
                           ('предыдущая сборка', row['output_path'])):
            if not path or not assets.path(path).is_file():
                raise ValueError('Не найден сохранённый файл: ' + name + '. Новая генерация не запускалась.')
        for name in ('ny_music.mp3','jingle_bells.mp3'):
            if not (settings.examples / 'audio' / name).is_file():
                raise ValueError('Не найден файл монтажа: ' + name)
        if not isinstance(json.loads(row['details']).get('name'), str):
            raise ValueError('Не найдено сохранённое имя ребёнка.')
        stamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-%f')
        backup = database.DATABASE_PATH.with_name('database.before-dm-rebuild-' + stamp + '.sqlite3')
        with sqlite3.connect(backup) as copy:
            db.backup(copy)
        request_id = uuid4().hex
        target = assets.filename('mp4', row['id'], 'greeting-' + request_id + '.mp4')
        archive = assets.path(assets.filename('json', row['id'], 'rebuild-' + request_id + '.json'))
        # Existing output and public URL remain recoverable; no file is removed or renamed.
        with archive.open('x', encoding='utf-8') as output:
            json.dump({'order_id':row['id'], 'previous_output_path':row['output_path'],
                'previous_output_url':row['output_url'], 'video_id':row['video_id'],
                'raw_path':row['raw_path'], 'approved_audio_path':row['approved_audio_path'] or row['audio_path'],
                'target':target, 'reason':'remove_fire_overlay', 'created_at_utc':stamp}, output, ensure_ascii=False)
        db.execute('BEGIN IMMEDIATE')
        active = db.execute("SELECT id FROM ded_moroz_orders WHERE user_id=? AND id<>? AND status NOT IN ('done','cancelled')",
                            (config.admin_user_id, row['id'])).fetchone()
        if active:
            raise ValueError('Есть другой незавершённый заказ. Пересборка не поставлена в очередь.')
        cursor = db.execute("""UPDATE ded_moroz_orders SET status='render',output_path=?,output_url=NULL,
            error=NULL,next_attempt=0 WHERE id=? AND user_id=? AND status='done'
            AND output_path IS ? AND raw_path IS ? AND approved_audio_path IS ? AND audio_path IS ?""",
            (target,row['id'],config.admin_user_id,row['output_path'],row['raw_path'],row['approved_audio_path'],row['audio_path']))
        if cursor.rowcount != 1:
            raise ValueError('Заказ изменился. Пересборка не поставлена в очередь.')
        db.commit()
        return {'order_id':row['id'], 'status':'render', 'backup':str(backup),
                'previous_output':row['output_path'], 'target':target, 'paid_requests':0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('order_id', nargs='?', help='Order ID; otherwise the latest admin order (must be completed)')
    args = parser.parse_args()
    config = Config.from_env()
    print(json.dumps(queue_rebuild(config,Settings.from_config(config),args.order_id), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
