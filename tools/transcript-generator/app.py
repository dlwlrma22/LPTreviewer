from __future__ import annotations

import json
import hashlib
import random
import re
import shutil
import subprocess
import tempfile
import threading
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api._errors import NoTranscriptFound, TranscriptsDisabled, VideoUnavailable

BASE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = BASE_DIR.parent.parent
STATIC_DIR = BASE_DIR / "static"
GENERATED_DIR = PROJECT_DIR / "generated"
TRANSCRIPT_DIR = GENERATED_DIR / "transcripts"
QUESTION_DIR = GENERATED_DIR / "questions"
BACKUP_DIR = GENERATED_DIR / "backups"
PRODUCTION_BANK = PROJECT_DIR / "melvin-questions.js"
BASE_QUESTIONS = PROJECT_DIR / "reviewer-questions.js"
OLLAMA_URL = "http://localhost:11434"
DEFAULT_MODEL = "qwen3:8b"
OLLAMA_CONTEXT_SIZE = 8192
APPEND_MARKER = "\n  ];\n\n  window.REVIEWER_QUESTIONS"
APPROVAL_LOCK = threading.Lock()

KNOWN_SECTIONS = [
    "Science and Technology", "Mathematics", "Filipino Communication",
    "English Communication", "Philippine History", "Life and Works of Rizal",
    "The Contemporary World", "Art Appreciation", "Ethics", "Understanding the Self",
    "Learners and Learning Principles", "Curriculum, Methods, and Technology",
    "Teaching Profession", "Assessment of Learning", "Field Study and Action Research",
]

app = FastAPI(title="LEPT Transcript Generator", version="2.0.0")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class SessionFields(BaseModel):
    speaker: str = Field(default="Melvin", min_length=1, max_length=100)
    session_number: str = Field(min_length=1, max_length=50)
    transcript_type: str = Field(pattern=r"^(GenEd|ProfEd)$")
    bank_name: str = Field(min_length=1, max_length=150)
    prefix: str = Field(min_length=1, max_length=80)


class ExtractRequest(BaseModel):
    url: str = Field(min_length=1, max_length=500)
    language_code: str | None = Field(default=None, max_length=20)


class SaveRequest(BaseModel):
    session_number: str = Field(min_length=1, max_length=50)
    transcript_type: str = Field(min_length=1, max_length=20)
    transcript: str = Field(min_length=1, max_length=1_000_000)
    filename: str | None = Field(default=None, max_length=120)
    overwrite: bool = False


class GenerateRequest(SessionFields):
    transcript: str = Field(min_length=1, max_length=1_000_000)
    question_count: int = Field(default=30, ge=1, le=100)
    model: str = Field(default=DEFAULT_MODEL, min_length=1, max_length=100)
    accept_fewer: bool = False
    allow_topic_reuse: bool = False
    topic_pool: list[dict[str, Any]] = Field(default_factory=list, max_length=40)


class DraftRequest(SessionFields):
    transcript: str = Field(min_length=1, max_length=1_000_000)
    questions: list[Any] = Field(min_length=1, max_length=100)
    topic_pool: list[dict[str, Any]] = Field(default_factory=list, max_length=40)


class RegenerateRequest(DraftRequest):
    question_index: int = Field(ge=0)
    model: str = Field(default=DEFAULT_MODEL, min_length=1, max_length=100)


class AIValidateRequest(BaseModel):
    transcript: str = Field(min_length=1, max_length=1_000_000)
    transcript_type: str = Field(pattern=r"^(GenEd|ProfEd)$")
    questions: list[Any] = Field(min_length=1, max_length=100)
    model: str = Field(default=DEFAULT_MODEL, min_length=1, max_length=100)


class ApproveRequest(SessionFields):
    transcript: str = Field(min_length=1, max_length=1_000_000)
    questions: list[Any] = Field(min_length=1, max_length=100)
    confirmed: bool = False


def safe_fragment(value: str, fallback: str = "session") -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "-", value).strip("-").lower()
    return cleaned or fallback


def extract_video_id(value: str) -> str:
    parsed = urlparse(value.strip())
    hostname = parsed.netloc.lower().removeprefix("www.")
    if hostname == "youtu.be":
        video_id = parsed.path.strip("/").split("/")[0]
    elif hostname in {"youtube.com", "m.youtube.com", "music.youtube.com"}:
        if parsed.path == "/watch":
            video_id = parse_qs(parsed.query).get("v", [""])[0]
        elif parsed.path.startswith(("/shorts/", "/embed/", "/live/")):
            parts = parsed.path.strip("/").split("/")
            video_id = parts[1] if len(parts) > 1 else ""
        else:
            video_id = ""
    else:
        video_id = ""
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
        raise HTTPException(status_code=400, detail="Enter a valid YouTube video URL.")
    return video_id


def transcript_list(client: YouTubeTranscriptApi, video_id: str) -> Any:
    return client.list(video_id) if hasattr(client, "list") else client.list_transcripts(video_id)


def item_value(item: Any, key: str, default: Any = None) -> Any:
    return item.get(key, default) if isinstance(item, dict) else getattr(item, key, default)


def transcript_lines(transcript: Any) -> list[dict[str, Any]]:
    fetched = transcript.fetch() if hasattr(transcript, "fetch") else transcript
    raw_items = getattr(fetched, "snippets", fetched)
    return [{"start": item_value(item, "start"), "duration": item_value(item, "duration"), "text": item_value(item, "text", "")} for item in raw_items]


