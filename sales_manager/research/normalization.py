"""Deterministic product vocabulary, query seeds and conservative identities.

The vocabulary generates queries; entries are never treated as market results.
"""
import re
import unicodedata
from functools import lru_cache

COUNTRIES = ['Россия', 'Беларусь', 'Казахстан', 'Армения', 'Азербайджан',
             'Кыргызстан', 'Молдова', 'Таджикистан', 'Туркменистан', 'Узбекистан']
COUNTRY_CODES = dict(zip(COUNTRIES, ['RU', 'BY', 'KZ', 'AM', 'AZ', 'KG', 'MD', 'TJ', 'TM', 'UZ']))
COUNTRY_QIDS = dict(zip(COUNTRIES, ['Q159', 'Q184', 'Q232', 'Q399', 'Q227', 'Q813', 'Q217', 'Q863', 'Q874', 'Q265']))

CONSUMER_ELECTRONICS = 'потребительская электроника и гаджеты'
IT_EQUIPMENT = 'серверное, сетевое и IT-оборудование'

# Broad ordinary civilian categories, including B2B goods and consumables.
CATEGORIES = {
    CONSUMER_ELECTRONICS: ['смартфон', 'ноутбук', 'планшет', 'монитор', 'принтер', 'компьютер', 'телевизор', 'наушники', 'умные часы'],
    IT_EQUIPMENT: ['сервер', 'система хранения данных', 'сетевой коммутатор', 'контроллер'],
    'бытовая техника': ['холодильник', 'стиральная машина', 'пылесос', 'кофемашина', 'кондиционер'],
    'электрика и освещение': ['светильник', 'кабель', 'розетка', 'выключатель', 'электросчетчик', 'прибор учета'],
    'строительные материалы': ['цемент', 'кирпич', 'гипсокартон', 'утеплитель', 'сухая смесь'],
    'отделка и ремонт': ['краска', 'обои', 'плитка', 'ламинат', 'крепеж'],
    'сантехника': ['труба', 'смеситель', 'насос', 'радиатор'],
    'инструменты': ['шуруповерт', 'дрель', 'сварочный аппарат', 'измерительный прибор'],
    'металлоизделия': ['металлопрокат', 'подшипник', 'болт', 'пружина'],
    'промышленное оборудование': ['компрессор', 'электродвигатель', 'станок', 'конвейер'],
    'автотовары': ['шина', 'автозапчасть', 'автофильтр', 'автомасло', 'автомобиль с пробегом'],
    'транспорт и склад': ['велосипед', 'самокат', 'тележка', 'паллет', 'стеллаж'],
    'мебель': ['офисное кресло', 'стол', 'стул', 'диван', 'шкаф', 'матрас'],
    'дом и кухня': ['посуда', 'кухонная утварь', 'постельное белье', 'полотенце'],
    'одежда и текстиль': ['одежда', 'обувь', 'ткань', 'спецодежда'],
    'детские товары': ['игрушка', 'коляска', 'детская мебель', 'школьный рюкзак'],
    'спорт и отдых': ['спортивный инвентарь', 'тренажер', 'палатка', 'туристическое снаряжение', 'товары для сноубординга'],
    'канцелярия и офис': ['офисная бумага', 'канцелярские товары', 'картридж'],
    'упаковка': ['картонная коробка', 'упаковочная пленка', 'бумажный пакет', 'одноразовая посуда'],
    'клининг и гигиена': ['моющее средство', 'мыло', 'туалетная бумага', 'бумажное полотенце'],
    'косметика и уход': ['шампунь', 'крем', 'зубная паста', 'косметика', 'парфюмерия', 'средства для макияжа', 'средства для ухода за кожей', 'косметические наборы'],
    'украшения': ['ювелирные украшения', 'наручные часы'],
    'продукты питания': ['кофе', 'чай', 'крупа', 'мука', 'растительное масло', 'консервы', 'молоко', 'сыр', 'рыба', 'мясо', 'овощи', 'фрукты'],
    'сельское хозяйство': ['семена', 'садовый инструмент', 'теплица', 'система полива'],
    'зоотовары': ['корм для животных', 'наполнитель для животных', 'зоотовары'],
    'торговое оборудование': ['кассовый аппарат', 'витрина', 'холодильная витрина'],
    'общественное питание': ['кухонное оборудование', 'профессиональная посуда'],
    'полиграфия и творчество': ['книга', 'картон', 'краски для рисования', 'бумага для печати', 'виниловая пластинка'],
    'энергосбережение': ['солнечная панель', 'теплоизоляция', 'термостат'],
}
PRODUCTS = tuple(dict.fromkeys(p for group in CATEGORIES.values() for p in group))
ALIASES = {
    'виниловая пластинка': [r'винилов\w*\s+пластин\w*'],
    'офисное кресло': [r'офисн\w*\s+(?:кресл|кресел|стул|стуль)\w*', r'компьютерн\w*\s+(?:кресл|кресел)\w*'],
    'автофильтр': [r'автомобильн\w*\s+фильтр\w*', r'автофильтр\w*'],
    'бумажное полотенце': [r'бумажн\w*\s+полотен\w*'],
    'картонная коробка': [r'(?:упаковочн|картонн)\w*\s+короб\w*'],
    'смартфон': [r'смартфон\w*', r'smartphones?', r'iphone\s*\d*\w*', r'айфон\w*',
                 r'samsung\s+galaxy(?!\s+(?:tab|watch|buds|book|ring)\b)\w*',
                 r'redmi\s+(?:note(?:\s*\d+[a-z]?)?|[ak]?\d+[a-z]?)',
                 r'poco(?:phone)?\s+[cfmx]\d+[a-z]?', r'мобильн\w*\s+телефон\w*'],
    'ноутбук': [r'ноутбук\w*', r'лаптоп(?:а|у|ом|е|ы|ов|ам|ами|ах)?', r'laptops?',
                r'macbook\w*', r'макбук\w*', r'redmi\s*book'],
    'планшет': [r'планшет\w*', r'ipad\w*', r'айпад\w*', r'(?:samsung\s+)?galaxy\s+tab\w*',
                r'(?:redmi|xiaomi|poco)\s+pad', r'\b[a-z][a-z0-9-]{1,24}\s+(?:pad|tab)\s+(?:pro\b|[a-z]?\d{1,3}(?!\w|[%.,]\d?))'],
    'наушники': [r'наушник\w*', r'(?:samsung\s+)?galaxy\s+buds\w*', r'airpods\w*', r'redmi\s+buds',
                 r'\b[a-z][a-z0-9-]{1,24}\s+buds\s+(?:air\b|pro\b|[a-z]?\d{1,3}(?!\w|[%.,]\d?))'],
    'пылесос': [r'(?:робот[ -])?пылесос\w*'],
    'шина': [r'(?:авто)?шин[аы]\w*', r'автомобильн\w*\s+шин\w*'],
    'моющее средство': [r'моющ\w*\s+средств\w*', r'бытов\w*\s+хими\w*'],
    'кофе': [r'кофе'],
    'корм для животных': [r'корм(?:а|ов|у|ом|е|ы|ами|ах)?\s+для\s+животн\w*'],
    'канцелярские товары': [r'канцелярск\w*\s+товар\w*', r'канцтовар\w*'],
    'спецодежда': [r'спецодежд\w*', r'рабоч\w*\s+одежд\w*'],
    'прибор учета': [r'прибор\w*\s+уч[её]т\w*'],
    'электросчетчик': [r'(?:электро|электрическ\w*\s+)сч[её]тчик\w*'],
    'ювелирные украшения': [r'ювелирн\w*\s+(?:украшен|издел)\w*', r'украшени[яйею]\w*'],
    'умные часы': [r'смарт[ -]?час(?:ы|ов|ам|ами|ах)', r'smart[ -]?watch(?:es)?',
                  r'(?:samsung\s+)?galaxy\s+watch\w*',
                  r'apple\s+watch\w*', r'redmi\s+watch',
                  r'\b[a-z][a-z0-9-]{1,24}\s+watch\s+(?:gt\s*)?\d{1,2}(?!\w|[%.,]\d?)'],
    'обувь': [r'обув[ьи]\w*'],
    'средства для макияжа': [r'средств\w*\s+для\s+макияж\w*'],
    'средства для ухода за кожей': [r'(?:товар|средств)\w*\s+для\s+уход\w*\s+за\s+кож\w*'],
    'автомобиль с пробегом': [r'(?:автомобил|машин)\w*\s+с\s+пробег\w*'],
    'телевизор': [r'телевизор\w*', r'тв[- ](?:панел\w*|приемник\w*)',
                  r'(?:lcd|led|oled|qled|smart)[ -]тв'],
    'система хранения данных': [r'систем\w*\s+хранени\w*\s+данн\w*', r'схд'],
    'сетевой коммутатор': [r'(?:сетев\w*\s+)?коммутатор\w*'],
    'сервер': [r'(?:poweredge|proliant|thinksystem)\s+[a-z]{1,3}\d{2,4}\w*'],
}
UNSAFE = re.compile(r'\b(?:алкогол\w*|табак\w*|сигарет\w*|вейп\w*|оруж\w*|боеприпас\w*|наркот\w*|взрывчат\w*|пестицид\w*|лекарств\w*)\b', re.I)

