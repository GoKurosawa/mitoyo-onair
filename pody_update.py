"""Pody/RSS fail-closed reconciler. Production is untouched unless --output index.html.

Only public DOM is read; no audio download, private APIs, or title-based matching.
"""
from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import html
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from urllib.parse import unquote, urlsplit, urlunsplit
import xml.etree.ElementTree as ET

from discovery import Network, parse_feed

BASE = Path(__file__).resolve().parent
ESC = lambda value: html.escape(str(value), quote=True)
MARK = 'PODY_CONNECTED'


def safe_url(value, artwork=False):
    if not isinstance(value, str) or re.search(r'[\x00-\x20\x7f<>"\\]', value):
        raise ValueError('unsafe_url')
    if artwork and re.fullmatch(r'data:image/(?:jpeg|png|webp);base64,[A-Za-z0-9+/=]+', value):
        return value
    p = urlsplit(value)
    if p.scheme.lower() not in ('http', 'https') or not p.hostname or p.username or p.password:
        raise ValueError('unsafe_url')
    return value


def normalize_audio(value):
    """Exactly one unquote; preserve path case, query, fragment, port and slash."""
    safe_url(value)
    p = urlsplit(unquote(value))
    safe_url(urlunsplit(p))
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path, p.query, p.fragment))


def audio_keys(value):
    full = normalize_audio(value)
    keys = {full}
    p = urlsplit(full)
    # Only the known anchor wrapper grammar; never a substring or mp3 basename.
    if p.netloc == 'anchor.fm' and not p.query and not p.fragment:
        m = re.fullmatch(r'/s/[^/]+/podcast/play/\d+/(https://.+)', p.path)
        if m:
            inner = urlsplit(m[1])
            safe_url(m[1])
            keys.add(urlunsplit((inner.scheme.lower(), inner.netloc.lower(), inner.path, inner.query, inner.fragment)))
    return keys


def validate_source(source, programs):
    program = next((p for p in programs if p['id'] == source.get('program_id')), None)
    show = source.get('pody_program_id', '')
    if (source.get('manually_verified') is not True or not source.get('verification_basis')
            or not program or not program.get('rss') or source.get('rss') != program['rss']
            or source.get('rss_title') != program.get('rss_title', program['name'])
            or not re.fullmatch(r'[A-Za-z0-9]+', show)
            or source.get('listing_url') != f'https://pody.jp/player/{show}?kind=article'):
        raise ValueError('unverified_source_or_program_rss_id_mismatch')
    safe_url(source['rss'])
    return program


def fetch_feed(source, network=None):
    payload, _ = (network or Network()).get(source['rss'], 12 * 1024 * 1024)
    title, _ = parse_feed(payload)  # Entity/DTD and size/SSRF safeguards from existing code.
    if title != source['rss_title']:
        raise ValueError('channel_title_mismatch')
    channel = ET.fromstring(payload).find('channel')
    episodes = []
    for i, item in enumerate(channel.findall('item')):
        enclosure = item.find('enclosure')
        if enclosure is None or not enclosure.get('url'):
            continue
        url = enclosure.get('url')
        audio_keys(url)
        try:
            date = parsedate_to_datetime(item.findtext('pubDate', ''))
            if date.tzinfo is None:
                raise ValueError('rss_date_without_timezone')
            date = date.astimezone(timezone.utc).isoformat()
        except (ValueError, TypeError, OverflowError) as exc:
            raise ValueError('rss_date_invalid') from exc
        episodes.append({'guid': item.findtext('guid') or url, 'audio_url': url,
                         'date': date, 'rss_order': i, 'title': item.findtext('title', '')})
    if not episodes:
        raise ValueError('no_rss_enclosures')
    return episodes