def language_info(item: Any) -> dict[str, Any]:
    return {"code": getattr(item, "language_code", ""), "name": getattr(item, "language", "Unknown language"), "is_generated": bool(getattr(item, "is_generated", False))}


def ollama_json(path: str, payload: dict[str, Any] | None = None, timeout: int = 20) -> dict[str, Any]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = Request(f"{OLLAMA_URL}{path}", data=data, headers={"Content-Type": "application/json"}, method="GET" if payload is None else "POST")
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise HTTPException(status_code=502, detail=f"Ollama request failed: {detail}") from exc
    except (URLError, TimeoutError, ConnectionError) as exc:
        raise HTTPException(status_code=503, detail="Ollama is not running. Start Ollama and try again.") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=502, detail="Ollama returned an unreadable response.") from exc


def ensure_ollama_model(model: str) -> None:
    tags = ollama_json("/api/tags", timeout=8)
    installed = {str(item.get("name") or item.get("model") or "") for item in tags.get("models", [])}
    if model not in installed:
        raise HTTPException(status_code=422, detail=f"{model} is not installed. Run: ollama pull {model}")


def parse_model_json(content: str) -> Any:
    cleaned = content.strip()
    fenced = re.search(r"```(?:json)?\s*([\s\S]*?)```", cleaned, re.IGNORECASE)
    if fenced:
        cleaned = fenced.group(1).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=502, detail="Ollama did not return valid JSON. Try generating again.") from exc


def call_ollama(model: str, system: str, user: str, timeout: int = 600, response_schema: dict[str, Any] | None = None) -> Any:
    ensure_ollama_model(model)
    response = ollama_json("/api/chat", {"model": model, "stream": False, "format": response_schema or "json", "think": False, "options": {"temperature": 0.25, "num_ctx": OLLAMA_CONTEXT_SIZE}, "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}, timeout=timeout)
    content = response.get("message", {}).get("content", "")
    if not content:
        raise HTTPException(status_code=502, detail="Ollama returned an empty response.")
    return parse_model_json(content)


def allowed_sections() -> list[str]:
    sections: set[str] = set()
    try:
        sections.update(re.findall(r'"section"\s*:\s*"([^"]+)"', BASE_QUESTIONS.read_text(encoding="utf-8")))
        sections.update(re.findall(r'^\s*\[\s*"([^"]+)"\s*,\s*"', PRODUCTION_BANK.read_text(encoding="utf-8"), re.MULTILINE))
    except OSError:
        pass
    return sorted(sections or KNOWN_SECTIONS)


def normalize_topic_key(value: Any) -> str:
    words = re.findall(r"[a-z0-9]+", str(value or "").casefold())
    return " ".join(words)


def normalized_text(value: Any) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(value or "").casefold()))


STOP_WORDS = {"a", "an", "and", "are", "as", "at", "be", "by", "does", "for", "from", "how", "in", "is", "it", "of", "on", "or", "the", "this", "to", "was", "what", "which", "why", "with"}


def content_tokens(value: Any) -> set[str]:
    return {word for word in normalized_text(value).split() if len(word) > 2 and word not in STOP_WORDS}


def similarity(left: Any, right: Any) -> float:
    a, b = content_tokens(left), content_tokens(right)
    return len(a & b) / len(a | b) if a and b else 0.0


def text_supported(statement: Any, transcript: str, minimum_overlap: float = 0.72) -> bool:
    phrase, source = normalized_text(statement), normalized_text(transcript)
    if not phrase or not source:
        return False
    if phrase in source:
        return True
    words = content_tokens(phrase)
    return bool(words) and len(words & content_tokens(source)) / len(words) >= minimum_overlap


def normalize_draft_question(item: Any) -> dict[str, Any]:
    if isinstance(item, dict):
        return {
            "section": item.get("section"),
            "topic_key": normalize_topic_key(item.get("topic_key")),
            "question": item.get("question"),
            "options": item.get("options"),
            "correct_answer": item.get("correct_answer", item.get("answer")),
            "explanation": item.get("explanation"),
            "source_evidence": item.get("source_evidence"),
            "validation_override": bool(item.get("validation_override", False)),
        }
    if isinstance(item, list) and len(item) == 5:
        return {"section": item[0], "topic_key": "", "question": item[1], "options": item[2], "correct_answer": item[3], "explanation": item[4], "source_evidence": "", "validation_override": False}
    return {}


def question_to_tuple(item: Any) -> list[Any]:
    question = normalize_draft_question(item)
    return [question.get("section"), question.get("question"), question.get("options"), question.get("correct_answer"), question.get("explanation")]