# Explicit inflections for short stems and words with irregular stem changes.
SHORT_PRODUCT_FORMS = {
    'стол': r'стол(?:а|у|ом|е|ы|ов|ам|ами|ах)?',
    'стул': r'(?:стул(?:а|у|ом|е)?|стуль(?:я|ев|ям|ями|ях))',
    'шкаф': r'шкаф(?:а|у|ом|е|ы|ов|ам|ами|ах)?',
    'чай': r'ча(?:й|я|ю|ем|е|и|ев|ям|ями|ях)',
    'сыр': r'сыр(?:а|у|ом|е|ы|ов|ам|ами|ах)?',
    'мыло': r'мыл(?:о|а|у|ом|е)',
    'крем': r'крем(?:а|у|ом|е|ы|ов|ам|ами|ах)?',
    'мясо': r'мяс(?:о|а|у|ом|е)',
    'рыба': r'рыб(?:а|ы|е|у|ой|ою|ам|ами|ах)?',
    'крупа': r'круп(?:а|ы|е|у|ой|ою|ам|ами|ах)?',
    'мука': r'мук(?:а|и|е|у|ой|ою)?',
    'овощи': r'овощ(?:и|ей|ам|ами|ах)',
    'книга': r'книг(?:а|и|е|у|ой|ою|ам|ами|ах)?',
    'ткань': r'ткан(?:ь|и|ью|ей|ям|ями|ях)',
    'обои': r'обо(?:и|ев|ям|ями|ях)',
    'труба': r'труб(?:а|ы|е|у|ой|ою|ам|ами|ах)?',
    'дрель': r'дрел(?:ь|и|ью|ей|ям|ями|ях)',
    'насос': r'насос(?:а|у|ом|е|ы|ов|ам|ами|ах)?',
    'диван': r'диван(?:а|у|ом|е|ы|ов|ам|ами|ах)?',
    'болт': r'болт(?:а|у|ом|е|ы|ов|ам|ами|ах)?',
    'шина': r'шин(?:а|ы|е|у|ой|ою|ам|ами|ах)?',
    'пакет': r'пакет(?:а|у|ом|е|ы|ов|ам|ами|ах)?',
    'масло': r'(?:масл(?:о|а|у|ом|е|ам|ами|ах)|масел)',
    'белье': r'бель(?:е|я|ю|ем)',
    'полотенце': r'(?:полотенец|полотенц(?:е|а|у|ем|ам|ами|ах))',
    'корм': r'корм(?:а|у|ом|е|ы|ов|ам|ами|ах)?',
    'смесь': r'смес(?:ь|и|ью|ей|ям|ями|ях)',
    'часы': r'час(?:ы|ов|ам|ами|ах)',
    'умные': r'умн(?:ые|ых|ым|ыми)',
    'офисная': r'офисн(?:ая|ой|ою|ую|ые|ых|ым|ыми)',
    'бумага': r'бумаг(?:а|и|е|у|ой|ою|ам|ами|ах)?',
    'машина': r'(?:машин|машин(?:а|ы|е|у|ой|ою|ам|ами|ах))',
    'сухая': r'сух(?:ая|ой|ую|ие|их|им|ими|ое|ого|ому|ом)',
    'зубная': r'зубн(?:ая|ой|ою|ую|ые|ых|ым|ыми)',
    'паста': r'паст(?:а|ы|е|у|ой|ою|ам|ами|ах)?',
    'станок': r'(?:станок|станк(?:а|у|ом|е|и|ов|ам|ами|ах))',
    'игрушка': r'(?:игрушек|игрушк(?:а|и|е|у|ой|ою|ам|ами|ах))',
    'коляска': r'(?:колясок|коляск(?:а|и|е|у|ой|ою|ам|ами|ах))',
    'палатка': r'(?:палаток|палатк(?:а|и|е|у|ой|ою|ам|ами|ах))',
    'розетка': r'(?:розеток|розетк(?:а|и|е|у|ой|ою|ам|ами|ах))',
    'тележка': r'(?:тележек|тележк(?:а|и|е|у|ой|ою|ам|ами|ах))',
    'упаковочная': r'упаковочн(?:ая|ой|ою|ую|ые|ых|ым|ыми)',
    'пленка': r'(?:пл[её]нок|пл[её]нк(?:а|и|е|у|ой|ою|ам|ами|ах))',
    'краски': r'(?:красок|краск(?:а|и|е|у|ой|ою|ам|ами|ах))',
    'семена': r'(?:семя|семян|семен(?:а|и|ем|ам|ами|ах))',
}

