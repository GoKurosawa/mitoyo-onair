import argparse
from copy import deepcopy
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

import pody_update as p


class PodyUpdateTests(unittest.TestCase):
    def setUp(self):
        self.source = json.loads((p.BASE/'pody_sources.json').read_text())['sources'][0]
        self.programs = json.loads((p.BASE/'programs.json').read_text())
        self.href = 'https://pody.jp/player/lfT9M8syTrRnbeinQJFW/abc'
        self.audio = 'https://cdn.example/Ab.mp3?token=X'
        self.episodes = [{'guid':'g1','audio_url':self.audio,'date':'2026-10-21T00:00:00+00:00','rss_order':0}]
        detail = {'status':'ok','http_status':200,'final_url':self.href,'canonical':self.href,
                  'article_heading':'見出し','article_jsonld':True,'audio_urls':[self.audio],
                  'body_dom':{'status':'ok','selector':'.article-content','visible':True,
                              'paragraph_count':3,'text_length':400,'text_excerpt':'本文'*100,
                              'http_status':200,'final_url':self.href,'canonical':self.href,'heading':'見出し'}}
        self.obs = {'source_url':self.source['listing_url'],'status':'ok','total':1,'errors':[],
                    'pagination':{'complete':True,'expected_count':1},
                    'articles':[{'href':self.href,'title':'全文タイトル','has_article':True,'detail':detail}]}

    def reconcile(self, previous=None, now='2026-10-30T00:00:00+00:00'):
        return p.reconcile(self.source,self.obs,self.episodes,previous,now)

    def test_verified(self):
        saved, report = self.reconcile()
        self.assertEqual(report['verified'],1)
        self.assertEqual(saved['total'],1)

    def test_source_manual_required(self):
        self.source['manually_verified']=False
        with self.assertRaises(ValueError): p.validate_source(self.source,self.programs)

    def test_registered_id(self):
        self.source['program_id']=1
        with self.assertRaises(ValueError): p.validate_source(self.source,self.programs)

    def test_registered_rss(self):
        self.source['rss']='https://evil.example/rss'
        with self.assertRaises(ValueError): p.validate_source(self.source,self.programs)

    def test_observation_id(self):
        self.obs['source_url']='https://pody.jp/player/other?kind=article'
        self.assertIsNone(self.reconcile()[0])

    def test_article_id(self):
        self.obs['articles'][0]['href']=self.href.replace('lfT9M8syTrRnbeinQJFW','other')
        self.assertIsNone(self.reconcile()[0])

    def test_no_filename_match(self):
        self.obs['articles'][0]['detail']['audio_urls']=['https://other.example/Ab.mp3?token=X']
        self.assertIsNone(self.reconcile()[0])

    def test_query_not_dropped(self):
        self.obs['articles'][0]['detail']['audio_urls']=[self.audio.replace('token=X','token=Y')]
        self.assertIsNone(self.reconcile()[0])

    def test_case_only_host_scheme(self):
        self.assertEqual(p.normalize_audio('HTTPS://CDN.EXAMPLE/A.mp3'), 'https://cdn.example/A.mp3')
        self.assertNotEqual(p.normalize_audio('https://cdn.example/a.mp3'),p.normalize_audio('https://cdn.example/A.mp3'))

    def test_unquote_once(self):
        self.assertIn('%2F',p.normalize_audio('https://cdn.example/a%252Fb.mp3'))

    def test_anchor_exact_inner(self):
        wrapper='https://anchor.fm/s/abc/podcast/play/123/https%3A%2F%2FCDN.EXAMPLE%2FA.mp3'
        self.assertIn('https://cdn.example/A.mp3',p.audio_keys(wrapper))

    def test_arbitrary_wrapper_refused(self):
        self.assertEqual(len(p.audio_keys('https://evil.example/https%3A%2F%2Fcdn.example%2FA.mp3')),1)

    def test_duplicate_rss_audio(self):
        self.episodes.append(dict(self.episodes[0],guid='g2'))
        self.assertIsNone(self.reconcile()[0])

    def test_duplicate_article(self):
        self.obs['articles']*=2
        self.obs['total']=self.obs['pagination']['expected_count']=2
        self.assertIsNone(self.reconcile()[0])

    def test_guid_mismatch(self):
        self.obs['articles'][0]['detail']['rss_guid']='wrong'
        self.assertIsNone(self.reconcile()[0])

    def test_missing_body(self):
        del self.obs['articles'][0]['detail']['body_dom']
        saved,report=self.reconcile()
        self.assertIsNone(saved)
        self.assertEqual(report['audio_matched'],1)

    def test_evidence_failures(self):
        original=deepcopy(self.obs)
        for key,value in [('canonical',self.href+'/'),('article_jsonld',False),('status','failed'),('http_status',403),('final_url',self.href+'/')]:
            with self.subTest(key=key):
                self.obs=deepcopy(original)
                self.obs['articles'][0]['detail'][key]=value
                self.assertIsNone(self.reconcile()[0])

    def test_has_article_required(self):
        self.obs['articles'][0]['has_article']=False
        self.assertIsNone(self.reconcile()[0])

    def test_partial_keeps_cache_exactly(self):
        previous,_=self.reconcile()
        self.obs['status']='partial'
        saved,report=self.reconcile(previous)
        self.assertIs(saved,previous)
        self.assertEqual(report['status'],'held')

    def test_total_requires_complete_counts(self):
        for field,value in [('complete',False),('expected_count',2)]:
            with self.subTest(field=field):
                old=deepcopy(self.obs)
                self.obs['pagination'][field]=value
                self.assertIsNone(self.reconcile()[0])
                self.obs=old

    def test_first_seen_saved(self):
        previous,_=self.reconcile()
        saved,_=self.reconcile(previous,'2026-10-31T00:00:00+00:00')
        self.assertEqual(saved['articles'][0]['first_seen'],previous['articles'][0]['first_seen'])

    def test_first_seen_then_rss_date_stable_max4(self):
        saved,_=self.reconcile()
        a=saved['articles'][0]
        saved['articles']=[dict(a,href=str(i),rss_date=f'2026-10-{20+i}T00:00:00+00:00') for i in range(6)]
        saved['articles'].append(dict(a,href='later',first_seen='2026-10-31T00:00:00+00:00'))
        saved['active_hrefs']=[x['href'] for x in saved['articles']]
        result=p.selected_articles({'programs':{'13':saved}})
        self.assertEqual([a['href'] for a,_ in result],['later','5','4','3'])

    def test_preserve_history(self):
        previous,_=self.reconcile()
        previous['articles'].append(dict(previous['articles'][0],href='historic'))
        saved,_=self.reconcile(previous)
        self.assertEqual(len(saved['articles']),2)
        self.assertEqual(saved['total'],1)

    def test_url_security(self):
        for url in ['javascript:alert(1)','https://a.example/"x','https://a.example/\nx','https://u:p@a.example/x','data:image/svg+xml,evil']:
            with self.subTest(url=url):
                with self.assertRaises(ValueError): p.safe_url(url,True)

    def test_render_escaping_markers_legacy_regex(self):
        saved,_=self.reconcile()
        saved['articles'][0]['title']='<script>"&全文'
        cache={'programs':{'13':saved}}
        original=(p.BASE/'index.html').read_text()
        design=(p.BASE/'article_design.html').read_text()
        rendered=p.render(original,design,cache)
        self.assertIn('&lt;script&gt;&quot;&amp;全文',rendered)
        self.assertEqual(rendered.count('class="compact-card"'),1)
        self.assertNotIn('<dialog',rendered)
        self.assertNotIn('preview-note',rendered)
        self.assertIn('PODY_LINK_START',rendered)
        pattern=r'(<section class="feed-section" id="new">.*?<div class="feed">).*?(</div>\s*</div>\s*</section>)'
        changed,n=re.subn(pattern,lambda m:m[1]+'NEW'+m[2],rendered,flags=re.S)
        self.assertEqual(n,1)
        self.assertIn('class="compact-card"',changed)
        rendered2=p.render(rendered,design,cache)
        self.assertEqual(rendered2.count('PODY_LINK_START'),1)
        self.assertEqual(rendered2.count('class="compact-card"'),1)
        for cls in ['pody-intro','pody-foot']:
            self.assertEqual(re.search(fr'<p class="{cls}">.*?</p>',rendered,re.S)[0],re.search(fr'<p class="{cls}">.*?</p>',design,re.S)[0])

    def test_channel_title_and_xml_safety(self):
        class Net:
            def get(self,*a): return b'<rss><channel><title>wrong</title></channel></rss>',a[0]
        with self.assertRaisesRegex(ValueError,'channel_title_mismatch'): p.fetch_feed(self.source,Net())
        class Unsafe:
            def get(self,*a): return b'<!DOCTYPE rss><rss/>',a[0]
        with self.assertRaisesRegex(ValueError,'unsafe_xml'): p.fetch_feed(self.source,Unsafe())

    def test_multi_program_failure_isolated(self):
        with tempfile.TemporaryDirectory() as folder:
            folder=Path(folder)
            s2=dict(self.source,program_id=1)
            sources=folder/'sources.json';sources.write_text(json.dumps({'sources':[self.source,s2]}))
            obs=folder/'obs.json';obs.write_text(json.dumps(self.obs))
            args=argparse.Namespace(sources=sources,cache=folder/'cache.json',report=folder/'report.json',
                                    input=p.BASE/'index.html',design=p.BASE/'article_design.html',output=folder/'candidate.html',observation=obs)
            with patch.object(p,'fetch_feed',return_value=self.episodes): self.assertEqual(p.run(args),0)
            report=json.loads(args.report.read_text())
            self.assertEqual([r['status'] for r in report['programs']],['updated','held'])

    def test_browser_url_argument_and_partial(self):
        def fake(cmd,**kwargs):
            self.assertEqual(cmd[cmd.index('--url')+1],self.source['listing_url'])
            Path(cmd[cmd.index('--out')+1]).write_text(json.dumps(dict(self.obs,status='partial')))
            return subprocess_result()
        def subprocess_result():
            return argparse.Namespace(returncode=2,stderr='')
        with patch.object(p.subprocess,'run',side_effect=fake):
            self.assertEqual(p.browser_observation(self.source)['collector_exit_code'],2)


if __name__=='__main__':
    unittest.main()