def question_errors(questions: list[Any], transcript: str = "", sections: list[str] | None = None) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []
    seen_stems: dict[str, int] = {}
    seen_topics: dict[str, int] = {}
    normalized_questions: list[dict[str, Any]] = []
    valid_sections = sections or allowed_sections()
    for index, raw_item in enumerate(questions):
        def error(code: str, field: str, message: str) -> None:
            errors.append({"index": index, "code": code, "field": field, "message": message})
        if not isinstance(raw_item, dict):
            error("SCHEMA", "schema", "Draft question must use the internal named-field schema.")
            continue
        item = normalize_draft_question(raw_item)
        normalized_questions.append(item)
        section, question, options = item["section"], item["question"], item["options"]
        answer, explanation = item["correct_answer"], item["explanation"]
        topic_key, evidence = item["topic_key"], item["source_evidence"]
        if not isinstance(section, str) or not section.strip():
            error("SECTION_REQUIRED", "section", "Section is required.")
        elif section not in valid_sections:
            error("INVALID_SECTION", "section", f'"{section}" is not an allowed reviewer section.')
        if not topic_key:
            error("TOPIC_REQUIRED", "topic_key", "A normalized topic key is required.")
        elif topic_key in seen_topics:
            other = seen_topics[topic_key]
            error("CONCEPT_DUPLICATE", "topic_key", f"Question {index + 1} substantially overlaps Question {other + 1}: both test {topic_key}.")
        else:
            seen_topics[topic_key] = index
        if not isinstance(question, str) or not question.strip():
            error("QUESTION_REQUIRED", "question", "Question text is required.")
        else:
            normalized = re.sub(r"\s+", " ", question).strip().casefold()
            if normalized in seen_stems:
                error("DUPLICATE_QUESTION", "question", f"Duplicates question {seen_stems[normalized] + 1}.")
            else:
                seen_stems[normalized] = index
        if not isinstance(options, list):
            error("OPTIONS_SCHEMA", "options", "Options must be an array.")
        elif len(options) != 4:
            error("OPTIONS_COUNT", "options", "Exactly four options are required.")
        else:
            for option_index, option in enumerate(options):
                if not isinstance(option, str) or not option.strip():
                    error("BLANK_OPTION", f"option-{option_index}", f"Option {chr(65 + option_index)} is required.")
            if len({str(option).strip().casefold() for option in options}) != len(options):
                error("DUPLICATE_OPTIONS", "options", "Options must not be duplicated within a question.")
            elif any(similarity(options[a], options[b]) >= 0.9 for a in range(4) for b in range(a + 1, 4)):
                error("TRIVIAL_OPTION_VARIANTS", "options", "Options contain trivial or near-duplicate variants.")
        if isinstance(answer, bool) or not isinstance(answer, int):
            error("ANSWER_TYPE", "correct_answer", "Correct answer must be an integer.")
        elif answer < 0 or answer > 3:
            error("ANSWER_RANGE", "correct_answer", "Correct answer must be between 0 and 3.")
        elif isinstance(options, list) and len(options) == 4 and isinstance(evidence, str) and evidence.strip() and not text_supported(options[answer], evidence, 0.3):
            error("ANSWER_GROUNDING", "correct_answer", "The correct option is not supported closely enough by the supplied source evidence.")
        if not isinstance(explanation, str) or not explanation.strip():
            error("EXPLANATION_REQUIRED", "explanation", "Explanation is required.")
        if not isinstance(evidence, str) or not evidence.strip():
            error("SOURCE_EVIDENCE_REQUIRED", "source_evidence", "Source evidence is required.")
        elif transcript and not text_supported(evidence, transcript, 0.78):
            error("SOURCE_EVIDENCE_UNSUPPORTED", "source_evidence", "Source evidence could not be matched closely to the transcript.")
        if isinstance(explanation, str) and explanation.strip() and isinstance(evidence, str) and evidence.strip() and not text_supported(explanation, evidence, 0.35):
            error("EXPLANATION_GROUNDING", "explanation", "The explanation expands beyond the transcript or source evidence.")
    for index, current in enumerate(normalized_questions):
        for other_index in range(index):
            other = normalized_questions[other_index]
            if current.get("topic_key") == other.get("topic_key"):
                continue
            evidence_overlap = similarity(current.get("source_evidence"), other.get("source_evidence"))
            question_overlap = similarity(current.get("question"), other.get("question"))
            if evidence_overlap >= 0.92 or (evidence_overlap >= 0.76 and question_overlap >= 0.48):
                errors.append({"index": index, "code": "CONCEPT_DUPLICATE", "field": "topic_key", "message": f"Question {index + 1} substantially overlaps Question {other_index + 1}: their question meaning and source evidence are too similar."})
    return errors


def existing_catalog() -> dict[str, Any]:
    source = PRODUCTION_BANK.read_text(encoding="utf-8")
    base_source = BASE_QUESTIONS.read_text(encoding="utf-8")
    return {
        "banks": re.findall(r'\bbank:\s*"([^"]+)"', source),
        "prefixes": re.findall(r'\bprefix:\s*"([^"]+)"', source),
        "base_ids": re.findall(r'"id"\s*:\s*"([^"]+)"', base_source),
    }


