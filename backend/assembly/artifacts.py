"""Verify the pinned MiniLM checkpoint, downloading only in the demo profile."""
from hashlib import file_digest
import os
from pathlib import Path
import shutil
from tempfile import NamedTemporaryFile
from urllib.request import urlopen

from eval.rerank.rerank import TOKENIZER_SHA256, WEIGHTS_SHA256
from .model_profile import load_model_profile

MODEL_REVISION = "1427fd652930e4ba29e8149678df786c240d8825"
MODEL_URL = ("https://huggingface.co/cross-encoder/mmarco-mMiniLMv2-L12-H384-v1/resolve/"
             + MODEL_REVISION)
FILES = (
    ("tokenizer.json", TOKENIZER_SHA256),
    ("onnx/model_qint8_arm64.onnx", WEIGHTS_SHA256),
)


def checksum(path):
    with path.open("rb") as f:
        return file_digest(f, "sha256").hexdigest()


def install(directory, profile=None):
    if profile is None:
        profile = load_model_profile()
    directory = Path(directory)
    if not profile.is_local:
        directory.mkdir(exist_ok=True, parents=True)
    for relative, expected in FILES:
        target = directory / Path(relative).name
        if target.is_file() and checksum(target) == expected:
            continue
        if profile.is_local:
            raise ValueError(f"Local MiniLM file missing or checksum mismatch: {target.name}")
        with NamedTemporaryFile(dir=directory, delete=False) as output:
            temporary = Path(output.name)
        try:
            with urlopen(MODEL_URL + "/" + relative, timeout=60) as response, temporary.open("wb") as output:
                shutil.copyfileobj(response, output)
            if checksum(temporary) != expected:
                raise ValueError(f"Pinned model checksum did not match: {target.name}")
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
