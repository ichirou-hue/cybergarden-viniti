"""Словари предметной области: метрики, наборы данных, генераторы, задачи.

Модуль не «понимает» статью, а узнаёт устойчивые обозначения, которые
встречаются в шапках таблиц и в прозе. Всё, что не узнано, остаётся
пустым — это лучше, чем правдоподобная догадка.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .text import fold

# --- числа -----------------------------------------------------------------

#: Число в ячейке: ``.204``, ``0.33``, ``1,234``, ``12.4%``, ``3.1e-2``.
NUMBER_TOKEN = re.compile(
    r"^[+\-\u2212]?(?:\d{1,3}(?:[,\u00a0 ]\d{3})+|\d+(?:[.,]\d+)?|[.,]\d+)(?:[eE][+\-]?\d+)?\s*(?:%|ms|s)?$"
)
#: Отдельный знак «плюс-минус» в ячейке вида ``0.204 ± .005``.
PM = re.compile(r"^[±\u00b1]$")

_ARROWS = re.compile(r"[↑↓⇑⇓▲▼]")


def number_value(token: str) -> float | None:
    """Привести числовой токен к float, не меняя смысла записи."""
    text = token.strip().replace("\u2212", "-").replace("\u00a0", "").replace(" ", "")
    if text.endswith("%"):
        text = text[:-1]
    elif text.endswith("ms"):
        text = text[:-2]
    elif text.endswith("s"):
        text = text[:-1]
    if "," in text and "." in text:
        text = text.replace(",", "")
    elif "," in text:
        if re.fullmatch(r"[+\-]?0,\d+", text):
            text = text.replace(",", ".")
        elif re.fullmatch(r"[+\-]?\d{1,3}(?:,\d{3})+", text):
            text = text.replace(",", "")
        else:
            text = text.replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return None


def is_number(token: str) -> bool:
    return bool(NUMBER_TOKEN.match(token.strip()))


# --- метрики ---------------------------------------------------------------


@dataclass(frozen=True)
class Metric:
    name: str
    group: str = ""
    variant: str = ""
    direction: str = ""
    k: str = ""
    unit_hint: str = ""

    @property
    def key(self) -> tuple[str, str]:
        return (fold(self.name), self.k)


_DEFINITIONS: dict[str, tuple[str, str, str]] = {
    "EM": ("answer quality", "exact match after standard text normalization", "higher_is_better"),
    "F1": ("answer quality", "token-level overlap after standard text normalization", "higher_is_better"),
    "Accuracy": ("answer quality", "fraction of correct predictions", "higher_is_better"),
    "Precision": ("retrieval quality", "fraction of retrieved items that are relevant", "higher_is_better"),
    "Recall": ("retrieval quality", "fraction of relevant items that were retrieved", "higher_is_better"),
    "NDCG": ("retrieval quality", "normalized discounted cumulative gain at k", "higher_is_better"),
    "MRR": ("retrieval quality", "mean reciprocal rank", "higher_is_better"),
    "MAP": ("retrieval quality", "mean average precision", "higher_is_better"),
    "Hit Rate": ("retrieval quality", "fraction of queries with a relevant item in top k", "higher_is_better"),
    "AUC": ("classification quality", "area under the ROC curve", "higher_is_better"),
    "ROUGE-L": ("generation quality", "longest common subsequence overlap", "higher_is_better"),
    "ROUGE-1": ("generation quality", "unigram overlap", "higher_is_better"),
    "ROUGE-2": ("generation quality", "bigram overlap", "higher_is_better"),
    "BLEU": ("generation quality", "n-gram precision with brevity penalty", "higher_is_better"),
    "BLEU-1": ("generation quality", "unigram precision", "higher_is_better"),
    "BLEU-2": ("generation quality", "bigram precision", "higher_is_better"),
    "BLEU-3": ("generation quality", "trigram precision", "higher_is_better"),
    "BLEU-4": ("generation quality", "4-gram precision", "higher_is_better"),
    "BERTScore": ("generation quality", "contextual embedding similarity", "higher_is_better"),
    "Knowledge precision": ("knowledge selection", "precision of selected knowledge", "higher_is_better"),
    "Knowledge recall": ("knowledge selection", "recall of selected knowledge", "higher_is_better"),
    "Knowledge F1": ("knowledge selection", "F1 of selected knowledge", "higher_is_better"),
    "Faithfulness": ("groundedness", "share of answer claims supported by context", "higher_is_better"),
    "Answer relevance": ("groundedness", "relevance of the answer to the question", "higher_is_better"),
    "Context precision": ("retrieval quality", "precision of retrieved context", "higher_is_better"),
    "Context recall": ("retrieval quality", "recall of retrieved context", "higher_is_better"),
    "Latency": ("efficiency", "wall-clock time per query", "lower_is_better"),
    "Score": ("overall quality", "aggregate score as reported", "higher_is_better"),
    "WRR": ("watermark quality", "weighted robustness rate", "higher_is_better"),
    "Perplexity": ("language modelling quality", "perplexity of generated text", "lower_is_better"),
    "CDPA": ("watermark quality", "conditional detection performance", "higher_is_better"),
    "Answer correctness": ("answer quality", "correctness of the answer", "higher_is_better"),
    "Answer semantic similarity": ("answer quality", "semantic similarity to the reference answer", "higher_is_better"),
    "Context relevance": ("retrieval quality", "relevance of retrieved context", "higher_is_better"),
    "Context entity recall": ("retrieval quality", "recall of entities from the reference", "higher_is_better"),
    "Noise robustness": ("robustness", "quality under noisy retrieved context", "higher_is_better"),
    "Context utilization": ("groundedness", "how much of the context is used", "higher_is_better"),
    "Completeness": ("generation quality", "coverage of the reference answer", "higher_is_better"),
    "Helpfulness": ("generation quality", "usefulness of the answer", "higher_is_better"),
    "Correctness": ("answer quality", "correctness of the answer", "higher_is_better"),
    "Fluency": ("generation quality", "linguistic fluency", "higher_is_better"),
    "Coherence": ("generation quality", "logical coherence", "higher_is_better"),
    "Relevance": ("answer quality", "relevance of the answer", "higher_is_better"),
    "Groundedness": ("groundedness", "support of the answer by context", "higher_is_better"),
    "Hallucination rate": ("groundedness", "share of unsupported claims", "lower_is_better"),
    "Win rate": ("comparative quality", "share of pairwise wins", "higher_is_better"),
    "Throughput": ("efficiency", "queries processed per unit of time", "higher_is_better"),
    "Cost": ("efficiency", "monetary cost per query", "lower_is_better"),
    "Tokens per query": ("efficiency", "tokens consumed per query", "lower_is_better"),
    "Steps": ("efficiency", "retrieval and generation steps per query", "lower_is_better"),
    "G-Eval": ("overall quality", "LLM-judged score", "higher_is_better"),
    "RAGAS score": ("overall quality", "aggregate RAGAS score", "higher_is_better"),
    "Success rate": ("task success", "share of successfully completed tasks", "higher_is_better"),
}

_AT_K = r"(?:@\s*|\bat\s*k\s*=\s*|@?)(\d+)"
_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^n?dcg\s*@?\s*(\d+)", re.I), "NDCG"),
    (re.compile(r"^mrr(?:\s*@?\s*(\d+))?", re.I), "MRR"),
    (re.compile(r"^(?:map|mean\s+average\s+precision)(?:\s*@?\s*(\d+))?", re.I), "MAP"),
    (re.compile(r"^hit(?:s)?\s*(?:rate)?\s*@?\s*(\d+)", re.I), "Hit Rate"),
    (re.compile(r"^recall\s*@?\s*(\d+)", re.I), "Recall"),
    (re.compile(r"^precision\s*@?\s*(\d+)", re.I), "Precision"),
    (re.compile(r"^r\s*@\s*(\d+)", re.I), "Recall"),
    (re.compile(r"^p\s*@\s*(\d+)", re.I), "Precision"),
    (re.compile(r"^em\b|^exact\s+match", re.I), "EM"),
    (re.compile(r"^token[\s\-]?f1\b", re.I), "F1"),
    (re.compile(r"^f[\s\-]?1(?:\b|$)|^f1[\s\-]?score", re.I), "F1"),
    (re.compile(r"^wrr\b", re.I), "WRR"),
    (re.compile(r"^ppl\b|^perplexity\b", re.I), "Perplexity"),
    (re.compile(r"^cdpa\b", re.I), "CDPA"),
    (re.compile(r"^answer\s+correctness\b", re.I), "Answer correctness"),
    (re.compile(r"^answer\s+semantic\s+similarity\b", re.I), "Answer semantic similarity"),
    (re.compile(r"^context\s+relevance\b", re.I), "Context relevance"),
    (re.compile(r"^context\s+entity\s+recall\b", re.I), "Context entity recall"),
    (re.compile(r"^noise\s+robustness\b", re.I), "Noise robustness"),
    (re.compile(r"^context\s+utilization\b", re.I), "Context utilization"),
    (re.compile(r"^completeness\b", re.I), "Completeness"),
    (re.compile(r"^helpfulness\b", re.I), "Helpfulness"),
    (re.compile(r"^correctness\b", re.I), "Correctness"),
    (re.compile(r"^fluency\b", re.I), "Fluency"),
    (re.compile(r"^coherence\b", re.I), "Coherence"),
    (re.compile(r"^relevance\b", re.I), "Relevance"),
    (re.compile(r"^groundedness\b", re.I), "Groundedness"),
    (re.compile(r"^hallucination(?:\s+rate)?\b", re.I), "Hallucination rate"),
    (re.compile(r"^win\s+rate\b", re.I), "Win rate"),
    (re.compile(r"^throughput\b", re.I), "Throughput"),
    (re.compile(r"^cost\b", re.I), "Cost"),
    (re.compile(r"^tokens?\s*(?:/|per)\s*query\b", re.I), "Tokens per query"),
    (re.compile(r"^steps?\b", re.I), "Steps"),
    (re.compile(r"^g[\s\-]?eval\b", re.I), "G-Eval"),
    (re.compile(r"^ragas\b", re.I), "RAGAS score"),
    (re.compile(r"^success\s+rate\b", re.I), "Success rate"),
    (re.compile(r"^pass\s*@\s*(\d+)", re.I), "Pass@k"),
    (re.compile(r"^rouge[\s_\-]?l\b|^r[\s\-]?l\b", re.I), "ROUGE-L"),
    (re.compile(r"^rouge[\s_\-]?1\b", re.I), "ROUGE-1"),
    (re.compile(r"^rouge[\s_\-]?2\b", re.I), "ROUGE-2"),
    (re.compile(r"^rouge\b", re.I), "ROUGE-L"),
    (re.compile(r"^bleu[\s_\-]?1\b", re.I), "BLEU-1"),
    (re.compile(r"^bleu[\s_\-]?2\b", re.I), "BLEU-2"),
    (re.compile(r"^bleu[\s_\-]?3\b", re.I), "BLEU-3"),
    (re.compile(r"^bleu[\s_\-]?4\b", re.I), "BLEU-4"),
    (re.compile(r"^bleu", re.I), "BLEU"),
    (re.compile(r"^bert\s*score", re.I), "BERTScore"),
    (re.compile(r"^acc(?:uracy)?\b", re.I), "Accuracy"),
    (re.compile(r"^prec(?:ision)?\b", re.I), "Precision"),
    (re.compile(r"^rec(?:all)?\b", re.I), "Recall"),
    (re.compile(r"^(?:roc[\s\-]?)?auc\b", re.I), "AUC"),
    (re.compile(r"^k[\s\-]?p$", re.I), "Knowledge precision"),
    (re.compile(r"^k[\s\-]?r$", re.I), "Knowledge recall"),
    (re.compile(r"^k[\s\-]?f1$", re.I), "Knowledge F1"),
    (re.compile(r"^faithful(?:ness)?\b", re.I), "Faithfulness"),
    (re.compile(r"^answer\s+relevance\b", re.I), "Answer relevance"),
    (re.compile(r"^context\s+precision\b", re.I), "Context precision"),
    (re.compile(r"^context\s+recall\b", re.I), "Context recall"),
    (re.compile(r"^latency\b|^time\b|^runtime\b", re.I), "Latency"),
    (re.compile(r"^score\b", re.I), "Score"),
)


def parse_metric(text: str) -> Metric | None:
    """Узнать метрику в ячейке шапки: ``EM``, ``F1 ↑``, ``Recall@5 (%)``."""
    raw = _ARROWS.sub(" ", text or "").strip()
    if not raw:
        return None
    unit_hint = "%" if "%" in raw else ""
    cleaned = re.sub(r"\(%?\)", " ", raw)
    cleaned = cleaned.replace("%", " ").strip(" .,:;*")
    if not cleaned:
        return None

    candidates = [cleaned]
    parts = [p for p in re.split(r"[\s/]+", cleaned) if p]
    if len(parts) > 1:
        candidates.append(parts[-1])
        candidates.append(" ".join(parts[-2:]))
        candidates.append(parts[0])

    for candidate in candidates:
        for pattern, name in _PATTERNS:
            match = pattern.match(candidate)
            if not match:
                continue
            k = ""
            if match.groups():
                k = match.group(1) or ""
            group, variant, direction = _DEFINITIONS.get(name, ("", "", ""))
            return Metric(
                name=name,
                group=group,
                variant=variant,
                direction=direction,
                k=k,
                unit_hint=unit_hint,
            )
    return None


def looks_like_metric(text: str) -> bool:
    return parse_metric(text) is not None


# --- наборы данных ---------------------------------------------------------

#: Канонические написания, которые встречаются в шапках и прозе.
DATASETS: tuple[str, ...] = (
    "HotpotQA",
    "2WikiMultihopQA",
    "MuSiQue",
    "Natural Questions",
    "TriviaQA",
    "WebQuestions",
    "SQuAD",
    "MS MARCO",
    "BEIR",
    "FEVER",
    "SciFact",
    "NFCorpus",
    "SciDocs",
    "TREC-COVID",
    "TREC Deep Learning 2019",
    "TREC Deep Learning 2020",
    "Touché",
    "DBpedia-Entity",
    "PubMedQA",
    "BioASQ",
    "MedQA",
    "MedMCQA",
    "MMLU",
    "MMLU-med",
    "MIRAGE",
    "FeB4RAG",
    "CRAG",
    "CovidQA",
    "DelucionQA",
    "Emanual",
    "ExpertQA",
    "FinQA",
    "HAGRID",
    "SimpleQA",
    "TAT-QA",
    "TechQA",
    "FinanceBench",
    "LongHealth",
    "QuaLITY",
    "PopQA",
    "Bamboogle",
    "DROP",
    "WoW",
    "RAGTruth",
    "NewsQA",
    "SearchQA",
    "Dolly",
    "ASQA",
    "ELI5",
    "QAMPARI",
    "KILT",
    "RGB",
    "MultiHop-RAG",
    "DocBench",
    "MMLongBench-Doc",
    "XRAG",
    "SQUAD-E2E",
    "TRUE",
)

_ALIASES = {
    "2wiki": "2WikiMultihopQA",
    "2wikimultihopqa": "2WikiMultihopQA",
    "2wikimultihop": "2WikiMultihopQA",
    "hotpot": "HotpotQA",
    "nq": "Natural Questions",
    "msmarco": "MS MARCO",
    "trecdl19": "TREC Deep Learning 2019",
    "trecdl20": "TREC Deep Learning 2020",
}

_FOLDED = {fold(name): name for name in DATASETS}


def canonical_dataset(text: str) -> str | None:
    """Вернуть каноническое имя набора, если обозначение узнано."""
    cleaned = re.sub(r"[()\[\]]", " ", text or "").strip(" .,:;*")
    key = fold(cleaned)
    if not key:
        return None
    if key in _FOLDED:
        canonical = _FOLDED[key]
        # «Quality» как заголовок раздела не равно датасету QuaLITY.
        if cleaned != canonical and cleaned.istitle() and cleaned.lower() == canonical.lower():
            return None
        return canonical
    compact = key.replace(" ", "").replace("-", "")
    for name in DATASETS:
        if compact == fold(name).replace(" ", "").replace("-", ""):
            if cleaned != name and cleaned.istitle() and cleaned.lower() == name.lower():
                return None
            return name
    if compact in _ALIASES:
        return _ALIASES[compact]
    return None


# --- генераторы ------------------------------------------------------------

GENERATORS: tuple[str, ...] = (
    "GPT-4",
    "GPT-4o",
    "GPT-4o mini",
    "GPT-3.5",
    "GPT-3",
    "LLaMA",
    "Llama-3",
    "Qwen",
    "Mistral",
    "Mixtral",
    "Claude",
    "Gemini",
    "DeepSeek",
    "Vicuna",
    "Flan-T5",
    "T5",
    "Gemma",
    "Phi-3",
    "o1",
    "o3",
    "Command R",
    "Falcon",
    "Pythia",
    "OPT",
    "BLOOM",
)

_GEN_PATTERN = re.compile(
    r"^(gpt[\s\-]?\d|llama|qwen|mistral|mixtral|claude|gemini|deepseek|vicuna|"
    r"flan[\s\-]?t5|t5|gemma|phi[\s\-]?\d|o[13]\b|command[\s\-]?r|falcon|pythia|opt\b|bloom)",
    re.I,
)


def looks_like_generator(text: str) -> bool:
    cleaned = (text or "").strip()
    if not cleaned or len(cleaned) > 60:
        return False
    return bool(_GEN_PATTERN.match(cleaned))


_SIZE_PARENS = re.compile(r"\s*\((\d+(?:\.\d+)?\s*[BMK])\)")


def normalize_generator(text: str) -> str:
    """``FLAN-T5-XL (3B)`` → ``FLAN-T5-XL 3B``, как в эталоне."""
    cleaned = _SIZE_PARENS.sub(r" \1", (text or "").strip())
    return re.sub(r"\s+", " ", cleaned).strip(" *∗")


# --- задачи ----------------------------------------------------------------

TASK_HINTS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"multi[\s\-]?hop", re.I), "multi-hop question answering"),
    (re.compile(r"open[\s\-]?domain question answering|open[\s\-]?domain QA", re.I), "open-domain question answering"),
    (re.compile(r"fact verification|factual verification", re.I), "fact verification"),
    (re.compile(r"dialogue generation|conversational", re.I), "knowledge-grounded dialogue"),
    (re.compile(r"multiple[\s\-]?choice", re.I), "multiple-choice question answering"),
    (re.compile(r"code generation", re.I), "code generation"),
    (re.compile(r"retrieval (?:quality|evaluation|recall)", re.I), "retrieval evaluation"),
    (re.compile(r"question answering|\bQA\b", re.I), "question answering"),
)


def detect_tasks(text: str) -> list[str]:
    """Найти типы задач по прозе; порядок — от частного к общему."""
    found: list[str] = []
    for pattern, name in TASK_HINTS:
        if pattern.search(text or "") and name not in found:
            found.append(name)
    return found
