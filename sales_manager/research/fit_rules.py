"""Evidence-backed activity/product rules, independent from network and language models."""
import re

from .extraction import domain_of
from .normalization import (CATEGORIES, CONSUMER_ELECTRONICS, COUNTRY_CODES, IT_EQUIPMENT,
                            clean_text, matches_product, normalize_product, product_candidates)

RULE_VERSION = 'activity-fit-v9'

# A label is recorded only with a matching saved passage or a structured OSM tag.
ACTIVITIES = (
    ('office_furniture', 'офисная мебель', r'офисн\w*\s+мебел\w*|мебел\w*\s+для\s+офис\w*'),
    ('office_outfit', 'оснащение офисов', r'оснащ\w*\s+офис\w*|оборудован\w*\s+рабоч\w*\s+мест\w*|комплексн\w*\s+оснащ\w*\s+рабоч\w*'),
    ('autoparts', 'автозапчасти', r'автозапчаст\w*|автомобильн\w*\s+запчаст\w*|auto\s*parts'),
    ('car_service', 'обслуживание автомобилей', r'автосервис\w*|ремонт\w*\s+автомобил\w*|станци\w*\s+техническ\w*\s+обслуживан\w*'),
    ('fleet', 'эксплуатация транспорта', r'транспортн\w*\s+компани\w*|собственн\w*\s+автопарк\w*|грузоперевоз\w*'),
    ('cleaning', 'профессиональная уборка', r'клинингов\w*\s+(?:компани|услуг|организац)\w*|профессиональн\w*\s+уборк\w*|услуг\w*\s+(?:по\s+)?уборк\w*|уборк\w*\s+(?:офис|помещен)\w*'),
    ('hospitality', 'гостиничные услуги', r'гостиниц\w*|отел\w*|\bhotel\b'),
    ('food_service', 'общественное питание', r'\bкафе\b|кофейн\w*|ресторан\w*|общественн\w*\s+питан\w*|\bcafe\b|\brestaurant\b'),
    ('food_retail', 'продажа продуктов питания', r'продуктов\w*\s+магазин\w*|магазин\w*\s+продукт\w*|продаж\w*\s+продукт\w*\s+питан\w*'),
    ('office_beverages', 'обеспечение сотрудников напитками',
     r'(?:обеспеч\w*|снабж\w*|предоставля\w*|разда\w*).{0,40}(?:сотрудник|персонал|работник)\w*.{0,40}'
     r'(?:кофе|ча[йяею]|напитк)\w*|(?:кофе|ча[йяею]|напитк)\w*.{0,40}'
     r'(?:для\s+(?:сотрудник|персонал|работник)\w*|сотрудник|персонал|работник)\w*|'
     r'кофе[- ]?(?:пойнт|брейк|зона)|чайная\s+комната|меню\s+(?:кофе|ча[йяею]|напитк)\w*'),
    ('packaging', 'поставки упаковки', r'(?:постав\w*|производ\w*|каталог\w*|продаж\w*).{0,65}упаков\w*|упаковочн\w*\s+материал\w*'),
    ('shipping', 'комплектация и отправка товаров', r'отправ\w*\s+заказ\w*|упаков\w*\s+(?:сво\w*\s+)?продукци\w*|фулфилмент\w*|комплектаци\w*\s+заказ\w*'),
    ('manufacturer', 'производство продукции', r'(?:производител\w*|производим|производств\w*).{0,65}(?:продукц|товар|издел|мебел|одежд|продукт)\w*|(?:наш\w*|собственн\w*)\s+(?:завод|фабрик)\w*'),
    ('electronics', 'электроника', r'магазин\w*\s+электроник\w*|дистрибьютор\w*\s+электроник\w*|(?:продаж\w*|прода[её](?:т|м|ют)).{0,35}(?:электроник|компьютер)\w*|сервисн\w*\s+центр\w*[^.!?;\n]{0,65}(?:электроник|компьютер|ноутбук|смартфон|телефон|планшет)\w*'),
    ('it_supply', 'поставки IT-оборудования',
     r'(?:поставщик|дистрибьютор|интегратор)\w*.{0,40}(?:IT|ИТ|сервер|сетев)\w*|'
     r'(?:продаж\w*|поставля\w*|каталог\w*).{0,40}(?:серверн\w*\s+оборудован|систем\w*\s+хранени|сетев\w*\s+коммутатор)\w*'),
    ('server_ops', 'эксплуатация серверного оборудования',
     r'(?:дата[- ]?центр|центр\w*\s+обработк\w*\s+данн\w*|хостинг\w*)|'
     r'(?:эксплуатир\w*|развёртыва\w*|развертыва\w*|обслужива\w*|используем).{0,40}сервер\w*|'
     r'серверн\w*\s+(?:парк|инфраструктур|площадк)\w*'),
    ('construction', 'строительство и ремонт', r'строительн\w*\s+компани\w*|подрядчик\w*|услуг\w*.{0,35}ремонт\w*\s+помещен\w*|магазин\w*\s+стройматериал\w*'),
    ('furniture', 'продажа мебели', r'магазин\w*\s+мебел\w*|поставщик\w*\s+мебел\w*|каталог\w*\s+мебел\w*'),
    ('agriculture', 'сельское хозяйство', r'фермерск\w*\s+хозяйств\w*|агропредприят\w*|садов\w*\s+центр\w*'),
    ('pet_retail', 'зоотовары', r'зоомагазин\w*|магазин\w*\s+зоотовар\w*'),
)

