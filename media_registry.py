"""Private local SQLite metadata: restrictive permissions, version checks, short transactions."""
from contextlib import contextmanager
import os
from pathlib import Path
import sqlite3
import stat

VERSION=111


@contextmanager
def registry_connection(path, *, write=False):
    from private_media import SCHEMA, MediaUnavailable
    from config import BASE_DIR
    raw=Path(path).expanduser()
    if raw.is_symlink():raise MediaUnavailable('Registry symlink refused')
    path=raw.resolve()
    anchor=Path(str(path)+'.required')
    if path==(BASE_DIR/'database.sqlite3').resolve() or not path.parent.is_dir():
        raise MediaUnavailable('Invalid private registry path')
    created=False
    db=None
    try:
        if anchor.exists() and not path.exists():raise MediaUnavailable('Required private registry is missing; restore metadata before processing')
        if write:
            try:
                fd=os.open(path,os.O_CREAT|os.O_EXCL|os.O_RDWR,0o600)
                os.close(fd);created=True
            except FileExistsError:pass
        for candidate in (path,anchor,Path(str(path)+'-journal'),Path(str(path)+'-wal'),Path(str(path)+'-shm')):
            if candidate.exists():
                info=candidate.lstat()
                if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o077:
                    raise MediaUnavailable('Private registry permissions refused')
        db=sqlite3.connect(path.as_uri()+('?mode=rw' if write else '?mode=ro'),uri=True,timeout=10)
        db.row_factory=sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        version=db.execute('PRAGMA user_version').fetchone()[0]
        if version not in (0,VERSION):raise MediaUnavailable('Unsupported private registry version')
        if not created:
            names={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
            if names-{'media_objects','media_orders','media_grants','media_policies'}:
                raise MediaUnavailable('Unexpected tables in private registry')
            required={'media_objects':{'id','bucket','object_key','owner_namespace','owner_id','order_id','owner_access','state','size','sha256','operation_id','content_type','created_at'},
                      'media_orders':{'owner_namespace','owner_id','order_id','created_at'},
                      'media_grants':{'asset_id','recipient_namespace','recipient_id','expires_at','created_at'}}
            if version==VERSION or 'media_policies' in names:
                required['media_policies']={'owner_namespace','owner_id','operation_id','product','mode','created_at'}
            for table,columns in required.items():
                if not columns<={r[1] for r in db.execute('PRAGMA table_info('+table+')')}:
                    raise MediaUnavailable('Private registry schema is incompatible')
        if write:
            db.executescript(SCHEMA)
            db.execute('PRAGMA user_version='+str(VERSION))
            if not anchor.exists():
                try:
                    fd=os.open(anchor,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
                    with os.fdopen(fd,'w') as output:output.write('111\n')
                except FileExistsError:pass
        with db:yield db
    except (sqlite3.Error,OSError):
        raise MediaUnavailable('Private registry unavailable') from None
    finally:
        if db is not None:db.close()
