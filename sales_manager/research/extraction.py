"""Local HTML/contact extraction, without model calls or script execution."""
import json
import re
from itertools import groupby
from urllib.parse import unquote, urljoin, urlsplit

from bs4 import BeautifulSoup, NavigableString
from sales_manager.core.contacts import is_contact_email

EMAIL_RE = re.compile(r'(?<![\w.+-])[a-z0-9.!#$%&\'*+/=?^_`{|}~-]+@[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\.[a-z]{2,24}\b', re.I)
PHONE_RE = re.compile(r'(?<!\w)(?:\+\d[\d ()\-.]{8,24}\d|8[ (\-][\d ()\-.]{8,23}\d)(?!\w)')
INN_RE = re.compile(r'\bИНН\s*[:№]?\s*(\d{10}|\d{12})\b', re.I)
DEVELOPER_LABEL = r'\b(?:разработк[аи]|создани[ея]|дизайн)\s+(?:этого\s+)?сайт\w*\b'
DEVELOPER_CREDIT = r'\b(?:разработчик\w*\s+(?:этого\s+)?сайт\w*|сайт\s+(?:разработан|создан))\b'
OWN_CONTACT = (r'(?P<own>\b(?:наш[аи]\s+(?:почт|контакт)\w*|'
               r'(?:почт[аы]|контакт\w*)\s+(?:компани|магазин|кафе|организаци|филиал|продавц)\w*|'
               r'(?:единая|корпоративная)\s+почта|головн\w*\s+офис\w*)\b)')
OTHER_CONTACT = (r'(?P<other>\b(?:плат[её]жн\w*\s+(?:оператор|агент|посредник|систем|сервис)\w*|'
                 r'(?:реквизит\w*|контакт\w*|почта|поддержка)\s+(?:наш\w*\s+)?'
                 r'(?:партн[её]р|подрядчик|клиент|банка|эквайер|оператор|маркетплейс|платформ)\w*)\b)')
CONTACT_PARTY_RE = re.compile(rf'(?P<developer>{DEVELOPER_CREDIT}|{DEVELOPER_LABEL}(?=\s*[:—–-]))|{OTHER_CONTACT}|{OWN_CONTACT}', re.I)
FOOTER_CONTACT_PARTY_RE = re.compile(rf'(?P<developer>{DEVELOPER_CREDIT}|{DEVELOPER_LABEL})|{OTHER_CONTACT}|{OWN_CONTACT}', re.I)
LEGAL_PARTY_RE = re.compile(
    r'(?P<other>\b(?:плат[её]жн\w*\s+(?:оператор|агент|посредник|систем|сервис)\w*|'
    r'реквизит\w*\s+(?:наш\w*\s+)?(?:партн[её]р|клиент|банка|эквайер|оператор)\w*))|'
    r'(?P<own>\b(?:наш\w*\s+реквизит\w*|реквизит\w*\s+(?:наш\w*\s+)?(?:компани|продавц|организаци)\w*))',
    re.I)
