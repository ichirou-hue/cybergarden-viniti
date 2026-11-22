"""Safe adapter contract for future LLM-backed fillers.

The model is allowed to propose rows, not to define provenance. This module
parses a JSON response, restricts fields to the passport table, and requires
page-local evidence. ``extract.run`` still performs the authoritative verifier
check and replaces ``source_fragment`` with the matched literal text.
"""
import json

from . import verify


EVIDENCE_FIELDS = {"source_page", "source_fragment"}
FORBIDDEN_FIELDS = {"area", "publication_id", "result_id", "_line"}


class LLMContractError(ValueError):
    """The model response cannot be safely treated as filler candidates."""


def _response_rows(response):
    if isinstance(response, str):
        try:
            response = json.loads(response)
        except json.JSONDecodeError as exc:
            raise LLMContractError(f"ответ LLM не является JSON: {exc.msg}") from exc
    if isinstance(response, dict):
        response = response.get("candidates")
    if not isinstance(response, list):
        raise LLMContractError("ответ LLM должен быть массивом candidates")
    return response


def validate_candidates(response, window, spec):
    """Return safe candidate dicts from a model response.

    Only passport columns and the two evidence fields are accepted. The model
    cannot assign publication/result ids or cite a different page than the
    window. Literal evidence is checked here when possible and unconditionally
    checked again by ``verify.verify_record`` in ``extract.run``.
    """
    rows = _response_rows(response)
    allowed = set(spec.get("columns") or ()) | EVIDENCE_FIELDS
    page = window.get("page") if isinstance(window, dict) else None
    items = window.get("items") or [] if isinstance(window, dict) else []
    sources = [item for item in items if isinstance(item, dict)]
    window_page = verify.parse_page(page)
    if window_page is None:
        raise LLMContractError("окно не содержит числовой страницы")
    result = []
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise LLMContractError(f"candidate {index} должен быть объектом")
        forbidden = FORBIDDEN_FIELDS & set(row)
        if forbidden:
            raise LLMContractError(
                f"candidate {index}: запрещенные поля {sorted(forbidden)}")
        unknown = set(row) - allowed
        if unknown:
            raise LLMContractError(
                f"candidate {index}: неизвестные поля {sorted(unknown)}")
        fragment = row.get("source_fragment")
        if not isinstance(fragment, str) or not fragment.strip():
            raise LLMContractError(f"candidate {index}: нужен source_fragment")
        candidate_page = verify.parse_page(row.get("source_page"))
        if candidate_page is None or candidate_page != window_page:
            raise LLMContractError(
                f"candidate {index}: source_page должен быть страницей окна")
        if verify.is_locator(fragment):
            label = verify.locator_label(fragment)
            if not label or not verify.label_on_page(label, sources, window_page):
                raise LLMContractError(
                    f"candidate {index}: locator отсутствует на странице окна")
        elif not any(verify.quote_present(fragment, item.get("quote") or "")
                     for item in sources):
            raise LLMContractError(
                f"candidate {index}: source_fragment отсутствует в окне")
        result.append(dict(row))
    return result


class LLMJsonFiller:
    """Filler adapter around a callable that returns an LLM JSON response.

    ``backend(window, spec)`` may return a JSON string, a list, or
    ``{"candidates": [...]}``. No network or model dependency is built in.
    """

    def __init__(self, backend):
        if not callable(backend):
            raise TypeError("backend должен быть вызываемым")
        self.backend = backend

    def fill(self, window, spec):
        return validate_candidates(self.backend(window, spec), window, spec)