def clean_text(value):
    return ' '.join(unicodedata.normalize('NFKC', str(value)).casefold().replace('ё', 'е').split())

def normalize_product(name):
    text = clean_text(name).strip(' .,;:!?')
    if not text or len(text) > 160:
        raise ValueError('Введите название товара до 160 символов.')
    if UNSAFE.search(text):
        raise ValueError('Эта категория не поддерживается: выберите обычный гражданский товар.')
    # Specific aliases (e.g. бумажные полотенца) precede broad nouns.
    for canonical, expressions in ALIASES.items():
        if canonical != 'смартфон' and any(re.search(r'(?<!\w)' + p + r'(?!\w)', text) for p in expressions):
            return canonical
    # A phone brand is weaker evidence than an explicitly named product type.
    for product in sorted(PRODUCTS, key=len, reverse=True):
        if product != 'смартфон' and matches_product(product, text):
            return product
    for canonical, expressions in ALIASES.items():
        if any(re.search(r'(?<!\w)' + p + r'(?!\w)', text) for p in expressions):
            return canonical
    for product in sorted(PRODUCTS, key=len, reverse=True):
        if matches_product(product, text):
            return product
    return text

def branch_identity(name):
    """Keep explicit electronics models in their own workspaces."""
    kind = normalize_product(name)
    text = clean_text(name).strip(' .,;:!?')
    if electronics_group(kind):
        model_marker = re.search(r'\b(?:iphone|айфон\w*|ipad|айпад\w*|airpods|macbook|макбук\w*|'
                                 r'redmi\s*book|[a-z][a-z0-9-]*\s+(?:watch|buds|pad|tab|galaxy))\b', text)
        brand_phone = kind == 'смартфон' and re.search(r'\b(?:samsung|xiaomi|redmi|poco)\b', text)
        model_number = re.search(r'\b[a-z]*\d+[a-z0-9-]*\b', text)
        if model_marker or brand_phone or model_number:
            return 'branch:' + text
    return kind


