"""Deterministic market facts: typed measurements and publication signals.

Amounts from different metrics, units, periods or markets are never added.
"""
import hashlib
import json
import math
import re
from calendar import monthrange
from collections import defaultdict
from datetime import date, datetime, timezone
from functools import lru_cache

from .normalization import ALIASES, clean_text, normalize_product, product_candidates, product_phrase
from .extraction import domain_of

SENTENCE_BOUNDARY = (r'(?<!тыс\.)(?<!млн\.)(?<!млрд\.)(?<!руб\.)(?<!шт\.)'
                     r'(?<!\bг\.)(?<!\bгг\.)(?<!\bкв\.)(?<=[.!?])\s+|[\r\n]+')
CLAUSE_BOUNDARY = re.compile(r'[,;]\s*(?=[a-zа-яё])', re.I)


METRICS = {
    'sales': 'Продажи', 'purchases': 'Закупки', 'orders': 'Заказы',
    'searches': 'Поисковые запросы', 'mentions': 'Упоминания', 'imports': 'Импорт',
    'sales_growth': 'Изменение продаж', 'purchases_growth': 'Изменение закупок',
    'orders_growth': 'Изменение заказов', 'searches_growth': 'Изменение поисковых запросов',
}
UNITS = {'units': 'шт.', 'RUB': 'руб.', 'USD': 'USD', 'EUR': 'EUR', 'kg': 'кг',
         'orders': 'заказов', 'queries': 'запросов', 'mentions': 'упоминаний',
         'contracts': 'контрактов', 'percent': '%'}
METRIC_UNITS = {'sales': {'units', 'RUB', 'USD', 'EUR'}, 'purchases': {'units', 'RUB', 'USD', 'EUR', 'contracts'},
                'orders': {'orders'}, 'searches': {'queries'}, 'mentions': {'mentions'},
                'imports': {'USD', 'kg', 'units'}}
MONTHS = ['январ', 'феврал', 'март', 'апрел', 'ма(?:й|я|е|ю|ем)', 'июн', 'июл',
          'август', 'сентябр', 'октябр', 'ноябр', 'декабр']
NEGATIVE = re.compile(r'\b(?:сниж\w*|сниз\w*|сократ\w*|сокращ\w*|упал\w*|упад\w*|падени\w*|пада\w*|уменьш\w*|обвал\w*|спад\w*|просел\w*|просед\w*|обруш\w*)\b|'
    r'на\s+\d+(?:[.,]\d+)?\s*%\s+(?:меньше|хуже)|'
    r'(?<![\w])[-−–]\s*\d|не\s+(?:вырос|раст|увелич|подтвержд)|нет\s+спрос|отсутств\w*\s+спрос', re.I)
FORECAST = re.compile(r'\b(?:прогноз\w*|ожида\w*|может|могут|предполож\w*|гипотез\w*|планиру\w*|вырастет|вырастут|'
    r'увеличится|увеличатся|будет|будут|будущ\w*|предстоящ\w*|предсказыва\w*|потенциальн\w*)\b', re.I)
SIGNAL = re.compile(r'спрос|продаж(?:и|ам|ами|ах)?\b|покуп\w*|потреблен|закуп|популяр|заказ|'
                    r'просмотр\w*|интерес\w*|присматрив\w*', re.I)
GROWTH = re.compile(r'\b(?:вырос\w*|возрос\w*|раст[её]т|растут|увеличил\w*|увеличени\w*|рост\w*|прирост\w*|'
                    r'стал[аи]?(?:\s+\w+){0,2}\s+чаще)', re.I)
TRANSACTION = re.compile(r'продан\w*|продал[аи]?\b|закупил[аи]?\b|закуплен\w*|приобрет[её]н\w*|купил[аи]?\b|приобр[её]л[аи]?\b|'
                         r'покупал[аи]?\b|покупали\b|заказывал[аи]?\b|заказывали\b|'
                         r'заказал[аи]?\b|получен\w*\s+заказ\w*|продаж\w*[^.!?]{0,90}\bсоставил\w*', re.I)
GENERAL_ARTICLE = re.compile(r'\?|календар\w*\s+спрос|как(?:ую|ой|ие|их)?\s+.*(?:покуп|выбир|прода|продви)|'
                             r'совет\w*\s+(?:по|для)\s+(?:продаж|покуп)', re.I)
COMPARISON_BASELINE = re.compile(r'\b(?:годом\s+ранее|годом\s+раньше|'
                                 r'год\s+назад|в\s+прошлом\s+году|за\s+аналогичн\w*\s+период\w*\s+прошл\w*)\b', re.I)
MARKET_START = date(2025, 1, 1)
MARKET_PERIOD_LABEL = f'с {MARKET_START.year} года по настоящее время'
NUMBER = re.compile(r'(?<![\w])\d+(?:[ \u00a0\u202f]\d{3})*(?:[.,]\d+)?')
YEAR_RANGE = re.compile(r'\b(20\d{2})\s*(?:[-–—]|\s+(?:до|по)\s+)\s*(20\d{2})\b', re.I)
SCALE_UNIT = re.compile(r'^\s*(?P<scale>тыс\.?|млн\.?|миллион\w*|млрд\.?|миллиард\w*)?\s*'
    r'(?P<unit>%|процент\w*|штук\w*|шт\.?\b|единиц\w*|руб\w*\.?|₽|доллар\w*|USD\b|\$|евро\b|EUR\b|€|заказ\w*|'
    r'поисков\w*\s+запрос\w*|запрос\w*|упоминан\w*|контракт\w*)?', re.I)
# These dimensions and containers cannot be compared as individual goods.
# Unsupported quantities remain publication signals until a typed metric exists.
NON_COUNT_UNIT = re.compile(
    r'^(?:килограмм\w*|грамм\w*|тонн\w*|литр\w*|миллилитр\w*|метр\w*|'
    r'квадратн\w*|кубическ\w*|упаков\w*|пач\w*|пакет\w*|короб\w*|банок|банки|'
    r'бутыл\w*|комплект\w*|набор\w*|kg|кг|г|мг|т|л|мл|м|мм|см|км|m[23²³]?)(?!\w)', re.I)
METRIC_WORDS = {
    'sales': re.compile(r'\b(?:продаж\w*|продан\w*|продал\w*|реализован\w*)', re.I),
    'purchases': re.compile(r'\b(?:закуп\w*|приобрет[её]н\w*|куплен\w*|купил[аи]?\b|приобр[её]л[аи]?\b)', re.I),
    'orders': re.compile(r'заказ\w*', re.I),
    'searches': re.compile(r'поисков\w*\s+запрос\w*|частотност\w*|запрос\w*\s+в\s+поиск\w*', re.I),
    'mentions': re.compile(r'упоминан\w*', re.I),
}
PRICE = re.compile(r'цен\w*|стоимост\w*|подорож\w*|инфляц\w*', re.I)
MARKET_SHARE = re.compile(r'\b(?:дол(?:я|ю|и|е|ей|ь)|удельн\w*\s+вес)\b', re.I)
CORPORATE_TRANSACTION = re.compile(
    r'\b(?:приобр[её]л[аи]?|купил[аи]?|продал[аи]?|продан[аы]?)\s+'
    r'(?:[а-яёa-z-]+\s+){0,3}(?:актив\w*|подразделени\w*|бизнес\w*|дол[юи]\b|'
    r'завод(?:а|ы)?\b|фабрик[ауи]\b|компанию\b|предприяти[ея]\b)|'
    r'\bсделк\w*\s+по\s+(?:покупке|продаже|приобретению)\s+'
    r'(?:[а-яёa-z-]+\s+){0,3}(?:актив\w*|подразделени\w*|бизнес\w*|компани[юи]\b|предприяти[ея]\b)', re.I)
