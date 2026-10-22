<div align="center">

<h1>按稿生成口播音频</h1>

<p><img src="docs/hero.svg" alt="按稿试听与生成的两段口播音频流程" width="860"></p>

<p><strong>交一份确认好的中文口播稿，用自己的音色先试听，再生成完整音频。</strong></p>

<p><a href="#交付什么">交付</a> · <a href="#工作流程">流程</a> · <a href="#快速开始">安装</a> · <a href="#安全与隐私">安全</a> · <a href="#边界与许可">边界</a></p>

<p><img src="docs/badges/stage.svg" alt="Stage: Experimental"> <img src="docs/badges/python.svg" alt="Python: 3.10–3.12"></p>

</div>

## 项目解决什么问题

这是 `generate-zhang-voiceover` 的可分发 Skill 包，面向需要中文口播和个人音色的创作者。写稿时就把内容说顺；音频阶段处理读法、分段、停顿与生成，始终使用同一份可见正文。

本项目处于实验阶段。代码与无模型测试可独立运行；真实声音合成需要自己配置模型、CUDA 环境和授权录音，仓库不含作者的声音。

## 交付什么

- 一条短试听 WAV，以及确认后的完整口播 WAV。
- 内部朗读计划：完整语义单元、受保护短语和段后停顿。
- 内部生成清单与试听确认记录，绑定正文、声音配置和实际音频。

普通用户只需交稿、听试听；无需管理 JSON。写稿 Skill 不是依赖，本仓库包含全部计划工具。

## 工作流程

| 用户操作 | Skill 内部处理 | 结果 |
| --- | --- | --- |
| 交一份已确认口播稿 | 检查读法和语义，建立并审核朗读计划 | 同一份正文，无隐藏改稿 |
| 听短试听 | 使用同一参考音色，按完整单元合成 | 试听 WAV |
| 明确接受试听 | 核对正文、计划、配置、参数和试听文件 | 完整 WAV 与可追溯清单 |

VoxCPM2 按计划单元逐字生成，使用计划中的段后停顿。当前适配器不把“意图”字段当成情绪控制指令；自然度仍通过听感判断。

## 快速开始

### 安装与第一次使用

将本仓库作为 Skill 文件夹安装，保留根目录的 `SKILL.md`、`scripts`、`references` 和 `agents`。例如在用户技能目录下克隆：

```text
git clone https://github.com/ZhangShixing0/generate-zhang-voiceover.git
```

不含模型的最短检查路径（在仓库目录运行）。依赖清单见 `requirements.txt` 和 `requirements-dev.txt`：

```text
python -m pip install -r requirements-dev.txt
python scripts/voiceover_plan.py build --text-file examples/voiceover.txt --output outputs/demo-plan.json
python scripts/voiceover_plan.py validate --text-file examples/voiceover.txt --plan outputs/demo-plan.json
python -m unittest discover -s scripts -p "test_*.py"
```

生成计划不会改动正文，也不会自动接受声音。代理需检查计划实际内容后再运行 `approve`。

准备真实合成时，按[本地配置说明](references/local-setup.md)安装运行环境，填写自己的参考声音与模型路径。完整命令和交接规则见 [SKILL.md](SKILL.md)。

给代理的第一条指令可以是：

> 使用 $generate-zhang-voiceover，按这份已确认的口播稿和我的本地声音配置生成试听。我确认后，再生成完整音频。

`--profile` 和 `ZHANG_VOICE_PROFILE` 支持外部配置；已有个人安装可继续使用自己的 `voice-profile.json`。不要把示例配置当成可直接合成的声音。

## 验证与限制

测试覆盖稿件一致性、硬分段、语义单元顺序、停顿范围、试听绑定、配置变更、实际音频被替换后的失效，以及用合成测试信号验证的拼接长度。测试不需要 GPU、模型或真人录音。

测试通过不证明音色自然。本次发布没有新生成真人声音，也没有宣称跨平台 GPU 合成验证。适配器沿用本地 VoxCPM2 路线并调用提示缓存接口；升级引擎后应重新测试。CPU 合成与其他语音引擎未作为本包支持路径。

默认试听按语速估算约 15–35 秒，完整单元边界可能影响实际时长。局部单元文件可用于排查，尚无自动单元重渲染命令。

可选的 Vosk 对齐脚本需要另外安装 Vosk 与中文模型；自动检查无法替代完整试听。

## 安全与隐私

只使用本人或已获授权的参考声音。公开包不包含作者录音、提示缓存、模型权重、个人配置、真实项目稿件或生成音频。模型、参考音频、缓存、计划、清单、试听和最终 WAV 均留在本地。

## 边界与许可

仓库公开不等于授予开源许可：作者尚未指定本项目的开源许可证，保留相应权利。第三方引擎及模型遵循各自许可。

模型侧文本改写与变速在计划模式中禁用。需要改词或删句时先修改可见稿件并确认，再重新生成；不能在声音阶段暗改。

语音引擎来自 [OpenBMB/VoxCPM](https://github.com/OpenBMB/VoxCPM)，本项目提供的是口播交接、计划执行与试听验证流程，不是语音模型本身。