OSM_ACTIVITIES = {
    ('shop', 'furniture'): 'furniture', ('shop', 'car_parts'): 'autoparts',
    ('shop', 'car_repair'): 'car_service', ('shop', 'electronics'): 'electronics',
    ('shop', 'computer'): 'electronics', ('shop', 'mobile_phone'): 'electronics',
    ('amenity', 'cafe'): 'food_service', ('amenity', 'restaurant'): 'food_service',
    ('tourism', 'hotel'): 'hospitality', ('office', 'cleaning'): 'cleaning',
    ('shop', 'hardware'): 'construction', ('shop', 'doityourself'): 'construction',
    ('craft', 'builder'): 'construction', ('shop', 'pet'): 'pet_retail',
    ('amenity', 'internet_cafe'): 'server_ops',
}

NEARBY_PLACE = (r'рядом(?:\s+с(?:\s+нами)?)?|поблизости|по\s+соседству|через\s+дорогу|'
                r'(?:вход|проход|дойти|пройти|доехать|проехать)\s+через|'
                r'в\s+соседн\w*\s+(?:здани|дом|помещени)\w*|напротив|мимо')
LOCATION_VERB = r'(?:наход|располож|работа|откр|действу|видн)\w*|есть'
OWN_SUBJECT = (r'(?:наш\w*|собственн\w*|у\s+нас|'
               r'мы\s*(?:[—–-]|являемся|владеем|открыли|открываем|управляем))')
COMMERCE_VERB = r'(?:закупаем|покупаем|используем|прода[её]м|поставля[её]м|принимаем)'
COMMERCE_CLAUSE = (rf',\s*(?=(?:а(?!\s+также\b)|но(?!\s+(?:и|также)\b)|зато|однако)\b|'
                   rf'(?:(?:и|мы)\s+)*(?:не\s+)?{COMMERCE_VERB}\b)'
                   rf'|\s+(?=и\s+(?:мы\s+)?(?:не\s+)?{COMMERCE_VERB}\b)')
SENTENCE_BREAK = re.compile(r'(?<=[.!?;])\s+|[\r\n]+')
CLAUSE_BOUNDARY = re.compile(SENTENCE_BREAK.pattern + '|' + COMMERCE_CLAUSE, re.I)
CONTEXT_BOUNDARY = re.compile(r'[.!?;\r\n]|' + COMMERCE_CLAUSE, re.I)
LOCATION_STATEMENT = (r'\b(?:(?:зарегистрир\w*|находимся|находится|располож\w*|открыт\w*|работаем)'
                      r'\s+(?:в\s+)?|(?:страна|адрес|местонахождени\w*)\s*[:—–-]\s*)')
COUNTRY_WORDS = {'Россия': r'росси(?:я|ю|и|ей|ею)|russia|рф',
                 'Беларусь': r'беларус\w*|belarus', 'Казахстан': r'казахстан\w*|kazakhstan',
                 'Кыргызстан': r'кыргыз\w*|киргиз\w*|kyrgyz\w*', 'Молдова': r'молдов\w*|moldova'}