# The existing collector is kept unchanged. A second, read-only public DOM pass
# records concrete body evidence which the original observation schema lacks.
BODY_JS = r"""
import fs from 'node:fs/promises';
import {pathToFileURL} from 'node:url';
const [input,output]=process.argv.slice(1);
const observation=JSON.parse(await fs.readFile(input,'utf8'));
const {chromium}=await import(process.env.PLAYWRIGHT_MODULE?pathToFileURL(process.env.PLAYWRIGHT_MODULE).href:'playwright');
const browser=await chromium.launch({headless:true});
try {
 const context=await browser.newContext({locale:'ja-JP'});
 await context.route('**/*',r=>['media','image','font'].includes(r.request().resourceType())?r.abort():r.continue());
 const page=await context.newPage();
 for(const item of observation.articles){
  if(item.detail?.status!=='ok')continue;
  try {
   const r=await page.goto(item.href,{waitUntil:'domcontentloaded',timeout:60000});
   if(r?.status()!==200||page.url()!==item.href)throw Error('body_http_or_redirect');
   await page.waitForSelector('.article-content p',{state:'visible',timeout:15000});
   const evidence=await page.evaluate(()=>{
    const root=document.querySelector('.article-content');
    const paragraphs=[...root.querySelectorAll('p')].filter(e=>e.getClientRects().length&&getComputedStyle(e).visibility!=='hidden').map(e=>e.innerText.trim()).filter(Boolean);
    const text=paragraphs.join('\n');
    return {selector:'.article-content',paragraph_count:paragraphs.length,text_length:text.length,text_excerpt:text.slice(0,240),
      heading:document.querySelector('h1')?.innerText.trim(),canonical:document.querySelector('link[rel="canonical"]')?.href,
      visible:!!root.getClientRects().length,checked_at:new Date().toISOString()};
   });
   item.detail.body_dom={...evidence,status:'ok',final_url:page.url(),http_status:r.status()};
  }catch(e){item.detail.body_dom={status:'failed',error:e.message};observation.errors.push({stage:'body_dom',href:item.href,message:e.message});observation.status='partial';}
 }
}finally{await browser.close();await fs.writeFile(output,JSON.stringify(observation,null,2)+'\n');}
"""


def browser_observation(source):
    env = os.environ.copy()
    module = env.get('PLAYWRIGHT_MODULE') or env.get('NODE_MODULE')
    if module:
        path = Path(module)
        if path.is_dir():
            path = path / ('playwright/index.mjs' if path.name == 'node_modules' else 'index.mjs')
        env['PLAYWRIGHT_MODULE'] = str(path)
    with tempfile.TemporaryDirectory(prefix='pody-') as folder:
        raw, enriched = Path(folder) / 'raw.json', Path(folder) / 'body.json'
        cmd = ['node', str(BASE / 'fetch_pody_browser.mjs'), '--url', source['listing_url'], '--out', str(raw)]
        result = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=600)
        if not raw.exists():
            raise ValueError('browser_no_observation: ' + result.stderr[-800:])
        observation = json.loads(raw.read_text())
        observation['collector_exit_code'] = result.returncode
        if result.returncode != 0:
            return observation
        result = subprocess.run(['node', '--input-type=module', '-e', BODY_JS, str(raw), str(enriched)],
                                env=env, capture_output=True, text=True, timeout=600)
        if result.returncode or not enriched.exists():
            raise ValueError('body_dom_pass_failed: ' + result.stderr[-800:])
        return json.loads(enriched.read_text())


def body_valid(detail, href):
    b = detail.get('body_dom') or {}
    return (b.get('status') == 'ok' and b.get('selector') == '.article-content'
            and b.get('visible') is True and b.get('paragraph_count', 0) >= 2
            and b.get('text_length', 0) >= 200 and len(b.get('text_excerpt', '')) >= 100
            and b.get('http_status') == 200 and b.get('final_url') == href
            and b.get('canonical') == href and b.get('heading') == detail.get('article_heading'))


