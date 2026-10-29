"""Monitor simples: sua conta le canais; seu bot envia copias dos posts relevantes."""
import asyncio
import copy
import json
import logging
import os
import re
import signal
import tempfile
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.errors import FloodWaitError
from telethon.sessions import MemorySession, StringSession

import dedup
from matching import matching_keywords
from storage import Store

BASE = Path(__file__).resolve().parent
load_dotenv(BASE / '.env')

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('monitor')

# Limite de seguranca do lote de deduplicacao, caso um canal despeje muitos posts.
MAX_PENDING_OFFERS = 200


def env_required(name: str) -> str:
    value = os.getenv(name, '').strip()
    if not value or value.startswith('COLE_'):
        raise ValueError(f'Configure {name} no ambiente ou no arquivo .env.')
    return value


def env_float(name: str, default: float, *, minimum: float, maximum: float) -> float:
    raw = os.getenv(name, '').strip()
    if not raw:
        return default
    try:
        value = float(raw.replace(',', '.'))
    except ValueError:
        raise ValueError(f'{name} precisa ser um numero.') from None
    if not minimum <= value <= maximum:
        raise ValueError(f'{name} precisa estar entre {minimum} e {maximum}.')
    return value


def utf16_length(text: str) -> int:
    """Telegram conta posicoes de formatacao em unidades UTF-16."""
    return len(text.encode('utf-16-le')) // 2


def apply_header(header: str, text: str, entities):
    """Acrescenta o cabecalho de origem deslocando a formatacao original."""
    if not header:
        return text, (entities or None)
    prefix = header + ('\n\n' if text else '')
    shift = utf16_length(prefix)
    moved = []
    for entity in entities or []:
        clone = copy.copy(entity)
        clone.offset += shift
        moved.append(clone)
    return prefix + text, (moved or None)


def split_targets(arg: str) -> list[str]:
    """Aceita varios grupos por comando, separados por espaco, virgula ou linha."""
    return [piece for piece in re.split(r'[\s,;]+', arg.strip()) if piece]


def dialog_handles(dialog) -> list[str]:
    entity = getattr(dialog, 'entity', None)
    handles = [getattr(entity, 'username', None)]
    handles += [getattr(item, 'username', None) for item in getattr(entity, 'usernames', None) or []]
    return [handle.casefold() for handle in handles if handle]


async def resolve_targets(user_client, store, tokens: list[str]):
    """Converte IDs, @usuarios e links t.me em dialogos acessiveis da conta.

    Retorna (resolvidos, nao_encontrados); cada resolvido e (chat_id, titulo).
    """
    cleaned = []
    for token in tokens:
        handle = re.sub(r'^(?:https?://)?(?:t\.me|telegram\.me)/', '', token, flags=re.IGNORECASE)
        cleaned.append(handle.lstrip('@').strip() or token)

    resolved: list[tuple[int, str]] = []
    unknown: list[str] = []
    # So consulta o Telegram quando algum alvo nao e um ID numerico direto.
    numeric = {}
    pending = []
    for original, handle in zip(tokens, cleaned):
        try:
            numeric[original] = int(handle)
        except ValueError:
            pending.append(original)
    watched = store.watched()
    dialogs = None
    if pending or any(chat_id not in watched for chat_id in numeric.values()):
        dialogs = [d async for d in user_client.iter_dialogs() if d.is_group or d.is_channel]
    by_id = {d.id: d for d in dialogs or []}
    by_handle = {handle: d for d in dialogs or [] for handle in dialog_handles(d)}

    for original, handle in zip(tokens, cleaned):
        chat_id = numeric.get(original)
        if chat_id is not None:
            dialog = by_id.get(chat_id)
            if dialog is not None:
                resolved.append((dialog.id, dialog.title))
            elif chat_id in watched:
                resolved.append((chat_id, watched[chat_id]))
            else:
                unknown.append(original)
            continue
        dialog = by_handle.get(handle.casefold())
        if dialog is None:
            unknown.append(original)
        else:
            resolved.append((dialog.id, dialog.title))
    # O mesmo grupo pode vir como ID e como @usuario no mesmo comando.
    return list(dict(resolved).items()), unknown


@dataclass
class PendingOffer:
    """Anuncio correspondente aguardando a janela de deduplicacao."""
    chat_id: int
    title: str
    message: object
    text: str
    price: float | None = None


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