COUNTRY_PATTERNS = {
    'Россия': r'росси\w*|russia|\bрф\b', 'Беларусь': r'беларус\w*',
    'Казахстан': r'казахстан\w*', 'Армения': r'армени\w*', 'Азербайджан': r'азербайджан\w*',
    'Кыргызстан': r'кыргыз\w*|киргиз\w*', 'Молдова': r'молдов\w*',
    'Таджикистан': r'таджикистан\w*', 'Туркменистан': r'туркменистан\w*', 'Узбекистан': r'узбекистан\w*',
    'Индия': r'\bинди[яиюей]\b|индийск\w*', 'Китай': r'кита[йяею]\w*|китайск\w*',
    'США': r'\bсша\b|соедин[её]нн\w*\s+штат\w*|\busa\b',
    'Евросоюз': r'евросоюз\w*|европейск\w*\s+союз\w*|\bес\b',
}
COUNTRY_ORIGIN = re.compile(
    r'\b(?:из|производства|происхождения|'
    r'(?:произвед[её]н|выращен|изготовлен|собран)\w*\s+(?:в|во|на))\s+'
    r'(?:(?:' + '|'.join(COUNTRY_PATTERNS.values()) + r')'
    r'(?:\s*[,/]\s*|\s+(?:и|или|а\s+также)\s+))*$', re.I)
MARKET_ADJECTIVE_SUBJECT = re.compile(
    r'\s+(?:покупател|потребител|пользовател|рын(?:ок|к)|территори|розниц|спрос|продаж)\w*', re.I)
COUNTRY_LINK = re.compile(r'\s*(?:и|или|/|,|а\s+также)\s*(?:(?:в|во|на)\s+)?', re.I)
COUNTRY_ELLIPSIS = re.compile(
    r'^\s*(?:(?:а|и|но|отдельно)\s+)?(?:(?:в|во|на)\s+)?(?:'
    + '|'.join(COUNTRY_PATTERNS.values()) + r')\s*[—–-]\s*(?P<change>на\s+)?'
    + r'(?P<number>' + NUMBER.pattern + r')\s*(?P<unit>%|штук\w*|шт\.?)\s*[.!]?$', re.I)


def period_from(text):
    """Use an explicit reporting period; publication date is never its substitute."""
    value = clean_text(text)
    months = '(?:' + '|'.join(MONTHS) + r')\w*'
    match = re.search(r'\b(' + months + r')\s*(?:[-–—]|по)\s*(' + months + r')\s+(20\d{2})\b', value)
    if match:
        first = next(i for i, expression in enumerate(MONTHS, 1) if re.match(expression, match[1]))
        last = next(i for i, expression in enumerate(MONTHS, 1) if re.match(expression, match[2]))
        return f'{match[3]}-{first:02d}/{match[3]}-{last:02d}'
    match = YEAR_RANGE.search(value)
    if match:
        return f'{match[1]}/{match[2]}'
    for number, month in enumerate(MONTHS, 1):
        match = re.search(r'\b' + month + r'\w*\s+(20\d{2})\b', value)
        if match:
            return f'{match[1]}-{number:02d}'
    match = re.search(r'\b([1-4]|iv|iii|ii|i|перв\w*|втор\w*|трет\w*|четверт\w*)\s*'
                      r'(?:-?\s*[йыи]\s*)?(?:квартал\w*|кв\.)\s+(20\d{2})\b', value)
    if match:
        quarters = {'i': 1, 'ii': 2, 'iii': 3, 'iv': 4}
        quarter = (int(match[1]) if match[1].isdigit() else quarters.get(match[1])
                   or next(number for stem, number in (('перв', 1), ('втор', 2), ('трет', 3), ('четверт', 4))
                           if match[1].startswith(stem)))
        return f'{match[2]}-Q{quarter}'
    match = re.search(r'\b(перв\w*|втор\w*|[12](?:-?[а-я]+)?)\s+полугоди\w*\s+(20\d{2})\b', value)
    if match:
        first = 1 if match[1].startswith(('перв', '1')) else 7
        return f'{match[2]}-{first:02d}/{match[2]}-{first + 5:02d}'
    match = re.search(r'\b(?:за|итогам)\s+(?:(?:первые|первых)\s+)?'
                      r'(1[0-2]|[1-9]|девять|девяти)\s+месяц\w*\s+(20\d{2})\b', value)
    if match:
        last = int(match[1]) if match[1].isdigit() else 9
        return f'{match[2]}-01/{match[2]}-{last:02d}'
    match = re.search(r'(?:\bв|\bза|итог\w*|период\w*)\s+(20\d{2})[-.](0[1-9]|1[0-2])\b', value)
    if match:
        return f'{match[1]}-{match[2]}'
    match = re.search(r'(?:\bв|\bза|итог\w*|период\w*)\s+(20\d{2})(?:\s*г(?:од\w*|\.)?)?\b', value)
    return match[1] if match else ''


@lru_cache(maxsize=512)
def _country_mentions(text):
    mentions = {}
    for country, pattern in COUNTRY_PATTERNS.items():
        for mention in re.finditer(pattern, text, re.I):
            if COUNTRY_ORIGIN.search(text[:mention.start()]):
                continue
            if re.search(r'ск[а-яё]*$', mention[0], re.I) and not MARKET_ADJECTIVE_SUBJECT.match(text[mention.end():]):
                continue
            mentions.setdefault(country, []).append(mention)
    return mentions


def country_from(text, fallback=''):
    mentions = _country_mentions(text)
    ordered = sorted((m.start(), m.end(), country) for country, matches in mentions.items() for m in matches)
    if any(left[2] != right[2] and COUNTRY_LINK.fullmatch(text[left[1]:right[0]])
           for left, right in zip(ordered, ordered[1:])):
        return ''
    # Origin adjectives ("китайских смартфонов") and imports "из США" do not
    # override an explicitly located market. Coordinated/multiple markets stay
    # ambiguous; the country selected for a search is never geographic evidence.
    located = []
    for country, matches in mentions.items():
        for mention in matches:
            if re.search(r'\b(?:в|во|на)\s+(?:(?:рынк\w*|территори\w*)\s+)?$', text[:mention.start()], re.I):
                located.append((country, mention))
    markets = {country for country, _ in located}
    if len(markets) == 1:
        return next(iter(markets))
    if markets:
        return ''
    if len(mentions) == 1:
        return next(iter(mentions))
    return fallback if fallback in mentions else ''


