---
name: generate-zhang-voiceover
description: 将一份已确认的中文口播稿按稳定单元生成 edge-tts 样片、本人或已授权音色的正式候选，或导入真人录音，并交付词级时间表与字幕。使用 Vosk 做质量检查；不变速、不在声音阶段改词、不制作视频。
---

# 按稿生成口播音频

对用户只有“出口播稿 → 出音频”两个入口。本技能负责第二段：接收同一份可见正文，内部建立朗读计划并执行。自然口语化主要在写稿时完成；普通用户不需要维护计划、哈希和分段文件。

## 接收一份正文

- 接收用户已确认的口播 TXT 或已明确授权朗读的文本。新项目可用 `02-voiceover-final.txt`；已有项目沿用用户正在编辑的路径。
- 用户说编辑好了后重新读取该文件，不能依赖对话旧副本。
- 不要求用户先调用 `convert-kaoyan-script-to-voiceover`。本技能自带计划工具。
- 如果输入仍是产品介绍或提纲，先说明需要整理为可朗读正文；有写稿授权时先把正文写好再继续。不要要求用户管理两份近义稿。
- 发现换词、删句、补卖点或改变 CTA 才能读顺时，把修改放回可见正文并取得对应确认；不能在合成器里暗改。
- 读法、重音、语速和停顿属于本阶段。公式或数字的读法需要改字时，在同一正文中明确呈现实际读法；不保留隐藏的“音频专用稿”。

## 内部准备计划

读取 [voiceover-plan-contract.md](references/voiceover-plan-contract.md)，用本技能目录中的脚本执行：

```text
python scripts/voiceover_plan.py build --text-file <正文.txt> --output <朗读计划.json>
python scripts/voiceover_plan.py validate --text-file <正文.txt> --plan <朗读计划.json>
```

逐单元检查实际文本、顺序、固定搭配、数学读法、语义完整性和停顿。空行是硬边界，不能跨段重组；找不到安全句末切点时回到可见正文调整。参阅 [math-pronunciation.md](references/math-pronunciation.md) 处理数学读法。

计划中的意图和参考状态是检查提示；当前 VoxCPM2 适配器使用单一参考音色，不声称自动执行情绪指令或逐句更换参考音色。语气是否自然以试听为准。

代理实际完成语义检查后可内部运行，不需要用户审批 JSON：

```text
python scripts/voiceover_plan.py approve --text-file <正文.txt> --plan <朗读计划.json>
```

这不代表用户接受声音。正文或规则变化后重新生成计划；计划改变后旧试听审批失效。不能为了省事直接把自动生成状态改成已检查。

## 选择本地声音配置

读取 [local-setup.md](references/local-setup.md)。解析优先级：`--profile` → `ZHANG_VOICE_PROFILE` → `references/voice-profile.local.json` → 已有 `references/voice-profile.json`。

本机已有用户接受的旧档案和参考缓存时继续使用，保留原文件和音色。新安装者提供自己的授权录音、模型路径与校验值，不能把示例配置视为已经可生成的声音。

只支持当前 VoxCPM2 自动合成路线；被用户否决的档案不再用于面向用户的生成。不要因为历史短句曾通过而覆盖后来的完整音频否决。只使用本人或已获授权的参考声音，不把私有录音、缓存或配置上传。

## 先生成试听

在已安装 VoxCPM2 的 Python 环境运行：

```text
python scripts/generate_voiceover.py --text-file <正文.txt> --plan <朗读计划.json> --inspect-chunks --style short-video
python scripts/generate_voiceover.py --text-file <正文.txt> --plan <朗读计划.json> --stage preview --preview-seconds 28 --style short-video --output <试听.wav> --keep-parts
```

默认抽取开头的完整单元，按语速估算约 15–35 秒；实际时长受语速和单元边界影响，短稿可以少于 15 秒。核对样本能代表完整稿的句长、标点和语义复杂度；不能用简单短句代替复杂句的质量检查。

实际合成逐字执行计划单元和段后停顿，不再按字符上限重新切分。禁止模型正文归一化、局部/全局变速和跨单元拼稿。模型错误只能重试或回到可见稿修改；不要用变速掩盖韵律断点。

向用户播放试听，简要请其判断音色、语速连续性、固定搭配停顿、专有名词和语气。只有用户明确接受后才记录：

```text
python scripts/audio_workflow.py --manifest <试听.manifest.json> --approval <试听确认.json> --confirmed-by-user
```

该参数是对已经发生的用户确认的记录，不是自动批准开关。

## 再生成完整音频

```text
python scripts/generate_voiceover.py --text-file <正文.txt> --plan <朗读计划.json> --stage final --approval <试听确认.json> --style short-video --output <完整口播.wav> --keep-parts
```

生成器核对正文、计划、声音配置、生成参数、试听音频及其清单。任一变化都不能沿用旧审批。使用新输出名，不覆盖已生成或已接受的音频。

试听通过不保证整条无误。最终仍需核对开头、中段、结尾、受保护短语、异常语速提示和完整内容。机器检查不能代替听感；参阅 [audio-approval-contract.md](references/audio-approval-contract.md)。

## 真人录制与修复

用户更愿意真实录制时，使用同一份正文接收录音。默认只做用户授权的降噪、均衡、响度与首尾裁切；不为了追时长拉伸局部语速。

自动生成出现问题时，定位单元与前后句，判断是正文、计划还是生成问题。当前 CLI 生成预览或完整版本；单元文件用于定位，不把尚未实现的单元重渲染命令当作功能。若手动替换某个单元，保持原文、更新清单并重新听前后衔接。

如需词级对齐，使用 `scripts/align_units_vosk.py` 与 `scripts/qa_plan_alignment.py`。对齐工具需要另装 Vosk 模型；其结果只是筛查证据。

## 交付

默认只展示可直接播放的 WAV、时长和必要的待听问题。内部保留计划、清单与确认记录，但不要把哈希和技术步骤变成用户必须操作的流程。

声音试听未通过时交付状态写“试听”；完整文件生成成功也不能自动写成“用户已接受”。始终区分文件生成、自动检查和用户听感确认。

## 新版单元管线

先用临时声音做样片，样片和正式音频共用稳定 `unit_id`，但正式音频必须重新测量真实时间：

```text
python scripts/generate_voiceover.py --text-file <正文.txt> --plan <朗读计划.json> --stage draft --output <样片.wav>
```

正式预览需要 Vosk 中文模型。每个单元生成三个候选，按去除停顿的短语语速、识别文本、置信度、无标点停顿、响度、音高和相邻衔接选最优；三个都失败时写 review 报告并停止交付。没有词级对齐证据不会自动通过。`--speed` 已删除，任何阶段都不变速。

阈值和失败证据见 [audio-quality-contract.md](references/audio-quality-contract.md)。

正文更新时，使用 `voiceover_plan.py build --previous-plan` 保留未变单元编号；文字哈希变化的单元才重新生成。每个接受的单元单独记录：

```text
python scripts/audio_workflow.py --manifest <候选.manifest.json> --unit-id u001 --confirmed-by-user
```

正式组装输出逐单元 WAV、`timeline.json`、SRT 和完整 WAV。时间表包含词和短语起止时间、说话区间及 `sfx_safe_intervals`，供视频字幕、动画和音效避让使用。

真人整段录音先导出带编号台本，再用 Vosk 对齐 JSON 切分；转写不一致、边界低置信度或无法落在词边界时停止，不猜切点、不拉伸语速。样片会把脚本文字发送给 edge-tts 在线服务，参考录音、模型、缓存和正式音频仍只留在本地。
