import unittest
from datetime import datetime,timedelta
from daily_update import rank_key,program_rank_key,JST

class RankTests(unittest.TestCase):
    def setUp(self):
        self.now=datetime(2026,10,24,10,tzinfo=JST)
    def eps(self,*dates):return [{'date':d} for d in dates]
    def test_same_day_frequency_beats_time(self):
        frequent=self.eps('2026-10-23T01:00:00+09:00','2026-10-20T10:00:00+09:00')
        late=self.eps('2026-10-23T23:00:00+09:00')
        self.assertLess(rank_key({'id':2},frequent,self.now),rank_key({'id':1},late,self.now))
    def test_new_day_beats_frequency(self):
        self.assertLess(rank_key({'id':2},self.eps('2026-10-24T01:00:00+09:00'),self.now),rank_key({'id':1},self.eps('2026-10-23T01:00:00+09:00','2026-10-22T01:00:00+09:00'),self.now))
    def test_unknown_last(self):
        self.assertGreater(rank_key({'id':1},[],self.now),rank_key({'id':2},self.eps('2025-01-01T00:00:00+09:00'),self.now))
    def test_future_excluded(self):
        self.assertEqual(rank_key({'id':1},self.eps('2027-01-01T00:00:00+09:00'),self.now),(1,0,0,1))
    def test_old_frequency_excluded(self):
        self.assertEqual(rank_key({'id':1},self.eps('2026-01-01T00:00:00+09:00'),self.now)[2],0)
    def test_wataame_last_among_rss_even_when_newest(self):
        newest=self.eps('2026-10-24T01:00:00+09:00')
        self.assertGreater(program_rank_key({'id':13,'rss':'https://example.org/a'},newest,self.now),program_rank_key({'id':2,'rss':'https://example.org/b'},[],self.now))
    def test_wataame_before_unlinked(self):
        self.assertLess(program_rank_key({'id':13,'rss':'https://example.org/a'},[],self.now),program_rank_key({'id':2},[],self.now))
    def test_other_rss_still_updated_order(self):
        self.assertLess(program_rank_key({'id':2,'rss':'https://example.org/b'},self.eps('2026-10-24T01:00:00+09:00'),self.now),program_rank_key({'id':3,'rss':'https://example.org/c'},self.eps('2026-10-23T01:00:00+09:00'),self.now))

if __name__=='__main__':unittest.main()