def market_fact_clause(text):
    """Retain a reported fact before a separate explanation or future plan."""
    for separator in re.finditer(r'[,;]\s*', text):
        before, after = text[:separator.start()], text[separator.end():]
        explanatory = re.match(r'что\s+может\s+быть\s+связан\w*', after, re.I)
        plan = re.match(r'(?:(?:а|и)\s+)?(?:компани\w*|производител\w*|продавец|он[аи]?)\s+планиру\w*', after, re.I)
        if ((explanatory or plan) and not FORECAST.search(before)
                and (GROWTH.search(before) or TRANSACTION.search(before))):
            return before.rstrip()
    return text


def current_market_fact(text):
    """Reject declines and projected demand, including inside whole words."""
    fact = market_fact_clause(text)
    return bool(fact.strip()) and not (NEGATIVE.search(YEAR_RANGE.sub(' ', fact)) or FORECAST.search(fact))


def _comparison_baseline(text):
    for match in COMPARISON_BASELINE.finditer(text):
        prefix = text[:match.start()]
        if not (GROWTH.search(prefix) or TRANSACTION.search(prefix)
                or re.search(r'\b(?:чем|по\s+сравнению\s+с)\s*$', prefix, re.I)):
            return True
    return False


def _number_context(sentence, match):
    # A comma between digits belongs to a number; clause commas separate markets.
    splits = list(CLAUSE_BOUNDARY.finditer(sentence))
    left = max((part.end() for part in splits if part.end() <= match.start()), default=0)
    right = min((part.start() for part in splits if part.start() >= match.end()), default=len(sentence))
    return sentence[left:right], left


def _geographical_claim(text):
    return bool(_country_mentions(text) and (
        COUNTRY_ELLIPSIS.fullmatch(text) or TRANSACTION.search(text)
        or ((SIGNAL.search(text) or PRICE.search(text)) and
            (NUMBER.search(text) or GROWTH.search(text) or NEGATIVE.search(text) or FORECAST.search(text)))))


def _market_fact_spans(text):
    for part in re.split(SENTENCE_BOUNDARY, text, flags=re.I):
        sentence = market_fact_clause(' '.join(part.split()))
        start = 0
        if len(_country_mentions(sentence)) > 1:
            separators = list(CLAUSE_BOUNDARY.finditer(sentence))
            for index, separator in enumerate(separators):
                end = separators[index + 1].start() if index + 1 < len(separators) else len(sentence)
                first = sentence[start:separator.start()]
                second = sentence[separator.end():end]
                if _geographical_claim(first) and _geographical_claim(second):
                    yield first, sentence
                    start = separator.end()
        yield sentence[start:], sentence


def _market_country(*scopes):
    for scope in scopes:
        if _country_mentions(scope):
            return country_from(scope)
    return ''


