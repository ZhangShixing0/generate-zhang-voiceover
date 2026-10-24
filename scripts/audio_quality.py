"""Measured audio screening. Vosk timestamps are words, never fabricated characters."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import math
import re

import numpy as np


@dataclass(frozen=True)
class QualityPolicy:
    target_cps: float = 4.5
    target_tolerance: float = 0.20
    adjacent_tolerance: float = 0.10
    max_internal_pause: float = 0.10
    min_confidence: float = 0.75
    max_cer: float = 0.0
    max_level_jump_db: float = 6.0
    max_pitch_jump_semitones: float = 7.0
    phrase_chars: int = 8

    def __post_init__(self):
        for name, value in asdict(self).items():
            if not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise ValueError(f'Invalid quality setting: {name}')
        if self.target_cps <= 0 or self.phrase_chars < 1 or self.min_confidence > 1:
            raise ValueError('Invalid target rate, phrase size or confidence')


def normalized(text):
    return ''.join(c.lower() for c in text if c.isalnum())


def edit_distance(a, b):
    row = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(min(current[-1] + 1, row[j] + 1, row[j - 1] + (ca != cb)))
        row = current
    return row[-1]


def speech_intervals(audio, sample_rate):
    """10 ms energy activity; conservative masks are added at video delivery time."""
    audio = np.asarray(audio, dtype=np.float32)
    if audio.ndim != 1 or sample_rate <= 0 or not np.all(np.isfinite(audio)):
        raise ValueError('Expected finite mono audio and a positive sample rate')
    if not len(audio):
        return []
    frame = max(1, round(sample_rate * .01))
    levels = [float(np.sqrt(np.mean(audio[i:i+frame].astype(np.float64)**2)))
              for i in range(0, len(audio), frame)]
    threshold = max(1e-4, max(levels) * .02)
    active = [value > threshold for value in levels]
    regions = []
    start = None
    for i, state in enumerate(active + [False]):
        if state and start is None:
            start = i * frame
        if not state and start is not None:
            end = min(i * frame, len(audio))
            if regions and start / sample_rate - regions[-1][1] <= .020001:
                regions[-1][1] = end / sample_rate
            else:
                regions.append([start / sample_rate, end / sample_rate])
            start = None
    return regions


def active_duration(intervals, start, end):
    return sum(max(0.0, min(b, end) - max(a, start)) for a, b in intervals)


def acoustic_features(audio, sample_rate):
    intervals = speech_intervals(audio, sample_rate)
    active = np.concatenate([audio[round(a*sample_rate):round(b*sample_rate)] for a,b in intervals]) if intervals else np.array([])
    rms = float(np.sqrt(np.mean(active.astype(np.float64)**2))) if len(active) else 0
    # Downsample only for measurement; delivered audio is never stretched.
    step = max(1, int(sample_rate / 8000))
    signal = np.asarray(audio[::step], dtype=np.float64)
    rate = sample_rate / step
    frame, hop = round(rate*.05), round(rate*.025)
    pitches = []
    for i in range(0, len(signal) - frame + 1, hop):
        chunk = signal[i:i+frame]
        if np.sqrt(np.mean(chunk**2)) < max(1e-4, rms*.1):
            continue
        chunk = (chunk-chunk.mean()) * np.hanning(len(chunk))
        spectrum = np.fft.rfft(chunk, 2*frame)
        corr = np.fft.irfft(abs(spectrum)**2)[:frame]
        lo, hi = max(1, int(rate/450)), min(frame-1, int(rate/65))
        if hi <= lo or corr[0] <= 0:
            continue
        lag = lo + int(np.argmax(corr[lo:hi+1]))
        if corr[lag] / corr[0] >= .55:
            pitches.append(rate/lag)
    return {'rms_dbfs':20*math.log10(max(rms,1e-9)),
            'pitch_hz':float(np.median(pitches)) if pitches else None}


def checked_words(words, duration):
    checked, previous = [], 0.0
    for word in words:
        token = normalized(str(word.get('word','')))
        start, end, conf = (float(word.get(k, float('nan'))) for k in ('start','end','conf'))
        if (not token or not all(math.isfinite(v) for v in (start,end,conf))
                or not 0 <= start < end <= duration+.025 or start < previous-.002 or not 0 <= conf <= 1):
            raise ValueError('Invalid, overlapping, or out-of-range word alignment')
        checked.append({'word':token,'start':start,'end':min(duration,end),'conf':conf})
        previous = end
    return checked


def phrase_alignment(text, words, max_chars=8):
    """Map exact recognized words to script spans; never interpolate character times."""
    expected = normalized(text)
    if ''.join(w['word'] for w in words) != expected:
        return [], []
    positions = [i for i,c in enumerate(text) if c.isalnum()]
    cursor, group, groups, punctuation_gaps = 0, [], [], []
    for i, word in enumerate(words):
        word = dict(word, char_start=cursor, char_end=cursor+len(word['word']))
        cursor = word['char_end']
        group.append(word)
        punctuation = (text[positions[cursor-1]+1:positions[cursor]] if cursor < len(positions)
                       else text[positions[-1]+1:])
        has_pause = bool(re.search(r'[，。！？、；：,.!?;:]', punctuation))
        if has_pause and i+1 < len(words):
            punctuation_gaps.append([word['end'],words[i+1]['start']])
        next_size = len(words[i+1]['word']) if i+1 < len(words) else 0
        if has_pause or sum(len(w['word']) for w in group)+next_size > max_chars or i==len(words)-1:
            first, last = group[0], group[-1]
            end_pos = positions[last['char_end']] if last['char_end'] < len(positions) else len(text)
            groups.append({'text':text[positions[first['char_start']]:end_pos],
                           'start':first['start'],'end':last['end'],
                           'char_count':sum(len(w['word']) for w in group),'words':group})
            group=[]
    return groups, punctuation_gaps


def ratio_difference(left, right):
    return max(left,right)/min(left,right)-1 if min(left,right)>0 else float('inf')


def assess_audio(text, audio, sample_rate, words, policy=None):
    policy = policy or QualityPolicy()
    audio = np.asarray(audio,dtype=np.float32)
    duration = len(audio)/sample_rate
    intervals = speech_intervals(audio,sample_rate)
    reasons=[]
    try:
        words=checked_words(words,duration)
    except (TypeError,ValueError):
        words=[]
        reasons.append('invalid_alignment')
    expected, observed = normalized(text), ''.join(w['word'] for w in words)
    cer=edit_distance(expected,observed)/max(1,len(expected))
    if not words:
        reasons.append('missing_alignment')
    if not expected or cer > policy.max_cer:
        reasons.append('transcript_mismatch')
    if any(w['conf'] < policy.min_confidence for w in words):
        reasons.append('alignment_uncertain')
    phrases, punctuation_gaps = phrase_alignment(text,words,policy.phrase_chars)
    for phrase in phrases:
        speech=active_duration(intervals,phrase['start'],phrase['end'])
        phrase['speech_seconds']=speech
        phrase['chars_per_second']=phrase['char_count']/speech if speech else 0
        part=audio[round(phrase['start']*sample_rate):round(phrase['end']*sample_rate)]
        phrase.update(acoustic_features(part,sample_rate))
        if not speech or abs(phrase['chars_per_second']/policy.target_cps-1)>policy.target_tolerance+1e-8:
            reasons.append('target_rate')
    for left,right in zip(phrases,phrases[1:]):
        if ratio_difference(left['chars_per_second'],right['chars_per_second']) > policy.adjacent_tolerance+1e-8:
            reasons.append('phrase_rate_jump')
    gaps=[]
    if words:
        candidates=[(a[1],b[0]) for a,b in zip(intervals,intervals[1:])]
        candidates += [(a['end'],b['start']) for a,b in zip(words,words[1:])]
        for start,end in candidates:
            midpoint=(start+end)/2
            allowed=any(a-.025 <= midpoint <= b+.025 for a,b in punctuation_gaps)
            if end-start > policy.max_internal_pause+1e-8 and words[0]['start']<=midpoint<=words[-1]['end'] and not allowed:
                gaps.append({'start':start,'end':end,'seconds':end-start})
    if gaps:
        reasons.append('unpunctuated_pause')
    if not intervals:
        reasons.append('empty_speech')
    if len(audio) and np.max(np.abs(audio)) >= .999:
        reasons.append('clipping')
    # A non-exact transcript cannot supply trustworthy script phrase timings even
    # if someone deliberately relaxes CER for triage.
    if not phrases:
        reasons.append('unaligned_script')
    features=acoustic_features(audio,sample_rate)
    score=cer*100+len(gaps)*10+sum(abs(p['chars_per_second']/policy.target_cps-1) for p in phrases)/max(1,len(phrases))
    return {'status':'pass' if not reasons else 'needs_review','reasons':sorted(set(reasons)),
            'score':score,'cer':cer,'recognized_text':observed,'words':words,'phrases':phrases,
            'speech_intervals':intervals,'active_seconds':sum(b-a for a,b in intervals),
            'unexpected_pauses':gaps,'duration_seconds':duration,'policy':asdict(policy),
            'timing_granularity':'word','speech_detector':'energy_10ms',**features}


def transition(left, right, policy):
    a,b=left['phrases'][-1],right['phrases'][0]
    rate=ratio_difference(a['chars_per_second'],b['chars_per_second'])
    level=abs(a['rms_dbfs']-b['rms_dbfs'])
    pitch=abs(12*math.log2(a['pitch_hz']/b['pitch_hz'])) if a['pitch_hz'] and b['pitch_hz'] else None
    passed=(rate<=policy.adjacent_tolerance+1e-8 and level<=policy.max_level_jump_db
            and (pitch is None or pitch<=policy.max_pitch_jump_semitones))
    return {'status':'pass' if passed else 'needs_review','rate_difference':rate,
            'level_jump_db':level,'pitch_jump_semitones':pitch,
            'score':rate+level/6+(pitch or 0)/12}


class CandidateFailure(ValueError):
    pass


def choose_candidates(groups, policy=None):
    """Choose a globally coherent path; a low score never overrides a failed gate."""
    policy=policy or QualityPolicy()
    states=[]
    for unit_index, group in enumerate(groups):
        current=[]
        for index,candidate in enumerate(group):
            report=candidate['quality']
            if report['status']!='pass':
                continue
            if unit_index==0:
                current.append((report['score'],[index],candidate))
            else:
                options=[]
                for score,indices,previous in states:
                    join=transition(previous['quality'],report,policy)
                    if join['status']=='pass':
                        options.append((score+report['score']+join['score'],indices+[index],candidate))
                if options:
                    current.append(min(options,key=lambda row:row[0]))
        if not current:
            raise CandidateFailure(f'No acceptable candidate path at unit {unit_index+1}; review candidates and adjacent joins.')
        states=current
    if not states:
        raise CandidateFailure('No audio units')
    best=min(states,key=lambda row:row[0])
    return [group[index] for group,index in zip(groups,best[1])]
