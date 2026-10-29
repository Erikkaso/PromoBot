"""Agrupamento de anuncios repetidos publicados em grupos diferentes.

Varios canais divulgam a mesma promocao com textos levemente diferentes e links
de afiliado proprios. As funcoes abaixo comparam o conteudo util do anuncio
(ignorando precos, links e palavras de propaganda) e extraem o valor em R$ para
que apenas a publicacao mais barata de cada grupo semelhante seja entregue.
"""
import re
import unicodedata
from typing import NamedTuple, Sequence

from matching import normalize

DEFAULT_WINDOW_SECONDS = 5.0
DEFAULT_SIMILARITY = 0.65
# Por quanto tempo um anuncio ja entregue continua barrando repeticoes dele.
DEFAULT_HISTORY_SECONDS = 300.0

# Palavras de propaganda e conectores que aparecem em quase todo anuncio e por
# isso nao ajudam a distinguir um produto do outro.
# Artigos, preposicoes, conjuncoes e pronomes: nunca dizem qual e o produto.
GRAMMAR_WORDS = frozenset('''
    a o as os um uma uns umas ao aos de da do das dos dum duma em no na nos nas
    num numa por pelo pela pelos pelas para pra com sem sob sobre entre ate
    desde e ou mas nem que se como quando onde porque pq mais menos muito muita
    muitos muitas pouco pouca tambem so ja ainda la ali aqui cá ca eh er
    eu tu ele ela eles elas voce voces vc nos me te lhe meu minha seu sua
    nosso nossa este esta estes estas esse essa esses essas isto isso aquele
    aquela aquilo ser sao foi vai vem tem ter ta
'''.split())

# Palavras de propaganda que aparecem em quase todo anuncio.
AD_WORDS = frozenset('''
    aproveite aqui acesse acesso agora apenas ate baixo barato boleto bom cada
    caiu cartao clique com como compra comprar compre confira conferir cupom
    cupons desconto descontos direto dia dias esse esta este eles ela ele emb
    entrega envio frete gratis hoje ideal imperdivel inclusa incluso info item
    itens ja leve link links loja lojas mais melhor menor menos mes meses nao
    nesse neste nos nas nossa nosso nova novo oferta ofertas ola para parcelado
    parcelas pelo pela pix pouco preco precos primeira pro produto produtos
    promo promocao promocoes que quem rapido real reais sem sera seu sua super
    tem toda todo todos uma umas uns unidade unidades use usar valor valores
    ver veja vendido vendas vista
'''.split())

STOPWORDS = GRAMMAR_WORDS | AD_WORDS

_URL_RE = re.compile(
    r'https?://\S+|\b(?:www\.)?[\w-]+\.(?:com|br|net|org|me|io|co|shop|store)\b\S*',
    re.IGNORECASE,
)
_PRICE_RE = re.compile(r'R\$\s*(\d[\d.,]*)', re.IGNORECASE)
_INSTALLMENT_RE = re.compile(r'(?:\d{1,2}\s*x|vezes|parcelas?)\s*(?:de\s*)?$', re.IGNORECASE)
_CASH_RE = re.compile(r'(?:por|pix|vista|sai|fica|apenas|so|somente)\W*$', re.IGNORECASE)

# Medidas que distinguem variacoes do mesmo modelo (capacidade, tamanho, potencia).
_SPEC_RE = re.compile(
    r'\b(\d{1,5})\s?(tb|gb|mb|ghz|mhz|hz|mah|kwh|w|ml|litros?|l|polegadas|pol|'
    r'unidades?|un|kg|mp|fps|cm|mm|m)\b'
)
_UNIT_ALIASES = {'litro': 'l', 'litros': 'l', 'polegadas': 'pol',
                 'unidade': 'un', 'unidades': 'un'}

# Codigos de produto estaveis entre lojas: dois anuncios com o mesmo codigo sao
# o mesmo item, e com codigos diferentes sao itens diferentes.
_PRODUCT_ID_PATTERNS = (
    re.compile(r'/(?:dp|gp/product)/([A-Z0-9]{10})\b', re.IGNORECASE),      # Amazon
    re.compile(r'\b(MLB)-?(\d{8,})\b', re.IGNORECASE),                      # Mercado Livre
    re.compile(r'\bi\.(\d+)\.(\d+)\b'),                                     # Shopee
    re.compile(r'aliexpress\.\S*?/item/(\d{6,})', re.IGNORECASE),           # AliExpress
    re.compile(r'kabum\.com\.br/produto/(\d{3,})', re.IGNORECASE),          # KaBuM
    re.compile(r'magazine\S*\.com\.br/\S*?/p/([\w]{4,})', re.IGNORECASE),   # Magalu
)


