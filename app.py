"""Monitor simples: sua conta le canais; seu bot envia copias dos posts relevantes."""
import asyncio
import json
import logging
import os
import signal
import tempfile
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.errors import FloodWaitError
from telethon.sessions import MemorySession, StringSession

from matching import matching_keywords
from storage import Store

BASE = Path(__file__).resolve().parent
load_dotenv(BASE / '.env')

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('monitor')


def env_required(name: str) -> str:
    value = os.getenv(name, '').strip()
    if not value or value.startswith('COLE_'):
        raise ValueError(f'Configure {name} no ambiente ou no arquivo .env.')
    return value


async def send_long_message(client, dest: int, text: str) -> None:
    """Respostas de comandos; corta linhas longas para limite do Telegram."""
    buffer = ''
    for line in text.splitlines(keepends=True):
        if len(buffer) + len(line) > 3900:
            if buffer:
                await client.send_message(dest, buffer, parse_mode=None, link_preview=False)
                buffer = ''
            while len(line) > 3900:
                await client.send_message(dest, line[:3900], parse_mode=None, link_preview=False)
                line = line[3900:]
        buffer += line
    if buffer:
        await client.send_message(dest, buffer, parse_mode=None, link_preview=False)


async def send_offer_copy(user_client, bot_client, owner_id: int, message, max_media_bytes: int) -> None:
    """Reenvia o texto original e, quando possivel, a foto/arquivo original.

    O bot nao faz encaminhamento nativo (forward) da origem porque nao participa
    dos canais; em vez disso copia conteudo legivel e midia. Nao inclui cabecalho.
    """
    text = message.raw_text or ''
    has_media_file = message.photo is not None or message.document is not None
    file_size = getattr(getattr(message, 'file', None), 'size', None)
    can_copy_media = has_media_file and (file_size is None or file_size <= max_media_bytes)

    if can_copy_media:
        try:
            with tempfile.TemporaryDirectory(prefix='oferta_') as temp_dir:
                downloaded = await user_client.download_media(message, file=temp_dir)
                if downloaded:
                    caption_fits = len(text) <= 1024
                    await bot_client.send_file(
                        owner_id,
                        downloaded,
                        caption=text if caption_fits else '',
                        formatting_entities=(message.entities or None) if caption_fits else None,
                        parse_mode=None,
                    )
                    if not caption_fits and text:
                        await send_original_text(bot_client, owner_id, message)
                    return
        except Exception:
            log.warning('Nao foi possivel copiar a midia; enviando so o texto.', exc_info=True)

    if text:
        await send_original_text(bot_client, owner_id, message)
    else:
        # Sem texto para corresponder a palavras-chave, mas serve como fallback.
        await bot_client.send_message(owner_id, '[Mensagem sem texto]', parse_mode=None)


async def send_original_text(bot_client, owner_id: int, message) -> None:
    text = message.raw_text or ''
    if len(text) <= 4096:
        await bot_client.send_message(
            owner_id, text, formatting_entities=message.entities or None,
            parse_mode=None, link_preview=True,
        )
    else:
        # Telegram limita mensagens a 4096 caracteres; textos grandes sao divididos.
        for start in range(0, len(text), 3900):
            await bot_client.send_message(
                owner_id, text[start:start + 3900], parse_mode=None, link_preview=False,
            )