EVIDENCE_BLOCKS = {'address', 'article', 'aside', 'blockquote', 'div', 'dl', 'footer', 'header',
                   'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'li', 'main', 'p', 'section', 'tr'}
HEADINGS = {'h1', 'h2', 'h3', 'h4', 'h5', 'h6'}
TEXT_BLOCKS = HEADINGS | {'blockquote', 'dd', 'dt', 'p', 'title', 'tr'}
LISTS = {'ol', 'ul'}
AGGREGATOR_DOMAINS = {'wikipedia.org', 'wikidata.org', 'openstreetmap.org', '2gis.ru',
                      '2gis.kz', 'yandex.ru', 'google.com', 'avito.ru', 'allbiz.ru',
                      'all.biz', 'flagma.ru', 'flagma.kz', 'rusprofile.ru', 'list-org.com',
                      'tass.ru', 'ria.ru', 'rbc.ru', 'kommersant.ru', 'vc.ru', 'dzen.ru',
                      'ozon.ru', 'wildberries.ru', 'megamarket.ru', 'aliexpress.ru',
                      'kaspi.kz', 'satu.kz', 'prom.ua', 'deal.by'}
MARKETPLACE_DOMAINS = {'ozon.ru', 'wildberries.ru', 'megamarket.ru', 'aliexpress.ru',
                       'kaspi.kz', 'satu.kz', 'prom.ua', 'deal.by'}
SELLER_NAME = re.compile(
    r'(?:продавец|магазин|seller|store)\s*[:—–-]?\s*([«"]?)([^«»"\n|]{2,80})\1', re.I)

def domain_of(url):
    host = (urlsplit(url).hostname or '').casefold().rstrip('.')
    return host[4:] if host.startswith('www.') else host

def is_directory(url):
    domain = domain_of(url)
    return any(domain == d or domain.endswith('.' + d) for d in AGGREGATOR_DOMAINS)

def is_marketplace(url):
    domain = domain_of(url)
    return any(domain == d or domain.endswith('.' + d) for d in MARKETPLACE_DOMAINS)

def marketplace_seller_hint(title='', text='', url=''):
    """Marketplace pages only hint at a seller; they are not official company evidence."""
    if not is_marketplace(url):
        return ''
    blob = ' '.join(part for part in (title, text) if part)
    match = SELLER_NAME.search(blob)
    if not match:
        return ''
    name = ' '.join(match.group(2).split()).strip(' .,;:')
    return name[:180]

def emails_in(text):
    return sorted({e.casefold().strip('.,;') for e in EMAIL_RE.findall(text)
                   if is_contact_email(e) and not e.casefold().endswith(('.png', '.jpg', '.jpeg', '.gif', '.svg', '.webp'))})

def phone_normalize(value, country=''):
    value = unquote(str(value)).split(';')[0]
    digits = re.sub(r'\D', '', value)
    if not 10 <= len(digits) <= 15:
        return ''
    if country == 'Россия' and len(digits) == 11 and digits.startswith('8'):
        digits = '7' + digits[1:]
    return ('+' if value.strip().startswith('+') or (country == 'Россия' and digits.startswith('7')) else '') + digits


def parse_feed(xml, url, country=''):
    """Parse bounded RSS/Atom without network entities or DTD expansion."""
    from xml.etree import ElementTree as ET
    from email.utils import parsedate_to_datetime
    from .providers import SearchResult, InvalidResponseError
    if '<!DOCTYPE' in xml.upper() or '<!ENTITY' in xml.upper():
        raise InvalidResponseError('RSS содержит неподдерживаемые XML-объявления.')
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise InvalidResponseError('Лента вернула некорректный XML.') from exc
    tag = lambda el: el.tag.rsplit('}', 1)[-1]
    if tag(root) not in ('rss', 'feed', 'RDF'):
        raise InvalidResponseError('Источник не вернул RSS/Atom.')
    output = []
    for entry in list(root.iter()):
        if tag(entry) not in ('item', 'entry'):
            continue
        fields = {tag(el): el for el in entry}
        def value(name):
            el = fields.get(name)
            return ''.join(el.itertext()).strip() if el is not None else ''
        links = [el for el in entry if tag(el) == 'link']
        # Atom can also contain self/enclosure links; only an alternate link
        # identifies the article. RSS uses the text of its link element.
        link = next((''.join(el.itertext()).strip() for el in links
                     if ''.join(el.itertext()).strip().startswith(('https://', 'http://'))), '')
        if not link:
            alternatives = [el for el in links if el.get('rel', 'alternate') == 'alternate']
            alternatives.sort(key=lambda el: el.get('type', '') not in ('', 'text/html', 'application/xhtml+xml'))
            link = next((el.get('href', '').strip() for el in alternatives
                         if el.get('href', '').strip().startswith(('https://', 'http://'))), '')
        if not link.startswith(('https://', 'http://')):
            continue
        snippet = BeautifulSoup(value('description') or value('summary') or value('content'), 'html.parser').get_text(' ', strip=True)
        published = value('pubDate') or value('published') or value('updated')
        if published:
            try:
                published = parsedate_to_datetime(published).isoformat()
            except (ValueError, TypeError, OverflowError):
                pass
        output.append(SearchResult(link, value('title')[:300], snippet[:5000], country,
            published, {'provider': 'rss', 'feed_url': url}))
        if len(output) >= 200:
            break
    return output


def sitemap_links(xml, site_url, limit=30):
    from xml.etree import ElementTree as ET
    if '<!DOCTYPE' in xml.upper() or '<!ENTITY' in xml.upper():
        return []
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return []
    if root.tag.rsplit('}', 1)[-1] != 'urlset':
        return []  # No recursive unbounded sitemap indexes.
    candidates = []
    for el in root.iter():
        if el.tag.rsplit('}', 1)[-1] != 'loc':
            continue
        target = (el.text or '').strip()
        if domain_of(target) != domain_of(site_url) or not target.startswith(('http://', 'https://')):
            continue
        score = sum(w in target.casefold() for w in ('contact', 'about', 'company', 'catalog', 'product', 'kontakt', 'контакт'))
        if score:
            candidates.append((score, target))
    return list(dict.fromkeys(v for _, v in sorted(candidates, reverse=True)))[:limit]


def external_links(html, url, limit=10):
    """Potential organization links in public articles/directories, not official claims."""
    soup = BeautifulSoup(html, 'html.parser')
    output = []
    for a in soup.find_all('a', href=True):
        target = urljoin(url, str(a['href'])).split('#')[0]
        if target.startswith(('https://', 'http://')) and domain_of(target) != domain_of(url) and not is_directory(target):
            if not any(domain_of(target).endswith(d) for d in ('facebook.com', 't.me', 'vk.com', 'youtube.com', 'twitter.com', 'instagram.com', 'wikimedia.org')):
                output.append(target)
    return list(dict.fromkeys(output))[:limit]


def _evidence_blocks(soup):
    """Keep contacts and identifiers with their enclosing HTML text, including link targets."""
    blocks = {}
    for node in soup.descendants:
        kind, value = 'text', ''
        if type(node) is NavigableString:
            value = node.strip()
        elif node.name == 'a':
            href = str(node.get('href', '')).strip()
            if href.lower().startswith('mailto:'):
                kind, value = 'email', unquote(href[7:].split('?')[0])
            elif href.lower().startswith('tel:'):
                kind, value = 'phone', href[4:]
        if value:
            block = node.find_parent(EVIDENCE_BLOCKS)
            parts = blocks.setdefault(id(block), (block, []))[1]
            if kind != 'text':
                label = node.get_text(' ', strip=True)
                if label:
                    parts.append(('text', label))
            parts.append((kind, value))
    for block, parts in blocks.values():
        label = ''
        previous = block.previous_sibling if block and block.name not in HEADINGS else None
        while previous and isinstance(previous, NavigableString) and not str(previous).strip():
            previous = previous.previous_sibling
        # A heading or short role label applies only to the next sibling block.
        if previous and getattr(previous, 'name', None) in HEADINGS:
            label = previous.get_text(' ', strip=True)
        elif previous and getattr(previous, 'name', None) in ('div', 'dt', 'p'):
            adjacent = previous.get_text(' ', strip=True)
            if (len(adjacent) <= 120 and not INN_RE.search(adjacent) and not EMAIL_RE.search(adjacent)
                    and (LEGAL_PARTY_RE.search(adjacent) or CONTACT_PARTY_RE.search(adjacent)
                         or re.fullmatch(rf'(?:{DEVELOPER_CREDIT}|{DEVELOPER_LABEL})\s*[:—–-]?', adjacent, re.I))):
                label = adjacent
        if label:
            parts.insert(0, ('text', label))
        yield block, parts


def _visible_text(soup):
    parts, previous = [], None
    for node in soup.strings:
        value = node.strip()
        if not value:
            continue
        # Keep a list under its heading as one evidence unit. Inline formatting
        # within a paragraph or list must not break an ordinary product phrase.
        block = node.find_parent(LISTS) or node.find_parent(TEXT_BLOCKS)
        if parts:
            parts.append(' ' if block is previous else '\n')
        parts.append(value)
        previous = block
    return ''.join(parts)[:100000]


def legal_id_roles(text):
    """Return the identifiers described as this site's and those of named third-party roles."""
    own, other = set(), set()
    role = None
    markers = iter(LEGAL_PARTY_RE.finditer(text))
    marker = next(markers, None)
    for match in INN_RE.finditer(text):
        while marker and marker.end() <= match.start():
            role, marker = marker.lastgroup, next(markers, None)
        (other if role == 'other' else own).add(match[1])
    return own, other


def _page_contacts_and_legal_ids(soup, country, site_domain, qualified_only=False):
    emails, phone_values, developer_emails, excluded_emails, own_ids, other_ids = (set() for _ in range(6))
    for block, parts in _evidence_blocks(soup):
        context = ' '.join(value for kind, value in parts if kind == 'text')
        own, other = legal_id_roles(context)
        own_ids.update(own)
        other_ids.update(other)
        # Keep unrelated contacts outside the credit. A page can list both
        # its own mailbox and that of the developer in the same footer.
        footer = block and (block.name == 'footer' or block.find_parent('footer'))
        contact_roles = FOOTER_CONTACT_PARTY_RE if footer else CONTACT_PARTY_RE
        role, snippets, own_tel = '', {key: [] for key in ('', 'own', 'developer', 'other')}, []
        for kind, group in groupby(parts, key=lambda item: item[0]):
            values = [value for _, value in group]
            if kind == 'text':
                text = ' '.join(values)
                start = 0
                for marker in contact_roles.finditer(text):
                    snippets[role].append(text[start:marker.start()])
                    role, start = marker.lastgroup, marker.end()
                snippets[role].append(text[start:])
            elif kind == 'email':
                snippets[role].extend(values)
            elif role in ('', 'own'):
                own_tel.extend(values)
        unlabelled = emails_in(' '.join(snippets['']))
        emails.update(emails_in(' '.join(snippets['own'])))
        for email in unlabelled:
            host = email.rsplit('@', 1)[-1]
            same_site = host == site_domain or host.endswith('.' + site_domain)
            # Unlabelled same-site mail is kept. Off-domain unlabelled mail stays a
            # candidate until a developer/payment role marks it third-party; a later
            # crawl can then retire it after the role becomes explicit.
            if qualified_only and not same_site:
                excluded_emails.add(email)
            else:
                emails.add(email)
        developer_emails.update(emails_in(' '.join(snippets['developer'])))
        excluded_emails.update(emails_in(' '.join(snippets['other'])))
        own_text = ' '.join(snippets[''] + snippets['own'])
        phone_values.update(raw for raw in own_tel + PHONE_RE.findall(own_text) if phone_normalize(raw))
    phones = {value for raw in phone_values if (value := phone_normalize(raw, country))}
    own_emails = emails - developer_emails - excluded_emails
    return (sorted(own_emails), sorted(phones), sorted(phone_values), sorted(developer_emails - own_emails),
            sorted((excluded_emails | developer_emails) - own_emails), sorted(own_ids), sorted(other_ids - own_ids))


def _record_values(value):
    return value if isinstance(value, list) else [value]


def _record_text(value):
    return ' '.join(value.split())[:400] if isinstance(value, str) else ''


def _organization_records(soup, url, country):
    records = []
    for tag in soup.find_all('script', attrs={'type': 'application/ld+json'}, limit=20):
        try:
            pending = [(entry, '') for entry in _record_values(json.loads(tag.string or tag.get_text()))]
        except (ValueError, TypeError):
            continue
        for index in range(100):
            if index >= len(pending):
                break
            entry, parent = pending[index]
            if not isinstance(entry, dict):
                continue
            kinds = [kind.rsplit('/', 1)[-1] for kind in _record_values(entry.get('@type')) if isinstance(kind, str)]
            if any(kind in ('Organization', 'Corporation', 'LocalBusiness', 'Store', 'Restaurant', 'CafeOrCoffeeShop',
                            'Hotel') or kind.endswith(('Store', 'Business', 'Organization')) for kind in kinds):
                name = _record_text(entry.get('name'))
                if name:
                    address = entry.get('address')
                    region = ''
                    if isinstance(address, dict):
                        region = _record_text(address.get('addressLocality')) or _record_text(address.get('addressRegion'))
                        address = ', '.join(dict.fromkeys(filter(None, (region, _record_text(address.get('streetAddress'))))))
                    else:
                        address = _record_text(address)
                    parent_reference = entry.get('parentOrganization') or entry.get('branchOf')
                    if isinstance(parent_reference, dict):
                        parent = _record_text(parent_reference.get('name')) or parent
                    points = [entry] + [point for point in _record_values(entry.get('contactPoint')) if isinstance(point, dict)]
                    emails = {email for point in points for email in emails_in(_record_text(point.get('email')))}
                    phones = {phone for point in points
                              if (phone := phone_normalize(_record_text(point.get('telephone')), country))}
                    references = [_record_text(value) for value in _record_values(entry.get('url'))
                                  + _record_values(entry.get('sameAs')) if isinstance(value, str)]
                    links = [urljoin(url, value) for value in references if value]
                    records.append({'name': name, 'parent': parent, 'address': address, 'region': region,
                                    'branch_name': name if parent else '', 'emails': sorted(emails),
                                    'phones': sorted(phones), 'urls': links,
                                    'description': _record_text(entry.get('description')), 'types': kinds})
                    for key in ('department', 'subOrganization', 'location'):
                        pending.extend((child, name) for child in _record_values(entry.get(key))[:30] if child)
            for key in ('@graph', 'mainEntity'):
                pending.extend((child, parent) for child in _record_values(entry.get(key))[:30] if child)
    return records


def _branch_blocks(soup, site_domain, country):
    """Read explicit branch sections with their own location and contacts."""
    branches = []
    for block in soup.find_all(('article', 'section', 'div', 'li')):
        marker = ' '.join([str(block.get('id', '')), *block.get('class', [])])
        heading = block.find(('h2', 'h3', 'h4', 'h5'), recursive=False)
        title = heading.get_text(' ', strip=True) if heading else ''
        if not (re.search(r'(?:^|[\s_-])(?:branch|filial)(?:$|[\s_-])', marker, re.I)
                or re.match(r'^(?:филиал|отделение)\b', title, re.I)):
            continue
        address_node = block.find('address') or block.find(attrs={'itemprop': 'streetAddress'})
        address = address_node.get_text(' ', strip=True) if address_node else _record_text(block.get('data-address'))
        region_node = block.find(attrs={'itemprop': 'addressLocality'})
        region = region_node.get_text(' ', strip=True) if region_node else _record_text(block.get('data-city'))
        title = title or _record_text(block.get('data-branch'))
        if not (address or region or title) or block.find(('article', 'section', 'div', 'li'), class_=re.compile(r'\bbranch\b')):
            continue
        emails, phones, *_ = _page_contacts_and_legal_ids(block, country, site_domain)
        branches.append({'name': title, 'branch_name': title or region or address, 'address': address, 'region': region,
                         'emails': emails, 'phones': phones, 'description': _visible_text(block),
                         'urls': [], 'types': []})
        block.decompose()
    return branches


def parse_page(html, url, country=''):
    soup = BeautifulSoup(html, 'html.parser')
    site_domain = domain_of(url)
    records = _organization_records(soup, url, country)
    primary = next((entry for entry in records if not entry['parent'] and
                    set(entry['types']).intersection({'Organization', 'Corporation'})), None)
    if primary is None and len(records) == 1:
        primary = records[0]
    title = soup.title.get_text(' ', strip=True)[:250] if soup.title else ''
    og = soup.find('meta', attrs={'property': 'og:site_name'})
    og_name = og.get('content') if og else ''
    organization_name = (primary['name'] if primary else '') or _record_text(og_name)
    if not organization_name:
        organization_name = re.split(r'\s[|—–]\s', title)[0][:180] or domain_of(url)
    links = []
    for link in soup.find_all('a', href=True):
        href = str(link['href']).strip()
        if href and not href.lower().startswith(('#', 'javascript:', 'data:', 'mailto:', 'tel:')):
            absolute = urljoin(url, href)
            if absolute.startswith(('https://', 'http://')) and len(absolute) < 2048:
                label = link.get_text(' ', strip=True)
                score = sum(word in (href + ' ' + label).casefold() for word in (
                    'contact', 'kontakt', 'контакт', 'about', 'компани', 'каталог', 'catalog', 'услуг', 'service',
                    'проект', 'project', 'филиал', 'branch', 'адрес', 'locations'))
                if score and (domain_of(absolute) == site_domain or re.search(r'контакт|филиал|адрес|contact|branch', label, re.I)):
                    links.append((score, absolute.split('#')[0]))
    for tag in soup(['script', 'style', 'noscript', 'iframe', 'svg']):
        tag.decompose()
    text = _visible_text(soup)
    branches = _branch_blocks(soup, site_domain, country)
    emails, phones, phone_values, developer_emails, excluded_emails, legal_ids, external_legal_ids = (
        _page_contacts_and_legal_ids(soup, country, site_domain))
    local_records = [entry for entry in records if entry is not primary and
                     (entry['parent'] or (entry['address'] and primary and entry['name'] == primary['name']))]
    branches.extend(dict(entry, branch_name=entry['branch_name'] or entry['region'] or entry['address']) for entry in local_records)
    if primary and not is_directory(url):
        emails = sorted(set(emails) | (set(primary['emails']) - set(excluded_emails)))
        phones = sorted(set(phones) | set(primary['phones']))
    for branch in branches:
        branch['parent'] = branch.get('parent') or organization_name
    if local_records:
        # Local data embedded in structured records belongs to the listed
        # locations, even if the surrounding markup repeats it unlabelled.
        location_emails = {value for branch in local_records for value in branch['emails']}
        location_phones = {value for branch in local_records for value in branch['phones']}
        emails = sorted((set(emails) - location_emails) | (set(primary['emails']) if primary else set()))
        phones = sorted((set(phones) - location_phones) | (set(primary['phones']) if primary else set()))
    return {'url': url, 'title': title, 'name': organization_name, 'text': text,
            'emails': emails, 'phones': phones, 'phone_values': phone_values, 'developer_emails': developer_emails,
            'excluded_emails': excluded_emails, 'organizations': records, 'branches': branches,
            'organization_text': _visible_text(soup),
            'legal_id': legal_ids[0] if country == 'Россия' and len(legal_ids) == 1 else '',
            'legal_ids': legal_ids, 'external_legal_ids': external_legal_ids,
            'links': list(dict.fromkeys(item[1] for item in sorted(links, reverse=True)))[:40]}