def _to_float(raw: str) -> float | None:
    """Converte '1.999,90', '1.999' ou '19.90' no valor correspondente."""
    raw = raw.strip().strip('.,')
    if not raw:
        return None
    cut = max(raw.rfind('.'), raw.rfind(','))
    if cut == -1:
        digits, decimals = raw, ''
    else:
        separator, decimals = raw[cut], raw[cut + 1:]
        # Virgula nunca separa milhar; ponto so e decimal com dois digitos.
        is_decimal = len(decimals) != 3 if separator == ',' else len(decimals) == 2
        digits, decimals = (raw[:cut], decimals) if is_decimal else (raw, '')
    digits = re.sub(r'[.,]', '', digits)
    if not digits.isdigit():
        return None
    try:
        return float(f'{digits}.{decimals}' if decimals else digits)
    except ValueError:
        return None


_LIMIT_RE = re.compile(
    r'^(?P<phrase>.*?)\s*(?:\||<=|\b(?:ate|maximo|max|limite)\b)\s*(?:R\$)?\s*(?P<amount>\d[\d.,]*)\s*$',
    re.IGNORECASE,
)


def parse_amount(raw: str) -> float | None:
    """Valor digitado pelo usuario ('2000', 'R$ 1.999,90'); None se invalido."""
    match = re.fullmatch(r'\s*(?:R\$)?\s*(\d[\d.,]*)\s*', raw or '', re.IGNORECASE)
    return _to_float(match.group(1)) if match else None


def split_limit(arg: str) -> tuple[str, float | None]:
    """'RTX 5060 | 2000' ou 'RTX 5060 ate R$ 2.000' -> ('RTX 5060', 2000.0).

    Sem limite informado devolve o texto inteiro como frase.
    """
    match = _LIMIT_RE.match(normalize_accents(arg.strip()))
    if not match or not match.group('phrase').strip():
        return arg.strip(), None
    amount = _to_float(match.group('amount'))
    if amount is None:
        return arg.strip(), None
    # A frase vem do texto original, so cortando o trecho do limite.
    return arg.strip()[:match.end('phrase')].strip(), amount


def normalize_accents(text: str) -> str:
    """Remove acentos preservando posicoes (so para localizar 'ate'/'maximo')."""
    return ''.join(ch if len(unicodedata.normalize('NFD', ch)) == 1
                   else unicodedata.normalize('NFD', ch)[0] for ch in text)


def extract_prices(text: str) -> list[float]:
    """Valores em R$ do texto, descartando parcelas ('12x de R$ 99')."""
    prices = []
    for match in _PRICE_RE.finditer(text or ''):
        before = text[max(0, match.start() - 20):match.start()]
        if _INSTALLMENT_RE.search(before):
            continue
        value = _to_float(match.group(1))
        if value is not None and value > 0:
            prices.append(value)
    return prices


def offer_price(text: str) -> float | None:
    """Preco de venda do anuncio.

    Prefere o valor anunciado como preco final ('por R$', 'no pix R$'); sem essa
    marcacao usa o menor valor, que costuma ser o preco com desconto e nao o
    'de R$' riscado.
    """
    marked, every = [], []
    for match in _PRICE_RE.finditer(text or ''):
        before = text[max(0, match.start() - 20):match.start()]
        if _INSTALLMENT_RE.search(before):
            continue
        value = _to_float(match.group(1))
        if value is None or value <= 0:
            continue
        every.append(value)
        if _CASH_RE.search(before):
            marked.append(value)
    candidates = marked or every
    return min(candidates) if candidates else None


def product_ids(text: str) -> set[str]:
    """Codigos de produto encontrados nos links, independentes do afiliado."""
    found = set()
    for pattern in _PRODUCT_ID_PATTERNS:
        for match in pattern.finditer(text or ''):
            found.add(''.join(match.groups()).casefold())
    return found


def clean_text(text: str) -> str:
    """Texto normalizado sem links nem precos, que variam entre os canais.

    Numero e unidade viram um token so ('4 litros' e '4L' -> '4l'), para que
    escritas diferentes da mesma medida se encontrem.
    """
    without_links = _URL_RE.sub(' ', text or '')
    plain = normalize(_PRICE_RE.sub(' ', without_links))
    return _SPEC_RE.sub(lambda m: m.group(1) + _UNIT_ALIASES.get(m.group(2), m.group(2)), plain)


