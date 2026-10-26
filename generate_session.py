"""Gera TG_USER_SESSION uma vez, por login local ou conversão da sessão existente."""
import argparse
import asyncio
import os
import sqlite3
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.crypto import AuthKey
from telethon.sessions import MemorySession, StringSession


def export_existing(path: Path) -> str:
    path = path.resolve(strict=True)
    con = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
    try:
        row = con.execute('SELECT dc_id, server_address, port, auth_key FROM sessions LIMIT 1').fetchone()
    finally:
        con.close()
    if not row or not row[3]:
        raise ValueError('O arquivo não contém uma sessão autorizada. Execute sem --from-session para fazer login.')
    session = MemorySession()
    session.set_dc(row[0], row[1], row[2])
    session.auth_key = AuthKey(row[3])
    return StringSession.save(session)


async def login() -> str:
    client = TelegramClient(StringSession(), int(os.environ['TG_API_ID']), os.environ['TG_API_HASH'])
    try:
        await client.start(phone=lambda: input('Seu celular (+55DDDNUMERO): ').strip())
        user = await client.get_me()
        if user.bot:
            raise ValueError('Autentique sua conta pessoal, não um bot.')
        return client.session.save()
    finally:
        await client.disconnect()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--from-session', type=Path, help='Arquivo .session existente; conversão offline.')
    parser.add_argument('--output', type=Path, default=Path('data/telegram.env'))
    args = parser.parse_args()
    if args.output.exists():
        parser.error('O arquivo de saída já existe; escolha outro com --output.')
    load_dotenv(Path(__file__).resolve().parent / '.env')
    value = export_existing(args.from_session) if args.from_session else asyncio.run(login())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, 'x', encoding='utf-8', opener=lambda p, flags: os.open(p, flags, 0o600)) as output:
        output.write(f'TG_USER_SESSION={value}\n')
    print(f'Sessão salva em {args.output}. Use TG_USER_SESSION do arquivo no ambiente do container.')


if __name__ == '__main__':
    main()
