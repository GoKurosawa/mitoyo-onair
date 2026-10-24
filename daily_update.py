"""Fetch RSS, rank program cards, and refresh the episode section. Standard library only."""
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from email.utils import parsedate_to_datetime
from urllib.request import Request, urlopen
from urllib.parse import urlparse
import xml.etree.ElementTree as ET
import json, re, html

BASE = Path(__file__).resolve().parent
JST = ZoneInfo('Asia/Tokyo')

def rank_key(program, episodes, now):
    dates = [datetime.fromisoformat(e['date']).astimezone(JST) for e in episodes]
    dates = [d for d in dates if d <= now]
    if not dates:
        return (1, 0, 0, program.get('default_order', program['id']))
    latest = max(dates)
    frequency = sum(now - timedelta(days=90) <= d <= now for d in dates)
    return (0, -latest.date().toordinal(), -frequency, program.get('default_order', program['id']))

def fetch(program, now):
    req = Request(program['rss'], headers={'User-Agent': 'MitoyoOnAir/1.0 RSS reader'})
    with urlopen(req, timeout=40) as response:
        payload = response.read(12 * 1024 * 1024 + 1)
    if len(payload) > 12 * 1024 * 1024:
        raise ValueError('RSS size limit exceeded')
    channel = ET.fromstring(payload).find('channel')
    if channel is None or channel.findtext('title', '').strip() != program['name']:
        raise ValueError('RSS title mismatch')
    result = []; seen = set()
    for item in channel.findall('item'):
        try:
            date = parsedate_to_datetime(item.findtext('pubDate', '')).astimezone(JST)
        except (ValueError, TypeError, OverflowError):
            continue
        title = item.findtext('title', '').strip()
        enclosure = item.find('enclosure')
        link = item.findtext('link', '')
        if urlparse(link).scheme not in ('https', 'http'):
            link = enclosure.get('url', '') if enclosure is not None else ''
        if date > now or not title or urlparse(link).scheme not in ('https', 'http'):
            continue
        guid = item.findtext('guid') or link
        if guid in seen:
            continue
        seen.add(guid)
        result.append({'date': date.isoformat(), 'title': title, 'link': link, 'guid': guid, 'show': program['name']})
    if not result:
        raise ValueError('No usable public episodes; retain previous data')
    return result

def main():
    now = datetime.now(JST)
    programs = json.loads((BASE / 'programs.json').read_text())
    cache_path = BASE / 'rss_cache.json'
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    errors = []; successes = 0
    for p in programs:
        if not p.get('rss'):
            continue
        try:
            episodes = fetch(p, now)
            cache[str(p['id'])] = {'rss': p['rss'], 'episodes': episodes}
            successes += 1
        except Exception as exc:
            errors.append({'id': p['id'], 'error': str(exc)})
    if successes == 0:
        raise RuntimeError('All RSS fetches failed; site and cache left unchanged: ' + str(errors))
    def entries(p):
        saved = cache.get(str(p['id']), {})
        return saved.get('episodes', []) if saved.get('rss') == p.get('rss') else []
    ordered = sorted(programs, key=lambda p: rank_key(p, entries(p), now))
    path = BASE / 'index.html'; source = path.read_text()
    match = re.search(r'(<main class="grid" id="grid">)(.*?)(</main>)', source, re.S)
    assert match
    cards = {int(re.search(r'data-program-id="(\d+)"', c)[1]): c for c in re.findall(r'<article class="card".*?</article>', match[2], re.S)}
    assert set(cards) == {p['id'] for p in programs}
    source = source[:match.start()] + match[1] + '\n' + '\n\n'.join(cards[p['id']] for p in ordered) + '\n' + match[3] + source[match.end():]
    episodes = [e for p in programs for e in entries(p) if datetime.fromisoformat(e['date']) <= now]
    episodes.sort(key=lambda e: datetime.fromisoformat(e['date']), reverse=True)
    escape = lambda value: html.escape(value, quote=True)
    items = []
    for e in episodes[:12]:
        items.append('<article class="ep rss-episode"><div class="ep-copy"><p class="from">' + escape(e['show']) + '</p><h3 class="ttl">' + escape(e['title']) + '</h3></div><a class="play" href="' + escape(e['link']) + '" target="_blank" rel="noopener noreferrer">聴く →</a></article>')
    pattern = r'(<section class="feed-section" id="new">.*?<div class="feed">).*?(</div>\s*</div>\s*</section>)'
    source, count = re.subn(pattern, lambda m: m[1] + '\n' + '\n'.join(items) + '\n' + m[2], source, flags=re.S)
    assert count == 1 and len(items) > 0
    path.write_text(source)
    # Only content changes are stored, so unchanged feeds do not create daily commits.
    cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'successful_feeds': successes, 'errors': errors, 'order': [p['name'] for p in ordered], 'displayed': len(items)}, ensure_ascii=False))

if __name__ == '__main__':
    main()