def _landmark_activity(before, matched, after):
    """Keep an owned activity with an address, but exclude businesses used as landmarks."""
    before = clean_text(before)
    if re.search(rf'\b{OWN_SUBJECT}\s*$', before):
        return False
    if re.search(rf'\b(?:{NEARBY_PLACE})(?:\s+(?:{LOCATION_VERB}))?[\s:—–-]*$', before):
        return True
    if re.match(rf'(?:(?:{LOCATION_VERB})\s+)?(?:{NEARBY_PLACE})\b', clean_text(after)):
        return True
    # A multi-sentence citation cannot borrow ownership from an earlier claim.
    for clause in CLAUSE_BOUNDARY.split(matched):
        statement = clean_text(clause)
        if (re.search(rf'\b(?:{NEARBY_PLACE})\b', statement)
                and not re.match(rf'(?:в\s+)?{OWN_SUBJECT}(?:\s|$)', statement)):
            return True
    return False


def _denied_assortment(sentence):
    """A denial is evidence to review, not an affirmative assortment claim."""
    value = clean_text(sentence)
    return bool(re.search(
        r'\bне\s+(?:продаем|продает|продают|поставляем|поставляет|реализу\w*|предлага\w*|'
        r'занима\w*\s+(?:оптов\w*\s+|розничн\w*\s+)?(?:продаж\w*|торговл\w*))|'
        r'\b(?:продаж\w*|поставк\w*|реализаци\w*|торговл\w*)[^.!?;]{0,100}\bне\s+'
        r'(?:осуществля\w*|производ\w*|вед[её]\w*)|'
        r'\bнет\b[^.!?;]{0,80}\bассортимент\w*|'
        r'\bассортимент\w*[^.!?;]{0,80}\bнет\b|'
        r'\bотсутств\w*[^.!?;]{0,80}\b(?:ассортимент\w*|продаж\w*|каталог\w*)|'
        r'\bне\s+представлен\w*[^.!?;]{0,80}\bассортимент\w*', value))


def _own_activity(text, start, end, code=''):
    """Exclude explicit reporting/client examples rather than treating them as trade."""
    left = max(0, start - 150)
    separators = [separator for separator in CONTEXT_BOUNDARY.finditer(text, left, min(len(text), start + 30))
                  if separator.end() <= start]
    if separators:
        left = separators[-1].end()
    matched = text[start:end]
    right = end if matched.rstrip().endswith(('.', '!', '?', ';')) else min(len(text), end + 120)
    separator = CONTEXT_BOUNDARY.search(text, end, right)
    if separator:
        right = separator.start()
    before = text[left:start]
    after = text[end:right]
    fragment = clean_text(before + matched + after)
    if re.search(r'\bнаш\w*\s+(?:клиент|партнер)\w*|'
                 r'\b(?:клиент|партнер)\w*\s*[:—–-]|'
                 r'\b(?:клиент|партнер)\w*\s+(?:(?:компани|организаци)\w*\s*[:—–-]|'
                 r'(?:владе|закупа|использу|прода|поставля|откр|работа|явля|сообщ|рассказ|заяви)\w*)|'
                 r'\b(?:кейс|портфолио)\b|'
                 r'\b(?:в|из|по)\s+(?:его|ее|их)\s+(?:каталог|магазин|ассортимент|производств)\w*|'
                 r'\b(?:вчера|недавно)\s+открыл\w*\s+(?!наш\w*\b)', fragment):
        return False
    if _landmark_activity(before, matched, after):
        return False
    # Client lists are often separated from their heading by a line break.
    line_start = text.rfind('\n', 0, start) + 1
    preceding = text[:line_start].rstrip(' \t\r\n:—–-')
    heading = preceding.rsplit('\n', 1)[-1] if preceding else ''
    if (re.fullmatch(r'(?:наш\w*\s+)?(?:клиент|партнер)\w*', clean_text(heading))
            and not re.match(r'(?:мы|наш\w*)\b', fragment)):
        return False
    if code == 'electronics' and _denied_assortment(fragment):
        return False
    if re.search(r'разраб\w*\s+(?:сайт|программ|crm)|созда[её]м\s+сайт|создани\w*\s+сайт|'
                 r'продвижени\w*\s+сайт|публикуем\s+новост|новости\s+рынка|обзор\s+рынка|'
                 r'новостн\w*\s+портал|обучени\w*\s+персонал|наши\s+клиенты\s*[—–:-]', fragment):
        return False
    before_clean = clean_text(before)
    if (not re.search(r'\bне\s+только\s*$', before_clean, re.I)
            and re.search(r'\b(?:не|нет|без)(?:\s+(?:(?:являемся|является|занимаемся|предлагаем|оказываем|работаем\s+как)\s*)?(?:[а-яё]+\s*){0,2})?$', before_clean, re.I)):
        return False
    if code in ('food_service', 'hospitality', 'cleaning', 'manufacturer', 'agriculture'):
        if re.search(r'(?:мебел\w*|оборудован\w*|сайт\w*|crm|программ\w*|посуд\w*|реклам\w*).{0,70}\bдля\b', clean_text(before)):
            return False
        if re.search(r'наши\s+(?:клиенты|партнеры)|портфолио|реализованные\s+проекты', clean_text(before)):
            return False
    return True


