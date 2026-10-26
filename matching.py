"""Comparacao de frases de interesse com mensagens do Telegram."""
import re
import unicodedata


def normalize(value: str) -> str:
    value = unicodedata.normalize('NFKD', value).casefold()
    value = ''.join(ch for ch in value if not unicodedata.combining(ch))
    value = re.sub(r'[^\w]+', ' ', value, flags=re.UNICODE)
    return ' '.join(value.split())


def matches(keyword: str, message: str) -> bool:
    """Frase completa, sem diferenciar maiusculas, acentos ou pontuacao."""
    key, text = normalize(keyword), normalize(message)
    return bool(key and text and f' {key} ' in f' {text} ')


def matching_keywords(keywords: list[str], message: str) -> list[str]:
    return [keyword for keyword in keywords if matches(keyword, message)]