async def main() -> None:
    api_id = int(env_required('TG_API_ID'))
    api_hash = env_required('TG_API_HASH')
    bot_token = env_required('TG_BOT_TOKEN')
    max_media_bytes = int(os.getenv('MAX_MEDIA_MB', '15')) * 1024 * 1024
    keywords = json.loads(os.getenv('INITIAL_KEYWORDS', '[]'))
    chat_ids = json.loads(os.getenv('INITIAL_CHAT_IDS', '[]'))
    allowed_user_ids = json.loads(os.getenv('TG_ALLOWED_USER_IDS', '[]'))
    if not isinstance(allowed_user_ids, list) or any(
        type(uid) is not int or not 0 < uid < 2 ** 63 for uid in allowed_user_ids
    ):
        raise ValueError('TG_ALLOWED_USER_IDS precisa ser um array JSON de IDs inteiros positivos.')
    if not isinstance(keywords, list) or any(not isinstance(k, str) for k in keywords):
        raise ValueError('INITIAL_KEYWORDS precisa ser um array JSON de frases.')
    if not isinstance(chat_ids, list) or any(type(c) is not int for c in chat_ids):
        raise ValueError('INITIAL_CHAT_IDS precisa ser um array JSON de IDs inteiros.')
    session_string = env_required('TG_USER_SESSION')
    try:
        session = StringSession(session_string)
    except Exception:
        # Não reproduzir o conteúdo da credencial na mensagem de erro.
        raise ValueError('TG_USER_SESSION inválida; gere novamente com generate_session.py.') from None
    user_client = TelegramClient(session, api_id, api_hash)
    bot_client = TelegramClient(MemorySession(), api_id, api_hash)
    store = None
    send_lock = asyncio.Lock()
    task = asyncio.current_task()
    loop = asyncio.get_running_loop()
    try:
        loop.add_signal_handler(signal.SIGTERM, task.cancel)
    except (NotImplementedError, RuntimeError):
        pass  # Windows; o container Linux suporta SIGTERM.

    try:
        store = Store(
            host=os.getenv('MYSQL_HOST', 'localhost'),
            port=int(os.getenv('MYSQL_PORT', '3306')),
            user=os.getenv('MYSQL_USER', 'telegram_ofertas'),
            password=env_required('MYSQL_PASSWORD'),
            database=os.getenv('MYSQL_DATABASE', 'telegram_ofertas'),
        )
        await user_client.connect()
        if not await user_client.is_user_authorized():
            raise ValueError('TG_USER_SESSION revogada. Gere outra com generate_session.py.')
        user = await user_client.get_me()
        if user is None or user.bot:
            raise ValueError('TG_USER_SESSION deve ser da sua conta pessoal, não de um bot.')
        owner_id = user.id
        authorized_users = {owner_id, *allowed_user_ids}
        log.info('Conta pessoal conectada: %s (ID %s)', user.first_name, owner_id)

        await bot_client.connect()
        await bot_client.sign_in(bot_token=bot_token)
        bot = await bot_client.get_me()
        log.info('Bot conectado: @%s. Envie /start a ele usando sua conta pessoal.', bot.username)
        if chat_ids:
            dialogs = {d.id: d.title async for d in user_client.iter_dialogs() if d.is_group or d.is_channel}
            missing = set(chat_ids) - dialogs.keys()
            if missing:
                raise ValueError(f'INITIAL_CHAT_IDS contém grupos/canais inacessíveis: {sorted(missing)}')
            for chat_id in chat_ids:
                store.watch(chat_id, dialogs[chat_id])
        for phrase in keywords:
            store.add_keyword(phrase)

        @user_client.on(events.NewMessage(incoming=True))
        async def capture(event):
            if event.chat_id is None or event.chat_id not in store.watched():
                return
            msg = event.message
            if getattr(msg, 'noforwards', False):
                log.info('Mensagem %s em canal protegido ignorada', msg.id)
                return
            content = msg.raw_text or ''
            if not content:
                return
            found = matching_keywords(store.keywords(), content)
            if not found:
                return

            async with send_lock:
                if store.notified(event.chat_id, msg.id):
                    return
                delivered = False
                for recipient_id in sorted(authorized_users):
                    try:
                        await send_offer_copy(user_client, bot_client, recipient_id, msg, max_media_bytes)
                        delivered = True
                    except FloodWaitError as exc:
                        log.warning('Telegram pediu pausa de %s segundos para destinatario %s',
                                    exc.seconds, recipient_id)
                    except Exception:
                        log.exception('Falha ao avisar destinatario %s sobre msg %s', recipient_id, msg.id)
                if delivered:
                    store.mark_notified(event.chat_id, msg.id)
                    log.info('ALERTA | %s | msg=%s | palavras=%s',
                             store.watched().get(event.chat_id), msg.id, ', '.join(found))

        @bot_client.on(events.NewMessage(incoming=True))
        async def commands(event):
            # Os usuarios autorizados compartilham canais, filtros e alertas.
            if not event.is_private or event.sender_id not in authorized_users:
                return
            raw = (event.raw_text or '').strip()
            if not raw.startswith('/'):
                return
            cmd, _, arg = raw.partition(' ')
            cmd = cmd.split('@')[0].casefold()
            arg = arg.strip()

            try:
                if cmd in ('/start', '/ajuda', '/help'):
                    answer = (
                        'Monitor de ofertas ativo. Configuracao compartilhada entre usuarios autorizados.\n\n'
                        '/add RTX 5060 - cadastrar frase\n'
                        '/remover RTX 5060 - remover frase\n'
                        '/lista - listar frases\n'
                        '/grupos - listar seus grupos e canais\n'
                        '/monitorar -100123... - acompanhar canal\n'
                        '/parar -100123... - parar de acompanhar\n'
                        '/status - consultar configuracao\n'
                        '/teste RTX 5060 por R$ 2000 - testar correspondencia\n\n'
                        'Primeiro use /grupos, depois /monitorar e /add. '
                        'O bot envia as mensagens correspondentes sem alterar o texto.'
                    )
                elif cmd in ('/add', '/adicionar'):
                    if not arg:
                        answer = 'Uso: /add RTX 5060'
                    else:
                        new = store.add_keyword(arg)
                        answer = (f'Palavra-chave cadastrada: {arg}' if new
                                  else 'Essa palavra-chave ja existe.')
                elif cmd in ('/remover', '/remove'):
                    answer = ('Palavra-chave removida.' if store.remove_keyword(arg)
                              else 'Palavra-chave nao encontrada.') if arg else 'Uso: /remover RTX 5060'
                elif cmd in ('/lista', '/list'):
                    items = store.keywords()
                    answer = 'Palavras-chave:\n' + '\n'.join('• ' + i for i in items) if items else 'Nenhuma palavra-chave. Use /add produto.'
                elif cmd in ('/grupos', '/groups'):
                    dialogs = [d async for d in user_client.iter_dialogs() if d.is_group or d.is_channel]
                    lines = [f'{d.id}: {d.title}' + (' [monitorando]' if d.id in store.watched() else '')
                             for d in dialogs]
                    answer = 'Grupos e canais da sua conta:\n' + '\n'.join(lines) if lines else 'Nenhum grupo ou canal encontrado.'
                elif cmd in ('/monitorar', '/watch'):
                    try:
                        chat_id = int(arg)
                    except ValueError:
                        answer = 'Uso: /monitorar -100123456789'
                    else:
                        target = None
                        async for dialog in user_client.iter_dialogs():
                            if (dialog.is_channel or dialog.is_group) and dialog.id == chat_id:
                                target = dialog
                                break
                        if target is None:
                            answer = 'ID nao encontrado nos grupos/canais da sua conta. Use /grupos.'
                        else:
                            store.watch(chat_id, target.title)
                            answer = f'Monitorando agora: {target.title} ({chat_id})'
                elif cmd in ('/parar', '/unwatch'):
                    try:
                        chat_id = int(arg)
                    except ValueError:
                        answer = 'Uso: /parar -100123456789'
                    else:
                        answer = 'Canal removido da lista.' if store.unwatch(chat_id) else 'Canal nao estava sendo monitorado.'
                elif cmd == '/status':
                    watched = store.watched()
                    keywords = store.keywords()
                    answer = (f'Conta: {user.first_name} (ID {owner_id})\n'
                              f'Bot: @{bot.username}\n'
                              f'Canais monitorados: {len(watched)}\n'
                              f'Palavras-chave: {len(keywords)}\n'
                              'Funcionando enquanto este programa estiver aberto.')
                elif cmd in ('/teste', '/test'):
                    if not arg:
                        answer = 'Uso: /teste Notebook gamer Ryzen 7 R$ 3000'
                    else:
                        found = matching_keywords(store.keywords(), arg)
                        answer = ('Correspondencia encontrada: ' + ', '.join(found)
                                  if found else 'Nenhuma palavra-chave corresponde a esse texto.')
                else:
                    answer = 'Comando desconhecido. Envie /ajuda.'
                await send_long_message(bot_client, event.sender_id, answer)
            except Exception:
                log.exception('Erro ao processar comando: %s', cmd)
                await bot_client.send_message(event.sender_id, 'Erro ao executar comando. Veja o terminal.', parse_mode=None)

        log.info('Monitor pronto: %s canal(is), %s palavra(s)-chave.',
                 len(store.watched()), len(store.keywords()))
        await asyncio.gather(
            user_client.run_until_disconnected(),
            bot_client.run_until_disconnected(),
        )
    finally:
        try:
            await user_client.disconnect()
        finally:
            try:
                await bot_client.disconnect()
            finally:
                if store is not None:
                    store.close()


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, asyncio.CancelledError):
        print('\nEncerrado.')
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
