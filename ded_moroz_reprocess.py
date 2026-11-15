"""Offline processing of an unapproved Ded Moroz preview; never calls a TTS API."""
import argparse
import asyncio
import json
import re
import sqlite3
from datetime import datetime

import database
from config import Config
from ded_moroz import Settings
from heygen_tts import HeyGenTTSAdapter, display_text
from multstudio.assets import Assets


async def reprocess(order_id, config, settings, *, source_version=None):
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
        source = row
        if source_version is not None:
            if type(source_version) is not int or source_version < 0:
                raise ValueError('Некорректная исходная версия.')
            source = db.execute('SELECT * FROM ded_moroz_audio_versions WHERE order_id=? AND version=?',
                                (order_id, source_version)).fetchone()
            if not source or not source['path'] or source['provider'] != 'heygen':
                raise ValueError('Сохранённая версия HeyGen не найдена в этом заказе.')
            # Tags/punctuation may differ, but do not restore another child's words/facts.
            words = lambda text: re.findall(r'\w+', display_text(text).casefold())
            if words(source['text']) != words(row['text']):
                raise ValueError('В выбранной версии другой текст поздравления.')
            if source['voice_id'] != settings.voice_id or source['model'] != settings.tts_model:
                raise ValueError('Голос или модель выбранной версии не совпадают с настройками.')
        text = source['text']
        signature = adapter.fingerprint(text)
        source_path = source['path'] if source_version is not None else row['audio_path']
        old = assets.path(source_path)
        manifest = json.loads(old.with_suffix('.heygen.json').read_text())
        if (manifest['request_sha256'] != signature
            or manifest['audio_sha256'] != assets.checksum(old)):
            raise ValueError('Текст, голос или файл не совпадают с сохранённой озвучкой.')
        current_meta = assets.path(row['audio_path']).with_suffix('.heygen.json')
        current_manifest = json.loads(current_meta.read_text())
        selection = current_meta.with_name(current_meta.name.replace('.heygen.json','.source-selection.json'))
        if selection.is_file():
            current_manifest['source_asset'] = json.loads(selection.read_text())['source_asset']
        if source_version is not None and source_version == row['version']:
            raise ValueError('Выберите предыдущую версию из архива.')
        provenance = {'version': source_version, 'path': source_path,
                      'sha256': assets.checksum(old)} if source_version is not None else None
        if (current_manifest.get('delivery_sha256') == adapter.delivery_hash()
            and current_manifest.get('source_asset') == provenance):
            if current_manifest.get('audio_sha256') != assets.checksum(assets.path(row['audio_path'])):
                raise ValueError('Текущий MP3 повреждён.')
            return {'changed': False, 'version': row['version']}
        raw = source_path if source_version is not None else assets.filename(
            'mp3', order_id, f"speech-v{row['version']}.raw.mp3")
        if not assets.path(raw).is_file():
            raise ValueError('Исходный MP3 отсутствует. Платный синтез не запускался.')
        backup = database.DATABASE_PATH.with_name('database.before-dm-effects-' +
            datetime.now().strftime('%Y%m%d-%H%M%S-%f') + '.sqlite3')
        with sqlite3.connect(backup) as copy:
            db.backup(copy)
        version = row['version'] + 1
        audio, meta, _ = adapter.names(order_id, version)
        # Explicit archive selection uses its audible final MP3; never use the latest output by mistake.
        await adapter.process_delivery(raw, audio, meta, job_id=order_id, signature=signature)
        if provenance:
            metadata = json.loads(assets.path(meta).read_text())
            metadata['source_asset'] = provenance
            # Separate immutable provenance, so the adapter's cache manifest is never overwritten.
            await assets.write(json.dumps(metadata).encode(), 'json', job_id=order_id,
                               name=meta.split('/')[-1].replace('.heygen.json','.source-selection.json'))
        # Keep an original for further free adjustments of this new version.
        await assets.write(assets.path(raw).read_bytes(), 'mp3', job_id=order_id,
                           name=f'speech-v{version}.raw.mp3')
        db.execute('BEGIN IMMEDIATE')
        cursor = db.execute("""UPDATE ded_moroz_orders SET version=?,text=?,audio_path=?,audio_url=NULL,
            speech_provider_url=NULL,status='audio_publish',preview_sent=0,preview_mid=NULL,
            preview_version=NULL,error=NULL,next_attempt=0 WHERE id=? AND version=?
            AND status='audio_ready' AND audio_path=? AND approved_audio_path IS NULL
            AND approved_audio_url IS NULL AND video_id IS NULL AND output_path IS NULL AND text=?""",
            (version, text, audio, order_id, row['version'], row['audio_path'],row['text']))
        if cursor.rowcount != 1:
            db.rollback()
            raise ValueError('Заказ изменился во время обработки. Новая озвучка не подключена.')
        db.execute("""INSERT INTO ded_moroz_audio_versions
            (order_id,version,text,voice_id,provider_url,path,provider,submitted_text,model,request_sha256)
            VALUES (?,?,?,?,?,?,?,?,?,?)""", (order_id, version, text, settings.voice_id,
                '', audio, 'heygen', text, settings.tts_model, signature))
        db.commit()
        return {'changed': True, 'version': version, 'source_version': source_version,
                'backup': str(backup), 'delivery': adapter.delivery}


def main():
    from dataclasses import replace
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('order_id')
    parser.add_argument('--source-version', type=int)
    parser.add_argument('--speed', type=float)
    parser.add_argument('--pitch', type=float)
    parser.add_argument('--echo-delay', type=float)
    parser.add_argument('--echo-decay', type=float)
    args = parser.parse_args()
    config = Config.from_env()
    defaults = (0.8,0,0,0) if args.source_version is not None else (0.85,-1.5,100,0.18)
    speed,pitch,delay,decay = (v if v is not None else default for v,default in
        zip((args.speed,args.pitch,args.echo_delay,args.echo_decay),defaults))
    settings = replace(Settings.from_config(config), speech_speed=speed,
        pitch_semitones=pitch, echo_delay_ms=delay, echo_decay=decay)
    result = asyncio.run(reprocess(args.order_id, config, settings, source_version=args.source_version))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