def tokens(text: str) -> set[str]:
    """Palavras que descrevem o produto, sem links, precos nem propaganda."""
    return {word for word in clean_text(text).split()
            if len(word) > 1 and word not in STOPWORDS}


def specs(text: str) -> dict[str, set[str]]:
    """Medidas declaradas no anuncio agrupadas por unidade ('tb' -> {'1'}).

    Servem para separar variacoes do mesmo modelo: 1TB e 2TB, 50 e 65 polegadas.
    """
    found: dict[str, set[str]] = {}
    for value, unit in _SPEC_RE.findall(clean_text(text)):
        found.setdefault(_UNIT_ALIASES.get(unit, unit), set()).add(value.lstrip('0') or '0')
    return found


def conflicting_specs(first: dict[str, set[str]], second: dict[str, set[str]]) -> bool:
    """Mesma unidade com valores totalmente diferentes indica outro modelo."""
    return any(values and second[unit] and not (values & second[unit])
               for unit, values in first.items() if unit in second)


def codes(text: str) -> set[str]:
    """Codigos de modelo do texto: G502, K552, 510BT, DDR5, 5060, 8GB."""
    return {word for word in tokens(text)
            if (any(ch.isdigit() for ch in word)
                and (any(ch.isalpha() for ch in word) or len(word) > 2))}


def conflicting_codes(first: set[str], second: set[str]) -> bool:
    """Dois anuncios que citam modelos e nao compartilham nenhum sao produtos diferentes."""
    return bool(first) and bool(second) and not (first & second)


def similarity(first: str, second: str) -> float:
    """Semelhanca entre 0 e 1 de dois anuncios pelo vocabulario do produto."""
    return token_similarity(tokens(first), tokens(second))


def _weight(word: str) -> int:
    """Marca, modelo e medida (tem digito) identificam melhor que palavra comum."""
    return 3 if any(ch.isdigit() for ch in word) else 1


def _mass(words) -> int:
    return sum(_weight(word) for word in words)


def token_similarity(first: set[str], second: set[str]) -> float:
    if not first or not second:
        return 0.0
    common = _mass(first & second)
    jaccard = common / _mass(first | second)
    # Contencao cobre o caso de um canal repetir o anuncio com texto bem maior.
    containment = common / min(_mass(first), _mass(second))
    return (jaccard + containment) / 2


class Fingerprint(NamedTuple):
    """Resumo comparavel de um anuncio: nomes, codigos de link, medidas e modelos."""
    tokens: frozenset
    ids: frozenset
    specs: dict
    codes: frozenset


def fingerprint(text: str) -> Fingerprint:
    return Fingerprint(frozenset(tokens(text)), frozenset(product_ids(text)),
                       specs(text), frozenset(codes(text)))


def same_fingerprint(first: Fingerprint, second: Fingerprint,
                     threshold: float = DEFAULT_SIMILARITY) -> bool:
    if first.ids and second.ids:
        # Codigos de produto no link sao decisivos nos dois sentidos.
        return bool(first.ids & second.ids)
    # Na duvida preferimos entregar duas vezes a esconder uma oferta diferente.
    if conflicting_specs(first.specs, second.specs) or conflicting_codes(first.codes, second.codes):
        return False
    return token_similarity(first.tokens, second.tokens) >= threshold


def same_offer(first: str, second: str, threshold: float = DEFAULT_SIMILARITY) -> bool:
    return same_fingerprint(fingerprint(first), fingerprint(second), threshold)


def cluster_fingerprints(prints: Sequence[Fingerprint],
                         threshold: float = DEFAULT_SIMILARITY) -> list[list[int]]:
    """Agrupa indices de anuncios equivalentes, preservando a ordem de chegada."""
    clusters: list[list[int]] = []
    for index, described in enumerate(prints):
        for cluster in clusters:
            if any(same_fingerprint(described, prints[other], threshold) for other in cluster):
                cluster.append(index)
                break
        else:
            clusters.append([index])
    return clusters


def cluster_offers(texts: Sequence[str], threshold: float = DEFAULT_SIMILARITY) -> list[list[int]]:
    return cluster_fingerprints([fingerprint(text) for text in texts], threshold)
