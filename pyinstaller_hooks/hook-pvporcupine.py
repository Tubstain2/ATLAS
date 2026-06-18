"""
PyInstaller hook for sherpa-onnx (replaces former pvporcupine hook).

Collects the sherpa-onnx native shared library and all sub-modules.
The wake word ONNX models live in ~/.atlas/wake_word/ and are NOT bundled —
they are downloaded automatically on first run.
"""

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

datas = collect_data_files('sherpa_onnx')
hiddenimports = collect_submodules('sherpa_onnx')
