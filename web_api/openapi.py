"""Offline contract export. Uses test settings and never connects to DB/SMTP."""
import argparse
import json
from pathlib import Path
import secrets
from auth.email_sender import FakeEmailSender
from .app import create_app
from .config import APISettings


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('output',type=Path)
    args=parser.parse_args()
    settings=APISettings(database=Path('/nonexistent-contract-only.sqlite3'),secret=secrets.token_bytes(32),environment='test')
    schema=create_app(settings,sender=FakeEmailSender()).openapi()
    args.output.write_text(json.dumps(schema,ensure_ascii=False,indent=2)+'\n')


if __name__=='__main__':main()