def _page_clauses(pages):
    for page in pages:
        text = page.get('text', '')
        start = 0
        for separator in CLAUSE_BOUNDARY.finditer(text):
            yield page, start, separator.start()
            start = separator.end()
        yield page, start, len(text)


def _sentence_quote(text, start, end):
    left = max(0, start - 260)
    for separator in SENTENCE_BREAK.finditer(text, left, start):
        left = separator.end()
    right = min(len(text), left + 260)
    separator = SENTENCE_BREAK.search(text, end, right)
    return text[left:separator.start() if separator else right].strip()


def _company_country(pages, metadata, country):
    if metadata.get('entity_id') or (metadata.get('provider') == 'overpass' and metadata.get('source_url')):
        return country
    candidates = {item for item in (country, *metadata.get('company_countries', ())) if item in COUNTRY_CODES}
    text = clean_text(' '.join(page.get('text', '') for page in pages))
    terms = {item: r'\b(?:' + COUNTRY_WORDS.get(item, re.escape(item.casefold()[:-2]) + r'\w*') + r')\b'
             for item in candidates}
    explicit = [item for item, pattern in terms.items() if re.search(LOCATION_STATEMENT + pattern, text)]
    if explicit:
        return explicit[0] if len(explicit) == 1 else ''
    tld = [item for item in candidates if any(domain_of(page.get('url', '')).endswith('.' + COUNTRY_CODES[item].lower())
                                              for page in pages)]
    if tld:
        return tld[0] if len(tld) == 1 else ''
    mentioned = [item for item, pattern in terms.items() if re.search(pattern, text)]
    return mentioned[0] if len(mentioned) == 1 else ''


def activity_profile(pages, metadata, country):
    facts = []
    for code, label, pattern in ACTIVITIES:
        for page in pages:
            text = page.get('text', '')
            # A page can mention a client's activity first and its own later.
            # Evaluate every occurrence against its own surrounding sentence.
            for original in re.finditer(pattern, text.replace('ё', 'е'), re.I):
                if not _own_activity(text, original.start(), original.end(), code):
                    continue
                quote = text[max(0, original.start() - 65):original.end() + 100].strip()
                facts.append({'code': code, 'label': label, 'quote': quote[:350],
                              'source_id': page.get('source_id'), 'source_url': page.get('url', '')})
                break
            if facts and facts[-1]['code'] == code:
                break
    labels = {code: label for code, label, _ in ACTIVITIES}
    if metadata.get('provider') == 'overpass' and metadata.get('source_url'):
        for tag, value in sorted(metadata.get('tags', {}).items()):
            code = OSM_ACTIVITIES.get((tag, value))
            if code and code not in {f['code'] for f in facts}:
                snippet = metadata.get('structured_text', '')
                quote = f'{tag}={value}' if f'{tag}={value}' in snippet else f'{tag}: {value}'
                facts.append({'code': code, 'label': labels[code], 'quote': quote,
                              'source_id': metadata.get('structured_source_id'),
                              'source_url': metadata['source_url']})
    return {'activities': facts, 'country': _company_country(pages, metadata, country), 'rule_version': RULE_VERSION}