def event_key(product, metric, value, unit, period, country, excerpt='', context=''):
    # Identical quantities are not evidence of an identical transaction.
    # Retain the whole textual context so verbatim reprints still link together.
    context = clean_text(context or excerpt).strip(' .!?')
    identity = [normalize_product(product), metric, None if value is None else float(value),
                unit, period, country, clean_text(excerpt).strip(' .!?'), context]
    return hashlib.sha256(json.dumps(identity, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def _event_context(text, title, excerpt):
    prefix = title + '. '
    if title and text.startswith(prefix):
        body = text[len(prefix):].strip()
        headline, separator, remainder = body.partition('\n')
        if separator and clean_text(headline) == clean_text(title):
            body = remainder
        if body and clean_text(excerpt).strip(' .!?') in clean_text(body):
            return body
    return text or excerpt


def _has_product(product, text):
    canonical = normalize_product(product)
    if canonical == 'самокат' and re.search(r'сервис\w*\s+доставк|яндекс\s+лавк|фров|[«"]Самокат[»"]', text, re.I):
        if not re.search(r'электросамокат|кикшер|катани|двухкол[её]с|продаж\w*\s+самокатов|самокаты\s+для', text, re.I):
            return False
    folded = text.replace('ё', 'е').replace('Ё', 'Е')
    return any(_direct_product_spans(canonical, folded))


@lru_cache(maxsize=1)
def _quantity_morphology():
    from pymorphy3 import MorphAnalyzer
    from pymorphy3_dicts_ru import get_path
    return MorphAnalyzer(path=get_path(), lang='ru')


@lru_cache(maxsize=4096)
def _quantity_word(word):
    variants = _quantity_morphology().parse(word)
    known = next((parsed for parsed in variants if parsed.is_known), None)
    return (known.normal_form, known.tag.POS) if known else (word, None)


QUALIFIER = re.compile(r'\b(для|к|ко)\s+((?:[а-яёa-z-]+(?:\s+|,\s*)){0,5})$', re.I)
LIST_SEPARATOR = r'(?:,\s*(?:(?:и|а\s+также)\s+)?|(?:и|или|либо|а\s+также)\s+)'
PRODUCT_LIST_JOIN = re.compile(r'\s*' + LIST_SEPARATOR + r'(.*?)\s*$', re.I)
LEFT_LIST_NOUN = re.compile(r'(?P<word>\b[а-яё-]+)\b\s*' + LIST_SEPARATOR + r'$', re.I)
RIGHT_LIST_WORDS = re.compile(r'^\s*' + LIST_SEPARATOR + r'(?P<words>(?:[а-яё-]+\s*){1,4})', re.I)


@lru_cache(maxsize=4096)
def _product_patterns(product):
    return (product_phrase(product), *(re.compile(r'(?<!\w)' + alias + r'(?!\w)', re.I)
                                       for alias in ALIASES.get(product, ())))


@lru_cache(maxsize=4096)
def _is_adjectival_product_form(word):
    variants = _quantity_morphology().parse(word)
    best = next((variant for variant in variants if variant.is_known), variants[0])
    return best.tag.POS in ('ADJF', 'PRTF')


def _is_qualified_product(text, match):
    qualifier = QUALIFIER.search(text[:match.start()])
    if not qualifier:
        return False
    modifiers = re.findall(r'[а-яa-z-]+', qualifier[2].casefold())
    parts = [_quantity_word(word)[1] for word in modifiers]
    if all(part in ('ADJF', 'PRTF') for part in parts):
        return True
    if (not re.search(r',|\b(?:и|или)\b', qualifier[2], re.I)
            or any(part in ('VERB', 'INFN') for part in parts)):
        return False
    case = 'gent' if qualifier[1].casefold() == 'для' else 'datv'
    words = re.findall(r'[а-я]+', match[0].casefold())
    return any(variant.tag.POS == 'NOUN' and variant.tag.case == case
               for word in words for variant in _quantity_morphology().parse(word))


def _direct_product_spans(product, text):
    for pattern in _product_patterns(product):
        for match in pattern.finditer(text):
            form = match[0].casefold()
            if form != product and re.fullmatch(r'[а-я]+', form) and _is_adjectival_product_form(form):
                continue
            if _is_qualified_product(text, match):
                continue
            yield match.span()


@lru_cache(maxsize=256)
def _sentence_products(sentence):
    folded = sentence.replace('ё', 'е').replace('Ё', 'Е')
    spans = {(start, end, product) for product in product_candidates(sentence)
             for start, end in _direct_product_spans(product, folded)}
    ordered = sorted(spans, key=lambda span: (span[0], -span[1], -len(span[2]), span[2]))
    kept = []
    for span in ordered:
        if not kept or kept[-1][1] <= span[0]:
            kept.append(span)
    return tuple(kept)


def _direct_count_subject(product, remainder):
    """The counted noun must be the product, not stores or its accessories."""
    canonical = normalize_product(product)
    if not _has_product(canonical, remainder[:100]):
        return False
    expected_nouns = {_quantity_word(word)[0] for word in re.findall(r'[а-яёa-z]+', canonical)
                      if _quantity_word(word)[1] == 'NOUN'}
    for word in list(re.finditer(r'[а-яёa-z]+', remainder, re.I))[:8]:
        if _has_product(canonical, remainder[:word.end()]):
            return True
        lemma, part = _quantity_word(word[0].casefold())
        if part in ('ADJF', 'PRTF') or (part == 'NOUN' and lemma in expected_nouns):
            continue
        return False
    return False


def _counted_quantity(product, tail):
    suffix = SCALE_UNIT.match(tail)
    unit = (suffix.group('unit') or '').casefold()
    remainder = tail[suffix.end():].lstrip()
    if unit:
        if not re.match(r'штук|шт\.?\b|единиц', unit):
            return False
        # A bare "500 units" can refer back to an explicit nearby product.
        if not remainder or re.match(r'[.!?;,)]', remainder):
            return True
        if re.match(r'(?:в|во|за|по|из|на)\b', remainder, re.I):
            return True
        return _direct_count_subject(product, remainder)
    if NON_COUNT_UNIT.match(remainder):
        # The container can itself be the researched good (e.g. a cardboard box).
        first = remainder.split(maxsplit=1)[0] if remainder else ''
        if not _has_product(product, first):
            return False
    return _direct_count_subject(product, remainder)


def article_text(html):
    """Read article content only; the company parser retains its separate full-site view."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, 'html.parser')
    main = None
    for selector in ('[itemprop="articleBody"]', '.article-body', '.article__body', '.article__text',
                     '.news-detail-text', '.news-text', '.item_fulltext', 'article', 'main', '[role="main"]'):
        main = soup.select_one(selector)
        if main is not None:
            break
    if main is None:
        main = soup.body or soup
    for node in main.select('script,style,noscript,nav,header,footer,aside,form,.tags,.tag-list,.article-tags,.article__tags,.breadcrumbs,.related,.recommended,.menu,.navigation,.sidebar,.cookie-banner'):
        node.decompose()
    # Inline links/spans must not break one fact into unrelated fragments.
    # Preserve semantic block boundaries, flatten formatting within paragraphs.
    for node in main.select('br'):
        node.replace_with('\n')
    for node in main.select('p,li,h1,h2,h3,h4,blockquote,td,div,section'):
        node.insert_before('\n')
        node.insert_after('\n')
    return '\n'.join(line for line in (' '.join(line.split())
                     for line in main.get_text(' ').splitlines()) if line)[:100000]


def usable_market_excerpt(text):
    return (bool(text) and not re.search(r'\b(?:теги|темы)\s*:|#[a-zа-яё][\w-]*', text, re.I)
            and not CORPORATE_TRANSACTION.search(text))


def reported_interest(text, context=''):
    """Require a reported purchase or an actual change in buyer interest."""
    text = market_fact_clause(text)
    if not usable_market_excerpt(text) or GENERAL_ARTICLE.search(text) or not current_market_fact(text) or _comparison_baseline(text):
        return False
    if MARKET_SHARE.search(text):
        return any(reported_interest(clause, context) for clause in CLAUSE_BOUNDARY.split(text)
                   if not MARKET_SHARE.search(clause))
    # A malformed comparison such as "купили на 19% ноутбуков, чем годом
    # ранее" omits the direction. It cannot establish a positive signal merely
    # because it contains a purchase verb; preserve complete source wording.
    if (re.search(r'на\s+\d+(?:[.,]\d+)?\s*%', text, re.I)
            and re.search(r'\bчем\b|годом\s+ранее|по\s+сравнению', text, re.I)
            and not (GROWTH.search(text) or re.search(r'\bбольше\b|\bчаще\b', text, re.I))):
        return False
    if TRANSACTION.search(text):
        return True
    if re.search(r'(?:лидер\w*\s+продаж|сам\w*\s+(?:популярн|востребованн)\w*)'
                 r'[^.!?]{0,80}\bстал[аи]?\b|'
                 r'\b(?:стал[аи]?|оказал\w*)\b[^.!?]{0,80}'
                 r'(?:сам\w*\s+(?:популярн|востребованн)\w*|лидер\w*\s+продаж)|'
                 r'\bвошл\w*\s+в\s+(?:топ|число\s+сам\w*\s+популярн)', text, re.I):
        return True
    if re.search(r'\b(?:закупк|тендер)\w*[^.!?]{0,140}\b(?:это|объявлен\w*|размещ\w*|опублик\w*|провод\w*)', text, re.I):
        return True
    if re.search(r'\bв\s+\d+(?:[.,]\d+)?\s+раз[аы]?\b', text, re.I) and GROWTH.search(text) and not PRICE.search(text):
        normalized_context, normalized_text = ' '.join(context.split()), ' '.join(text.split())
        position = normalized_context.find(normalized_text)
        preceding = normalized_context[max(0, position - 600):position] if position >= 0 else ''
        if re.search(r'\b(?:заказ\w*|продаж\w*|спрос|покуп\w*)', preceding, re.I):
            return True
    for change in GROWTH.finditer(text):
        metric_distances = [abs(metric.start() - change.start()) for metric in SIGNAL.finditer(text)]
        if not metric_distances or min(metric_distances) > 85:
            continue
        price_distance = min((abs(price.start() - change.start()) for price in PRICE.finditer(text)), default=9999)
        if min(metric_distances) < price_distance:
            return True
    return False


def _reported_product_excerpt(product, sentence, context):
    clauses = CLAUSE_BOUNDARY.split(sentence) if PRICE.search(sentence) or MARKET_SHARE.search(sentence) else [sentence]
    for clause in clauses:
        if MARKET_SHARE.search(clause):
            continue
        if PRICE.search(clause) and not any(pattern.search(clause) for pattern in METRIC_WORDS.values()):
            continue
        if _has_product(product, clause) and reported_interest(clause, context):
            return clause
    return ''


def _stamp(value):
    if not value:
        return None
    try:
        result = (datetime.strptime(value, '%Y%m%dT%H%M%SZ') if re.fullmatch(r'\d{8}T\d{6}Z', value)
                  else datetime.fromisoformat(value.replace('Z', '+00:00')))
        return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def _period_bounds(period):
    bounds = []
    for part in period.split('/'):
        match = re.fullmatch(r'(20\d{2})(?:-(Q[1-4]|0[1-9]|1[0-2]))?', part)
        if not match:
            return None
        year, section = int(match[1]), match[2]
        first, last = (1, 12) if not section else ((3 * int(section[1]) - 2, 3 * int(section[1]))
                                                  if section.startswith('Q') else (int(section), int(section)))
        bounds.append((date(year, first, 1), date(year, last, monthrange(year, last)[1])))
    if len(bounds) > 2 or bounds[0][0] > bounds[-1][0]:
        return None
    return bounds[0][0], bounds[-1][1]


def market_publication_possible(published, as_of):
    """A dated publication can document a historical interval retrospectively."""
    if not published:
        return True
    stamp = _stamp(published)
    return stamp is not None and MARKET_START <= stamp.date() <= as_of.date()


def _current_fact(item, as_of):
    published = item.get('published_at') or ''
    if not market_publication_possible(published, as_of):
        return False
    stamp = _stamp(published)
    latest = stamp.date() if stamp else as_of.date()
    period = item.get('period') or ''
    if not period:
        return stamp is not None
    bounds = _period_bounds(period)
    if not bounds:
        return False
    first, last = bounds
    # A started period may be partial within the publication or assessment year.
    return MARKET_START <= first <= latest and last.year <= latest.year


def _number_belongs(product, sentence, number, offset, length):
    """Require a direct product in this clause and exclude undivided product lists."""
    mentions = _sentence_products(sentence)
    candidates = []
    for index, (start, end, name) in enumerate(mentions):
        if offset <= start < offset + length:
            distance = max(start - number.end(), number.start() - end, 0)
            candidates.append((distance, -len(name), index))
    if not candidates:
        return False
    index = min(candidates)[2]
    if mentions[index][2] != normalize_product(product):
        return False
    for left, right in ((index - 1, index), (index, index + 1)):
        if left < 0 or right >= len(mentions) or mentions[left][2] == mentions[right][2]:
            continue
        joined = PRODUCT_LIST_JOIN.fullmatch(sentence[mentions[left][1]:mentions[right][0]])
        if joined and all(_quantity_word(word.casefold())[1] in ('ADJF', 'PRTF') for word in joined[1].split()):
            return False
    start, end, _ = mentions[index]
    return not _unknown_coordinated_noun(sentence, start, end)


@lru_cache(maxsize=4096)
def _noun_cases(word):
    return {variant.tag.case for variant in _quantity_morphology().parse(word)
            if variant.tag.POS == 'NOUN' and variant.is_known}


def _unknown_coordinated_noun(sentence, start, end):
    left = LEFT_LIST_NOUN.search(sentence[:start])
    right = RIGHT_LIST_WORDS.match(sentence[end:])
    if not left and not right:
        return False
    product_words = re.findall(r'[а-яё]+', sentence[start:end].casefold())
    cases = next((cases for word in product_words if (cases := _noun_cases(word))), set())
    if not cases:
        return False
    if left and cases.intersection(_noun_cases(left['word'].casefold())):
        unit = SCALE_UNIT.fullmatch(left['word'])
        if not (unit and unit['unit'] and re.search(r'\d\s*$', sentence[:left.start()])):
            return True
    if right:
        words = re.findall(r'[а-яё-]+', right['words'].casefold())
        noun = next((word for word in words if not _is_adjectival_product_form(word)), '')
        if re.match(r'\s*[—–-]\s*(?:на\s+)?\d', sentence[end + right.end():], re.I):
            return False
        return bool(noun and cases.intersection(_noun_cases(noun)))
    return False


def bundle_budget(sentence, number, unit):
    """An entire equipment lot's budget cannot be one listed product's value."""
    if unit not in ('RUB', 'USD', 'EUR'):
        return False
    local, _ = _number_context(sentence, number)
    bundle = re.search(r'оборудован\w*\s+на\s+\d|(?:общ\w*|совокупн\w*)\s+стоимост\w*|стоимост\w*\s+комплект\w*', local, re.I)
    listed_goods = len(product_candidates(sentence)) >= 2 or bool(re.search(r',\s*[^.!?]{2,100}\s+и\s+', sentence, re.I))
    return bool(bundle and listed_goods)


def _metric_for(sentence, position, unit):
    choices = [(match.start() > position, abs(match.start() - position), name)
               for name, pattern in METRIC_WORDS.items()
               for match in pattern.finditer(sentence)]
    if not choices:
        return ''
    _, distance, metric = min(choices)
    if distance > 140:
        return ''
    price_distance = min((abs(match.start() - position) for match in PRICE.finditer(sentence)), default=9999)
    if price_distance < distance:
        return ''
    if unit == 'queries':
        return 'searches' if metric == 'searches' else ''
    if unit == 'orders':
        return 'orders' if metric in ('orders', 'sales', 'purchases') else ''
    if unit == 'mentions':
        return 'mentions'
    if unit == 'percent':
        return metric + '_growth' if metric not in ('mentions',) else ''
    return metric if metric in ('sales', 'purchases') else ''


def _metric_context(sentence, local, offset, position):
    local_position = position - offset
    # A contrast with its own metric ends the previous one. Only an explicit
    # dash omission, e.g. «ноутбуков — 100 штук», can inherit a preceding metric.
    if (offset and re.search(r'[—–-]\s*(?:на\s+)?$', local[:local_position], re.I)
            and not any(pattern.search(local) for pattern in METRIC_WORDS.values())):
        return sentence[:offset + len(local)], position
    return local, local_position


def _percentage_change(local, position, suffix, metric_context):
    if MARKET_SHARE.search(local) or re.match(r'\s*(?:рынка\b|пункт\w*\b|от\s+)', suffix, re.I):
        return False
    prefix = local[:position]
    if not re.search(r'(?:\b(?:на|составил\w*|достиг\w*)|[+:])\s*$', prefix, re.I):
        return False
    return bool(GROWTH.search(metric_context))


def _structured(product, entry, text, published_at):
    if not isinstance(entry, dict):
        return None
    metric, unit, value = entry.get('metric'), entry.get('unit'), entry.get('value')
    period, country = str(entry.get('period', '')).strip(), str(entry.get('country', '')).strip()
    if metric not in METRICS or unit not in UNITS or not period or not country:
        return None
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        return None
    if not re.fullmatch(r'20\d{2}(?:-(?:0[1-9]|1[0-2]|Q[1-4]))?(?:/20\d{2}(?:-(?:0[1-9]|1[0-2]))?)?', period):
        period = period_from('за ' + period)
        if not period:
            return None
    if metric.endswith('_growth') != (unit == 'percent'):
        return None
    if not metric.endswith('_growth') and unit not in METRIC_UNITS.get(metric, set()):
        return None
    excerpt = str(entry.get('excerpt') or text).strip()[:1000]
    if not excerpt or not _has_product(product, excerpt):
        return None
    # The adapter's typed fields must agree with the preserved source excerpt.
    numbers, counted_numbers = [], []
    for match in NUMBER.finditer(excerpt):
        parsed = float(re.sub(r'[ \u00a0\u202f]', '', match[0]).replace(',', '.'))
        scale = (SCALE_UNIT.match(excerpt[match.end():]).group('scale') or '').casefold()
        parsed *= 1000 if scale.startswith('тыс') else (1_000_000_000 if scale.startswith(('млрд', 'миллиард')) else (1_000_000 if scale else 1))
        numbers.append(parsed)
        if _counted_quantity(product, excerpt[match.end():]):
            counted_numbers.append(parsed)
    if not any(math.isclose(value, parsed, rel_tol=1e-12, abs_tol=1e-6) for parsed in numbers):
        return None
    unit_patterns = {'USD': r'\bUSD\b|\$|доллар', 'EUR': r'\bEUR\b|€|евро', 'RUB': r'руб|₽', 'kg': r'\bкг\b|\bkg\b|килограмм',
                     'orders': r'заказ', 'queries': r'запрос', 'mentions': r'упомин',
                     'contracts': r'контракт', 'percent': r'%|процент'}
    if unit != 'units' and not re.search(unit_patterns[unit], excerpt, re.I):
        return None
    if unit == 'units' and not any(math.isclose(value, parsed, rel_tol=1e-12, abs_tol=1e-6)
                                   for parsed in counted_numbers):
        return None
    if period != period_from(excerpt) and period not in excerpt:
        return None
    stated_country = country_from(excerpt)
    if stated_country and country != stated_country:
        return None
    return dict(metric=metric, value=value, unit=unit, period=period[:100], country=country[:100],
                published_at=str(entry.get('published_at') or published_at), excerpt=excerpt,
                measured=metric != 'mentions')


def extract_observations(product, text, country='', published_at='', metadata=None, *, title=''):
    """Extract explicit typed numbers, otherwise keep one public demand signal."""
    output = []
    for entry in (metadata or {}).get('measurements', []):
        item = _structured(product, entry, text, published_at)
        if item:
            output.append(item)
    previous, previous_sentence = [], ''
    for sentence, original_sentence in _market_fact_spans(text):
        ellipsis = COUNTRY_ELLIPSIS.fullmatch(sentence)
        if ellipsis and original_sentence == previous_sentence and len(previous) == 1:
            prior = previous[0]
            unit = 'percent' if ellipsis['unit'] == '%' else 'units'
            actual_country = country_from(sentence)
            if (actual_country and prior['country'] != actual_country and prior['unit'] == unit
                    and (unit == 'percent' or not ellipsis['change'])):
                raw = re.sub(r'[ \u00a0\u202f]', '', ellipsis['number']).replace(',', '.')
                value = float(raw)
                if value > 0 and (unit == 'percent' or value.is_integer()):
                    output.append({**prior, 'value': value, 'country': actual_country,
                                   'excerpt': original_sentence[:1000]})
            continue
        previous, previous_sentence = [], original_sentence
        if not usable_market_excerpt(sentence) or not _has_product(product, sentence) or not current_market_fact(sentence) or _comparison_baseline(sentence):
            continue
        period = period_from(sentence) or period_from(original_sentence) or period_from(text)
        actual_country = _market_country(sentence, text)
        sentence_measures = []
        date_spans = [match.span() for match in re.finditer(r'(?<!\d)20\d{2}-(?:0[1-9]|1[0-2])(?:-\d{2})?(?!\d)', sentence)]
        date_spans.extend(match.span() for match in YEAR_RANGE.finditer(sentence))
        for match in NUMBER.finditer(sentence):
            if any(start <= match.start() and match.end() <= end for start, end in date_spans):
                continue
            if re.fullmatch(r'20\d{2}', match[0]) and (re.match(r'\s*г(?:од|\.)', sentence[match.end():], re.I)
                    or re.search(r'(?:\bв|\bза|итог\w*)\s*$', sentence[:match.start()], re.I)):
                continue
            local, offset = _number_context(sentence, match)
            if _comparison_baseline(local) or not _number_belongs(product, sentence, match, offset, len(local)):
                continue
            actual_period = period_from(local) or period
            suffix = SCALE_UNIT.match(sentence[match.end():])
            scale, raw_unit = suffix.group('scale') or '', (suffix.group('unit') or '').casefold()
            prefix_currency = re.search(r'([$€])\s*$', sentence[:match.start()])
            if not raw_unit and prefix_currency:
                raw_unit = prefix_currency[1]
            value = float(re.sub(r'[ \u00a0\u202f]', '', match[0]).replace(',', '.'))
            if scale.startswith('тыс'):
                value *= 1000
            elif scale.startswith(('млрд', 'миллиард')):
                value *= 1_000_000_000
            elif scale:
                value *= 1_000_000
            if raw_unit.startswith(('%', 'процент')):
                unit = 'percent'
            elif raw_unit.startswith(('руб', '₽')):
                unit = 'RUB'
            elif raw_unit.startswith(('доллар', 'usd', '$')):
                unit = 'USD'
            elif raw_unit.startswith(('евро', 'eur', '€')):
                unit = 'EUR'
            elif 'запрос' in raw_unit:
                unit = 'queries'
            elif raw_unit.startswith('заказ'):
                unit = 'orders'
            elif raw_unit.startswith('упомин'):
                unit = 'mentions'
            elif raw_unit.startswith('контракт'):
                unit = 'contracts'
            elif raw_unit:
                unit = 'units'
                if not _counted_quantity(product, sentence[match.end():]):
                    continue
            else:
                # An explicit counted product, e.g. "продано 500 смартфонов".
                if not _counted_quantity(product, sentence[match.end():]):
                    continue
                unit = 'units'
            metric_context, metric_position = _metric_context(sentence, local, offset, match.start())
            metric = _metric_for(metric_context, metric_position, unit)
            if not metric or not actual_period:
                continue
            if unit in ('RUB', 'USD', 'EUR'):
                # Unit prices are not the total value of a market's sales.
                after_currency = sentence[match.end() + suffix.end():]
                if re.match(r'\s*(?:за|/|на\s+од\w*)\s*', after_currency, re.I):
                    continue
                before_number = sentence[:match.start()].rstrip('$€ \t')
                if not re.search(r'(?:составил\w*|достиг\w*|на\s+сумм\w*|'
                                 r'об[ъь][её]м\w*[^.!?]{0,90}|'
                                 r'(?:продан\w*|закуплен\w*|куплен\w*)[^.!?]{0,70}\bна)\s*$',
                                 before_number, re.I):
                    continue
            if bundle_budget(sentence, match, unit):
                continue
            if unit in ('units', 'orders', 'queries', 'mentions', 'contracts') and not value.is_integer():
                continue
            if metric.endswith('_growth') and not _percentage_change(
                    local, match.start() - offset, sentence[match.end() + suffix.end():], metric_context):
                continue
            sentence_measures.append(dict(metric=metric, value=value, unit=unit, period=actual_period,
                country=actual_country, published_at=published_at, excerpt=sentence[:1000],
                measured=metric != 'mentions'))
        if len({name for _, _, name in _sentence_products(sentence)}) == 1:
            previous = sentence_measures
        if actual_country:
            output.extend(sentence_measures)
            if not sentence_measures and (excerpt := _reported_product_excerpt(product, sentence, text)):
                output.append(dict(metric='public_signal', value=None, unit='', period=period,
                    country=actual_country, published_at=published_at, excerpt=excerpt[:1000], measured=False))
    unique = {}
    for item in output:
        context = _event_context(text, title, item['excerpt'])
        key = event_key(product, item['metric'], item['value'], item['unit'], item['period'], item['country'],
                        item['excerpt'], context)
        unique.setdefault(key, {**item, 'event_key': key})
    return list(unique.values())


def _format_number(value):
    return f'{value:,.6f}'.rstrip('0').rstrip('.').replace(',', ' ').replace('.', ',')


def measurement_basis(item):
    scope = ''
    if item['unit'] == 'percent':
        for match in NUMBER.finditer(item.get('excerpt') or ''):
            value = float(re.sub(r'[ \u00a0\u202f]', '', match[0]).replace(',', '.'))
            if math.isclose(value, item['value'], rel_tol=1e-12):
                local, _ = _number_context(item['excerpt'], match)
                gender = re.search(r'\b(?:женск|мужск)\w*\s+[а-яё ]+', local, re.I)
                if gender:
                    scope = ' · ' + gender[0].strip()
                break
    return f"{METRICS.get(item['metric'], item['metric'])}: {_format_number(item['value'])} {UNITS.get(item['unit'], item['unit'])}{scope} · {item['period']} · {item['country']}"


DEMAND_METRICS = {'sales', 'purchases', 'orders', 'searches',
                  'sales_growth', 'purchases_growth', 'orders_growth', 'searches_growth'}


@lru_cache(maxsize=2048)
def _parsed_market_text(product, content, country, published_at):
    # Source text is part of the key, so edited sources are revalidated.
    # Extraction rules are fixed for the lifetime of this in-memory cache.
    return extract_observations(product, content, country, published_at)


def _validated_excerpt(item, as_of):
    excerpt = item.get('excerpt') or ''
    if not usable_market_excerpt(excerpt) or not _has_product(item['name'], excerpt) or not _current_fact(item, as_of):
        return ''
    public = item['metric'] == 'public_signal'
    value, unit = item['value'], item['unit']
    if not public:
        if item['metric'] not in DEMAND_METRICS or not item['measured']:
            return ''
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            return ''
        if unit in ('units', 'orders', 'queries', 'mentions', 'contracts') and not float(value).is_integer():
            return ''
    # Old typed rows must pass the corrected number/product association too.
    # A source can supply preceding context, but it cannot substitute a separate claim.
    source = item.get('text') or ''
    saved = clean_text(excerpt).strip(' .!?')
    scopes = [excerpt]
    if source and source != excerpt:
        scopes.append(source)
    for content in scopes:
        for row in _parsed_market_text(item['name'], content, item['country'], item.get('published_at') or ''):
            if (row['metric'] == item['metric'] and row['unit'] == unit
                    and row['country'] == item['country'] and row['period'] == item['period']
                    and (public or (row['value'] is not None and math.isclose(row['value'], value, rel_tol=1e-12)))):
                checked = clean_text(row['excerpt']).strip(' .!?')
                if not GENERAL_ARTICLE.search(row['excerpt']) and (checked in saved or saved in checked):
                    return row['excerpt']
    return ''


def _accepted_facts(store, countries, as_of):
    """One source-of-truth selection for ranking and its source preview."""
    if not countries:
        return []
    placeholders = ','.join('?' for _ in countries)
    observations = store.query('''SELECT o.id AS observation_id,o.product_id,o.metric,o.value,o.unit,
        o.period,o.country,o.measured,o.event_key,o.created_at,p.name,
        COALESCE(l.source_id,o.source_id) AS source_id,
        COALESCE(NULLIF(l.excerpt,''),o.excerpt) AS excerpt,
        COALESCE(NULLIF(l.published_at,''),NULLIF(s.published_at,''),o.published_at) AS published_at,
        s.url,s.title,s.text,s.fetched_at
        FROM market_observations o JOIN products p ON p.id=o.product_id
        LEFT JOIN market_observation_sources l ON l.observation_id=o.id
        JOIN sources s ON s.id=COALESCE(l.source_id,o.source_id)
        WHERE o.country IN (''' + placeholders + ''')
        ORDER BY o.published_at DESC,o.created_at DESC,o.id DESC''', tuple(countries))
    output = []
    for item in observations:
        excerpt = _validated_excerpt(item, as_of)
        if excerpt:
            output.append({**item, 'excerpt': excerpt})
    # Recheck legacy records from their saved text, without changing or deleting history.
    # This also recovers old reprints not yet linked to an existing observation.
    legacy = store.query('''SELECT ms.product_id,p.name,ms.country,ms.created_at,ms.source_id,
        s.url,s.title,s.text,s.published_at,s.fetched_at,ms.description
        FROM market_signals ms JOIN products p ON p.id=ms.product_id
        JOIN sources s ON s.id=ms.source_id
        WHERE ms.country IN (''' + placeholders + ''')
        AND NOT EXISTS(SELECT 1 FROM market_observations o
            LEFT JOIN market_observation_sources l ON l.observation_id=o.id
            WHERE o.product_id=ms.product_id AND (o.source_id=ms.source_id OR l.source_id=ms.source_id))
        ORDER BY ms.created_at DESC,ms.id DESC''', tuple(countries))
    for source in legacy:
        text = source['text'] or source['description'] or source['title'] or ''
        for extracted in _parsed_market_text(source['name'], text, source['country'], source['published_at']):
            item = {**source, **extracted, 'observation_id': None}
            if item['country'] in countries:
                excerpt = _validated_excerpt(item, as_of)
                if excerpt:
                    output.append({**item, 'excerpt': excerpt})
    # Re-evaluate legacy identities without deleting any source link.
    for item in output:
        context = _event_context(item.get('text') or '', item.get('title') or '', item['excerpt'])
        item['event_key'] = event_key(item['name'], item['metric'], item['value'], item['unit'],
                                      item['period'], item['country'], item['excerpt'], context)
    # Aggregate old model-specific observations into the product kind without
    # changing or splitting any existing workspace.
    product_ids = {}
    for row in store.query('SELECT id,name FROM products ORDER BY id'):
        try:
            kind = normalize_product(row['name'])
        except ValueError:
            continue
        existing = product_ids.get(kind)
        if existing is None or (clean_text(row['name']) == kind and not existing[1]):
            product_ids[kind] = (row['id'], clean_text(row['name']) == kind, row['name'])
    for item in output:
        kind = normalize_product(item['name'])
        selected = product_ids[kind]
        item['name'] = selected[2] if selected[1] else kind
        item['product_id'] = selected[0]
    unique = {}
    for item in output:
        unique.setdefault((item['product_id'], item['event_key'], item['url']), item)
    return list(unique.values())


def _public_basis(item):
    excerpt = item['excerpt']
    clauses = re.split(SENTENCE_BOUNDARY + r'|,\s+(?=[а-яёa-z])', excerpt, flags=re.I)
    matching = [part for part in clauses if _has_product(item['name'], part)]
    # Prefer the product's own concrete change over an introductory generic list.
    relevant = next((part for part in matching if re.search(r'\d+(?:[.,]\d+)?\s*(?:%|раз\w*)', part)),
                    matching[0] if matching else excerpt)
    kind = ' '.join(relevant.split())
    if re.search(r'оборудован\w*\s+на\s+\d', excerpt, re.I) and re.search(r'\bзакупк', excerpt, re.I):
        kind = 'Закупка комплекта оборудования'
    elif not GROWTH.search(relevant) and GROWTH.search(excerpt):
        kind = 'Интерес растёт: ' + kind
    if len(kind) > 160:
        kind = kind[:157].rstrip() + '…'
    period = item['period'] or item['published_at'][:10]
    return f"{kind} · {period} · {item['country']}"


def market_evidence(store, product_id, countries, as_of=None):
    """Only sources backing accepted facts for this product, geography and window."""
    as_of = as_of or datetime.now(timezone.utc)
    sources = {}
    for item in _accepted_facts(store, countries, as_of):
        if item['product_id'] != product_id:
            continue
        source = sources.setdefault(item['url'], {key: item[key] for key in
            ('url', 'title', 'text', 'published_at', 'fetched_at')})
        source.setdefault('id', item['source_id'])
        source.setdefault('excerpts', [])
        if item['excerpt'] not in source['excerpts']:
            source['excerpts'].append(item['excerpt'])
    for source in sources.values():
        source['excerpt'] = source['fact'] = '\n'.join(source['excerpts'])
    return sorted(sources.values(), key=lambda item: (item['published_at'], item['fetched_at'], item['url']), reverse=True)

def import_statistics(store, countries):
    """Historical imports are separate statistics, never current demand."""
    if not countries:
        return []
    placeholders = ','.join('?' for _ in countries)
    rows = store.query('''SELECT p.name,o.value,o.unit,o.period,o.country,o.excerpt,
        s.url,s.title,s.text,s.published_at,s.fetched_at,s.id
        FROM market_observations o JOIN products p ON p.id=o.product_id
        JOIN sources s ON s.id=o.source_id
        WHERE o.metric='imports' AND o.country IN (''' + placeholders + ''')
        ORDER BY o.period DESC,p.name,o.country,o.unit''', tuple(countries))
    for row in rows:
        row['fact'] = row['excerpt']
    return rows


def market_ranking(store, countries, limit=10, as_of=None):
    """Compare measured cohorts; otherwise show independent publication signals."""
    as_of = as_of or datetime.now(timezone.utc)
    facts = _accepted_facts(store, countries, as_of)
    by_product, cohorts = defaultdict(list), defaultdict(dict)
    for item in facts:
        by_product[item['product_id']].append(item)
        if item['measured']:
            cohort = (item['metric'], item['unit'], item['period'], item['country'])
            current = cohorts[cohort].get(item['product_id'])
            if current is None or (item['published_at'], item['event_key']) > (current['published_at'], current['event_key']):
                cohorts[cohort][item['product_id']] = item
    order = list(METRICS)
    # Only one comparable cohort can define a common percentage scale.  A
    # single observation cannot establish a comparison between products.
    comparable = [(key, products) for key, products in cohorts.items()
                  if len(products) >= 2 and max(row['value'] for row in products.values()) > 0]
    comparable.sort(key=lambda pair: (-len(pair[1]), order.index(pair[0][0]), pair[0][1:]))
    selected = comparable[0][1] if comparable else {}
    maximum = max((row['value'] for row in selected.values()), default=0)
    result, chosen = [], set()
    def append(item, measured, demand_percent=None):
        pid = item['product_id']
        if pid in chosen:
            return
        evidence = by_product[pid]
        measures = {row['event_key']: row for row in evidence if row['measured']}
        sources = {}
        for row in evidence:
            sources.setdefault(row['url'], row['source_id'])
        if measured:
            basis = measurement_basis(item)
        else:
            # Select the most specific saved fact for the label, without changing rank.
            detail = max((row for row in evidence if not row['measured']), key=lambda row: (
                bool(re.search(r'\d+(?:[.,]\d+)?\s*(?:%|раз\w*)', _public_basis(row))),
                row['published_at'], row['created_at'], row['event_key']))
            basis = _public_basis(detail)
        result.append({'id': pid, 'name': item['name'], 'basis': basis, 'descriptions': basis,
                       'measured': measured, 'measurements': list(measures.values()), 'demand_percent': demand_percent,
                       'count': len(sources), 'source_ids': sorted(sources.values())})
        chosen.add(pid)
    for item in sorted(selected.values(), key=lambda row: (-row['value'], row['name'].casefold(), row['product_id'])):
        if len(result) >= limit:
            break
        append(item, True, 100 * item['value'] / maximum)
    def publication_order(rows):
        # One verbatim event remains one report even when copied to many sites.
        # Keep its first saved source as its origin; repeated crawls and newly
        # discovered reprints cannot multiply the independent publisher count.
        originals = {}
        for row in sorted(rows, key=lambda row: row['source_id']):
            originals.setdefault(row['event_key'], row)
        publishers = {domain_of(row['url']) for row in originals.values()}
        stamps = [_stamp(row.get('published_at') or '') for row in originals.values()]
        newest_day = max((stamp.date().toordinal() for stamp in stamps if stamp), default=0)
        return (-len(publishers), -newest_day, rows[0]['name'].casefold(), rows[0]['product_id'])

    # Publication activity is an explicit proxy, never a measured demand %.
    remaining = sorted(by_product.values(), key=publication_order)
    for rows in remaining:
        if len(result) >= limit:
            break
        item = min(rows, key=lambda row: (not row['measured'], row['event_key']))
        append(item, bool(item['measured']))
    preliminary = any(row['demand_percent'] is None for row in result) or not result
    caption = ('Предварительный рейтинг по публичным признакам спроса: независимые публикации и их свежесть' if preliminary else
               'Измеренные показатели: сравнение внутри одинаковых периодов и единиц')
    ranked_ids = {row['id'] for row in result}
    publication_days = (_stamp(item['published_at'] or '') for item in facts if item['product_id'] in ranked_ids)
    newest = max((stamp.date() for stamp in publication_days if stamp), default=None)
    return {'rows': result, 'caption': caption, 'preliminary': preliminary,
            'latest_publication': newest.isoformat() if newest else None}