async def send_offer_copy(user_client, bot_client, owner_id: int, message,
                          max_media_bytes: int, header: str = '') -> None:
    """Reenvia o texto original e, quando possivel, a foto/arquivo original.

    O bot nao faz encaminhamento nativo (forward) da origem porque nao participa
    dos canais; em vez disso copia conteudo legivel e midia. O unico acrescimo e
    o cabecalho de origem, quando configurado.
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
                    caption, entities = apply_header(header, text, message.entities)
                    caption_fits = len(caption) <= 1024
                    await bot_client.send_file(
                        owner_id,
                        downloaded,
                        caption=caption if caption_fits else header,
                        formatting_entities=entities if caption_fits else None,
                        parse_mode=None,
                    )
                    if not caption_fits and text:
                        await send_original_text(bot_client, owner_id, message)
                    return
        except Exception:
            log.warning('Nao foi possivel copiar a midia; enviando so o texto.', exc_info=True)

    if text:
        await send_original_text(bot_client, owner_id, message, header)
    else:
        # Sem texto para corresponder a palavras-chave, mas serve como fallback.
        await bot_client.send_message(
            owner_id, (header + '\n\n' if header else '') + '[Mensagem sem texto]', parse_mode=None,
        )


async def send_original_text(bot_client, owner_id: int, message, header: str = '') -> None:
    body, entities = apply_header(header, message.raw_text or '', message.entities)
    if len(body) <= 4096:
        await bot_client.send_message(
            owner_id, body, formatting_entities=entities,
            parse_mode=None, link_preview=True,
        )
    else:
        # Telegram limita mensagens a 4096 caracteres; textos grandes sao divididos.
        for start in range(0, len(body), 3900):
            await bot_client.send_message(
                owner_id, body[start:start + 3900], parse_mode=None, link_preview=False,
            )


async def main() -> None:
    api_id = int(env_required('TG_API_ID'))
    api_hash = env_required('TG_API_HASH')
    bot_token = env_required('TG_BOT_TOKEN')
    max_media_bytes = int(os.getenv('MAX_MEDIA_MB', '15')) * 1024 * 1024
    dedup_window = env_float('DEDUP_WINDOW_SECONDS', dedup.DEFAULT_WINDOW_SECONDS,
                             minimum=0.0, maximum=120.0)
    dedup_similarity = env_float('DEDUP_SIMILARITY', dedup.DEFAULT_SIMILARITY,
                                 minimum=0.0, maximum=1.0)
    show_source = os.getenv('SHOW_SOURCE', '1').strip().casefold() not in ('0', 'false', 'nao', 'no')
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
    pending: list[PendingOffer] = []
    flush_task: asyncio.Task | None = None
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

        def source_header(winner: PendingOffer, duplicates: list[PendingOffer]) -> str:
            if not show_source:
                return ''
            header = f'Fonte: {winner.title}'
            if duplicates:
                others = ', '.join(dict.fromkeys(offer.title for offer in duplicates))
                header += (f'\nAnuncio igual ignorado em: {others}'
                           if len(duplicates) == 1 else
                           f'\n{len(duplicates)} anuncios iguais ignorados em: {others}')
            return header

        async def deliver(offer: PendingOffer, duplicates: list[PendingOffer]) -> None:
            header = source_header(offer, duplicates)
            delivered = False
            for recipient_id in sorted(authorized_users):
                try:
                    await send_offer_copy(user_client, bot_client, recipient_id,
                                          offer.message, max_media_bytes, header)
                    delivered = True
                except FloodWaitError as exc:
                    log.warning('Telegram pediu pausa de %s segundos para destinatario %s',
                                exc.seconds, recipient_id)
                except Exception:
                    log.exception('Falha ao avisar destinatario %s sobre msg %s',
                                  recipient_id, offer.message.id)
            if not delivered:
                return
            # Os semelhantes descartados tambem ficam marcados para nunca reaparecerem.
            for sent in (offer, *duplicates):
                store.mark_notified(sent.chat_id, sent.message.id)
            log.info('ALERTA | %s | msg=%s | preco=%s | duplicados=%s',
                     offer.title, offer.message.id, offer.price, len(duplicates))

        async def flush_pending() -> None:
            nonlocal flush_task
            try:
                await asyncio.sleep(dedup_window)
                async with send_lock:
                    batch = list(pending)
                    pending.clear()
                    flush_task = None
                    for group in dedup.cluster_offers([o.text for o in batch], dedup_similarity):
                        offers = [batch[index] for index in group]
                        # Menor preco vence; sem preco ou empate, o primeiro que chegou.
                        winner = min(offers, key=lambda o: (o.price is None, o.price or 0.0))
                        await deliver(winner, [o for o in offers if o is not winner])
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception('Falha ao processar o lote de anuncios')
                async with send_lock:
                    pending.clear()
                    flush_task = None

        @user_client.on(events.NewMessage(incoming=True))
        async def capture(event):
            nonlocal flush_task
            if event.chat_id is None:
                return
            watched = store.watched()
            if event.chat_id not in watched:
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
            if store.notified(event.chat_id, msg.id):
                return
            if any(o.chat_id == event.chat_id and o.message.id == msg.id for o in pending):
                return
            if len(pending) >= MAX_PENDING_OFFERS:
                log.warning('Lote cheio (%s anuncios); msg %s enviada sem deduplicacao',
                            MAX_PENDING_OFFERS, msg.id)
                async with send_lock:
                    await deliver(PendingOffer(event.chat_id, watched[event.chat_id], msg,
                                               content, dedup.offer_price(content)), [])
                return

            pending.append(PendingOffer(event.chat_id, watched[event.chat_id], msg,
                                        content, dedup.offer_price(content)))
            log.info('CAPTURA | %s | msg=%s | palavras=%s', watched[event.chat_id],
                     msg.id, ', '.join(found))
            if flush_task is None or flush_task.done():
                # A janela comeca no primeiro anuncio; os semelhantes que chegarem
                # ate o fim dela sao comparados juntos.
                flush_task = asyncio.create_task(flush_pending())

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
                        '/monitorar -100123 -100456 - acompanhar varios de uma vez\n'
                        '/monitorar todos - acompanhar todos os seus grupos e canais\n'
                        '/parar -100123 -100456 - parar de acompanhar\n'
                        '/status - consultar configuracao\n'
                        '/teste RTX 5060 por R$ 2000 - testar correspondencia\n'
                        '/similar texto A || texto B - comparar dois anuncios\n\n'
                        'Primeiro use /grupos, depois /monitorar e /add. '
                        'O bot envia as mensagens correspondentes sem alterar o texto.\n'
                        f'Anuncios semelhantes publicados em ate {dedup_window:g}s em grupos '
                        'diferentes viram um unico alerta, com o menor preco em R$.'
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
                    watched = store.watched()
                    lines = [f'{d.id}: {d.title}' + (' [monitorando]' if d.id in watched else '')
                             for d in dialogs]
                    answer = 'Nenhum grupo ou canal encontrado.'
                    if lines:
                        answer = 'Grupos e canais da sua conta:\n' + '\n'.join(lines)
                        livres = [str(d.id) for d in dialogs if d.id not in watched]
                        if livres:
                            # Pronto para copiar: um unico comando adiciona todos de uma vez.
                            answer += ('\n\nPara monitorar todos os que faltam:\n/monitorar '
                                       + ' '.join(livres))
                elif cmd in ('/monitorar', '/watch'):
                    targets = split_targets(arg)
                    if not targets:
                        answer = ('Uso: /monitorar -1001111111111 -1002222222222\n'
                                  'Aceita varios grupos de uma vez, separados por espaco, '
                                  'virgula ou quebra de linha, e tambem @usuario ou link t.me.\n'
                                  '/monitorar todos acompanha todos os seus grupos e canais.')
                    else:
                        if len(targets) == 1 and targets[0].casefold() in ('todos', 'all', 'tudo'):
                            resolved = [(d.id, d.title) async for d in user_client.iter_dialogs()
                                        if d.is_group or d.is_channel]
                            unknown = []
                        else:
                            resolved, unknown = await resolve_targets(user_client, store, targets)
                        watched = store.watched()
                        added, already = [], []
                        for chat_id, title in resolved:
                            (already if chat_id in watched else added).append(f'{title} ({chat_id})')
                            store.watch(chat_id, title)
                        lines = []
                        if added:
                            lines.append(f'Monitorando agora ({len(added)}):\n' +
                                         '\n'.join('• ' + item for item in added))
                        if already:
                            lines.append(f'Ja estavam na lista ({len(already)}):\n' +
                                         '\n'.join('• ' + item for item in already))
                        if unknown:
                            lines.append(f'Nao encontrados nos seus grupos/canais ({len(unknown)}):\n' +
                                         '\n'.join('• ' + item for item in unknown) +
                                         '\nUse /grupos para conferir os IDs.')
                        answer = '\n\n'.join(lines) or 'Nenhum grupo ou canal valido informado.'
                elif cmd in ('/parar', '/unwatch'):
                    targets = split_targets(arg)
                    if not targets:
                        answer = 'Uso: /parar -1001111111111 -1002222222222'
                    else:
                        resolved, unknown = await resolve_targets(user_client, store, targets)
                        removed = [f'{title} ({chat_id})' for chat_id, title in resolved
                                   if store.unwatch(chat_id)]
                        missing = len(resolved) - len(removed) + len(unknown)
                        lines = []
                        if removed:
                            lines.append(f'Removidos da lista ({len(removed)}):\n' +
                                         '\n'.join('• ' + item for item in removed))
                        if missing:
                            lines.append(f'{missing} alvo(s) nao estavam sendo monitorados.')
                        answer = '\n\n'.join(lines) or 'Nenhum grupo ou canal valido informado.'
                elif cmd == '/status':
                    watched = store.watched()
                    keywords = store.keywords()
                    answer = (f'Conta: {user.first_name} (ID {owner_id})\n'
                              f'Bot: @{bot.username}\n'
                              f'Canais monitorados: {len(watched)}\n'
                              f'Palavras-chave: {len(keywords)}\n'
                              f'Janela anti-duplicados: {dedup_window:g}s '
                              f'(semelhanca minima {dedup_similarity:.2f})\n'
                              f'Cabecalho de origem: {"sim" if show_source else "nao"}\n'
                              'Funcionando enquanto este programa estiver aberto.')
                elif cmd in ('/teste', '/test'):
                    if not arg:
                        answer = 'Uso: /teste Notebook gamer Ryzen 7 R$ 3000'
                    else:
                        found = matching_keywords(store.keywords(), arg)
                        price = dedup.offer_price(arg)
                        answer = ('Correspondencia encontrada: ' + ', '.join(found)
                                  if found else 'Nenhuma palavra-chave corresponde a esse texto.')
                        if price is not None:
                            answer += f'\nPreco identificado: R$ {price:.2f}'
                elif cmd in ('/similar', '/comparar'):
                    first, separator, second = arg.partition('||')
                    if not separator or not first.strip() or not second.strip():
                        answer = ('Uso: /similar texto do anuncio A || texto do anuncio B\n'
                                  'Mostra o quanto os dois seriam considerados o mesmo anuncio.')
                    else:
                        score = dedup.similarity(first, second)
                        duplicate = dedup.same_offer(first, second, dedup_similarity)
                        answer = (f'Semelhanca: {score:.2f} (minimo {dedup_similarity:.2f})\n'
                                  f'Seriam tratados como o mesmo anuncio: '
                                  f'{"sim" if duplicate else "nao"}\n'
                                  f'Preco A: {dedup.offer_price(first)} | '
                                  f'Preco B: {dedup.offer_price(second)}')
                else:
                    answer = 'Comando desconhecido. Envie /ajuda.'
                await send_long_message(bot_client, event.sender_id, answer)
            except Exception:
                log.exception('Erro ao processar comando: %s', cmd)
                await bot_client.send_message(event.sender_id, 'Erro ao executar comando. Veja o terminal.', parse_mode=None)

        log.info('Monitor pronto: %s canal(is), %s palavra(s)-chave, janela anti-duplicados %ss.',
                 len(store.watched()), len(store.keywords()), dedup_window)
        await asyncio.gather(
            user_client.run_until_disconnected(),
            bot_client.run_until_disconnected(),
        )
    finally:
        if flush_task is not None and not flush_task.done():
            # Anuncios ainda na janela se perdem no desligamento, como qualquer
            # evento recebido fora do ar.
            flush_task.cancel()
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