def evaluate_fit(product_name, pages, metadata, country):
    product = normalize_product(product_name)
    profile = activity_profile(pages, metadata, country)
    facts = profile['activities']

    # NOT_FIT is used for explicit incompatibility, never absence of a word.
    for page, start, end in _page_clauses(pages):
        text = page.get('text', '')
        sentence = text[start:end]
        if (re.search(r'деятельность\s+прекращена|компания\s+ликвидирована', sentence, re.I)
                or (re.search(r'\bне\s+(?:закупаем|покупаем|используем)|предложения.{0,60}не\s+принимаем', sentence, re.I)
                    and (matches_product(product, sentence) or product in product_candidates(sentence)))):
            if not _own_activity(text, start, end):
                continue
            profile.update(rule='explicit_incompatibility', relationship='Явный отказ от направления')
            return {'status': 'NOT_FIT', 'potential': False, 'profile': profile,
                    'reason': 'Не подходит: на сайте указано «' + _sentence_quote(text, start, end) + '».'}

    strong, weak, relationship = set(), set(), ''
    if product == 'офисное кресло':
        strong = {'office_furniture', 'office_outfit'}
        weak = {'furniture', 'manufacturer'}
        relationship = 'кресла дополняют офисную мебель и оснащение рабочих мест'
    elif product in CATEGORIES['автотовары']:
        strong = {'autoparts', 'car_service', 'fleet'}
        relationship = 'товар соответствует ассортименту автозапчастей или обслуживанию транспорта'
    elif product in CATEGORIES['клининг и гигиена']:
        strong = {'cleaning', 'hospitality', 'food_service'}
        relationship = 'расходные материалы используются для уборки и гигиены помещений'
    elif product in CATEGORIES['упаковка']:
        strong = {'packaging', 'shipping'}
        weak = {'manufacturer'}
        relationship = 'упаковка может использоваться для комплектации и отгрузки продукции'
    elif product in ('кофе', 'чай'):
        # Coffee/tea rules stay product-specific: menu, purchase, staff drinks or food service.
        strong = {'food_service', 'food_retail', 'hospitality', 'office_beverages'}
        relationship = 'напиток подтверждён меню, закупкой или обеспечением сотрудников'
    elif product in CATEGORIES['продукты питания'] or product in CATEGORIES['общественное питание']:
        strong = {'food_service', 'food_retail', 'hospitality'}
        relationship = 'товар соответствует приготовлению и продаже продуктов питания'
    elif product in CATEGORIES[CONSUMER_ELECTRONICS]:
        strong = {'electronics'}
        relationship = 'товар дополняет ассортимент потребительской электроники'
    elif product in CATEGORIES[IT_EQUIPMENT]:
        strong = {'it_supply', 'server_ops'}
        relationship = 'товар связан с закупкой, развёртыванием или эксплуатацией серверного оборудования'
    elif any(product in CATEGORIES[c] for c in ('строительные материалы', 'отделка и ремонт', 'сантехника', 'инструменты', 'электрика и освещение')):
        strong = {'construction'}
        relationship = 'товар используется в строительстве, ремонте или соответствующем ассортименте'
    elif product in CATEGORIES['мебель'] or product in ('мебель', 'офисная мебель'):
        strong = {'furniture', 'office_furniture'}
        weak = {'hospitality', 'office_outfit'}
        relationship = 'мебель соответствует ассортименту или оснащению помещений'
    elif product in CATEGORIES['сельское хозяйство']:
        strong = {'agriculture'}
        relationship = 'товар относится к снабжению сельского хозяйства'
    elif product in CATEGORIES['зоотовары']:
        strong = {'pet_retail'}
        relationship = 'товар дополняет ассортимент зоомагазина'
    elif product in CATEGORIES['промышленное оборудование'] or product in CATEGORIES['металлоизделия']:
        weak = {'manufacturer'}
        relationship = 'оборудование может использоваться на производстве; назначение требуется уточнить'

    # Exact product use (используем/эксплуатируем) is as strong as purchase for IT and chairs.
    for page, start, end in _page_clauses(pages):
        text = page.get('text', '')
        sentence = text[start:end]
        usage = re.search(r'\b(?:используем|эксплуатируем|развёртываем|развертываем|оснащаем)\b', sentence, re.I)
        if (not usage or _denied_assortment(sentence) or not _own_activity(text, start, end)
                or not (matches_product(product, sentence) or product in product_candidates(sentence))):
            continue
        if product in CATEGORIES[IT_EQUIPMENT] or product == 'офисное кресло' or product in ('кофе', 'чай'):
            profile.update(rule='documented_use', relationship='организация использует товар')
            profile['activities'].append({'code': 'documented_use', 'label': 'использование организацией',
                'quote': sentence[:350], 'source_id': page.get('source_id'), 'source_url': page.get('url', '')})
            return {'status': 'FIT', 'potential': False, 'profile': profile,
                    'reason': f'Факт: «{sentence[:300]}». Связь: организация использует товар. '
                              f'Гипотеза: возможно новое предложение товара «{product_name}».'}

    matching = next((f for f in facts if f['code'] in strong), None)
    potential = next((f for f in facts if f['code'] in weak), None)
    if matching:
        profile.update(rule='activity:' + matching['code'], relationship=relationship)
        return {'status': 'FIT', 'potential': False, 'profile': profile,
                'reason': f'Факт: {matching["label"]} — «{matching["quote"]}». Связь: {relationship}. Гипотеза: возможно предложение товара «{product_name}».'}
    # Exact product evidence must refer to the company's own commerce or purchases.
    for page, start, end in _page_clauses(pages):
        text = page.get('text', '')
        sentence = text[start:end]
        purchase = re.search(r'\b(?:закупаем|покупаем)\b', sentence, re.I)
        assortment = re.search(r'магазин|каталог|прода[её]м|продаж|поставля[её]м|ассортимент|дистрибьют', sentence, re.I)
        if (not (purchase or assortment) or _denied_assortment(sentence) or not _own_activity(text, start, end)
                or not (matches_product(product, sentence) or product in product_candidates(sentence))):
            continue
        if purchase:
            code, label, relationship = 'documented_purchase', 'закупки организации', 'организация закупает товар'
        else:
            code, label, relationship = 'documented_assortment', 'ассортимент организации', 'товар входит в ассортимент организации'
        profile.update(rule=code, relationship=relationship)
        profile['activities'].append({'code': code, 'label': label,
            'quote': sentence[:350], 'source_id': page.get('source_id'), 'source_url': page.get('url', '')})
        return {'status': 'FIT', 'potential': False, 'profile': profile,
                'reason': f'Факт: «{sentence[:300]}». Связь: {relationship}. Гипотеза: возможно новое предложение товара «{product_name}».'}
    if potential:
        profile.update(rule='potential:' + potential['code'], relationship=relationship)
        return {'status': 'INSUFFICIENT_EVIDENCE', 'potential': True, 'profile': profile,
                'reason': f'Факт: {potential["label"]} — «{potential["quote"]}». Связь: {relationship}. Нужна проверка применимости товара «{product_name}».'}
    profile.update(rule='no_relation', relationship='')
    return {'status': 'INSUFFICIENT_EVIDENCE', 'potential': False, 'profile': profile,
            'reason': 'Нужна проверка: связь товара с деятельностью компании пока не установлена.'}


