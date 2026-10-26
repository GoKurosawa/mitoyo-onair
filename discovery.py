"""Bounded public discovery. No credentials, third-party dependencies or publishing."""
from __future__ import annotations
import argparse
import copy
import hashlib
import html
from html.parser import HTMLParser
import ipaddress
import json
from pathlib import Path
import re
import socket
import http.client
import urllib.request
import time
import unicodedata
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import urlsplit, urlunsplit, urljoin, urlencode
from urllib.request import Request, build_opener, HTTPRedirectHandler, ProxyHandler
import xml.etree.ElementTree as ET

BASE = Path(__file__).resolve().parent
JST = ZoneInfo('Asia/Tokyo')
KEYWORDS = ['三豊', 'みとよ', '仁尾', '詫間', '高瀬', '山本', '豊中', '財田', '観音寺', '琴平', '丸亀']
# Adapters are deliberately narrow. Extending this list requires operator review.
HOSTS = {'anchor.fm', 'creators.spotify.com', 'podcasters.spotify.com',
         'listen.style', 'stand.fm', 'radiotalk.jp', 'podcasters.amazon.com'}
HTML_LIMIT = 2 * 1024 * 1024
FEED_LIMIT = 8 * 1024 * 1024


def normalize(value):
    # Do not discard punctuation/emoji: similar names must not collapse together.
    return ' '.join(unicodedata.normalize('NFKC', value).casefold().split())


def title_matches(program, title):
    return normalize(title) in {normalize(x) for x in [program['name']] + program.get('aliases', [])}


def clean(value, limit=300):
    text = html.unescape(re.sub(r'<[^>]*>', ' ', str(value)))
    text = re.sub(r'\S*@\S*', '[非保存]', text)
    return re.sub(r'[\x00-\x1f\x7f]', ' ', text).strip()[:limit]