def run_question_bank_check(bank_path: Path = PRODUCTION_BANK) -> dict[str, Any]:
    node_script = r"""
const path = require('path'); global.window = {};
require(path.resolve(process.argv[1])); require(path.resolve(process.argv[2]));
const questions = window.REVIEWER_QUESTIONS || [], seen = new Set(), duplicateIds = [], malformed = [];
questions.forEach((q, index) => {
  if (seen.has(q.id)) duplicateIds.push(q.id); else seen.add(q.id);
  if (!q || typeof q.id !== 'string' || !q.id || typeof q.bank !== 'string' || !q.bank || typeof q.section !== 'string' || !q.section || typeof q.question !== 'string' || !q.question || !Array.isArray(q.options) || q.options.length !== 4 || q.options.some(o => typeof o !== 'string' || !o.trim()) || !Number.isInteger(q.answer) || q.answer < 0 || q.answer > 3 || typeof q.explanation !== 'string' || !q.explanation.trim()) malformed.push(index);
});
console.log(JSON.stringify({ count: questions.length, duplicateIds, malformed }));
"""
    try:
        syntax = subprocess.run(["node", "--check", str(bank_path)], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20, check=False)
        if syntax.returncode != 0:
            raise ValueError(syntax.stderr.strip() or "JavaScript syntax check failed.")
        result = subprocess.run(["node", "-e", node_script, str(BASE_QUESTIONS), str(bank_path)], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20, check=False)
    except FileNotFoundError as exc:
        raise ValueError("Node.js is required for final JavaScript validation.") from exc
    except subprocess.TimeoutExpired as exc:
        raise ValueError("Final JavaScript validation timed out.") from exc
    if result.returncode != 0:
        raise ValueError(result.stderr.strip() or "Question bank could not be executed safely.")
    try:
        report = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError("Final JavaScript validation returned an invalid report.") from exc
    if report["duplicateIds"]:
        raise ValueError(f"Duplicate question IDs: {', '.join(report['duplicateIds'])}")
    if report["malformed"]:
        raise ValueError(f"Malformed questions at indexes: {report['malformed']}")
    source = bank_path.read_text(encoding="utf-8")
    banks = re.findall(r'\bbank:\s*"([^"]+)"', source)
    prefixes = re.findall(r'\bprefix:\s*"([^"]+)"', source)
    duplicate_banks = sorted({value for value in banks if banks.count(value) > 1})
    duplicate_prefixes = sorted({value for value in prefixes if prefixes.count(value) > 1})
    if duplicate_banks:
        raise ValueError(f"Duplicate banks: {', '.join(duplicate_banks)}")
    if duplicate_prefixes:
        raise ValueError(f"Duplicate prefixes: {', '.join(duplicate_prefixes)}")
    report["banks"] = banks
    report["prefixes"] = prefixes
    return report


def target_answer_positions(count: int, seed: str) -> list[int]:
    positions = [index % 4 for index in range(count)]
    numeric_seed = int(hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16], 16)
    random.Random(numeric_seed).shuffle(positions)
    return positions


def redistribute_answers(questions: list[Any], seed: str) -> list[dict[str, Any]]:
    targets = target_answer_positions(len(questions), seed)
    redistributed: list[dict[str, Any]] = []
    for index, raw_item in enumerate(questions):
        item = normalize_draft_question(raw_item)
        options, answer = item.get("options"), item.get("correct_answer")
        if isinstance(options, list) and len(options) == 4 and isinstance(answer, int) and not isinstance(answer, bool) and 0 <= answer <= 3:
            correct_text = options[answer]
            reordered = list(options)
            reordered.pop(answer)
            reordered.insert(targets[index], correct_text)
            item["options"] = reordered
            item["correct_answer"] = targets[index]
        redistributed.append(item)
    return redistributed


def validation_report(bank_name: str, prefix: str, questions: list[Any], transcript: str = "", transcript_type: str = "GenEd") -> dict[str, Any]:
    sections = allowed_sections()
    errors = question_errors(questions, transcript, sections)
    general: list[str] = []
    clean_bank, clean_prefix = bank_name.strip(), prefix.strip()
    if not clean_bank:
        general.append("Bank name is required.")
    if not clean_prefix:
        general.append("Prefix is required.")
    elif not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", clean_prefix):
        general.append("Prefix may contain lowercase letters, numbers, and single hyphens only.")
    try:
        catalog = existing_catalog()
        if clean_bank and clean_bank in catalog["banks"]:
            general.append(f'Bank name "{clean_bank}" already exists.')
        if clean_prefix and clean_prefix in catalog["prefixes"]:
            general.append(f'Prefix "{clean_prefix}" already exists.')
    except OSError as exc:
        general.append(f"Could not inspect the production bank: {exc}")
    generated_ids = [f"{clean_prefix}-{index + 1}" for index in range(len(questions))]
    if len(generated_ids) != len(set(generated_ids)):
        general.append("Generated question IDs are duplicated.")
    elif 'catalog' in locals() and set(generated_ids).intersection(catalog["base_ids"]):
        general.append("Generated question IDs conflict with the base reviewer bank.")
    serializable = True
    try:
        json.dumps(questions, ensure_ascii=False)
    except (TypeError, ValueError):
        serializable = False
        general.append("Questions cannot be serialized safely as JavaScript data.")
    answers = [normalize_draft_question(item).get("correct_answer") for item in questions]
    distribution = {str(index): answers.count(index) for index in range(4)}
    if answers and max(distribution.values()) - min(distribution.values()) > 1:
        general.append(f"Answer positions are unevenly distributed: A={distribution['0']}, B={distribution['1']}, C={distribution['2']}, D={distribution['3']}.")
    return {"valid": not errors and not general, "errors": errors, "general_errors": general, "generated_ids": generated_ids, "serializable": serializable, "answer_distribution": distribution, "allowed_sections": sections, "transcript_type": transcript_type}


def draft_payload(request: DraftRequest | GenerateRequest, questions: list[Any]) -> dict[str, Any]:
    return {"speaker": request.speaker.strip(), "session_number": request.session_number.strip(), "transcript_type": request.transcript_type, "bank_name": request.bank_name.strip(), "prefix": request.prefix.strip(), "transcript": request.transcript, "questions": [normalize_draft_question(item) for item in questions], "topic_pool": getattr(request, "topic_pool", []), "updated_at": datetime.now().astimezone().isoformat(timespec="seconds")}