def reconcile(source, observation, episodes, previous, now):
    issues, accepted = [], []
    report = {'program_id': source['program_id'], 'status': 'held', 'issues': issues,
              'observed': len(observation.get('articles', [])), 'audio_matched': 0, 'verified': 0,
              'matches': [], 'pagination': observation.get('pagination', {}),
              'collector_exit_code': observation.get('collector_exit_code', 0)}
    if observation.get('source_url') != source['listing_url']:
        issues.append({'reason': 'observation_source_id_mismatch'})
        return previous, report
    items = observation.get('articles', [])
    pagination = observation.get('pagination', {})
    if (observation.get('status') != 'ok' or observation.get('errors')
            or pagination.get('complete') is not True
            or pagination.get('expected_count') != len(items) or observation.get('total') != len(items)
            or observation.get('collector_exit_code', 0) != 0):
        issues.append({'reason': 'partial_or_pagination_count_mismatch', 'errors': observation.get('errors', [])})
    hrefs = Counter(a.get('href') for a in items)
    guid_counts = Counter(e['guid'] for e in episodes)
    used_guids = set()
    for item in items:
        href, detail = item.get('href', ''), item.get('detail') or {}
        reasons = []
        if not re.fullmatch(r'https://pody\.jp/player/' + re.escape(source['pody_program_id']) + r'/[A-Za-z0-9]+', href):
            reasons.append('article_program_id_or_url_mismatch')
        if hrefs[href] != 1:
            reasons.append('duplicate_article_url')
        matches = set()
        try:
            for audio in detail.get('audio_urls', []):
                keys = audio_keys(audio)
                matches.update(i for i, e in enumerate(episodes) if keys & audio_keys(e['audio_url']))
        except ValueError:
            reasons.append('invalid_audio_url')
        episode = episodes[next(iter(matches))] if len(matches) == 1 else None
        if episode:
            report['audio_matched'] += 1
            report['matches'].append({'href': href, 'rss_guid': episode['guid'],
                                      'rss_enclosure': episode['audio_url'],
                                      'article_audio_urls': detail.get('audio_urls', []),
                                      'body_dom': detail.get('body_dom'),
                                      'canonical': detail.get('canonical'),
                                      'article_jsonld': detail.get('article_jsonld')})
            if guid_counts[episode['guid']] != 1 or episode['guid'] in used_guids:
                reasons.append('duplicate_rss_episode')
            used_guids.add(episode['guid'])
            if detail.get('rss_guid') and detail['rss_guid'] != episode['guid']:
                reasons.append('rss_guid_mismatch')
        else:
            reasons.append('audio_mismatch' if not matches else 'audio_match_not_unique')
        if (item.get('has_article') is not True or detail.get('status') != 'ok'
                or detail.get('http_status') != 200 or detail.get('article_jsonld') is not True
                or detail.get('jsonld_errors') or not detail.get('article_heading')
                or detail.get('canonical') != href or detail.get('final_url') != href):
            reasons.append('article_public_evidence_missing_or_canonical_mismatch')
        if not body_valid(detail, href):
            reasons.append('dom_body_confirmation_missing')
        if not item.get('title'):
            reasons.append('title_missing')
        if reasons:
            issues.append({'href': href, 'reasons': reasons})
        else:
            old = next((a for a in (previous or {}).get('articles', []) if a['href'] == href), {})
            accepted.append({'href': href, 'title': item['title'], 'guid': episode['guid'],
                             'audio_url': episode['audio_url'], 'rss_date': episode['date'],
                             'rss_order': episode['rss_order'], 'first_seen': old.get('first_seen', now),
                             'program_id': source['program_id']})
    report['verified'] = len(accepted)
    if issues:
        report['retained_previous'] = previous is not None
        return previous, report  # Whole program transaction; never delete on failure.
    report.update(status='updated', total=len(accepted))
    # Retain historic verified entries, but use only this complete snapshot for display/total.
    history = {a['href']: a for a in (previous or {}).get('articles', [])}
    history.update({a['href']: a for a in accepted})
    return {'source': deepcopy(source), 'total': len(accepted), 'confirmed_at': now,
            'active_hrefs': [a['href'] for a in accepted], 'articles': list(history.values())}, report


def selected_articles(cache):
    entries = []
    for saved in cache.get('programs', {}).values():
        for article in saved['articles']:
            if article['href'] in saved['active_hrefs']:
                entries.append((article, saved))
    # Stable RSS date order within the same first_seen batch; later discovery wins.
    entries.sort(key=lambda pair: pair[0].get('rss_order', 0))
    entries.sort(key=lambda pair: (pair[0]['first_seen'], pair[0]['rss_date']), reverse=True)
    return entries[:4]


