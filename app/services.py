import json
import mimetypes
import secrets
from pathlib import Path

import httpx
from docx import Document as WordDocument
from fastapi import HTTPException, UploadFile
from pypdf import PdfReader

from app.config import settings
from app.models import Question

ALLOWED_EXTENSIONS = {".pdf", ".docx", ".txt"}


def create_course_code() -> str:
    return secrets.token_urlsafe(6).replace("-", "").replace("_", "")[:8].upper()


def _extract_text(path: Path, suffix: str) -> str:
    if suffix == ".txt":
        return path.read_text(encoding="utf-8-sig", errors="replace")
    if suffix == ".docx":
        doc = WordDocument(path)
        paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
        for table in doc.tables:
            paragraphs.extend(" | ".join(cell.text for cell in row.cells) for row in table.rows)
        return "\n".join(paragraphs)
    reader = PdfReader(str(path))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


async def save_material(upload: UploadFile, course_id: int) -> tuple[str, str, str]:
    filename = Path(upload.filename or "material").name
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise HTTPException(415, "Поддерживаются только PDF, DOCX и TXT")
    data = await upload.read(settings.max_upload_bytes + 1)
    if not data or len(data) > settings.max_upload_bytes:
        raise HTTPException(413, "Файл пуст или превышает 20 МБ")
    valid = (suffix == ".pdf" and data.startswith(b"%PDF")) or (
        suffix == ".docx" and data.startswith(b"PK\x03\x04")
    ) or (suffix == ".txt" and b"\x00" not in data)
    if not valid:
        raise HTTPException(415, "Содержимое файла не соответствует его формату")
    folder = Path(settings.upload_dir) / str(course_id)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{secrets.token_hex(12)}{suffix}"
    path.write_bytes(data)
    try:
        text = _extract_text(path, suffix)
    except Exception as exc:
        path.unlink(missing_ok=True)
        raise HTTPException(422, "Не удалось извлечь текст из файла") from exc
    if not text.strip():
        path.unlink(missing_ok=True)
        raise HTTPException(422, "В файле не найден текст")
    mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    return str(path), text[:1_000_000], mime


def grade_answer(question: Question, answer) -> float | None:
    kind = question.type
    expected = json.loads(question.answer_json)
    if kind in {"single_choice", "short_text"}:
        if isinstance(answer, str) and isinstance(expected, str):
            return float(answer.strip().casefold() == expected.strip().casefold())
        return float(answer == expected)
    if kind == "multiple_choice":
        expected_set = set(expected or [])
        actual_set = set(answer or [])
        if not expected_set:
            return 0.0
        return max(0.0, len(expected_set & actual_set) - len(actual_set - expected_set)) / len(expected_set)
    if kind == "matching":
        expected_map = expected or {}
        actual_map = answer or {}
        if not expected_map:
            return 0.0
        return sum(actual_map.get(k) == v for k, v in expected_map.items()) / len(expected_map)
    return None


async def ai_json(prompt: str) -> dict:
    if not settings.ai_api_key or not settings.ai_base_url or not settings.ai_model:
        raise HTTPException(503, "Сервис ИИ не настроен; можно добавить вопросы вручную")
    url = settings.ai_base_url.rstrip("/") + "/chat/completions"
    try:
        async with httpx.AsyncClient(timeout=55) as client:
            response = await client.post(
                url,
                headers={"Authorization": f"Bearer {settings.ai_api_key}"},
                json={
                    "model": settings.ai_model,
                    "response_format": {"type": "json_object"},
                    "messages": [
                        {"role": "system", "content": "Верни только JSON по заданной схеме."},
                        {"role": "user", "content": prompt},
                    ],
                },
            )
            response.raise_for_status()
            return json.loads(response.json()["choices"][0]["message"]["content"])
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        raise HTTPException(502, "Сервис ИИ временно недоступен; повторите запрос или добавьте вопросы вручную") from exc