def semantic_evidence_allowed(product_name, pages, quote):
    """Check saved evidence for an AI decision without repeating its fit decision.

    The AI can establish relationships outside the activity vocabulary. Local
    checks only reject absent quotes, explicit refusals and third-party claims.
    """
    excerpt = clean_text(quote)
    if len(excerpt) < 12 or all(_denied_assortment(clause) for clause in CLAUSE_BOUNDARY.split(excerpt)):
        return False
    if evaluate_fit(product_name, pages, {}, '')['status'] == 'NOT_FIT':
        return False
    product = normalize_product(product_name)
    for page, start, end in _page_clauses(pages):
        text = page.get('text', '')
        sentence = text[start:end]
        if (_denied_assortment(sentence) and (matches_product(product, sentence) or product in product_candidates(sentence))
                and _own_activity(text, start, end)):
            return False
    if re.fullmatch(r'(?:добро пожаловать|здравствуйте|рады вас видеть|свяжитесь с нами|'
                    r'главная|контакты)(?:\s+на\s+(?:наш\s+)?сайт\w*)?[.! ]*', excerpt):
        return False
    for page in pages:
        text = '\n'.join(clean_text(line) for line in page.get('text', '').splitlines())
        pattern = r'\s+'.join(re.escape(word) for word in excerpt.split())
        for match in re.finditer(pattern, text):
            if _own_activity(text, match.start(), match.end()):
                return True
    return False
