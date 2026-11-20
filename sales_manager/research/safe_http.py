"""Bounded public-page fetcher with DNS pinning and robots.txt checks.

Connections use a validated public IP, while TLS still validates the original
hostname. This avoids the DNS validation/connection rebinding race.
"""
import http.client
import codecs
import ipaddress
import re
import socket
import ssl
import threading
import time
from dataclasses import dataclass
from email.message import Message
from html.parser import HTMLParser
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

USER_AGENT = 'SalesManagerFirst/1.0 (public business research; robots respected)'
_DNS_SLOTS = threading.BoundedSemaphore(4)
_ROBOTS_BACKOFF = 45

class FetchError(RuntimeError):
    pass


def _robots_normalize(value):
    """Normalize URI octets, decoding only percent-encoded unreserved ASCII."""
    value = quote(value, safe="/%?:@!$&'()*+,;=-._~")
    def replace(match):
        octet = int(match.group(1), 16)
        char = chr(octet)
        return char if octet < 128 and (char.isalnum() or char in '-._~') else '%%%02X' % octet
    return re.sub(r'%([0-9a-fA-F]{2})', replace, value)


class RobotsRules:
    """RFC 9309 group selection and longest octet-pattern matching."""
    def __init__(self, lines):
        self.groups = []
        agents, rules, delays, saw_rule = [], [], [], False
        for line in lines:
            line = line.split('#', 1)[0].strip()
            if ':' not in line:
                continue
            name, value = (part.strip() for part in line.split(':', 1))
            name = name.casefold()
            if name == 'user-agent':
                if saw_rule:
                    self.groups.append((agents, rules, delays))
                    agents, rules, delays, saw_rule = [], [], [], False
                if value:
                    agents.append(value.casefold())
            elif agents and name in ('allow', 'disallow'):
                saw_rule = True
                if value:
                    rules.append((name == 'allow', value))
            elif agents and name == 'crawl-delay':
                try:
                    delay = float(value)
                    if delay >= 0:
                        delays.append(delay)
                except ValueError:
                    pass
        if agents:
            self.groups.append((agents, rules, delays))

    def _selected(self, user_agent):
        product = user_agent.casefold()
        matched = [g for g in self.groups if any(a != '*' and a in product for a in g[0])]
        return matched or [g for g in self.groups if '*' in g[0]]

    def can_fetch(self, user_agent, url):
        parsed = urlsplit(url)
        path = parsed.path or '/'
        if path == '/robots.txt':
            return True
        if parsed.query:
            path += '?' + parsed.query
        path = _robots_normalize(path.replace('*', '%2A').replace('$', '%24'))
        best = (-1, True)
        for _, rules, _ in self._selected(user_agent):
            for allow, pattern in rules:
                anchored = pattern.endswith('$')
                if anchored:
                    pattern = pattern[:-1]
                parts = [_robots_normalize(part) for part in pattern.split('*')]
                expression = '^' + '.*'.join(re.escape(part) for part in parts) + ('$' if anchored else '')
                if re.search(expression, path):
                    # Percent escapes represent one octet, and '*'/'$' are not octets.
                    length = sum(len(re.sub(r'%[0-9A-F]{2}', 'X', part)) for part in parts)
                    best = max(best, (length, allow))
        return best[1]

    def crawl_delay(self, user_agent):
        delays = [delay for _, _, values in self._selected(user_agent) for delay in values]
        return max(delays, default=0)

def validate_url(url):
    if not isinstance(url, str) or len(url) > 2048 or re.search(r'[\x00-\x20\x7f\\]', url):
        raise FetchError('Некорректный адрес сайта.')
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or '').encode('idna').decode('ascii').lower().rstrip('.')
        port = parsed.port
    except (ValueError, UnicodeError) as exc:
        raise FetchError('Некорректный адрес сайта.') from exc
    if parsed.scheme.lower() not in ('http', 'https') or not host or parsed.username or parsed.password:
        raise FetchError('Разрешены только публичные HTTP/HTTPS-сайты без логина в URL.')
    if host == 'localhost' or host.endswith(('.localhost', '.local', '.internal', '.home', '.lan')):
        raise FetchError('Локальные и внутренние адреса запрещены.')
    if port not in (None, 80, 443):
        raise FetchError('Разрешены только стандартные веб-порты 80 и 443.')
    netloc = ('[' + host + ']') if ':' in host else host
    if port:
        netloc += ':' + str(port)
    path = quote(parsed.path or '/', safe="/%:@!$&'()*+,;=-._~")
    query = quote(parsed.query, safe="%/?@:!$&'()*+,;=-._~")
    return urlunsplit((parsed.scheme.lower(), netloc, path, query, ''))

def public_addresses(host, port):
    try:
        entries = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise FetchError('Не удалось определить адрес сайта.') from exc
    addresses = list(dict.fromkeys(item[4][0] for item in entries))
    if not addresses:
        raise FetchError('У сайта нет доступного адреса.')
    for address in addresses:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError as exc:
            raise FetchError('Некорректный сетевой адрес.') from exc
        if not ip.is_global or ip.is_multicast or ip.is_unspecified:
            raise FetchError('Доступ к локальным, служебным и приватным сетям запрещён.')
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped and not ip.ipv4_mapped.is_global:
            raise FetchError('Приватный IPv4-адрес запрещён.')
    return addresses


