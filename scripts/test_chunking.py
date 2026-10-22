from __future__ import annotations

import unittest
from pathlib import Path

from generate_voiceover import (
    apply_chunk_limit_overrides,
    analyze_pacing,
    build_chunk_manifest,
    clean_text,
    load_profile,
    resolve_chunk_limits,
)


class ChunkingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.profile = load_profile(Path(__file__).resolve().parents[1] / "references/voice-profile.example.json")
        self.min_chars, self.max_chars = resolve_chunk_limits(
            self.profile,
            "short-video",
        )

    def chunks(self, text: str) -> list[dict]:
        return build_chunk_manifest(
            text,
            min_chars=self.min_chars,
            max_chars=self.max_chars,
        )

    def test_blank_lines_are_hard_boundaries(self) -> None:
        text = (
            "我们的原则也很简单。只要能够免费提供给大家，我们就尽量免费。\n\n"
            "这套数学强化系统现在已经免费开放。\n\n"
            "账号由我们统一发放，拿到以后直接登录，不需要自己注册。"
        )
        chunks = self.chunks(text)

        self.assertEqual([chunk["paragraph_index"] for chunk in chunks], [1, 2, 3])
        self.assertEqual(
            [chunk["text"] for chunk in chunks],
            [
                "我们的原则也很简单。只要能够免费提供给大家，我们就尽量免费。",
                "这套数学强化系统现在已经免费开放。",
                "账号由我们统一发放，拿到以后直接登录，不需要自己注册。",
            ],
        )

    def test_pronunciation_safe_rewrite_stays_in_separate_sentences(self) -> None:
        text = (
            "题目做错以后，你在旁边画了一个红叉。"
            "看解析的时候，你也觉得自己已经懂了。"
            "可是，同一类题稍微换个问法，你还是会卡在第一步。"
        )
        chunks = self.chunks(text)

        self.assertEqual(
            chunks[0]["text"],
            "题目做错以后，你在旁边画了一个红叉。",
        )
        self.assertEqual(
            chunks[1]["text"],
            "看解析的时候，你也觉得自己已经懂了。",
        )

    def test_chunks_never_exceed_style_maximum(self) -> None:
        text = (
            "每天做完题以后，要记录题型、题号、正确情况、错误原因、"
            "需要二刷的内容、晚上复盘的重点、第二天需要调整的任务。"
        )
        chunks = self.chunks(text)

        self.assertTrue(chunks)
        self.assertTrue(all(len(chunk["text"]) <= self.max_chars for chunk in chunks))

    def test_chunk_override_can_keep_a_complete_opening_sentence(self) -> None:
        min_chars, max_chars = apply_chunk_limit_overrides(
            self.min_chars,
            self.max_chars,
            None,
            60,
        )
        chunks = build_chunk_manifest(
            "第一个网页发出去以后，有同学提到，每次使用前还要领账号、找密码，真正开始做题之前，多了一步。",
            min_chars=min_chars,
            max_chars=max_chars,
        )
        self.assertEqual(len(chunks), 1)

    def test_chunking_preserves_all_non_whitespace_text(self) -> None:
        text = "第一段需要完整保留。\n\n第二段也不能被静默换词。"
        chunks = self.chunks(text)
        generated_text = "".join(chunk["text"] for chunk in chunks)
        normalized_source = clean_text(text).replace("\n", "")

        self.assertEqual(generated_text, normalized_source)

    def test_pacing_analysis_flags_fast_outlier(self) -> None:
        records = [
            {
                "char_count": 20,
                "duration_seconds": 5.0,
                "chars_per_second": 4.0,
            },
            {
                "char_count": 20,
                "duration_seconds": 5.0,
                "chars_per_second": 4.0,
            },
            {
                "char_count": 20,
                "duration_seconds": 2.5,
                "chars_per_second": 8.0,
            },
        ]

        quality = analyze_pacing(records, self.profile)

        self.assertEqual(quality["status"], "needs-review")
        self.assertIn("pacing-fast", records[2]["warnings"])


if __name__ == "__main__":
    unittest.main()
