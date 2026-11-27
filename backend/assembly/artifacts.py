"""Fetch and verify the exact published MiniLM checkpoint."""
from hashlib import file_digest
import os
from pathlib import Path
import shutil
from tempfile import NamedTemporaryFile
from urllib.request import urlopen

from eval.rerank.rerank import TOKENIZER_SHA256, WEIGHTS_SHA256

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


def install(directory):
    directory = Path(directory)
    directory.mkdir(exist_ok=True, parents=True)
    for relative, expected in FILES:
        target = directory / Path(relative).name
        if target.exists() and checksum(target) == expected:
            continue
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
