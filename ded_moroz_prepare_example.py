"""Make a short public preview from the saved completed greeting, without paid APIs."""
import argparse
import asyncio
import sqlite3
from pathlib import Path

import database
from config import Config
from ded_moroz import Settings
from ded_moroz_customer import compact_video
from ded_moroz_sales import EXAMPLE


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input',type=Path,help='Optional local original MP4; otherwise latest completed administrator greeting')
    args=parser.parse_args();config=Config.from_env();settings=Settings.from_config(config)
    source=args.input
    if source is None:
        with sqlite3.connect(f'file:{database.DATABASE_PATH}?mode=ro',uri=True) as db:
            row=db.execute("SELECT output_path FROM ded_moroz_orders WHERE user_id=? AND status='done' AND output_path IS NOT NULL ORDER BY rowid DESC LIMIT 1",(config.admin_user_id,)).fetchone()
        if not row:raise SystemExit('Не найден готовый пример администратора. Укажите --input /путь/к/mark_ded_moroz.mp4')
        source=settings.root/row[0]
    if not source.is_file():raise SystemExit('Файл примера не найден.')
    asyncio.run(compact_video(source,EXAMPLE,preview_seconds=15))
    print('Готово: первые 15 секунд, компактный MP4:',EXAMPLE)


if __name__=='__main__':main()
