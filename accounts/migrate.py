"""Admin CLI: read-only preflight by default, explicit --apply to mutate selected DB."""
import argparse
import asyncio

from .errors import AccountError
from .migrations import inspect_schema, migrate


def main():
    parser = argparse.ArgumentParser(description='Аддитивная миграция аккаунтов; без --apply только чтение.')
    parser.add_argument('--database', required=True, help='Явный путь к существующей SQLite-базе')
    parser.add_argument('--apply', action='store_true', help='Явно разрешить применение миграции к выбранной базе')
    args = parser.parse_args()
    try:
        if args.apply:
            changed = asyncio.run(migrate(args.database))
            print('Миграция аккаунтов применена.' if changed else 'Миграция уже применена; изменений нет.')
        else:
            state = asyncio.run(inspect_schema(args.database))
            print('Только проверка: '+state+'. Для применения требуется --apply и согласованная резервная копия.')
    except (AccountError, ValueError):
        parser.exit(1, 'Проверка/миграция аккаунтов остановлена. Проверьте выбранный файл и совместимость схемы; журналирование не менялось.\n')


if __name__ == '__main__':
    main()
