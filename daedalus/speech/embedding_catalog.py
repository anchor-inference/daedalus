"""The pinned multilingual text model behind conversation search and memory, installed by the shared
model download manager.

The model is ``sergeyzh/rubert-mini-frida`` (MIT): a seven-layer, 312-wide BERT distilled from FRIDA
for Russian and English. It replaced multilingual E5 small after a bake-off on real conversations:
better on Russian chat queries, level on English ones, and two to three times faster on one CPU core,
which is what decides how long a first index of every past conversation takes. The upstream
repository carries no ONNX file, so the weights are our own export (opset 17, dynamic batch and
sequence, output ``last_hidden_state``) at :data:`MODEL_URL`; the tokenizer is upstream's file at a
pinned revision.
"""
from dataclasses import dataclass
from pathlib import Path

REVISION = "32a82388a9f99c99eca74494731f7ee6e9a9fe3a"
"""The upstream commit the export and the tokenizer were taken from."""

MODEL_URL = "https://github.com/anchor-inference/daedalus/releases/download/models-rubert-mini-frida-1/rubert-mini-frida-fp32.onnx"
"""Where the exported weights are fetched from: the one place to change when they move. A copy with the
pinned checksum placed in the model's directory beforehand is taken instead of a download
(:meth:`daedalus.speech.models.Downloads._seeded`)."""

TOKENIZER_URL = f"https://huggingface.co/sergeyzh/rubert-mini-frida/resolve/{REVISION}/tokenizer.json"


@dataclass(frozen=True)
class ModelFile:
    id: str
    archive: str
    url: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class EmbeddingModel:
    id: str = "rubert-mini-frida"
    label: str = "ruBERT mini FRIDA"
    archive: str = "rubert-mini-frida"
    url: str = MODEL_URL
    dimension: int = 312
    licence: str = "MIT"
    languages: tuple[str, ...] = ("ru", "en")
    query_prefix: str = "search_query: "
    passage_prefix: str = "search_document: "
    """The model was trained with these instructions in front of the text; without them its queries and
    passages land in different places. Memory notes and chat titles take the passage one."""
    max_tokens: int = 256
    """Inputs are cut here. A 1,000-character passage is about 270 tokens, so this trims only the tail, and
    it makes a passage half again as fast to encode as the 512 the model accepts, at no measured loss."""
    min_similarity: float = 0.15
    """The cosine below which a passage is no semantic answer at all. Calibrated on a set built from real
    conversations (547 queries over 6,000 passages): an unrelated pair scores 0.18 at the median and 0.45
    at the 99th percentile, while the best relevant passage of a query scores 0.40 at the median and 0.15
    at the 2nd percentile. No line separates the two without dropping real answers, so this is a floor
    under the noise and the ranking does the rest: up to 0.2 it cost nothing measurable, at 0.25 it began
    to lose chats found by their title, at 0.3 a tenth of them. E5 needed 0.78 because all of its scores
    sit in a narrow cone between 0.75 and 0.95; on this model 0.78 would discard nearly every answer."""
    files: tuple[ModelFile, ...] = (
        ModelFile("rubert-mini-frida", "model.onnx", MODEL_URL, 128791720,
                  "9aa00d5a1aa9e87b52c4cef3d4e3ecde53b982e244b9b9904f6551fa7ae415ab"),
        ModelFile("rubert-mini-frida", "tokenizer.json", TOKENIZER_URL, 2413691,
                  "139995a730139fb2158226e69c5879159c020a71c33c87d65124914663f618ad"),
    )

    @property
    def sha256(self) -> str:
        return self.files[0].sha256

    @property
    def size_bytes(self) -> int:
        return sum(f.size_bytes for f in self.files)

    @property
    def unpacked_bytes(self) -> int:
        return self.size_bytes

    @property
    def space(self) -> str:
        """Everything that makes two vectors comparable. When it changes, the index starts over."""
        return f"{self.id}:{self.sha256[:16]}:mean:{self.dimension}:{self.max_tokens}:int8"


MODEL = EmbeddingModel()
MODELS = (MODEL,)


def get(model_id: str) -> EmbeddingModel:
    if model_id != MODEL.id:
        raise KeyError(model_id)
    return MODEL


def resolve(directory: Path, model: EmbeddingModel) -> None:
    for file in model.files:
        if not (directory / file.archive).is_file():
            raise ValueError("the embedding model is incomplete")
