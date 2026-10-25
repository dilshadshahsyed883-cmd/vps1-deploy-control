"""Unit tests for non-destructive scalable SML duplicate review.

No model download, database mutation, or media deletion is performed.
"""
from pathlib import Path
import sys
import unittest

APP = Path(__file__).resolve().parents[1] / 'apps/self-media-library/part01'
sys.path.insert(0, str(APP))
from duplicate_review import review_duplicates


def asset(ident, sha=None, kind='image', timestamp='2026-10-25T00:00:00', quality=50):
    return {'id': ident, 'type': kind, 'sha256': sha, 'created_at': timestamp,
            'quality': quality, 'width': 1920, 'height': 1080}


def meta(dhash=None, vector=None, version='model-1', quality=60):
    import json
    return {'machine_json': json.dumps({'quality': {'dhash': dhash, 'score': quality}}),
            'embedding_json': json.dumps(vector) if vector is not None else None,
            'model_version': version}


def project(a):
    return {'id': a['id'], 'type': a['type']}


class DuplicateReviewTests(unittest.TestCase):
    def run_review(self, assets, metadata=None, **kwargs):
        return review_duplicates(assets, metadata or {}, project, **kwargs)

    def test_exact_sha_matches_without_ai_model_and_never_deletes(self):
        sha = 'a' * 64
        r = self.run_review([asset('one', sha), asset('two', sha), asset('other', 'b'*64)])
        self.assertEqual(r['total_groups'], 1)
        self.assertEqual(r['groups'][0]['group_size'], 2)
        self.assertEqual(r['groups'][0]['matches'][0]['exact'], True)
        self.assertEqual(r['groups'][0]['matches'][0]['similarity'], 1.0)
        self.assertFalse(r['automatic_deletion'])
        self.assertFalse(r['incomplete'])

    def test_perceptual_thresholds_and_model_version_are_checked(self):
        assets = [asset('a'), asset('b'), asset('far'), asset('wrongmodel'), asset('orthogonal')]
        rows = {
            ('image', 'a'): meta('0000000000000000', [1.,0.]),
            ('image', 'b'): meta('0000000000000001', [1.,0.]),
            ('image', 'far'): meta('ffffffffffffffff', [1.,0.]),
            ('image', 'wrongmodel'): meta('0000000000000000', [1.,0.], version='other'),
            ('image', 'orthogonal'): meta('0000000000000000', [0.,1.]),
        }
        r = self.run_review(assets, rows)
        self.assertEqual(r['total_groups'], 1)
        ids = {r['groups'][0]['asset']['id']} | {x['asset']['id'] for x in r['groups'][0]['matches']}
        self.assertEqual(ids, {'a','b'})
        self.assertGreater(r['comparisons'], 0)

    def test_same_sha_different_media_type_is_not_a_duplicate_pair(self):
        sha = 'c'*64
        r = self.run_review([asset('video',sha,'video'),asset('image',sha,'image')])
        self.assertEqual(r['total_groups'],0)

    def test_pagination_and_quality_recommendation(self):
        sha1, sha2 = '1'*64, '2'*64
        rows = [asset('old1',sha1,timestamp='2026-01-01',quality=1),
                asset('old2',sha1,timestamp='2026-01-01',quality=70),
                asset('new1',sha2,timestamp='2026-10-25',quality=4),
                asset('new2',sha2,timestamp='2026-10-25',quality=8)]
        first = self.run_review(rows,limit=1,offset=0)
        second = self.run_review(rows,limit=1,offset=1)
        self.assertEqual(first['total_groups'],2)
        self.assertEqual(len(first['groups']),1)
        self.assertEqual(first['groups'][0]['recommended_keep'],'new2')
        self.assertEqual(second['groups'][0]['recommended_keep'],'old2')
        self.assertEqual(self.run_review(rows,limit=1,offset=2)['groups'],[])

    def test_comparison_budget_surfaces_partial_coverage(self):
        rows = [asset(str(i)) for i in range(7)]
        metadata = {('image',str(i)): meta('0000000000000000',[1.,float(i)*.001]) for i in range(7)}
        r = self.run_review(rows,metadata,pair_budget=1)
        self.assertTrue(r['incomplete'])
        self.assertLessEqual(r['comparisons'],1)
        self.assertFalse(r['automatic_deletion'])

    def test_invalid_hashes_and_vectors_do_not_create_matches(self):
        rows = [asset('a'),asset('b')]
        metadata={('image','a'):meta('invalid', [1,0]),
                  ('image','b'):meta('0000000000000000', [1,0])}
        self.assertEqual(self.run_review(rows,metadata)['total_groups'],0)

    def test_chunk_index_has_no_false_negative_with_six_bit_errors(self):
        # With seven disjoint chunks, <= 6 bit differences share one full chunk.
        rows = [asset('base'),asset('six-errors')]
        metadata={('image','base'):meta('0000000000000000',[1,0]),
                  ('image','six-errors'):meta('000000000000003f',[1,0])}
        self.assertEqual(self.run_review(rows,metadata)['total_groups'],1)


if __name__ == '__main__':
    unittest.main()