def render(source_html, design_html, cache):
    styles = re.findall(r'<style>(.*?)</style>', design_html, re.S)
    compact = next(s for s in styles if '.compact-grid{' in s)
    base = next(s for s in styles if '.pody-section{' in s)
    # Extract only article-layout declarations, not old preview/modal styles.
    rules = ''.join(re.findall(r'\.pody-(?:section|heading|intro|foot)[^{]*\{[^{}]*\}', base))
    css = rules + compact + '''
.compact-card{display:grid;grid-template-columns:72px minmax(0,1fr);grid-template-rows:auto 1fr auto;column-gap:16px;row-gap:4px;align-items:start;padding:14px 18px;border-radius:12px;border:1px solid #d7bdab}
.compact-art{width:72px;height:72px;border-radius:8px;object-fit:contain}
.compact-top-copy{min-width:0;display:flex;flex-direction:column;align-items:flex-start}
.compact-show{margin:0;line-height:1.7;font-size:.72rem}
.compact-card h3{grid-column:1 / -1;margin:0;line-height:1.7;font-family:'Noto Sans JP',sans-serif;font-size:.92rem;font-weight:700}
.compact-actions{grid-column:1 / -1;align-self:end;margin-top:auto;padding-top:0;border-top:0;display:block;width:100%;text-align:right}
.compact-read{display:inline-block;background:none;color:#9f3b20;text-decoration:none;font-size:.8rem;font-weight:700;padding:0;line-height:1.8;white-space:normal;text-align:right}
.compact-read:hover{background:none;text-decoration:underline}
@media(min-width:641px){.compact-card{grid-template-rows:auto auto auto;align-content:start}.compact-art{grid-column:1;grid-row:1 / 4}.compact-top-copy{grid-column:2;grid-row:1}.compact-card h3{grid-column:2;grid-row:2}.compact-actions{grid-column:2;grid-row:3;margin-top:0}}
@media(max-width:640px){.compact-card{display:grid;grid-template-columns:56px minmax(0,1fr);column-gap:8px;row-gap:4px;padding:14px 8px}.compact-art{width:56px;height:56px}.compact-show{font-size:.72rem;margin:0;line-height:1.7}.compact-card h3{font-size:.92rem;line-height:1.7;margin:0}.compact-actions{padding-top:0;margin-top:auto;text-align:right}.compact-read{font-size:.8rem;padding:0;white-space:normal;text-align:right}}
    '''
    template = re.search(r'<div class="reading-background-band">(.*?)</section></div>', design_html, re.S)
    if not template:
        raise ValueError('v9_article_block_missing')
    block = template[0]
    cards = {int(re.search(r'data-program-id="(\d+)"', c)[1]): c
             for c in re.findall(r'<article class="card".*?</article>', source_html, re.S)}
    rendered = []
    for a, saved in selected_articles(cache):
        s = saved['source']
        safe_url(a['href']); safe_url(s['listing_url'])
        card = cards[s['program_id']]
        image = re.search(r'<img[^>]*src="([^"]+)"', card)
        if not image:
            raise ValueError('required_artwork_missing')
        artwork = safe_url(html.unescape(image[1]), artwork=True)
        name = s['rss_title']
        rendered.append('<article class="compact-card">'
                        f'<img class="compact-art" src="{ESC(artwork)}" alt="{ESC(name)}" width="52" height="52">'
                         '<div class="compact-top-copy">'
                        f'<a class="compact-show" href="{ESC(s["listing_url"])}" target="_blank" rel="noopener noreferrer">{ESC(name)}（全{int(saved["total"])}件）</a>'
                           '</div>'
                           f'<h3>{ESC(a["title"])}</h3><div class="compact-actions">'
                           f'<a class="compact-read" href="{ESC(a["href"])}" target="_blank" rel="noopener noreferrer">この記事を読む ↗</a></div></article>')
    block, count = re.subn(r'(<div class="compact-grid">).*?(</div><p class="pody-foot">)',
                           lambda m: m[1] + ''.join(rendered) + m[2], block, flags=re.S)
    if count != 1:
        raise ValueError('v9_grid_missing')
    # Validate static application links while preserving original copy/markup byte-for-byte.
    for href in re.findall(r'href="([^"]+)"', block):
        safe_url(html.unescape(href))
    for key in ('STYLE', 'ARTICLES'):
        source_html = re.sub(r'<!-- '+MARK+'_'+key+r'_START -->.*?<!-- '+MARK+'_'+key+r'_END -->', '', source_html, flags=re.S)
    for pid, card in cards.items():
        clean = re.sub(r'<!-- PODY_LINK_START -->.*?<!-- PODY_LINK_END -->', '', card, flags=re.S)
        saved = cache.get('programs', {}).get(str(pid))
        if saved and saved['total']:
            url = safe_url(saved['source']['listing_url'])
            link = f'<!-- PODY_LINK_START --><a class="pf pody" href="{ESC(url)}" target="_blank" rel="noopener noreferrer">Pody（記事で読む）</a><!-- PODY_LINK_END -->'
            clean, n = re.subn(r'(<div class="platforms">.*?)(</div>)', lambda m: m[1]+link+m[2], clean, count=1, flags=re.S)
            if n != 1:
                raise ValueError('program_platforms_missing')
        source_html = source_html.replace(card, clean, 1)
    source_html = source_html.replace('</head>', f'<!-- {MARK}_STYLE_START --><style>{css}</style><!-- {MARK}_STYLE_END --></head>', 1)
    # Never place a new div inside the feed: daily_update's legacy regex stays intact.
    feed = re.search(r'<section class="feed-section" id="new">.*?</section>', source_html, re.S)
    if not feed:
        raise ValueError('production_feed_section_missing')
    source_html = source_html[:feed.end()] + f'<!-- {MARK}_ARTICLES_START -->'+block+f'<!-- {MARK}_ARTICLES_END -->' + source_html[feed.end():]
    return source_html


