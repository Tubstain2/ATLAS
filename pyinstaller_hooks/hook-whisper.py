"""
PyInstaller hook for openai-whisper.

Collects:
  - whisper/assets/  (mel_filters.npz, gpt2.tiktoken, multilingual.tiktoken)
  - All Python sub-modules (whisper.audio, whisper.model, etc.)
"""

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

datas = collect_data_files('whisper', includes=['assets/*'])
hiddenimports = collect_submodules('whisper')
