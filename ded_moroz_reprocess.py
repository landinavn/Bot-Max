"""Offline processing of an unapproved Ded Moroz preview; never calls a TTS API."""
import argparse
import asyncio
import json
import sqlite3
from datetime import datetime

import database
from config import Config
from ded_moroz import Settings
from heygen_tts import HeyGenTTSAdapter
from multstudio.assets import Assets


async def reprocess(order_id, config, settings):
    assets = Assets(settings.root, config)
    adapter = HeyGenTTSAdapter(None, settings.voice_id, assets, model=settings.tts_model,
        speed=settings.speech_speed, pitch_semitones=settings.pitch_semitones,
        echo_delay_ms=settings.echo_delay_ms, echo_decay=settings.echo_decay)
    with sqlite3.connect(f'file:{database.DATABASE_PATH}?mode=rw', uri=True) as db:
        db.row_factory = sqlite3.Row
        row = db.execute('SELECT * FROM ded_moroz_orders WHERE id=?', (order_id,)).fetchone()
        if (not row or config.admin_user_id is None or row['user_id'] != config.admin_user_id
            or row['status'] != 'audio_ready' or row['approved_audio_path']
            or row['approved_audio_url'] or row['video_id'] or row['output_path']):
            raise ValueError('Нужна неутверждённая озвучка администратора в состоянии audio_ready.')
        signature = adapter.fingerprint(row['text'])
        old = assets.path(row['audio_path'])
        manifest = json.loads(old.with_suffix('.heygen.json').read_text())
        if (manifest['request_sha256'] != signature
            or manifest['audio_sha256'] != assets.checksum(old)):
            raise ValueError('Текст, голос или файл не совпадают с сохранённой озвучкой.')
        if manifest.get('delivery_sha256') == adapter.delivery_hash():
            return {'changed': False, 'version': row['version']}
        raw = assets.filename('mp3', order_id, f"speech-v{row['version']}.raw.mp3")
        if not assets.path(raw).is_file():
            raise ValueError('Исходный MP3 отсутствует. Платный синтез не запускался.')
        backup = database.DATABASE_PATH.with_name('database.before-dm-effects-' +
            datetime.now().strftime('%Y%m%d-%H%M%S-%f') + '.sqlite3')
        with sqlite3.connect(backup) as copy:
            db.backup(copy)
        version = row['version'] + 1
        audio, meta, _ = adapter.names(order_id, version)
        # Process the ORIGINAL, never compound effects by processing a previous output.
        await adapter.process_delivery(raw, audio, meta, job_id=order_id, signature=signature)
        # Keep an original for further free adjustments of this new version.
        await assets.write(assets.path(raw).read_bytes(), 'mp3', job_id=order_id,
                           name=f'speech-v{version}.raw.mp3')
        db.execute('BEGIN IMMEDIATE')
        cursor = db.execute("""UPDATE ded_moroz_orders SET version=?,audio_path=?,audio_url=NULL,
            speech_provider_url=NULL,status='audio_publish',preview_sent=0,preview_mid=NULL,
            preview_version=NULL,error=NULL,next_attempt=0 WHERE id=? AND version=?
            AND status='audio_ready' AND audio_path=? AND approved_audio_path IS NULL
            AND approved_audio_url IS NULL AND video_id IS NULL AND output_path IS NULL""",
            (version, audio, order_id, row['version'], row['audio_path']))
        if cursor.rowcount != 1:
            db.rollback()
            raise ValueError('Заказ изменился во время обработки. Новая озвучка не подключена.')
        db.execute("""INSERT INTO ded_moroz_audio_versions
            (order_id,version,text,voice_id,provider_url,path,provider,submitted_text,model,request_sha256)
            VALUES (?,?,?,?,?,?,?,?,?,?)""", (order_id, version, row['text'], settings.voice_id,
                '', audio, 'heygen', row['text'], settings.tts_model, signature))
        db.commit()
        return {'changed': True, 'version': version, 'backup': str(backup), 'delivery': adapter.delivery}


def main():
    from dataclasses import replace
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('order_id')
    parser.add_argument('--speed', type=float, default=0.85)
    parser.add_argument('--pitch', type=float, default=-1.5)
    parser.add_argument('--echo-delay', type=float, default=100)
    parser.add_argument('--echo-decay', type=float, default=0.18)
    args = parser.parse_args()
    config = Config.from_env()
    settings = replace(Settings.from_config(config), speech_speed=args.speed,
        pitch_semitones=args.pitch, echo_delay_ms=args.echo_delay, echo_decay=args.echo_decay)
    result = asyncio.run(reprocess(args.order_id, config, settings))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