def electronics_group(kind):
    return next((group for group in (CONSUMER_ELECTRONICS, IT_EQUIPMENT)
                 if kind in CATEGORIES[group]), '')

@lru_cache(maxsize=4096)
def product_phrase(product):
    words = re.findall(r'[a-zа-я0-9]+', product)
    if not words:
        return re.compile(r'(?!)')
    # Strip only a possible inflection, never a final consonant:
    # картон -> картона, but never карточка / картофель.
    def expression(word):
        if word in SHORT_PRODUCT_FORMS:
            return SHORT_PRODUCT_FORMS[word]
        if len(word) < 6:
            return re.escape(word)
        stem = word[:-1] if word[-1] in 'аоеыуяиьй' else word
        return re.escape(stem) + r'[а-яa-z]*'
    phrase = r'\s+'.join(expression(word) for word in words)
    return re.compile(r'(?<!\w)' + phrase + r'(?!\w)', re.I)


def matches_product(product, text):
    return bool(product_phrase(product).search(clean_text(text)))

def product_candidates(text):
    value = clean_text(text)
    found = {p for p in PRODUCTS if matches_product(p, value)}
    for canonical, expressions in ALIASES.items():
        if any(re.search(r'(?<!\w)' + pattern + r'(?!\w)', value) for pattern in expressions):
            found.add(canonical)
    if 'бумажное полотенце' in found:
        remainder = re.sub(ALIASES['бумажное полотенце'][0], '', value)
        if not matches_product('полотенце', remainder):
            found.discard('полотенце')
    if found.intersection({'средства для макияжа', 'средства для ухода за кожей', 'косметические наборы'}):
        found.discard('косметика')
    return sorted(found)

def company_identity(name):
    return re.sub(r'[^a-zа-я0-9]', '', clean_text(name))


def organization_key(name, domain='', parent=''):
    """Stable organization identity shared by headquarters and confirmed branches."""
    root = company_identity(parent or name)
    host = re.sub(r'[^a-z0-9.-]', '', clean_text(domain))
    return root or host


def company_identity_key(name, *, domain='', url='', address='', branch_name='', source_url='',
                         parent='', country='', legal_id=''):
    """Unique card key: structured id, or confirmed name/address/site/country combination.

    Legal id, when already known, only separates cards that share a site; it is
    never used alone to match companies across different official pages.
    """
    source = clean_text(source_url)
    if source.startswith('https://www.openstreetmap.org/') or source.startswith('https://www.wikidata.org/wiki/q'):
        return 'src:' + source
    org = organization_key(name, domain, parent)
    place = clean_text(address) or clean_text(branch_name)
    site = clean_text(url) or clean_text(domain)
    market = clean_text(country)
    entity = re.sub(r'\D', '', str(legal_id or ''))
    marker = '|'.join(part for part in (org, place, site, market, entity) if part)
    return ('id:' + marker) if marker else ''

