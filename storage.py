"""Persistência MySQL de frases, canais e mensagens notificadas."""
import pymysql
from matching import normalize


class Store:
    def __init__(self, *, host: str, user: str, password: str, database: str, port: int = 3306):
        self.con = pymysql.connect(
            host=host, port=port, user=user, password=password, database=database,
            charset='utf8mb4', autocommit=True, connect_timeout=10,
            read_timeout=15, write_timeout=15,
        )
        try:
            with self.con.cursor() as cur:
                for statement in (
                    '''CREATE TABLE IF NOT EXISTS keywords (
                        phrase VARCHAR(120) NOT NULL,
                        normalized VARCHAR(768) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin PRIMARY KEY
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4''',
                    '''CREATE TABLE IF NOT EXISTS watched_chats (
                        chat_id BIGINT PRIMARY KEY,
                        title TEXT NOT NULL
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4''',
                    '''CREATE TABLE IF NOT EXISTS notified (
                        chat_id BIGINT NOT NULL,
                        message_id BIGINT NOT NULL,
                        sent_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY (chat_id, message_id)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4''',
                ):
                    cur.execute(statement)
        except Exception:
            self.con.close()
            raise

    def _execute(self, sql, params=(), *, fetch=False):
        # Conexões ociosas podem exceder wait_timeout. Cada operação é autocommit.
        self.ensure_connection()
        with self.con.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall() if fetch else cur.rowcount

    def ensure_connection(self):
        try:
            self.con.ping(reconnect=False)
        except pymysql.Error:
            self.con.connect()

    def add_keyword(self, phrase: str) -> bool:
        phrase = phrase.strip()
        norm = normalize(phrase)
        if not norm or len(phrase) > 120 or len(norm) > 768:
            raise ValueError('A palavra-chave precisa ter entre 1 e 120 caracteres (normalização até 768).')
        return self._execute(
            'INSERT INTO keywords (phrase, normalized) VALUES (%s, %s) '
            'ON DUPLICATE KEY UPDATE normalized=keywords.normalized', (phrase, norm),
        ) > 0

    def remove_keyword(self, phrase: str) -> bool:
        return self._execute('DELETE FROM keywords WHERE normalized=%s', (normalize(phrase),)) > 0

    def keywords(self) -> list[str]:
        return [row[0] for row in self._execute('SELECT phrase FROM keywords ORDER BY phrase', fetch=True)]

    def watch(self, chat_id: int, title: str) -> None:
        self._execute(
            'INSERT INTO watched_chats (chat_id, title) VALUES (%s, %s) '
            'ON DUPLICATE KEY UPDATE title=%s', (chat_id, title, title),
        )

    def unwatch(self, chat_id: int) -> bool:
        return self._execute('DELETE FROM watched_chats WHERE chat_id=%s', (chat_id,)) > 0

    def watched(self) -> dict[int, str]:
        return {int(chat_id): title for chat_id, title in self._execute(
            'SELECT chat_id, title FROM watched_chats ORDER BY title', fetch=True,
        )}

    def notified(self, chat_id: int, message_id: int) -> bool:
        return bool(self._execute(
            'SELECT 1 FROM notified WHERE chat_id=%s AND message_id=%s',
            (chat_id, message_id), fetch=True,
        ))

    def mark_notified(self, chat_id: int, message_id: int) -> None:
        self._execute(
            'INSERT INTO notified (chat_id, message_id) VALUES (%s, %s) '
            'ON DUPLICATE KEY UPDATE message_id=notified.message_id', (chat_id, message_id),
        )

    def close(self):
        if self.con.open:
            self.con.close()