def write_json(path, value):
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def atomic_write(path, text):
    path = Path(path)
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, delete=False) as f:
        temporary = Path(f.name)
        f.write(text)
    temporary.replace(path)


def run(args):
    sources = json.loads(args.sources.read_text())['sources']
    programs = json.loads((BASE / 'programs.json').read_text())
    old = json.loads(args.cache.read_text()) if args.cache.exists() else {'schema_version': 1, 'programs': {}}
    cache = deepcopy(old)
    now = datetime.now(timezone.utc).isoformat()
    reports = []
    identities = Counter(s.get('program_id') for s in sources)
    for s in sources:
        pid = str(s.get('program_id'))
        try:
            validate_source(s, programs)
            if identities[s['program_id']] != 1:
                raise ValueError('duplicate_source_program_id')
            previous = old['programs'].get(pid)
            if previous and previous['source'] != s:
                raise ValueError('cache_source_changed_requires_manual_review')
            episodes = fetch_feed(s)
            if args.observation:
                raw = json.loads(args.observation.read_text())
                obs = raw if 'source_url' in raw else raw['observations'][s['pody_program_id']]
            else:
                obs = browser_observation(s)
            saved, report = reconcile(s, obs, episodes, previous, now)
            report['observation_mode'] = 'file' if args.observation else 'live_browser_with_body_dom'
            report['observation_at'] = obs.get('observed_at')
            if saved is not None:
                cache['programs'][pid] = saved
        except Exception as exc:
            report = {'program_id': s.get('program_id'), 'status': 'held', 'issues': [{'reason': str(exc)}],
                      'retained_previous': pid in old['programs']}
        reports.append(report)
    # Only explicitly verified, unchanged sources may render historic data.
    allowed = {}
    for s in sources:
        try:
            validate_source(s, programs)
            entry = cache['programs'].get(str(s['program_id']))
            if entry and entry['source'] == s and identities[s['program_id']] == 1:
                allowed[str(s['program_id'])] = entry
        except ValueError:
            pass
    view = {'programs': allowed}
    report = {'schema_version': 1, 'generated_at': now, 'programs': reports,
              'displayed': len(selected_articles(view)), 'output': args.output.name}
    try:
        result = render(args.input.read_text(), args.design.read_text(), view)
    except Exception as exc:
        report['render_error'] = str(exc)
        write_json(args.report, report)
        print(json.dumps(report, ensure_ascii=False))
        return 1
    atomic_write(args.output, result)
    write_json(args.cache, cache)
    write_json(args.report, report)
    print(json.dumps(report, ensure_ascii=False))
    # A held program is nonfatal to other updates and the daily RSS pipeline.
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for flag, default in [('sources', 'pody_sources.json'), ('cache', 'pody_cache.json'),
                          ('report', 'pody_report.json'), ('input', 'index.html'),
                          ('design', 'article_design.html'),
                          ('output', 'index.pody-connected.html')]:
        parser.add_argument('--'+flag, type=Path, default=BASE / default)
    parser.add_argument('--observation', type=Path, help='既存実測JSONを読む。未指定時のみブラウザ実取得。')
    return run(parser.parse_args())


if __name__ == '__main__':
    raise SystemExit(main())
