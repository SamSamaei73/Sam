"""The trusted skill vocabulary and alias normalization.

ONLY this module can mint a canonical skill id. A model may propose a skill
that is not here, but the proposal stays UNVERIFIED and can never be promoted:
it has no canonical id (see ``sam.professional.extract``). Nothing here is a
claim about the owner: the vocabulary is generic and says nothing about who
has which skill. A skill becomes a professional claim only when trusted
ingested evidence mentions it.

Matching is deterministic. All-caps acronyms (``JS``, ``RAG``, ``LLM``) match
case-sensitively so ordinary words ("rag", "tf") never match; everything else
matches case-insensitively on word boundaries. Ambiguous short words that would
false-match ordinary text (a bare ``R``, ``C``, ``Go``) are deliberately absent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

from sam.professional.models import ClaimCategory

_TECH = ClaimCategory.TECHNOLOGY
_SKILL = ClaimCategory.SKILL


@dataclass(frozen=True)
class VocabEntry:
    skill_id: str
    display: str
    kind: ClaimCategory
    aliases: tuple[str, ...] = ()


def _e(skill_id: str, display: str, kind: ClaimCategory, *aliases: str) -> VocabEntry:
    return VocabEntry(skill_id, display, kind, aliases)


_ENTRIES: tuple[VocabEntry, ...] = (
    # ----------------------------------------------------- technologies
    _e("python", "Python", _TECH),
    _e("javascript", "JavaScript", _TECH, "JS", "ECMAScript"),
    _e("typescript", "TypeScript", _TECH, "TS"),
    _e("java", "Java", _TECH),
    _e("cpp", "C++", _TECH),
    _e("csharp", "C#", _TECH),
    _e("golang", "Golang", _TECH),
    _e("rust", "Rust", _TECH),
    _e("sql", "SQL", _TECH),
    _e("bash", "Bash", _TECH, "shell scripting"),
    _e("html", "HTML", _TECH, "HTML5"),
    _e("css", "CSS", _TECH, "CSS3"),
    _e("react", "React", _TECH, "ReactJS", "React.js", "React JS"),
    _e("react-native", "React Native", _TECH, "ReactNative"),
    _e("nextjs", "Next.js", _TECH, "NextJS", "Next JS"),
    _e("vue", "Vue", _TECH, "Vue.js", "VueJS"),
    _e("angular", "Angular", _TECH, "AngularJS"),
    _e("nodejs", "Node.js", _TECH, "NodeJS", "Node JS"),
    _e("tailwind", "Tailwind CSS", _TECH, "TailwindCSS", "Tailwind"),
    _e("vite", "Vite", _TECH),
    _e("fastapi", "FastAPI", _TECH, "Fast API", "Fast-API"),
    _e("django", "Django", _TECH),
    _e("flask", "Flask", _TECH),
    _e("pytorch", "PyTorch", _TECH, "torch", "Py Torch"),
    _e("pytorch-geometric", "PyTorch Geometric", _TECH, "PyG"),
    _e("tensorflow", "TensorFlow", _TECH, "TF", "Tensor Flow"),
    _e("keras", "Keras", _TECH),
    _e("scikit-learn", "scikit-learn", _TECH, "sklearn", "scikit learn"),
    _e("pandas", "Pandas", _TECH),
    _e("numpy", "NumPy", _TECH),
    _e(
        "huggingface", "Hugging Face", _TECH, "HuggingFace", "Hugging Face Transformers"
    ),
    _e("langchain", "LangChain", _TECH),
    _e("llamaindex", "LlamaIndex", _TECH, "Llama Index"),
    _e("docker", "Docker", _TECH),
    _e("kubernetes", "Kubernetes", _TECH, "k8s"),
    _e("terraform", "Terraform", _TECH),
    _e("aws", "AWS", _TECH, "Amazon Web Services"),
    _e("azure", "Azure", _TECH, "Microsoft Azure"),
    _e("gcp", "GCP", _TECH, "Google Cloud", "Google Cloud Platform"),
    _e("mlflow", "MLflow", _TECH, "ML Flow"),
    _e("airflow", "Airflow", _TECH, "Apache Airflow"),
    _e("spark", "Apache Spark", _TECH, "PySpark"),
    _e("git", "Git", _TECH),
    _e("github-actions", "GitHub Actions", _TECH),
    _e("linux", "Linux", _TECH),
    _e("postgresql", "PostgreSQL", _TECH, "Postgres"),
    _e("mongodb", "MongoDB", _TECH, "Mongo"),
    _e("redis", "Redis", _TECH),
    _e("sqlite", "SQLite", _TECH),
    _e("elasticsearch", "Elasticsearch", _TECH),
    _e("graphql", "GraphQL", _TECH),
    _e("jupyter", "Jupyter", _TECH, "Jupyter Notebook"),
    # ------------------------------------------------------------ skills
    _e("machine-learning", "Machine Learning", _SKILL, "ML"),
    _e("deep-learning", "Deep Learning", _SKILL),
    _e("nlp", "Natural Language Processing", _SKILL, "NLP"),
    _e(
        "graph-neural-networks",
        "Graph Neural Networks",
        _SKILL,
        "GNN",
        "GNNs",
        "graph neural network",
    ),
    _e(
        "graph-convolutional-networks",
        "Graph Convolutional Networks",
        _SKILL,
        "GCN",
        "GCNs",
        "graph convolutional network",
    ),
    _e("computer-vision", "Computer Vision", _SKILL),
    _e(
        "llm",
        "Large Language Models",
        _SKILL,
        "LLM",
        "LLMs",
        "large language model",
    ),
    _e(
        "generative-ai",
        "Generative AI",
        _SKILL,
        "Gen AI",
        "GenAI",
        "Gen-AI",
        "Generative Artificial Intelligence",
    ),
    _e(
        "rag",
        "Retrieval-Augmented Generation",
        _SKILL,
        "RAG",
        "retrieval augmented generation",
    ),
    _e(
        "semantic-embeddings",
        "Semantic Embeddings",
        _SKILL,
        "sentence embeddings",
        "text embeddings",
        "semantic embedding",
    ),
    _e(
        "agentic-ai",
        "Agentic AI",
        _SKILL,
        "AI agents",
        "LLM agents",
        "agentic systems",
        "agent systems",
    ),
    _e("prompt-engineering", "Prompt Engineering", _SKILL),
    _e("fine-tuning", "Fine-tuning", _SKILL, "finetuning", "fine tuning"),
    _e("reinforcement-learning", "Reinforcement Learning", _SKILL),
    _e("mlops", "MLOps", _SKILL),
    _e("data-engineering", "Data Engineering", _SKILL),
    _e("rest-apis", "REST APIs", _SKILL, "REST", "RESTful", "REST API"),
    _e("microservices", "Microservices", _SKILL),
    _e("ci-cd", "CI/CD", _SKILL, "continuous integration"),
    _e("api-design", "API Design", _SKILL),
    _e(
        "frontend-development",
        "Frontend Development",
        _SKILL,
        "front-end development",
        "front end development",
    ),
    _e(
        "full-stack-development",
        "Full-Stack Development",
        _SKILL,
        "full stack development",
        "fullstack development",
        "full-stack",
    ),
)

VOCABULARY: dict[str, VocabEntry] = {entry.skill_id: entry for entry in _ENTRIES}

_ACRONYM = re.compile(r"^[A-Z0-9/+#]{2,6}$")


@dataclass(frozen=True)
class Mention:
    entry: VocabEntry
    start: int
    end: int
    text: str


def _normalize(text: str) -> str:
    return re.sub(r"[\s\-_]+", " ", text.strip().lower())


def _alias_forms(entry: VocabEntry) -> tuple[str, ...]:
    return (entry.display, *entry.aliases)


@lru_cache(maxsize=1)
def _exact_index() -> dict[str, VocabEntry]:
    index: dict[str, VocabEntry] = {}
    for entry in _ENTRIES:
        index[_normalize(entry.skill_id)] = entry
        for form in _alias_forms(entry):
            index.setdefault(_normalize(form), entry)
    return index


@lru_cache(maxsize=1)
def _scanner() -> tuple[tuple[re.Pattern[str], VocabEntry], ...]:
    patterns: list[tuple[re.Pattern[str], VocabEntry]] = []
    for entry in _ENTRIES:
        for form in sorted(set(_alias_forms(entry)), key=len, reverse=True):
            flags = 0 if _ACRONYM.match(form) else re.IGNORECASE
            body = re.escape(form).replace(r"\ ", r"[\s\-]+")
            pattern = re.compile(rf"(?<![A-Za-z0-9_]){body}(?![A-Za-z0-9_])", flags)
            patterns.append((pattern, entry))
    return tuple(patterns)


def normalize_skill(text: str) -> VocabEntry | None:
    """Exact, case-insensitive alias lookup (``JS`` -> JavaScript). Returns
    ``None`` for anything not in the trusted vocabulary: never a guess."""

    return _exact_index().get(_normalize(text))


def find_mentions(text: str) -> list[Mention]:
    """Every vocabulary mention in ``text``, longest match first, with
    overlaps removed (so ``React Native`` is not also ``React``)."""

    found: list[Mention] = []
    for pattern, entry in _scanner():
        for match in pattern.finditer(text):
            found.append(Mention(entry, match.start(), match.end(), match.group(0)))
    found.sort(key=lambda m: (m.start, -(m.end - m.start)))
    result: list[Mention] = []
    last_end = -1
    for mention in found:
        if mention.start >= last_end:
            result.append(mention)
            last_end = mention.end
    return result


def mentioned_entries(text: str) -> list[VocabEntry]:
    """Distinct vocabulary entries mentioned in ``text``, in first-mention order."""

    seen: dict[str, VocabEntry] = {}
    for mention in find_mentions(text):
        seen.setdefault(mention.entry.skill_id, mention.entry)
    return list(seen.values())


__all__ = [
    "VOCABULARY",
    "Mention",
    "VocabEntry",
    "find_mentions",
    "mentioned_entries",
    "normalize_skill",
]
