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


def matching_keywords(keywords: list[str], message: str,
                      limits: dict[str, float | None] | None = None,
                      price: float | None = None) -> list[str]:
    """Frases presentes na mensagem e cujo valor maximo (se houver) comporta o preco.

    Anuncio sem preco identificavel passa pelo filtro: nao da pra comparar, e
    perder uma oferta custa mais que receber uma a mais.
    """
    found = [keyword for keyword in keywords if matches(keyword, message)]
    if not limits or price is None:
        return found
    return [keyword for keyword in found
            if limits.get(keyword) is None or price <= limits[keyword]]
