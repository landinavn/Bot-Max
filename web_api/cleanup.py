"""Explicit one-shot cleanup; use a separate timer later, never an import-time task."""
import asyncio
from auth.repository import AuthRepository
from auth.settings import AuthSettings
from .config import APISettings


def main():
    settings=APISettings.from_env()
    repository=AuthRepository(settings.database,AuthSettings(secret=settings.secret,environment=settings.environment),
                              timeout=settings.database_timeout)
    asyncio.run(repository.cleanup(int(repository.clock())))
    print('Expired authentication records cleaned.')


if __name__=='__main__':main()