def _bounded_dns(host, port, deadline, check_cancel):
    # The OS may block in getaddrinfo. Timed-out calls retain their slot until
    # they actually exit, so repeated attempts cannot accumulate threads.
    if not _DNS_SLOTS.acquire(blocking=False):
        raise FetchError('Служба DNS занята; используем другие источники.')
    finished = threading.Event()
    result = []

    def resolve():
        try:
            result.append(public_addresses(host, port))
        except Exception as exc:
            result.append(exc)
        finally:
            _DNS_SLOTS.release()
            finished.set()

    try:
        threading.Thread(target=resolve, name='market-dns', daemon=True).start()
    except RuntimeError:
        _DNS_SLOTS.release()
        raise FetchError('Не удалось запустить проверку DNS.') from None
    while True:
        check_cancel()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise FetchError('Превышено время определения адреса DNS.')
        if finished.wait(min(.1, remaining)):
            check_cancel()
            if isinstance(result[0], Exception):
                raise result[0]
            return result[0]

class _PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, port, address, timeout):
        try:
            import truststore
            context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        except ImportError:
            context = ssl.create_default_context()
        super().__init__(host, port, timeout=timeout, context=context)
        self.address = address

    def connect(self):
        raw = socket.create_connection((self.address, self.port), self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise

@dataclass
class FetchResult:
    url: str
    status: int
    content: bytes
    content_type: str = ''
    charset: str = ''

    @property
    def media_type(self):
        # Match the actual MIME type, never a token inside its parameters.
        return self.content_type.partition(';')[0].strip().casefold()

    @property
    def text(self):
        encoding = self.charset
        if not encoding:
            if self.content.startswith(codecs.BOM_UTF8):
                encoding = 'utf-8-sig'
            elif self.content.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
                encoding = 'utf-16'
            elif self.media_type in ('', 'text/html', 'application/xhtml+xml'):
                parser = _CharsetParser()
                parser.feed(self.content[:8192].decode('ascii', errors='ignore'))
                encoding = parser.charset
        try:
            return self.content.decode(encoding or 'utf-8', errors='replace')
        except (LookupError, UnicodeError, ValueError):
            return self.content.decode('utf-8', errors='replace')


class _CharsetParser(HTMLParser):
    """Read HTML declarations only; arbitrary body text cannot select a codec."""
    charset = ''

    def handle_starttag(self, tag, attrs):
        if tag != 'meta' or self.charset:
            return
        attrs = dict(attrs)
        value = attrs.get('charset') or ''
        if not value and (attrs.get('http-equiv') or '').casefold() == 'content-type':
            header = Message()
            header['Content-Type'] = attrs.get('content') or ''
            value = header.get_content_charset() or ''
        if value:
            try:
                codecs.lookup(value)
            except (LookupError, ValueError):
                return
            self.charset = value


class SafeFetcher:
    def __init__(self, timeout=15, max_bytes=2_000_000, max_redirects=4, cancel_check=None):
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.max_redirects = max_redirects
        self.cancel_check = cancel_check
        self._robots = {}
        self._robots_checked_at = {}
        self._robots_retry_at = {}
        self._last_request = {}
        self._robots_locks = {}

    def _check_cancel(self):
        if self.cancel_check is not None:
            self.cancel_check()

    def _wait(self, delay):
        if self.cancel_check is None:
            time.sleep(delay)
            return
        while delay > 0:
            self._check_cancel()
            step = min(.1, delay)
            time.sleep(step)
            delay -= step
        self._check_cancel()

    def _request(self, url, limit, timeout=None, truncate=False):
        """One request, with validated and pinned IP; no automatic redirects."""
        self._check_cancel()
        parsed = urlsplit(validate_url(url))
        port = parsed.port or (443 if parsed.scheme == 'https' else 80)
        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        addresses = _bounded_dns(parsed.hostname, port, deadline, self._check_cancel)
        conn = None
        error = None
        for address in addresses[:3]:
            self._check_cancel()
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                if parsed.scheme == 'https':
                    conn = _PinnedHTTPS(parsed.hostname, port, address, remaining)
                else:
                    conn = http.client.HTTPConnection(address, port, timeout=remaining)
                conn.request('GET', urlunsplit(('', '', parsed.path or '/', parsed.query, '')),
                             headers={'Host': parsed.netloc, 'User-Agent': USER_AGENT,
                                      'Accept': ('text/html,text/plain,application/xhtml+xml,'
                                                 'application/xml,text/xml,application/rss+xml,application/atom+xml'),
                                      'Accept-Encoding': 'identity', 'Connection': 'close'})
                if conn.sock is not None:
                    conn.sock.settimeout(max(.01, deadline - time.monotonic()))
                response = conn.getresponse()
                headers = {k.lower(): v for k, v in response.getheaders()}
                if response.status in (301, 302, 303, 307, 308):
                    return response.status, headers, b''
                length = headers.get('content-length', '')
                if not truncate and length.isdigit() and int(length) > limit:
                    raise FetchError('Страница превышает допустимый размер.')
                chunks, total = [], 0
                while True:
                    self._check_cancel()
                    if time.monotonic() >= deadline:
                        raise FetchError('Превышено время загрузки.')
                    chunk = response.read(min(65536, limit + 1 - total))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    total += len(chunk)
                    if total > limit and truncate:
                        return response.status, headers, b''.join(chunks)[:limit]
                    if total > limit or time.monotonic() > deadline:
                        raise FetchError('Превышен лимит размера или времени загрузки.')
                return response.status, headers, b''.join(chunks)
            except FetchError:
                raise
            except (OSError, http.client.HTTPException) as exc:
                error = exc
            finally:
                if conn:
                    conn.close()
        raise FetchError('Сайт недоступен или не прошёл проверку TLS.') from error

    def _raw(self, url, limit, respect_robots=False, truncate=False):
        current = validate_url(url)
        deadline = time.monotonic() + self.timeout
        max_redirects = max(5, self.max_redirects) if truncate else self.max_redirects
        for hop in range(max_redirects + 1):
            self._check_cancel()
            if respect_robots:
                self._check_robots(current)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise FetchError('Превышено время загрузки.')
            if truncate:
                status, headers, content = self._request(current, limit, timeout=remaining, truncate=True)
            else:
                status, headers, content = self._request(current, limit, timeout=remaining)
            if status in (301, 302, 303, 307, 308):
                location = headers.get('location')
                if not location or hop == max_redirects:
                    raise FetchError('Слишком много перенаправлений сайта.')
                current = validate_url(urljoin(current, location))
                continue
            ctype = headers.get('content-type', '')
            header = Message()
            header['Content-Type'] = ctype
            charset = header.get_content_charset() or ''
            return FetchResult(current, status, content, ctype, charset)
        raise FetchError('Не удалось загрузить страницу.')

    def _check_robots(self, url):
        parsed = urlsplit(url)
        default_port = 443 if parsed.scheme == 'https' else 80
        origin = f'{parsed.scheme}://{parsed.netloc.removesuffix(f":{default_port}")}'
        # Independent hosts may redirect to one origin during a parallel batch.
        # Reserve its next permitted request atomically, including robots load.
        lock = self._robots_locks.setdefault(origin, threading.RLock())
        self._check_cancel()
        while not lock.acquire(timeout=.1):
            self._check_cancel()
        try:
            self._check_cancel()
            self._check_robots_locked(url, origin)
        finally:
            lock.release()

    def _check_robots_locked(self, url, origin):
        timestamp = time.monotonic()
        if timestamp < self._robots_retry_at.get(origin, 0):
            raise FetchError('robots.txt временно недоступен. Повторите позже.')
        self._robots_retry_at.pop(origin, None)
        if origin not in self._robots or timestamp - self._robots_checked_at.get(origin, 0) >= 86400:
            result = self._raw(origin + '/robots.txt', 500 * 1024, truncate=True)
            if result.status == 429 or 500 <= result.status < 600:
                self._robots_retry_at[origin] = time.monotonic() + _ROBOTS_BACKOFF
                raise FetchError('robots.txt временно недоступен. Повторите позже.')
            if result.status in (401, 403):
                raise FetchError('Сайт ограничил доступ к robots.txt.')
            robot = RobotsRules(result.text.splitlines() if result.status == 200 else [])
            self._robots[origin] = robot
            self._robots_checked_at[origin] = time.monotonic()
        robot = self._robots[origin]
        if not robot.can_fetch('SalesManagerFirst', url):
            raise FetchError('Сайт запретил автоматическое чтение этого раздела (robots.txt).')
        delay = max(0.35, float(robot.crawl_delay('SalesManagerFirst') or 0))
        if delay > 60:
            raise FetchError('Сайт требует длительную паузу robots.txt; автоматический обход остановлен.')
        elapsed = time.monotonic() - self._last_request.get(origin, 0)
        if elapsed < delay:
            self._wait(delay - elapsed)
        self._last_request[origin] = time.monotonic()

    def fetch(self, url):
        result = self._raw(url, self.max_bytes, respect_robots=True)
        if result.status != 200:
            raise FetchError(f'Сайт ответил HTTP {result.status}; данные не получены.')
        if result.media_type and result.media_type not in ('text/html', 'text/plain', 'application/xhtml+xml'):
            raise FetchError('Этот тип документа не поддерживается; ожидается HTML или текст.')
        return result

    def fetch_document(self, url):
        """RSS/sitemap XML follows the same public-IP, robots and size policy."""
        result = self._raw(url, self.max_bytes, respect_robots=True)
        if result.status != 200:
            raise FetchError(f'Источник ответил HTTP {result.status}; документ не получен.')
        if result.media_type and not (result.media_type in (
                'application/xml', 'text/xml', 'text/plain', 'text/html', 'application/rss', 'application/atom')
                or result.media_type.endswith('+xml')):
            raise FetchError('Ожидается XML/RSS или публичная текстовая страница.')
        return result