def save_draft_file(request: DraftRequest | GenerateRequest, questions: list[Any]) -> Path:
    QUESTION_DIR.mkdir(parents=True, exist_ok=True)
    destination = QUESTION_DIR / f"melvin-session-{safe_fragment(request.session_number)}-draft.json"
    temporary = destination.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(draft_payload(request, questions), ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(destination)
    return destination


def relative_path(path: Path) -> str:
    return path.relative_to(PROJECT_DIR).as_posix()


def extract_questions_from_response(payload: Any) -> list[dict[str, Any]]:
    questions = payload.get("questions") if isinstance(payload, dict) else payload
    if not isinstance(questions, list):
        raise HTTPException(status_code=502, detail='Ollama JSON must contain a "questions" array.')
    return [normalize_draft_question(item) for item in questions]


def generation_system_prompt() -> str:
    return "You create LET/LEPT-style multiple-choice questions using only the supplied transcript. Never use outside knowledge. Follow the supplied topic assignments exactly. Each question must have one clearly correct answer, four distinct options, a concise explanation, and a short source excerpt that directly supports the concept, answer, and explanation. Return only the requested structured JSON."


def question_response_schema(count: int, sections: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "questions": {
                "type": "array",
                "minItems": count,
                "maxItems": count,
                "items": {
                    "type": "object",
                    "properties": {
                        "section": {"type": "string", "enum": sections or allowed_sections()},
                        "topic_key": {"type": "string"},
                        "question": {"type": "string"},
                        "options": {
                            "type": "array",
                            "minItems": 4,
                            "maxItems": 4,
                            "items": {"type": "string"},
                        },
                        "correct_answer": {"type": "integer", "minimum": 0, "maximum": 3},
                        "explanation": {"type": "string"},
                        "source_evidence": {"type": "string"},
                    },
                    "required": ["section", "topic_key", "question", "options", "correct_answer", "explanation", "source_evidence"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["questions"],
        "additionalProperties": False,
    }


def topic_pool_schema(sections: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "topics": {
                "type": "array",
                "minItems": 1,
                "maxItems": 40,
                "items": {
                    "type": "object",
                    "properties": {
                        "topic_key": {"type": "string"},
                        "source_evidence": {"type": "string"},
                        "suggested_section": {"type": "string", "enum": sections},
                    },
                    "required": ["topic_key", "source_evidence", "suggested_section"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["topics"],
        "additionalProperties": False,
    }


def extract_topic_pool(payload: Any, transcript: str) -> list[dict[str, str]]:
    raw_topics = payload.get("topics") if isinstance(payload, dict) else None
    if not isinstance(raw_topics, list):
        raise HTTPException(status_code=502, detail="Ollama did not return a valid topic pool.")
    topics: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in raw_topics:
        if not isinstance(raw, dict):
            continue
        key = normalize_topic_key(raw.get("topic_key"))
        evidence = str(raw.get("source_evidence") or "").strip()
        section = str(raw.get("suggested_section") or "").strip()
        repeats_evidence = any(similarity(evidence, topic["source_evidence"]) >= 0.9 for topic in topics)
        if key and key not in seen and not repeats_evidence and evidence and text_supported(evidence, transcript, 0.78) and section in allowed_sections():
            topics.append({"topic_key": key, "source_evidence": evidence, "suggested_section": section})
            seen.add(key)
    return topics


def validation_response_schema(count: int) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "results": {
                "type": "array",
                "minItems": count,
                "maxItems": count,
                "items": {
                    "type": "object",
                    "properties": {
                        "index": {"type": "integer"},
                        "source_supported": {"type": "boolean"},
                        "answer_supported": {"type": "boolean"},
                        "explanation_supported": {"type": "boolean"},
                        "section_correct": {"type": "boolean"},
                        "unique_concept": {"type": "boolean"},
                        "single_correct_answer": {"type": "boolean"},
                        "unsupported_expansion": {"type": "boolean"},
                        "suggested_section": {"type": "string"},
                        "issues": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["index", "source_supported", "answer_supported", "explanation_supported", "section_correct", "unique_concept", "single_correct_answer", "unsupported_expansion", "suggested_section", "issues"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["results"],
        "additionalProperties": False,
    }


VALIDATOR_BOOLEAN_FIELDS = ["source_supported", "answer_supported", "explanation_supported", "section_correct", "unique_concept", "single_correct_answer", "unsupported_expansion"]


def parse_validator_results(payload: Any, questions: list[Any], deterministic_errors: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    raw_results = payload.get("results") if isinstance(payload, dict) else None
    by_index = {item.get("index"): item for item in raw_results if isinstance(item, dict)} if isinstance(raw_results, list) else {}
    errors_by_index: dict[int, list[dict[str, Any]]] = {}
    for error in deterministic_errors or []:
        errors_by_index.setdefault(error["index"], []).append(error)
    results = []
    for index, raw_question in enumerate(questions):
        item = by_index.get(index)
        malformed = not isinstance(item, dict) or any(not isinstance(item.get(field), bool) for field in VALIDATOR_BOOLEAN_FIELDS) or not isinstance(item.get("issues"), list)
        if malformed:
            item = {"index": index, "source_supported": False, "answer_supported": False, "explanation_supported": False, "section_correct": False, "unique_concept": False, "single_correct_answer": False, "unsupported_expansion": True, "suggested_section": "", "issues": ["Validator returned malformed or incomplete structured output."]}
        else:
            item = dict(item)
            item["issues"] = [str(issue) for issue in item.get("issues", [])]
            item["suggested_section"] = str(item.get("suggested_section") or "")
        for error in errors_by_index.get(index, []):
            if error["code"] == "CONCEPT_DUPLICATE":
                item["unique_concept"] = False
            if error["code"].startswith("SOURCE_EVIDENCE"):
                item["source_supported"] = False
            if error["code"] == "ANSWER_GROUNDING":
                item["answer_supported"] = False
            if error["code"] == "EXPLANATION_GROUNDING":
                item["explanation_supported"] = False
                item["unsupported_expansion"] = True
            if error["code"] == "INVALID_SECTION":
                item["section_correct"] = False
            item["issues"].append(error["message"])
        item["status"] = "PASS" if all(item[field] for field in VALIDATOR_BOOLEAN_FIELDS[:-1]) and not item["unsupported_expansion"] else "FLAGGED"
        item["overridden"] = bool(normalize_draft_question(raw_question).get("validation_override"))
        results.append(item)
    return results


@app.get("/", response_class=FileResponse)
def index() -> Path:
    return STATIC_DIR / "index.html"


@app.get("/api/ollama-status")
def ollama_status(model: str = DEFAULT_MODEL) -> dict[str, Any]:
    tags = ollama_json("/api/tags", timeout=8)
    installed = sorted(str(item.get("name") or item.get("model") or "") for item in tags.get("models", []) if item.get("name") or item.get("model"))
    return {"available": True, "model": model, "installed": model in installed, "models": installed}


@app.post("/api/extract")
def extract(request: ExtractRequest) -> dict[str, Any]:
    video_id = extract_video_id(request.url)
    client = YouTubeTranscriptApi()
    try:
        entries = list(transcript_list(client, video_id))
        if not entries:
            raise NoTranscriptFound(video_id, [], [])
        languages = [language_info(item) for item in entries]
        selected = next((item for item in entries if getattr(item, "language_code", "") == request.language_code), None)
        if selected is None:
            selected = next((item for item in entries if getattr(item, "language_code", "").lower().startswith("en")), entries[0])
        return {"video_id": video_id, "selected_language": language_info(selected), "languages": languages, "lines": transcript_lines(selected)}
    except TranscriptsDisabled as exc:
        raise HTTPException(status_code=422, detail="Captions are disabled for this video.") from exc
    except VideoUnavailable as exc:
        raise HTTPException(status_code=404, detail="This video is unavailable or could not be found.") from exc
    except NoTranscriptFound as exc:
        raise HTTPException(status_code=422, detail="No captions are available for this video.") from exc
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail="YouTube could not provide captions right now.") from exc


@app.post("/api/save-transcript")
def save_transcript(request: SaveRequest) -> dict[str, Any]:
    filename = request.filename or f"session-{safe_fragment(request.session_number)}-raw.txt"
    if not re.fullmatch(r"[A-Za-z0-9_.-]+\.txt", filename, re.IGNORECASE):
        raise HTTPException(status_code=400, detail="The transcript filename is invalid.")
    TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
    destination = TRANSCRIPT_DIR / filename
    if destination.exists() and not request.overwrite:
        raise HTTPException(status_code=409, detail=f"A transcript named {filename} already exists.")
    destination.write_text(request.transcript, encoding="utf-8")
    return {"filename": filename, "path": relative_path(destination)}


@app.post("/api/generate")
def generate(request: GenerateRequest) -> dict[str, Any]:
    sections = allowed_sections()
    type_context = "GenEd favors communication, mathematics, science, history, society, arts, ethics, and self sections; ProfEd favors learning principles, curriculum and methods, teaching profession, assessment, and field study sections. Use subject fit over family when needed."
    topic_prompt = f"SOURCE TRANSCRIPT:\n{request.transcript}\n\nTASK: Identify up to 40 distinct, meaningful, testable concepts actually stated in the source. Return one concise normalized concept topic_key, one short closely preserved source_evidence excerpt, and the closest allowed reviewer section per topic. Do not treat allowed section names as transcript topics. Session type is {request.transcript_type}. {type_context} Allowed sections: {json.dumps(sections, ensure_ascii=False)}. Find distinct concepts before reusing any concept."
    topic_payload = call_ollama(request.model, "You extract a diverse pool of testable concepts from a transcript. Reject outside knowledge and return only structured JSON.", topic_prompt, response_schema=topic_pool_schema(sections))
    topic_pool = extract_topic_pool(topic_payload, request.transcript)
    if len(topic_pool) < request.question_count and not request.accept_fewer and not request.allow_topic_reuse:
        raise HTTPException(status_code=409, detail={"code": "INSUFFICIENT_UNIQUE_TOPICS", "message": f"Only {len(topic_pool)} sufficiently distinct testable concepts were found in the transcript.", "unique_topic_count": len(topic_pool), "requested": request.question_count, "topics": topic_pool})
    if not topic_pool:
        raise HTTPException(status_code=422, detail="No sufficiently grounded testable concepts were found in the transcript.")
    effective_count = min(request.question_count, len(topic_pool)) if request.accept_fewer else request.question_count
    assigned_topics = [topic_pool[index % len(topic_pool)] for index in range(effective_count)]
    prompt = f"SOURCE TRANSCRIPT:\n{request.transcript}\n\nTASK: Create exactly {effective_count} LET/LEPT-style multiple-choice questions for a {request.transcript_type} session. Use these topic assignments once each and in this order: {json.dumps(assigned_topics, ensure_ascii=False)}. Each question must test its assigned concept, use its assigned evidence or an equally close source excerpt, and use the assigned suggested_section unless another allowed section is clearly more accurate. Allowed section names are classification labels and must never become question content. Do not ask about the speaker's tone or purpose. Keep explanations narrowly limited to the evidence. Use four plausible, distinct options with exactly one transcript-supported correct answer."
    questions = extract_questions_from_response(call_ollama(request.model, generation_system_prompt(), prompt, response_schema=question_response_schema(effective_count, sections)))
    if len(questions) != effective_count:
        raise HTTPException(status_code=502, detail=f"Ollama returned {len(questions)} questions; {effective_count} were requested. Try again.")
    questions = redistribute_answers(questions, f"{request.bank_name}|{request.prefix}|{effective_count}")
    request.topic_pool = topic_pool
    draft_path = save_draft_file(request, questions)
    return {"questions": questions, "topic_pool": topic_pool, "requested_count": request.question_count, "generated_count": effective_count, "draft": relative_path(draft_path), "validation": validation_report(request.bank_name, request.prefix, questions, request.transcript, request.transcript_type)}


@app.post("/api/regenerate-question")
def regenerate_question(request: RegenerateRequest) -> dict[str, Any]:
    if request.question_index >= len(request.questions):
        raise HTTPException(status_code=400, detail="Question index is outside the current draft.")
    current = normalize_draft_question(request.questions[request.question_index])
    others = [normalize_draft_question(item) for index, item in enumerate(request.questions) if index != request.question_index]
    used_keys = {item["topic_key"] for item in others}
    unused_topics = [topic for topic in request.topic_pool if normalize_topic_key(topic.get("topic_key")) not in used_keys and all(similarity(topic.get("source_evidence"), other.get("source_evidence")) < 0.76 for other in others)]
    assigned_topic = unused_topics[0] if unused_topics else {"topic_key": current["topic_key"], "source_evidence": current["source_evidence"], "suggested_section": current["section"]}
    prompt = f"SOURCE TRANSCRIPT:\n{request.transcript}\n\nCURRENT QUESTION:\n{json.dumps(current, ensure_ascii=False)}\n\nOTHER TOPIC KEYS AND QUESTIONS TO AVOID:\n{json.dumps([{'topic_key': item['topic_key'], 'question': item['question'], 'source_evidence': item['source_evidence']} for item in others], ensure_ascii=False)}\n\nASSIGNED REPLACEMENT TOPIC:\n{json.dumps(assigned_topic, ensure_ascii=False)}\n\nTASK: Return exactly one materially different replacement question grounded only in the transcript and testing the assigned replacement topic. Use its topic_key, source evidence, and suggested section. Do not reuse the current concept or any listed concept."
    generated = extract_questions_from_response(call_ollama(request.model, generation_system_prompt(), prompt, response_schema=question_response_schema(1, allowed_sections())))
    if len(generated) != 1:
        raise HTTPException(status_code=502, detail="Ollama did not return exactly one replacement question.")
    candidate = generated[0]
    options, answer, target = candidate.get("options"), candidate.get("correct_answer"), current.get("correct_answer")
    if isinstance(options, list) and len(options) == 4 and isinstance(answer, int) and 0 <= answer <= 3 and isinstance(target, int) and 0 <= target <= 3:
        correct_text = options[answer]
        options = list(options)
        options.pop(answer)
        options.insert(target, correct_text)
        candidate["options"], candidate["correct_answer"] = options, target
    updated = [normalize_draft_question(item) for item in request.questions]
    updated[request.question_index] = candidate
    errors = [error for error in question_errors(updated, request.transcript) if error["index"] == request.question_index]
    if errors:
        raise HTTPException(status_code=502, detail={"message": "Replacement question failed validation.", "errors": errors})
    return {"question": candidate, "draft": relative_path(save_draft_file(request, updated)), "validation": validation_report(request.bank_name, request.prefix, updated, request.transcript, request.transcript_type)}


@app.post("/api/save-draft")
def save_draft(request: DraftRequest) -> dict[str, Any]:
    return {"draft": relative_path(save_draft_file(request, request.questions))}


@app.post("/api/validate")
def validate_questions(request: DraftRequest) -> dict[str, Any]:
    report = validation_report(request.bank_name, request.prefix, request.questions, request.transcript, request.transcript_type)
    return {**report, "draft": relative_path(save_draft_file(request, request.questions))}


@app.post("/api/validate-ai")
def validate_questions_ai(request: AIValidateRequest) -> dict[str, Any]:
    deterministic_errors = question_errors(request.questions, request.transcript)
    blocking_errors = [error for error in deterministic_errors if error["code"] not in {"CONCEPT_DUPLICATE", "ANSWER_GROUNDING", "EXPLANATION_GROUNDING", "SOURCE_EVIDENCE_UNSUPPORTED"}]
    if blocking_errors:
        raise HTTPException(status_code=422, detail="Deterministic validation must pass before AI validation.")
    records = []
    normalized = [normalize_draft_question(item) for item in request.questions]
    for index, item in enumerate(normalized):
        records.append({"index": index, "source_evidence": item["source_evidence"], "section": item["section"], "topic_key": item["topic_key"], "question": item["question"], "options": item["options"], "correct_answer": item["correct_answer"], "explanation": item["explanation"], "other_topic_keys": [other["topic_key"] for other_index, other in enumerate(normalized) if other_index != index]})
    prompt = f"SOURCE TRANSCRIPT:\n{request.transcript}\n\nQUESTIONS TO AUDIT:\n{json.dumps(records, ensure_ascii=False)}\n\nALLOWED SECTIONS:\n{json.dumps(allowed_sections(), ensure_ascii=False)}\nSESSION TYPE: {request.transcript_type}\n\nTASK: Independently evaluate every required boolean dimension for every question. Look specifically for reasons each question should be rejected. Do not assume the generator is correct. Compare the concept, indicated answer, explanation, and section against the transcript and the question's source evidence. Check whether another option could reasonably be correct. Mark unsupported_expansion true for any explanation claim not present in the evidence or transcript. Mark unique_concept false when another topic key or question tests substantially the same fact. Suggest the best allowed section and list concise, exact issues. Do not rewrite questions."
    payload = call_ollama(request.model, "You are a strict independent reviewer. Look specifically for reasons a question should be rejected. Do not defend the generator and do not use outside knowledge. Return only structured JSON.", prompt, response_schema=validation_response_schema(len(request.questions)))
    results = parse_validator_results(payload, request.questions, deterministic_errors)
    return {"results": results, "pass_count": sum(item["status"] == "PASS" for item in results), "flagged_count": sum(item["status"] == "FLAGGED" for item in results)}


def standalone_javascript(bank_name: str, prefix: str, questions: list[Any]) -> str:
    return f"""(() => {{
  const set = {{
    bank: {json.dumps(bank_name.strip(), ensure_ascii=False)},
    prefix: {json.dumps(prefix.strip(), ensure_ascii=False)},
    items: {json.dumps(questions, ensure_ascii=False, indent=2)}
  }};
  window.REVIEWER_QUESTIONS = window.REVIEWER_QUESTIONS || [];
  set.items.forEach((item, index) => {{
    const [section, question, options, answer, explanation] = item;
    window.REVIEWER_QUESTIONS.push({{ id: `${{set.prefix}}-${{index + 1}}`, bank: set.bank, section, question, options, answer, explanation }});
  }});
}})();
"""


@app.post("/api/download-js", response_class=FileResponse)
def download_js(request: DraftRequest) -> FileResponse:
    report = validation_report(request.bank_name, request.prefix, request.questions, request.transcript, request.transcript_type)
    if not report["valid"]:
        raise HTTPException(status_code=422, detail="Resolve deterministic validation errors before downloading JavaScript.")
    QUESTION_DIR.mkdir(parents=True, exist_ok=True)
    destination = QUESTION_DIR / f"melvin-session-{safe_fragment(request.session_number)}-questions.js"
    destination.write_text(standalone_javascript(request.bank_name, request.prefix, [question_to_tuple(item) for item in request.questions]), encoding="utf-8")
    return FileResponse(destination, media_type="text/javascript", filename=destination.name)


def append_set_source(source: str, bank_name: str, prefix: str, questions: list[Any]) -> str:
    marker_index = source.rfind(APPEND_MARKER)
    if marker_index < 0:
        raise ValueError("Could not find the safe session insertion point in melvin-questions.js.")
    items_json = json.dumps(questions, ensure_ascii=False, indent=2).replace("\n", "\n      ")
    block = ",\n    {\n" + f"      bank: {json.dumps(bank_name.strip(), ensure_ascii=False)},\n" + f"      prefix: {json.dumps(prefix.strip(), ensure_ascii=False)},\n" + f"      items: {items_json}\n" + "    }"
    return source[:marker_index] + block + source[marker_index:]


def unique_backup_path() -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    candidate = BACKUP_DIR / f"melvin-questions.backup-{stamp}.js"
    counter = 2
    while candidate.exists():
        candidate = BACKUP_DIR / f"melvin-questions.backup-{stamp}-{counter}.js"
        counter += 1
    return candidate


@app.post("/api/approve")
def approve_questions(request: ApproveRequest) -> dict[str, Any]:
    if not request.confirmed:
        raise HTTPException(status_code=400, detail="Explicit confirmation is required.")
    with APPROVAL_LOCK:
        report = validation_report(request.bank_name, request.prefix, request.questions, request.transcript, request.transcript_type)
        if not report["valid"]:
            raise HTTPException(status_code=422, detail={"message": "Validation failed. The reviewer was not modified.", "validation": report})
        try:
            source = PRODUCTION_BANK.read_text(encoding="utf-8")
            tuples = [question_to_tuple(item) for item in request.questions]
            updated = append_set_source(source, request.bank_name, request.prefix, tuples)
            backup = unique_backup_path()
            shutil.copy2(PRODUCTION_BANK, backup)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Backup or preparation failed. The reviewer was not modified: {exc}") from exc
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".js", prefix="melvin-questions-", dir=PRODUCTION_BANK.parent, delete=False) as stream:
                stream.write(updated)
                temporary = Path(stream.name)
            run_question_bank_check(temporary)
            temporary.replace(PRODUCTION_BANK)
            final_report = run_question_bank_check(PRODUCTION_BANK)
        except Exception as exc:
            try:
                shutil.copy2(backup, PRODUCTION_BANK)
            except OSError as restore_exc:
                raise HTTPException(status_code=500, detail=f"Final validation failed and automatic restore also failed: {exc}; restore error: {restore_exc}") from restore_exc
            raise HTTPException(status_code=500, detail=f"Final validation failed. The backup was restored automatically: {exc}") from exc
        finally:
            if temporary and temporary.exists():
                temporary.unlink()
        draft_path = save_draft_file(request, request.questions)
        return {"message": "Session added successfully.", "bank": request.bank_name.strip(), "prefix": request.prefix.strip(), "questions_added": len(request.questions), "backup": relative_path(backup), "reviewer_bank": PRODUCTION_BANK.name, "draft": relative_path(draft_path), "total_questions": final_report["count"]}