def public_url(url):
    """Validate DNS on every request/redirect. No userinfo, query tokens or local IPs."""
    try:
        p = urlsplit(url)
        if (p.scheme not in ('https', 'http') or not p.hostname or p.username or p.password
                or p.port not in (None, 80, 443) or p.query or p.fragment):
            raise ValueError('unsafe_url')
        host = p.hostname.rstrip('.').lower()
        if host == 'localhost' or host.endswith(('.localhost', '.local', '.internal')):
            raise ValueError('private_host')
        addresses = socket.getaddrinfo(host, p.port or (443 if p.scheme == 'https' else 80), type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
            raise ValueError('private_address')
        return urlunsplit((p.scheme, p.netloc, p.path or '/', '', ''))
    except (OSError, TypeError, ValueError) as exc:
        raise ValueError('unsafe_or_unresolved_url') from exc


def pinned_connection(address, timeout=15, source_address=None, **kwargs):
    host, port = address
    public_url(('https' if port == 443 else 'http') + '://' + host + ('' if port in (80, 443) else ':' + str(port)))
    records = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if any(not ipaddress.ip_address(r[4][0]).is_global for r in records):
        raise ValueError('dns_rebinding_blocked')
    # Connect to a validated numeric address, while http.client keeps the hostname for TLS/SNI.
    return socket.create_connection((records[0][4][0], port), timeout, source_address)


class PinnedHTTP(http.client.HTTPConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = pinned_connection


class PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = pinned_connection


class HTTP(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(PinnedHTTP, req)


class HTTPS(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(PinnedHTTPS, req, context=self._context)


class Redirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Network:
    def __init__(self):
        self.opener = build_opener(ProxyHandler({}), Redirects(), HTTP(), HTTPS())
        self.requests = 0
        self.deadline = time.monotonic() + 480

    def get(self, url, limit=HTML_LIMIT, apple=False):
        self.requests += 1
        if self.requests > 80 or time.monotonic() >= self.deadline:
            raise ValueError('run_network_budget_exceeded')
        if apple:
            p = urlsplit(url)
            if p.scheme != 'https' or p.netloc != 'itunes.apple.com' or p.path != '/search':
                raise ValueError('invalid_apple_endpoint')
            public_url(urlunsplit((p.scheme, p.netloc, p.path, '', '')))
        else:
            public_url(url)
        with self.opener.open(Request(url, headers={'User-Agent': 'MitoyoOnAir-Discovery/1.0',
                                                    'Accept-Encoding': 'identity'}), timeout=min(15, max(1, self.deadline - time.monotonic()))) as response:
            final = response.geturl()
            public_url(final.split('?')[0] if apple else final)
            body = response.read(limit + 1)
        if len(body) > limit:
            raise ValueError('response_too_large')
        return body, final


class Apple:
    def __init__(self, network, clock=time.monotonic, sleep=time.sleep):
        self.network, self.clock, self.sleep = network, clock, sleep
        self.cache = {}
        self.last = None
        self.calls = 0

    def search(self, term, general=False):
        key = (term, general)
        if key in self.cache:
            return self.cache[key]
        if self.last is not None:
            delay = 3.2 - (self.clock() - self.last)
            if delay > 0:
                self.sleep(delay)
        self.last = self.clock()
        self.calls += 1
        args = {'term': term, 'entity': 'podcast', 'country': 'JP', 'limit': 20}
        if not general:
            args['attribute'] = 'titleTerm'
        raw, _ = self.network.get('https://itunes.apple.com/search?' + urlencode(args), apple=True)
        data = json.loads(raw)
        if not isinstance(data.get('results'), list):
            raise ValueError('invalid_apple_response')
        self.cache[key] = data['results']
        return self.cache[key]


class Links(HTMLParser):
    def __init__(self, body, base):
        super().__init__()
        self.base, self.anchors, self.feeds = base, [], []
        self.feed(body.decode('utf-8', errors='replace'))

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        href = a.get('href', '')
        if tag not in ('a', 'link') or not href:
            return
        url = urljoin(self.base, href)
        if tag == 'a':
            self.anchors.append(url)
        rel = a.get('rel', '').lower().split()
        if ((tag == 'link' and 'alternate' in rel and a.get('type', '').lower() in
             ('application/rss+xml', 'application/atom+xml')) or
                (tag == 'a' and re.search(r'(?:/rss|/feed|\.rss|\.xml)/?$', urlsplit(url).path, re.I))):
            self.feeds.append(url)


def spotify_id(url):
    p = urlsplit(url)
    m = re.fullmatch(r'/show/([A-Za-z0-9]{22})/?', p.path)
    return m[1] if p.scheme == 'https' and p.netloc == 'open.spotify.com' and m else None


def trusted(url):
    p = urlsplit(url)
    patterns = {'anchor.fm': r'/[^/]+/?',
                'creators.spotify.com': r'/pod/show/[^/]+/?',
                'podcasters.spotify.com': r'/pod/show/[^/]+/?',
                'listen.style': r'/p/[^/]+/?',
                'stand.fm': r'/channels/[^/]+/?',
                'radiotalk.jp': r'/program/[^/]+/?'}
    return p.scheme == 'https' and p.hostname in patterns and bool(re.fullmatch(patterns[p.hostname], p.path))


def parse_feed(body):
    # Decode before checking: UTF-16 must not bypass entity rejection.
    if b'\x00' in body or re.search(br'<!\s*(?:DOCTYPE|ENTITY)', body, re.I):
        raise ValueError('unsafe_xml')
    root = ET.fromstring(body)
    channel = root.find('channel')
    if channel is None:
        raise ValueError('not_rss_channel')
    title = channel.findtext('title', '').strip()
    # Only direct channel link elements; never descriptions/items.
    links = [x.text.strip() for x in channel.findall('link') if x.text]
    return title, links


def verify_feed(program, url, network, direct_source=None):
    body, final = network.get(url, FEED_LIMIT)
    title, channel_links = parse_feed(body)
    if not title_matches(program, title):
        return None
    if direct_source and trusted(direct_source) and direct_source in program.get('official_urls', []):
        return {'rss': final, 'rss_title': title, 'method': 'verified_official_feed_link', 'source': direct_source}
    expected = program.get('spotify_id')
    # A channel link may point to Spotify directly, or to an allowlisted hosting page.
    for page in channel_links[:4]:
        if expected and spotify_id(page) == expected:
            return {'rss': final, 'rss_title': title, 'method': 'channel_spotify_id', 'source': page}
        if trusted(page):
            content, page_final = network.get(page)
            if not trusted(page_final):
                continue
            links = Links(content, page_final)
            if expected and any(spotify_id(a) == expected for a in links.anchors):
                return {'rss': final, 'rss_title': title, 'method': 'hosting_page_spotify_id', 'source': page_final}
    return None


def stamp(now):
    return now.astimezone(JST).isoformat()


def weekly_due(last, now):
    return not last or now - datetime.fromisoformat(last).astimezone(JST) >= timedelta(days=7)


def monthly_due(last, now):
    return not last or datetime.fromisoformat(last).astimezone(JST).strftime('%Y-%m') != now.strftime('%Y-%m')


def candidate_key(row):
    if row.get('apple_id'):
        return 'apple:' + str(row['apple_id'])
    return 'feed:' + hashlib.sha256(row.get('feed_url', '').encode()).hexdigest()[:24]


def save_candidate(rows, row, now):
    key = candidate_key(row)
    # Merge duplicate feed URLs too; preserve manual review history/status.
    existing = next((x for x in rows if x['key'] == key or
                     (row.get('feed_url') and x.get('feed_url') == row['feed_url'])), None)
    if existing:
        existing['last_seen'] = stamp(now)
        existing['keywords'] = sorted(set(existing.get('keywords', []) + row.get('keywords', [])))
        return
    rows.append(dict(row, key=key, status='pending_review', first_seen=stamp(now), last_seen=stamp(now)))


def apple_row(hit, kind, keyword=None, program=None):
    # Public metadata only. Do not persist description, email, raw XML or artwork.
    row = {'kind': kind, 'apple_id': str(hit.get('collectionId', '')),
           'title': clean(hit.get('collectionName', '')), 'author': clean(hit.get('artistName', '')),
           'keywords': [keyword] if keyword else [], 'evidence': 'Apple public metadata; locality unverified'}
    for src, dest in [('feedUrl', 'feed_url'), ('collectionViewUrl', 'apple_url')]:
        value = hit.get(src)
        if value:
            try:
                row[dest] = public_url(value)
            except ValueError:
                pass
    if program:
        row['program_id'] = program['id']
    return row


def discover(programs, state, candidates, now, network, force=False, max_programs=50, max_queries=11):
    programs, state, candidates = copy.deepcopy((programs, state, candidates))
    state.setdefault('weekly', {})
    state.setdefault('monthly_queries', {})
    errors, events = [], []
    apple = Apple(network)
    # Removed-program markers/history are retained but no longer scheduled.
    selected = [p for p in programs if not p.get('rss') and
                (force or weekly_due(state['weekly'].get(str(p['id'])), now))][:max_programs]
    for p in selected:
        sources = {}
        failed = False
        def error(stage):
            nonlocal failed
            failed = True
            errors.append({'scope': 'weekly', 'program_id': p['id'], 'stage': stage, 'error': 'request_or_validation_failed'})
        for official in p.get('official_urls', [])[:4]:
            if not trusted(official):
                events.append({'program_id': p['id'], 'status': 'unsupported_official_host'})
                continue
            try:
                body, final = network.get(official)
                if not trusted(final) or urlsplit(final).hostname != urlsplit(official).hostname:
                    raise ValueError('untrusted_redirect')
                for feed in Links(body, final).feeds[:5]:
                    sources[feed] = official
            except Exception:
                error('official_page')
        # Spotify is public HTML only; blocked/protected/no links is not a successful lookup.
        if p.get('spotify_url'):
            try:
                body, final = network.get(p['spotify_url'])
                links = Links(body, final)
                # External links are hints only; must be an exact operator-verified URL.
                for page in links.anchors:
                    if page in p.get('official_urls', []) and trusted(page):
                        page_body, page_final = network.get(page)
                        if trusted(page_final):
                            for feed in Links(page_body, page_final).feeds[:5]:
                                sources[feed] = page
                if not body or b'captcha' in body.lower() or spotify_id(final) != p.get('spotify_id'):
                    raise ValueError('protected_page')
                events.append({'program_id': p['id'], 'status': 'spotify_public_links_only'})
            except Exception:
                error('spotify_public_page_skipped')
        try:
            hits = apple.search(p['name'])
            for hit in hits:
                if title_matches(p, str(hit.get('collectionName', ''))) and hit.get('feedUrl'):
                    row = apple_row(hit, 'existing_rss', program=p)
                    save_candidate(candidates, row, now)
                    if row.get('feed_url'):
                        sources.setdefault(row['feed_url'], None)
        except Exception:
            error('apple_title_search')
        verified = {}
        for feed, source in list(sources.items())[:8]:
            try:
                result = verify_feed(p, feed, network, source)
                if result:
                    verified[result['rss']] = result
                else:
                    events.append({'program_id': p['id'], 'status': 'identity_not_proven'})
            except Exception:
                error('feed_identity_check')
        if len(verified) == 1 and not failed:
            result = next(iter(verified.values()))
            p.update(rss=result['rss'], rss_title=result['rss_title'], rss_provenance=dict(result, checked_at=stamp(now)))
            events.append({'program_id': p['id'], 'status': 'registered'})
        elif len(verified) > 1:
            events.append({'program_id': p['id'], 'status': 'ambiguous_review_required'})
        else:
            events.append({'program_id': p['id'], 'status': 'unfound_or_review_required'})
        if not failed:
            state['weekly'][str(p['id'])] = stamp(now)
    if force or monthly_due(state.get('monthly_completed'), now):
        queries = [q for q in KEYWORDS if force or monthly_due(state['monthly_queries'].get(q), now)][:max_queries]
        known_apple = {str(p.get('apple_id')) for p in programs if p.get('apple_id')}
        known_feeds = {p['rss'] for p in programs if p.get('rss')}
        for q in queries:
            try:
                hits = apple.search(q)
                if not hits:
                    hits = apple.search(q, general=True)
                for hit in hits:
                    if str(hit.get('collectionId')) in known_apple or hit.get('feedUrl') in known_feeds:
                        continue
                    # Common surnames/place names have especially weak geographic meaning.
                    # General-search results without the literal keyword in public title/author
                    # are excluded, not inflated into locality claims.
                    if normalize(q) not in normalize(str(hit.get('collectionName', '')) + ' ' + str(hit.get('artistName', ''))):
                        continue
                    row = apple_row(hit, 'new_program', keyword=q)
                    row['geographic_review'] = ('ambiguous_common_name' if q in ('高瀬', '山本', '豊中')
                                                else 'locality_unverified')
                    if row['apple_id'] and row['title']:
                        save_candidate(candidates, row, now)
                state['monthly_queries'][q] = stamp(now)
            except Exception:
                errors.append({'scope': 'monthly', 'keyword': q, 'error': 'request_or_validation_failed'})
        if all(not monthly_due(state['monthly_queries'].get(q), now) for q in KEYWORDS):
            state['monthly_completed'] = stamp(now)
        state.setdefault('monthly_reports', {})[now.strftime('%Y-%m')] = {
            'updated_at': stamp(now), 'completed': not monthly_due(state.get('monthly_completed'), now),
            'successful_queries': [q for q in KEYWORDS if not monthly_due(state['monthly_queries'].get(q), now)],
            'candidate_keys': [r['key'] for r in candidates if r.get('kind') == 'new_program'],
            'errors': [e for e in errors if e.get('scope') == 'monthly']}
    report = {'at': stamp(now), 'apple_calls': apple.calls, 'weekly_attempted': len(selected),
              'monthly_queries_attempted': len(locals().get('queries', [])), 'events': events, 'errors': errors,
              'candidate_count': len(candidates), 'dry_run': False}
    return programs, state, candidates, report


def markdown(report, rows):
    def esc(text):
        return html.escape(clean(text), quote=True).replace('|', '&#124;').replace('`', '&#96;').replace('[', '&#91;').replace(']', '&#93;')
    lines = ['# 公開ポッドキャスト探索', '', '日時: ' + esc(report['at']), '',
             '月次は件数を制限したAppleのみの検索です。地域との関係は未確認。新規番組は自動掲載しません。',
             '未発見は配信停止・RSS無効を意味しません。候補は運営者が確認してください。', '',
             f"Apple呼出: {report['apple_calls']} / 週次対象: {report['weekly_attempted']} / エラー: {len(report['errors'])}", '',
             '|種別|番組名|公開著者表記|Apple ID|状態|', '|---|---|---|---|---|']
    lines += ['|' + '|'.join(esc(r.get(k, '')) for k in ('kind', 'title', 'author', 'apple_id', 'status')) + '|' for r in rows]
    lines += ['', '## 処理結果', ''] + [esc(json.dumps(e, ensure_ascii=False)) + '  ' for e in report['events']]
    lines += ['', '## エラー（機密情報・本文は記録しません）', ''] + [esc(json.dumps(e, ensure_ascii=False)) + '  ' for e in report['errors']]
    return '\n'.join(lines) + '\n'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--force', action='store_true')
    parser.add_argument('--max-programs', type=int, default=50)
    parser.add_argument('--max-monthly-queries', type=int, default=11)
    args = parser.parse_args()
    if not 0 <= args.max_programs <= 50 or not 0 <= args.max_monthly_queries <= 11:
        parser.error('limits out of range')
    def load(name, default):
        path = BASE / name
        return json.loads(path.read_text()) if path.exists() else default
    try:
        old_programs = load('programs.json', [])
        programs, state, rows, report = discover(old_programs, load('discovery_state.json', {}),
                                               load('discovery_candidates.json', []), datetime.now(JST), Network(),
                                               args.force, args.max_programs, args.max_monthly_queries)
        report['dry_run'] = args.dry_run
        if not args.dry_run:
            # No configuration mutations on any discovery failure; successful request markers survive.
            if report['errors']:
                programs = old_programs
                for e in report['events']:
                    if e['status'] == 'registered':
                        state['weekly'].pop(str(e['program_id']), None)
                        e['status'] = 'registration_deferred_due_to_errors'
            outputs = {'programs.json': programs, 'discovery_state.json': state, 'discovery_candidates.json': rows}
            # Serialize first; atomic per-file replacement. Whole set rollback on a filesystem failure is not guaranteed.
            serialized = {name: json.dumps(value, ensure_ascii=False, indent=2) + '\n' for name, value in outputs.items()}
            for name, text in serialized.items():
                temp = BASE / (name + '.tmp')
                temp.write_text(text)
                temp.replace(BASE / name)
            (BASE / 'discovery_report.md').write_text(markdown(report, rows))
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1 if report['errors'] else 0
    except Exception:
        print(json.dumps({'error': 'discovery_aborted_inputs_preserved', 'dry_run': args.dry_run}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
