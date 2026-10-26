import copy
from datetime import datetime, timedelta
import json
import socket
import unittest
from unittest.mock import patch, Mock
from urllib.request import Request
import discovery as d

SID = '0pkKacW4UhfnmcYDpV3Lmh'
OTHER = '7io0sLtr6dztSdGzMiCjgq'
FEED = 'https://anchor.fm/s/118cebd14/podcast/rss'
# Known existing feed URL/name from supplied repo. Minimal synthetic channel,
# not a claim that live channel metadata contains these fixture links.
def feed(title='わたあめ職人が語るには', link=''):
    return ('<rss><channel><title>' + title + '</title><link>' + link +
            '</link><item><description>ignored</description></item></channel></rss>').encode()


class FakeNetwork:
    def __init__(self, title='わたあめ職人が語るには', links=None, hits=None):
        self.hits = hits if hits is not None else [{'collectionId': 123, 'collectionName': title, 'feedUrl': FEED}]
        self.links = links or {}
        self.calls = []

    def get(self, url, limit=d.HTML_LIMIT, apple=False):
        self.calls.append(url)
        if apple:
            return json.dumps({'results': self.hits}).encode(), url
        value = self.links.get(url, feed())
        if isinstance(value, Exception):
            raise value
        return value, url


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 26, 23, tzinfo=d.JST)
        self.p = {'id': 13, 'name': 'わたあめ職人が語るには', 'rss': None, 'aliases': [],
                  'spotify_id': SID, 'official_urls': []}
        self.dns = patch('discovery.socket.getaddrinfo', return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.8.8', 443))])
        self.dns.start()
        self.addCleanup(self.dns.stop)

    def run_discovery(self, net=None, programs=None, state=None, candidates=None, **kwargs):
        return d.discover(programs or [self.p], state or {}, candidates or [], self.now,
                          net or FakeNetwork(), max_queries=0, **kwargs)

    def test_title_only_no_register(self):
        programs, state, rows, report = self.run_discovery()
        self.assertIsNone(programs[0]['rss'])
        self.assertEqual(rows[0]['status'], 'pending_review')

    def test_wrong_spotify_id_reject(self):
        self.assertIsNone(d.verify_feed(self.p, FEED, FakeNetwork(links={FEED: feed(link='https://open.spotify.com/show/' + OTHER)})))

    def test_trusted_official_exact_title_accept(self):
        page = 'https://listen.style/p/example'
        self.p['official_urls'] = [page]
        net = FakeNetwork(links={page: ('<link rel="alternate" type="application/rss+xml" href="' + FEED + '">').encode()})
        programs, _, _, report = self.run_discovery(net)
        self.assertEqual(programs[0]['rss'], FEED)
        self.assertEqual(programs[0]['rss_title'], self.p['name'])

    def test_channel_spotify_id_accept(self):
        result = d.verify_feed(self.p, FEED, FakeNetwork(links={FEED: feed(link='https://open.spotify.com/show/' + SID)}))
        self.assertEqual(result['method'], 'channel_spotify_id')

    def test_hosting_page_anchor_accept(self):
        page = 'https://listen.style/p/example'
        net = FakeNetwork(links={FEED: feed(link=page), page: ('<a href="https://open.spotify.com/show/' + SID + '">listen</a>').encode()})
        self.assertEqual(d.verify_feed(self.p, FEED, net)['method'], 'hosting_page_spotify_id')

    def test_episode_description_never_proves_identity(self):
        payload = feed().replace(b'ignored', ('<a href="https://open.spotify.com/show/' + SID + '">x</a>').encode())
        self.assertIsNone(d.verify_feed(self.p, FEED, FakeNetwork(links={FEED: payload})))

    def test_social_channel_not_trusted(self):
        self.assertIsNone(d.verify_feed(self.p, FEED, FakeNetwork(links={FEED: feed(link='https://social.example/post/1')})))

    def test_episode_host_path_not_trusted(self):
        self.assertFalse(d.trusted('https://listen.style/p/example/episode123'))

    def test_registered_not_overwritten(self):
        self.p['rss'] = FEED
        programs, _, _, report = self.run_discovery()
        self.assertEqual(programs[0], self.p)
        self.assertEqual(report['weekly_attempted'], 0)

    def test_ambiguity_hold(self):
        otherfeed = 'https://example.org/podcast/rss'
        page = 'https://listen.style/p/example'
        self.p['official_urls'] = [page]
        net = FakeNetwork(hits=[], links={page: ('<a href="' + FEED + '">rss</a><a href="' + otherfeed + '">rss</a>').encode()})
        programs, _, _, report = self.run_discovery(net)
        self.assertIsNone(programs[0]['rss'])
        self.assertIn('ambiguous_review_required', [e['status'] for e in report['events']])

    def test_first_due(self):
        self.assertTrue(d.weekly_due(None, self.now))
        self.assertTrue(d.monthly_due(None, self.now))

    def test_week_boundary(self):
        self.assertTrue(d.weekly_due((self.now - timedelta(days=7)).isoformat(), self.now))
        self.assertFalse(d.weekly_due((self.now - timedelta(days=7) + timedelta(seconds=1)).isoformat(), self.now))

    def test_month_jst_boundary(self):
        now = datetime(2026, 11, 1, tzinfo=d.JST)
        self.assertTrue(d.monthly_due('2026-10-31T14:59:59+00:00', now))
        self.assertFalse(d.monthly_due('2026-10-31T15:00:00+00:00', now))

    def test_rate_limit_cache(self):
        sleep = Mock()
        apple = d.Apple(FakeNetwork(), clock=lambda: 100, sleep=sleep)
        apple.search('a'); apple.search('a'); apple.search('b')
        self.assertEqual(apple.calls, 2)
        sleep.assert_called_once_with(3.2)

    def test_private_dns_blocked(self):
        for ip in ['127.0.0.1', '10.0.0.1', '169.254.169.254', '::1', 'fc00::1']:
            with self.subTest(ip=ip), patch('discovery.socket.getaddrinfo', return_value=[(2, 1, 6, '', (ip, 80))]):
                with self.assertRaises(ValueError): d.public_url('https://example.org/rss')

    def test_private_redirect_blocked(self):
        with self.assertRaises(ValueError):
            d.Redirects().redirect_request(Request('https://example.org'), None, 302, '', {}, 'http://localhost/rss')

    def test_rebinding_blocked(self):
        with patch('discovery.public_url', return_value='https://example.org'), patch('discovery.socket.getaddrinfo', return_value=[(2, 1, 6, '', ('127.0.0.1', 443))]):
            with self.assertRaises(ValueError): d.pinned_connection(('example.org', 443))

    def test_token_and_userinfo_rejected(self):
        for url in ['file:///etc/passwd', 'http://user:pass@example.org/rss', 'https://example.org/rss?token=secret']:
            with self.assertRaises(ValueError): d.public_url(url)

    def test_dtd_rejected(self):
        for payload in [b'<!DOCTYPE rss><rss/>', '<!DOCTYPE rss><rss/>'.encode('utf-16')]:
            with self.assertRaises(ValueError): d.parse_feed(payload)

    def test_errors_keep_marker_and_history(self):
        self.p['official_urls'] = ['https://listen.style/p/example']
        old = '2026-09-01T00:00:00+09:00'
        history = [{'key': 'manual:1', 'status': 'rejected', 'title': 'manual'}]
        programs, state, rows, report = self.run_discovery(FakeNetwork(links={self.p['official_urls'][0]: ValueError()}), state={'weekly': {'13': old, '999': old}}, candidates=history)
        self.assertEqual(state['weekly']['13'], old)
        self.assertEqual(state['weekly']['999'], old)
        self.assertEqual(rows[0], history[0])
        self.assertTrue(report['errors'])
        self.assertIsNone(programs[0]['rss'])

    def test_alias_normalization(self):
        self.p['aliases'] = ['ABC ラジオ']
        self.assertTrue(d.title_matches(self.p, 'ＡＢＣ   ラジオ'))
        self.assertFalse(d.title_matches(self.p, 'ABC ラジオ２'))

    def test_candidate_dedup_preserves_review(self):
        rows = []
        row = {'apple_id': '1', 'title': 'x', 'feed_url': FEED, 'keywords': ['三豊']}
        d.save_candidate(rows, row, self.now)
        rows[0]['status'] = 'rejected'
        d.save_candidate(rows, dict(row, apple_id='2', keywords=['丸亀']), self.now)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['status'], 'rejected')
        self.assertEqual(len(rows[0]['keywords']), 2)

    def test_safe_markdown(self):
        report = {'at': 'today', 'apple_calls': 0, 'weekly_attempted': 0, 'errors': [], 'events': []}
        text = d.markdown(report, [{'title': '<script>x</script> [x](javascript:x)|`', 'author': 'private@example.org'}])
        self.assertNotIn('<script>', text)
        self.assertNotIn('private@example.org', text)
        self.assertNotIn('[x]', text)

    def test_dry_run_no_files(self):
        from pathlib import Path
        import tempfile
        with tempfile.TemporaryDirectory(dir=d.BASE) as root:
            path = Path(root)
            (path / 'programs.json').write_text('[]')
            before = {p.name: p.read_bytes() for p in path.iterdir()}
            with patch.object(d, 'BASE', path), patch('sys.argv', ['discovery.py', '--dry-run', '--max-monthly-queries', '0']), patch('builtins.print'):
                self.assertEqual(d.main(), 0)
            self.assertEqual(before, {p.name: p.read_bytes() for p in path.iterdir()})

    def test_bounded_monthly_no_registration(self):
        state = {'monthly_queries': {q: d.stamp(self.now) for q in d.KEYWORDS[1:]}}
        with patch('discovery.Apple.search', return_value=[{'collectionId': 123, 'collectionName': '三豊 radio', 'feedUrl': FEED}]):
            programs, newstate, rows, report = d.discover([self.p], state, [], self.now, FakeNetwork(), max_programs=0, max_queries=1)
        self.assertIsNone(programs[0]['rss'])
        self.assertEqual(report['monthly_queries_attempted'], 1)
        self.assertEqual(newstate['monthly_completed'], d.stamp(self.now))
        self.assertEqual(rows[0]['kind'], 'new_program')

    def test_weak_general_metadata_excluded(self):
        with patch('discovery.Apple.search', return_value=[{'collectionId': 123, 'collectionName': 'unrelated', 'artistName': 'other'}]):
            _, _, rows, _ = d.discover([self.p], {}, [], self.now, FakeNetwork(), max_programs=0, max_queries=1)
        self.assertEqual(rows, [])

    def test_global_errors_prevent_configuration_changes(self):
        from pathlib import Path
        import tempfile
        with tempfile.TemporaryDirectory(dir=d.BASE) as root:
            path = Path(root)
            original = [self.p]
            changed = [dict(self.p, rss=FEED)]
            report = {'at': d.stamp(self.now), 'apple_calls': 0, 'weekly_attempted': 1,
                      'errors': [{'scope': 'monthly', 'error': 'failed'}],
                      'events': [{'program_id': 13, 'status': 'registered'}]}
            (path / 'programs.json').write_text(json.dumps(original))
            with patch.object(d, 'BASE', path), patch('sys.argv', ['discovery.py']), patch('builtins.print'), patch.object(d, 'discover', return_value=(changed, {'weekly': {'13': d.stamp(self.now)}}, [], report)):
                self.assertEqual(d.main(), 1)
            self.assertEqual(json.loads((path / 'programs.json').read_text()), original)
            self.assertNotIn('13', json.loads((path / 'discovery_state.json').read_text())['weekly'])
            self.assertIn('registration_deferred_due_to_errors', (path / 'discovery_report.md').read_text())

    def test_updater_uses_recorded_rss_title(self):
        import daily_update
        payload = b'<rss><channel><title>Alias</title><item><title>Episode</title><pubDate>Mon, 26 Oct 2026 01:00:00 GMT</pubDate><link>https://example.org/episode</link></item></channel></rss>'
        p = dict(self.p, rss=FEED, rss_title='Alias')
        with patch.object(daily_update.Network, 'get', return_value=(payload, FEED)):
            self.assertEqual(len(daily_update.fetch(p, self.now)), 1)
            p.pop('rss_title')
            with self.assertRaises(ValueError): daily_update.fetch(p, self.now)


if __name__ == '__main__':
    unittest.main()