def buyer_segments(product):
    p = normalize_product(product)
    if p == 'офисное кресло':
        return ['магазин офисной мебели', 'оптовый поставщик офисной мебели',
                'компания оснащение офисов', 'поставщик оборудования рабочих мест']
    if p in CATEGORIES[IT_EQUIPMENT]:
        return ['поставщик IT-оборудования', 'системный интегратор', 'закупка IT-оборудования',
                'дата-центр серверное оборудование']
    if p in CATEGORIES[CONSUMER_ELECTRONICS]:
        specialists = {
            'умные часы': 'магазин носимой электроники спортивных гаджетов',
            'наушники': 'магазин аудиотехники беспроводных аксессуаров',
            'планшет': 'магазин планшетов компьютерной техники',
            'смартфон': 'магазин мобильной техники',
        }
        return [specialists.get(p, 'магазин электроники'), 'дистрибьютор электроники',
                'оптовая закупка для розничной сети']
    for category in ('строительные материалы', 'отделка и ремонт', 'сантехника', 'инструменты', 'электрика и освещение'):
        if p in CATEGORIES[category]:
            return ['магазин стройматериалов', 'оптовый поставщик строительных материалов',
                    'строительная компания', 'подрядчик ремонт']
    if p in ('кофе', 'чай'):
        return ['кафе кофейня', 'ресторан гостиница', 'дистрибьютор напитков',
                'закупка снабжение офиса напитками']
    if p in CATEGORIES['продукты питания'] or p in CATEGORIES['общественное питание']:
        return ['ресторан', 'кафе', 'гостиница', 'продуктовый магазин']
    if p in CATEGORIES['клининг и гигиена']:
        return ['поставщик профессиональной гигиены', 'клининговая компания',
                'гостиница', 'управляющая компания']
    if p in CATEGORIES['автотовары']:
        return ['магазин автозапчастей', 'оптовый дистрибьютор автозапчастей',
                'автосервис', 'транспортная компания']
    if p in CATEGORIES['сельское хозяйство']:
        return ['фермерское хозяйство', 'садовый центр', 'агропредприятие']
    if p in CATEGORIES['упаковка']:
        return ['поставщик упаковочных материалов', 'оптовый дистрибьютор упаковки',
                'производитель продукции', 'интернет магазин отправка заказов', 'складской комплекс']
    if p in CATEGORIES['мебель']:
        return ['магазин мебели', 'оптовый поставщик мебели', 'компания оснащение помещений', 'гостиница']
    if p in CATEGORIES['промышленное оборудование'] or p in CATEGORIES['металлоизделия']:
        return ['дистрибьютор промышленного оборудования', 'производственное предприятие',
                'сервис промышленного оборудования']
    return ['закупка тендер заказчик', 'использование применение предприятие',
            'оптовый дистрибьютор ассортимент']

def osm_filters(product):
    p = normalize_product(product)
    if p in CATEGORIES[IT_EQUIPMENT]:
        return [('telecom', 'data_center')]
    if p in CATEGORIES[CONSUMER_ELECTRONICS]:
        filters = [('shop', 'electronics')]
        if p in ('смартфон', 'планшет'):
            filters.append(('shop', 'mobile_phone'))
        if p in ('планшет', 'ноутбук', 'компьютер', 'монитор', 'принтер'):
            filters.append(('shop', 'computer'))
        return filters
    if p in CATEGORIES['продукты питания'] or p in CATEGORIES['общественное питание']:
        return [('amenity', 'restaurant'), ('amenity', 'cafe'), ('tourism', 'hotel')]
    if p in CATEGORIES['клининг и гигиена']:
        return [('tourism', 'hotel'), ('office', 'cleaning')]
    if p in CATEGORIES['автотовары']:
        return [('shop', 'car_repair'), ('shop', 'car_parts')]
    if any(p in CATEGORIES[c] for c in ('строительные материалы', 'отделка и ремонт', 'сантехника', 'инструменты')):
        return [('shop', 'doityourself'), ('shop', 'hardware'), ('craft', 'builder')]
    if p in CATEGORIES['мебель']:
        return [('shop', 'furniture'), ('tourism', 'hotel')]
    if p in CATEGORIES['зоотовары']:
        return [('shop', 'pet')]
    return []
