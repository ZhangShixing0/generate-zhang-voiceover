# 本地声音配置

公开包只含代码、规则、合成示例文字和配置模板。它不包含作者声音、模型权重或私人路径。

## 无 GPU 检查

Python 3.10–3.12。计划脚本只使用标准库；测试和合成辅助处理需要 NumPy 与 SoundFile。在仓库根目录：

```text
python -m pip install -r requirements-dev.txt
python -m unittest discover -s scripts -p "test_*.py"
```

## 配置自己的声音

1. 按 [VoxCPM 官方说明](https://github.com/OpenBMB/VoxCPM#installation) 配置模型运行环境。本适配器沿用 CUDA 路线，已验证的本地版本为 `voxcpm==2.0.3`；它调用提示缓存接口，升级后需重新验证。先按自己的驱动安装匹配的 PyTorch。
2. 复制 `references/voice-profile.example.json` 为 `references/voice-profile.local.json`。
3. 设置 `model_dir` 和 `reference_audio`，只用本人或已授权的声音。`path_base: profile` 表示相对路径以配置 JSON 所在目录为基准。绝对路径同样可用。
4. 计算参考音频的 SHA-256，填入 `reference_sha256`。不要跳过校验；参考音频变化会影响试听审批。
5. 新安装无需预制提示缓存，保持 `prompt_cache_asset: null`；生成器会在输出位置建立自己的缓存。若指定旧缓存，必须同时填写 `prompt_cache_sha256`。只加载自己信任的本地缓存。
6. 保持 `generation.normalize: false`，在正文中明确需要朗读的字句。新声音状态为 `candidate_preview_only`。

计算校验值：

```text
python -c "import hashlib,pathlib; print(hashlib.sha256(pathlib.Path('local/reference.wav').read_bytes()).hexdigest())"
```

检查配置（需要本机已有模型与参考录音，不会开始合成）：

```text
python scripts/generate_voiceover.py --profile references/voice-profile.local.json --validate-only
```

配置优先级：命令行 `--profile`，环境变量 `ZHANG_VOICE_PROFILE`，`voice-profile.local.json`，最后是既有 `voice-profile.json`。已有的个人安装无需迁移或覆盖原声音配置。

模型、参考音频、缓存、计划、清单、试听和最终 WAV 均留在本地。公开仓库的 `.gitignore` 覆盖这些类型；发布前仍应检查实际候选文件。
