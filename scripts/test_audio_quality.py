import unittest
import numpy as np
from audio_quality import QualityPolicy, assess_audio, choose_candidates, CandidateFailure


def signal(duration, regions, rate=16000):
    audio=np.zeros(round(duration*rate),dtype=np.float32)
    for start,end in regions:
        a,b=round(start*rate),round(end*rate)
        audio[a:b]=.1*np.sin(2*np.pi*200*np.arange(b-a)/rate)
    return audio


def words(rows):
    return [{'word':t,'start':a,'end':b,'conf':.99} for t,a,b in rows]


class AudioQualityTests(unittest.TestCase):
    def test_punctuation_pause_does_not_reduce_speech_rate(self):
        report=assess_audio('一二三四，五六七八。',signal(3.4,[(.2,1.2),(2.2,3.2)]),16000,
                            words([('一二三四',.2,1.2),('五六七八',2.2,3.2)]),QualityPolicy(target_cps=4))
        self.assertEqual(report['status'],'pass',report['reasons'])
        self.assertAlmostEqual(report['active_seconds'],2)
        self.assertEqual([round(p['chars_per_second'],2) for p in report['phrases']],[4,4])

    def test_intra_word_silence_is_detected_without_fake_character_times(self):
        report=assess_audio('一口气标出。',signal(1.6,[(.1,.6),(.8,1.5)]),16000,
                            words([('一口气标出',.1,1.5)]),QualityPolicy(target_cps=4))
        self.assertIn('unpunctuated_pause',report['reasons'])
        self.assertEqual(report['timing_granularity'],'word')

    def test_adjacent_phrase_acceleration_and_slowdown_fail(self):
        for length in (.8,1.2):
            with self.subTest(length=length):
                report=assess_audio('一二三四，五六七八。',signal(3,[(.1,1.1),(1.4,1.4+length)]),16000,
                    words([('一二三四',.1,1.1),('五六七八',1.4,1.4+length)]),QualityPolicy(target_cps=4))
                self.assertIn('phrase_rate_jump',report['reasons'])

    def test_word_gap_without_punctuation_is_rejected(self):
        report=assess_audio('一口气标出。',signal(1.7,[(.1,.85),(1.01,1.51)]),16000,
            words([('一口气',.1,.85),('标出',1.01,1.51)]),QualityPolicy(target_cps=4))
        self.assertIn('unpunctuated_pause',report['reasons'])

    def test_missing_wrong_or_uncertain_recognition_never_passes(self):
        cases=[[],words([('一二三',.1,1.1)]),[{'word':'一二三四','start':.1,'end':1.1,'conf':.4}]]
        for alignment in cases:
            report=assess_audio('一二三四。',signal(1.2,[(.1,1.1)]),16000,alignment,QualityPolicy(target_cps=4))
            self.assertEqual(report['status'],'needs_review')

    def test_overlap_out_of_bounds_and_nan_alignment_fail_closed(self):
        for alignment in [words([('一二',.1,.7),('三四',.6,1.1)]),words([('一二三四',.1,9)]),words([('一二三四',float('nan'),1)])]:
            report=assess_audio('一二三四',signal(1.2,[(.1,1.1)]),16000,alignment)
            self.assertIn('invalid_alignment',report['reasons'])

    def test_policy_threshold_is_configurable(self):
        audio=signal(2.5,[(.1,1.1),(1.4,2.2)])
        alignment=words([('一二三四',.1,1.1),('五六七八',1.4,2.2)])
        report=assess_audio('一二三四，五六七八。',audio,16000,alignment,
                           QualityPolicy(target_cps=4.5,adjacent_tolerance=.3))
        self.assertEqual(report['status'],'pass',report['reasons'])

    def test_selector_checks_both_sides_instead_of_greedy_local_choice(self):
        def candidate(name,cps,score=0,status='pass',pitch=200):
            phrase={'chars_per_second':cps,'rms_dbfs':-20,'pitch_hz':pitch}
            return {'name':name,'quality':{'status':status,'score':score,'phrases':[phrase]}}
        groups=[[candidate('first',4)],
                [candidate('locally-cheapest',4.4),candidate('continuous',4.1,.1)],
                [candidate('last',3.9)]]
        self.assertEqual([c['name'] for c in choose_candidates(groups)],['first','continuous','last'])
        groups[1]=[candidate('pitch-jump',4,0,pitch=400),candidate('same-voice',4,.2)]
        self.assertEqual(choose_candidates(groups)[1]['name'],'same-voice')

    def test_all_failed_candidates_stop_instead_of_selecting_least_bad(self):
        group=[{'quality':{'status':'needs_review','score':i,'phrases':[]}} for i in range(3)]
        with self.assertRaises(CandidateFailure):
            choose_candidates([group])


if __name__=='__main__':
    unittest.main()
