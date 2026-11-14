import json
import random
from datetime import datetime, timedelta, timezone

from fastapi import Depends, FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.database import Base, engine, get_db
from app.models import (Answer, Attempt, AttemptEvent, AttemptQuestion, Course, Enrollment, Material,
                        Question, Role, Test, TestQuestion, Topic, User, utcnow)
from app.schemas import (AnswerIn, CourseIn, EventIn, GenerateIn, GradeIn, JoinIn, LoginIn,
                         ProtectedModeConfirm, QuestionIn, RegisterIn, TestIn, TopicIn, UserOut)
from app.security import create_token, current_user, hash_password, require_role, verify_password
from app.services import ai_json, create_course_code, grade_answer, save_material

Base.metadata.create_all(bind=engine)
app = FastAPI(title=settings.app_name, version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


def teacher_course(db: Session, course_id: int, user: User) -> Course:
    course = db.get(Course, course_id)
    if not course:
        raise HTTPException(404, "Курс не найден")
    if course.teacher_id != user.id:
        raise HTTPException(403, "Доступ только преподавателю курса")
    return course


def enrolled_course(db: Session, course_id: int, user: User) -> Course:
    course = db.get(Course, course_id)
    if not course:
        raise HTTPException(404, "Курс не найден")
    if course.teacher_id != user.id and not db.scalar(
        select(Enrollment.id).where(Enrollment.course_id == course_id, Enrollment.student_id == user.id)
    ):
        raise HTTPException(403, "Нет доступа к курсу")
    return course


def attempt_for(db: Session, attempt_id: int, user: User) -> Attempt:
    attempt = db.get(Attempt, attempt_id)
    if not attempt:
        raise HTTPException(404, "Попытка не найдена")
    if attempt.student_id != user.id:
        raise HTTPException(403, "Это чужая попытка")
    return attempt


def valid_question(db: Session, course_id: int, topic_id: int):
    topic = db.get(Topic, topic_id)
    if not topic or topic.course_id != course_id:
        raise HTTPException(422, "Вопрос должен быть привязан к теме этого курса")


def question_out(question: Question, include_answer: bool = True):
    result = {"id": question.id, "topic_id": question.topic_id, "type": question.type,
              "prompt": question.prompt, "options": json.loads(question.options_json),
              "rubric": question.rubric, "verified": question.verified}
    if include_answer:
        result["answer"] = json.loads(question.answer_json)
    return result


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/api/auth/register", status_code=201)
def register(data: RegisterIn, db: Session = Depends(get_db)):
    email = data.email.lower()
    if db.scalar(select(User.id).where(User.email == email)):
        raise HTTPException(409, "Этот адрес электронной почты уже зарегистрирован")
    user = User(name=data.name.strip(), email=email, password_hash=hash_password(data.password), role=data.role)
    db.add(user)
    db.commit()
    db.refresh(user)
    return {"access_token": create_token(user), "token_type": "bearer", "user": UserOut.model_validate(user)}


@app.post("/api/auth/login")
def login(data: LoginIn, db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.email == data.email.lower()))
    if not user or not verify_password(data.password, user.password_hash):
        raise HTTPException(401, "Неверный адрес электронной почты или пароль")
    return {"access_token": create_token(user), "token_type": "bearer", "user": UserOut.model_validate(user)}


@app.get("/api/auth/me")
def me(user: User = Depends(current_user)):
    return UserOut.model_validate(user)


@app.post("/api/auth/logout")
def logout(db: Session = Depends(get_db), user: User = Depends(current_user)):
    user.token_version += 1
    db.commit()
    return {"message": "Сеанс завершён"}


@app.post("/api/courses", status_code=201)
def create_course(data: CourseIn, db: Session = Depends(get_db), user: User = Depends(require_role("teacher"))):
    code = create_course_code()
    while db.scalar(select(Course.id).where(Course.code == code)):
        code = create_course_code()
    course = Course(teacher_id=user.id, title=data.title, description=data.description, code=code)
    db.add(course)
    db.commit()
    db.refresh(course)
    return course


@app.get("/api/courses")
def list_courses(db: Session = Depends(get_db), user: User = Depends(current_user)):
    if user.role == Role.teacher:
        courses = db.scalars(select(Course).where(Course.teacher_id == user.id)).all()
    else:
        courses = db.scalars(select(Course).join(Enrollment).where(Enrollment.student_id == user.id)).all()
    return courses


@app.post("/api/courses/join", status_code=201)
def join_course(data: JoinIn, db: Session = Depends(get_db), user: User = Depends(require_role("student"))):
    course = db.scalar(select(Course).where(Course.code == data.code.strip().upper()))
    if not course:
        raise HTTPException(404, "Курс с таким кодом не найден")
    if not db.scalar(select(Enrollment.id).where(Enrollment.course_id == course.id, Enrollment.student_id == user.id)):
        db.add(Enrollment(course_id=course.id, student_id=user.id))
        db.commit()
    return {"course_id": course.id, "title": course.title}


@app.get("/api/courses/{course_id}")
def get_course(course_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    return enrolled_course(db, course_id, user)


@app.get("/api/courses/{course_id}/participants")
def participants(course_id: int, db: Session = Depends(get_db), user: User = Depends(require_role("teacher"))):
    teacher_course(db, course_id, user)
    rows = db.execute(select(User.id, User.name, User.email, Enrollment.joined_at)
                      .join(Enrollment, Enrollment.student_id == User.id)
                      .where(Enrollment.course_id == course_id)).all()
    return [dict(id=r.id, name=r.name, email=r.email, joined_at=r.joined_at) for r in rows]


@app.post("/api/courses/{course_id}/materials", status_code=201)
async def upload_material(course_id: int, file: UploadFile = File(...), db: Session = Depends(get_db),
                          user: User = Depends(require_role("teacher"))):
    teacher_course(db, course_id, user)
    path, text, mime = await save_material(file, course_id)
    material = Material(course_id=course_id, filename=file.filename or "material", stored_path=path,
                        mime_type=mime, text=text, status="processed")
    db.add(material)
    db.commit()
    db.refresh(material)
    return {"id": material.id, "filename": material.filename, "status": material.status}


@app.get("/api/courses/{course_id}/materials")
def list_materials(course_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    enrolled_course(db, course_id, user)
    rows = db.scalars(select(Material).where(Material.course_id == course_id)).all()
    return [{"id": m.id, "filename": m.filename, "status": m.status, "uploaded_at": m.uploaded_at} for m in rows]


@app.get("/api/materials/{material_id}/content")
def material_content(material_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    material = db.get(Material, material_id)
    if not material:
        raise HTTPException(404, "Материал не найден")
    enrolled_course(db, material.course_id, user)
    return {"id": material.id, "filename": material.filename, "text": material.text}


@app.delete("/api/materials/{material_id}", status_code=204)
def delete_material(material_id: int, db: Session = Depends(get_db), user: User = Depends(require_role("teacher"))):
    material = db.get(Material, material_id)
    if not material:
        raise HTTPException(404, "Материал не найден")
    teacher_course(db, material.course_id, user)
    from pathlib import Path
    Path(material.stored_path).unlink(missing_ok=True)
    db.delete(material)
    db.commit()


@app.post("/api/courses/{course_id}/topics", status_code=201)
def create_topic(course_id: int, data: TopicIn, db: Session = Depends(get_db), user: User = Depends(require_role("teacher"))):
    teacher_course(db, course_id, user)
    topic = Topic(course_id=course_id, name=data.name)
    db.add(topic)
    db.commit()
    db.refresh(topic)
    return topic


@app.patch("/api/topics/{topic_id}")
def rename_topic(topic_id: int, data: TopicIn, db: Session = Depends(get_db), user: User = Depends(require_role("teacher"))):
    topic = db.get(Topic, topic_id)
    if not topic:
        raise HTTPException(404, "Тема не найдена")
    teacher_course(db, topic.course_id, user)
    topic.name = data.name
    db.commit()
    return topic


@app.get("/api/courses/{course_id}/topics")
def list_topics(course_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    enrolled_course(db, course_id, user)
    return db.scalars(select(Topic).where(Topic.course_id == course_id)).all()


@app.post("/api/courses/{course_id}/questions", status_code=201)
def create_question(course_id: int, data: QuestionIn, db: Session = Depends(get_db), user: User = Depends(require_role("teacher"))):
    teacher_course(db, course_id, user)
    valid_question(db, course_id, data.topic_id)
    if data.type in {"single_choice", "multiple_choice"} and not data.answer:
        raise HTTPException(422, "Для вопроса с выбором укажите правильный вариант")
    q = Question(course_id=course_id, topic_id=data.topic_id, type=data.type, prompt=data.prompt,
                 options_json=json.dumps(data.options, ensure_ascii=False),
                 answer_json=json.dumps(data.answer, ensure_ascii=False), rubric=data.rubric,
                 verified=data.verified)
    db.add(q)
    db.commit()
    db.refresh(q)
    return question_out(q)


@app.get("/api/courses/{course_id}/questions")
def list_questions(course_id: int, db: Session = Depends(get_db), user: User = Depends(require_role("teacher"))):
    teacher_course(db, course_id, user)
    return [question_out(q) for q in db.scalars(select(Question).where(Question.course_id == course_id)).all()]


@app.patch("/api/questions/{question_id}")
def edit_question(question_id: int, data: QuestionIn, db: Session = Depends(get_db), user: User = Depends(require_role("teacher"))):
    q = db.get(Question, question_id)
    if not q:
        raise HTTPException(404, "Вопрос не найден")
    teacher_course(db, q.course_id, user)
    valid_question(db, q.course_id, data.topic_id)
    if data.type in {"single_choice", "multiple_choice"} and not data.answer:
        raise HTTPException(422, "Для вопроса с выбором укажите правильный вариант")
    q.topic_id, q.type, q.prompt = data.topic_id, data.type, data.prompt
    q.options_json, q.answer_json = json.dumps(data.options, ensure_ascii=False), json.dumps(data.answer, ensure_ascii=False)
    q.rubric, q.verified = data.rubric, data.verified
    db.commit()
    return question_out(q)


@app.delete("/api/questions/{question_id}", status_code=204)
def delete_question(question_id: int, db: Session = Depends(get_db), user: User = Depends(require_role("teacher"))):
    q = db.get(Question, question_id)
    if not q:
        raise HTTPException(404, "Вопрос не найден")
    teacher_course(db, q.course_id, user)
    db.delete(q)
    db.commit()


@app.post("/api/courses/{course_id}/questions/generate", status_code=202)
async def generate_questions(course_id: int, data: GenerateIn, db: Session = Depends(get_db), user: User = Depends(require_role("teacher"))):
    teacher_course(db, course_id, user)
    material, topic = db.get(Material, data.material_id), db.get(Topic, data.topic_id)
    if not material or material.course_id != course_id or not topic or topic.course_id != course_id:
        raise HTTPException(422, "Материал и тема должны принадлежать курсу")
    if len(material.text) > 180_000:
        text = material.text[:180_000]
    else:
        text = material.text
    response = await ai_json(
        "Создай ровно " + str(data.count) + " учебных вопросов по тексту ниже. Типы: " +
        ", ".join(data.types) + ". Верни JSON {\"questions\":[{\"type\":...,\"prompt\":...,\"options\":[],\"answer\":...,\"rubric\":...}]} . "
        "Для long_text задай критерии в rubric. Не включай пояснения вне JSON. Учебный текст:\n" + text
    )
    generated = response.get("questions", [])
    if not isinstance(generated, list) or not generated:
        raise HTTPException(502, "ИИ вернул пустой черновик")
    created = []
    allowed = set(data.types)
    for item in generated[:data.count]:
        kind = item.get("type")
        if kind not in allowed or kind not in {"single_choice", "multiple_choice", "matching", "short_text", "long_text"}:
            continue
        answer = item.get("answer")
        if kind in {"single_choice", "multiple_choice"} and not answer:
            continue
        q = Question(course_id=course_id, topic_id=topic.id, type=kind,
                     prompt=str(item.get("prompt", ""))[:10000],
                     options_json=json.dumps(item.get("options", []), ensure_ascii=False),
                     answer_json=json.dumps(answer, ensure_ascii=False), rubric=str(item.get("rubric", "")),
                     source="ai", verified=False)
        if q.prompt.strip():
            db.add(q)
            created.append(q)
    if not created:
        raise HTTPException(502, "Не удалось разобрать вопросы ИИ")
    db.commit()
    for q in created:
        db.refresh(q)
    return {"status": "draft", "questions": [question_out(q) for q in created]}


@app.post("/api/courses/{course_id}/tests", status_code=201)
def create_test(course_id: int, data: TestIn, db: Session = Depends(get_db), user: User = Depends(require_role("teacher"))):
    teacher_course(db, course_id, user)
    if not data.questions:
        raise HTTPException(422, "Тест должен содержать хотя бы один вопрос")
    ids = [item.question_id for item in data.questions]
    questions = db.scalars(select(Question).where(Question.id.in_(ids), Question.course_id == course_id)).all()
    by_id = {q.id: q for q in questions}
    if len(by_id) != len(set(ids)) or any(not q.verified for q in questions):
        raise HTTPException(422, "В тест можно добавить только проверенные вопросы своего курса")
    test = Test(course_id=course_id, title=data.title, duration_minutes=data.duration_minutes,
                max_attempts=data.max_attempts)
    db.add(test)
    db.flush()
    for item in data.questions:
        db.add(TestQuestion(test_id=test.id, question_id=item.question_id, points=item.points))
    db.commit()
    db.refresh(test)
    return test


@app.get("/api/courses/{course_id}/tests")
def list_tests(course_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    enrolled_course(db, course_id, user)
    query = select(Test).where(Test.course_id == course_id)
    if user.role == Role.student:
        query = query.where(Test.status == "published")
    return db.scalars(query).all()


@app.post("/api/tests/{test_id}/publish")
def publish_test(test_id: int, db: Session = Depends(get_db), user: User = Depends(require_role("teacher"))):
    test = db.get(Test, test_id)
    if not test:
        raise HTTPException(404, "Тест не найден")
    teacher_course(db, test.course_id, user)
    count = db.scalar(select(TestQuestion.id).where(TestQuestion.test_id == test.id).limit(1))
    if not count:
        raise HTTPException(422, "Нельзя опубликовать тест без вопросов")
    test.status, test.published_at = "published", utcnow()
    db.commit()
    return {"id": test.id, "status": test.status, "published_at": test.published_at}


@app.post("/api/tests/{test_id}/attempts", status_code=201)
def start_attempt(test_id: int, data: ProtectedModeConfirm, db: Session = Depends(get_db), user: User = Depends(require_role("student"))):
    if not data.confirm_rules:
        raise HTTPException(422, "Перед началом теста подтвердите правила защищённого режима")
    test = db.get(Test, test_id)
    if not test or test.status != "published":
        raise HTTPException(404, "Опубликованный тест не найден")
    enrolled_course(db, test.course_id, user)
    active = db.scalar(select(Attempt).where(Attempt.test_id == test.id, Attempt.student_id == user.id, Attempt.status == "active"))
    if active:
        deadline = active.deadline_at.replace(tzinfo=timezone.utc) if active.deadline_at.tzinfo is None else active.deadline_at
        if utcnow() >= deadline:
            finish_attempt(db, active, "time_expired")
        else:
            raise HTTPException(409, "У вас уже есть активная попытка")
    attempt_count = len(db.scalars(select(Attempt.id).where(Attempt.test_id == test.id, Attempt.student_id == user.id)).all())
    if attempt_count >= test.max_attempts:
        raise HTTPException(409, "Попытки для этого теста закончились")
    rows = db.scalars(select(TestQuestion).where(TestQuestion.test_id == test.id)).all()
    random.shuffle(rows)
    attempt = Attempt(test_id=test.id, student_id=user.id, deadline_at=utcnow() + timedelta(minutes=test.duration_minutes))
    db.add(attempt)
    db.flush()
    for pos, row in enumerate(rows):
        question = db.get(Question, row.question_id)
        options = json.loads(question.options_json)
        random.shuffle(options)
        db.add(AttemptQuestion(attempt_id=attempt.id, question_id=row.question_id, position=pos,
                               points=row.points, options_order_json=json.dumps(options, ensure_ascii=False)))
    db.commit()
    return {"attempt_id": attempt.id, "status": "active", "deadline_at": attempt.deadline_at,
            "rules": ["Не переключайте вкладку и не скрывайте страницу теста.", "Попытка завершится при нарушении или по таймеру."]}


@app.get("/api/attempts/{attempt_id}/questions/{position}")
def attempt_question(attempt_id: int, position: int, db: Session = Depends(get_db), user: User = Depends(require_role("student"))):
    attempt = attempt_for(db, attempt_id, user)
    if attempt.status != "active":
        raise HTTPException(409, "Попытка уже завершена")
    deadline = attempt.deadline_at.replace(tzinfo=timezone.utc) if attempt.deadline_at.tzinfo is None else attempt.deadline_at
    if utcnow() >= deadline:
        finish_attempt(db, attempt, "time_expired")
        raise HTTPException(410, "Время теста истекло")
    row = db.scalar(select(AttemptQuestion).where(AttemptQuestion.attempt_id == attempt.id,
                                                AttemptQuestion.position == position))
    if not row:
        raise HTTPException(404, "Вопрос с таким номером не найден")
    q = db.get(Question, row.question_id)
    saved = db.scalar(select(Answer).where(Answer.attempt_id == attempt.id, Answer.question_id == q.id))
    return {"position": position, "total": len(db.scalars(select(AttemptQuestion.id).where(AttemptQuestion.attempt_id == attempt.id)).all()),
            "remaining_seconds": max(0, int(((attempt.deadline_at.replace(tzinfo=timezone.utc) if attempt.deadline_at.tzinfo is None else attempt.deadline_at) - utcnow()).total_seconds())),
            "question": {**question_out(q, include_answer=False), "options": json.loads(row.options_order_json)},
            "saved_answer": json.loads(saved.answer_json) if saved else None}


@app.put("/api/attempts/{attempt_id}/answers/{question_id}")
def save_answer(attempt_id: int, question_id: int, data: AnswerIn, db: Session = Depends(get_db), user: User = Depends(require_role("student"))):
    attempt = attempt_for(db, attempt_id, user)
    if attempt.status != "active":
        raise HTTPException(409, "Попытка уже завершена")
    deadline = attempt.deadline_at.replace(tzinfo=timezone.utc) if attempt.deadline_at.tzinfo is None else attempt.deadline_at
    if utcnow() >= deadline:
        finish_attempt(db, attempt, "time_expired")
        raise HTTPException(410, "Время теста истекло")
    if not db.scalar(select(AttemptQuestion.id).where(AttemptQuestion.attempt_id == attempt.id, AttemptQuestion.question_id == question_id)):
        raise HTTPException(403, "Вопрос не входит в этот вариант теста")
    answer = db.scalar(select(Answer).where(Answer.attempt_id == attempt.id, Answer.question_id == question_id))
    if not answer:
        answer = Answer(attempt_id=attempt.id, question_id=question_id, answer_json="null")
        db.add(answer)
    answer.answer_json = json.dumps(data.answer, ensure_ascii=False)
    q = db.get(Question, question_id)
    ratio = grade_answer(q, data.answer)
    if ratio is not None:
        row = db.scalar(select(AttemptQuestion).where(AttemptQuestion.attempt_id == attempt.id, AttemptQuestion.question_id == question_id))
        answer.score = ratio * row.points
    db.commit()
    return {"saved": True}


def finish_attempt(db: Session, attempt: Attempt, reason: str):
    if attempt.status != "active":
        return
    rows = db.scalars(select(AttemptQuestion).where(AttemptQuestion.attempt_id == attempt.id)).all()
    deadline = attempt.deadline_at.replace(tzinfo=timezone.utc) if attempt.deadline_at.tzinfo is None else attempt.deadline_at
    if reason == "submitted" and utcnow() >= deadline:
        reason = "time_expired"
    total = 0.0
    for row in rows:
        answer = db.scalar(select(Answer).where(Answer.attempt_id == attempt.id, Answer.question_id == row.question_id))
        if answer:
            q = db.get(Question, row.question_id)
            ratio = grade_answer(q, json.loads(answer.answer_json))
            if ratio is not None:
                answer.score = ratio * row.points
            total += answer.score or 0
    attempt.status = "terminated" if reason in {"focus_lost", "page_hidden"} else "finished"
    attempt.finish_reason, attempt.finished_at, attempt.score = reason, utcnow(), total
    db.commit()


@app.post("/api/attempts/{attempt_id}/events", status_code=201)
def attempt_event(attempt_id: int, data: EventIn, db: Session = Depends(get_db), user: User = Depends(require_role("student"))):
    attempt = attempt_for(db, attempt_id, user)
    if attempt.status != "active":
        raise HTTPException(409, "Попытка уже завершена")
    event = AttemptEvent(attempt_id=attempt.id, event_type=data.event_type, detail=data.detail)
    db.add(event)
    db.flush()
    if data.event_type in {"page_hidden", "focus_lost"}:
        finish_attempt(db, attempt, data.event_type)
    else:
        db.commit()
    return {"recorded": True, "attempt_status": attempt.status}


@app.post("/api/attempts/{attempt_id}/finish")
def submit_attempt(attempt_id: int, db: Session = Depends(get_db), user: User = Depends(require_role("student"))):
    attempt = attempt_for(db, attempt_id, user)
    finish_attempt(db, attempt, "submitted")
    return {"attempt_id": attempt.id, "status": attempt.status, "score": attempt.score}


@app.get("/api/attempts/{attempt_id}/result")
def attempt_result(attempt_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    attempt = db.get(Attempt, attempt_id)
    if not attempt:
        raise HTTPException(404, "Попытка не найдена")
    test = db.get(Test, attempt.test_id)
    enrolled_course(db, test.course_id, user)
    if user.role == Role.student and attempt.student_id != user.id:
        raise HTTPException(403, "Результаты других обучающихся недоступны")
    if attempt.status == "active":
        raise HTTPException(409, "Результат станет доступен после завершения")
    breakdown = {}
    rows = db.scalars(select(AttemptQuestion).where(AttemptQuestion.attempt_id == attempt.id)).all()
    max_by_topic, score_by_topic = {}, {}
    for row in rows:
        q = db.get(Question, row.question_id)
        topic = db.get(Topic, q.topic_id)
        ans = db.scalar(select(Answer).where(Answer.attempt_id == attempt.id, Answer.question_id == q.id))
        max_by_topic[topic.name] = max_by_topic.get(topic.name, 0) + row.points
        score_by_topic[topic.name] = score_by_topic.get(topic.name, 0) + (ans.score or 0 if ans else 0)
    for name, maximum in max_by_topic.items():
        score = score_by_topic[name]
        breakdown[name] = {"score": score, "max_score": maximum, "percent": round(score / maximum * 100, 1)}
    recommendations = [{"topic": name, "percent": result["percent"], "review_material_ids":
                        [m.id for m in db.scalars(select(Material).where(Material.course_id == test.course_id)).all()]}
                       for name, result in breakdown.items() if result["percent"] < 60]
    return {"attempt_id": attempt.id, "test_id": test.id, "status": attempt.status, "score": attempt.score,
            "topics": breakdown, "recommendations": recommendations}


@app.get("/api/courses/{course_id}/results")
def course_results(course_id: int, test_id: int | None = None, student_id: int | None = None,
                   db: Session = Depends(get_db), user: User = Depends(require_role("teacher"))):
    teacher_course(db, course_id, user)
    query = select(Attempt, User.name, Test.title).join(User, User.id == Attempt.student_id).join(Test, Test.id == Attempt.test_id).where(Test.course_id == course_id)
    if test_id is not None:
        query = query.where(Attempt.test_id == test_id)
    if student_id is not None:
        query = query.where(Attempt.student_id == student_id)
    rows = db.execute(query.order_by(Attempt.started_at.desc())).all()
    return [{"attempt_id": a.id, "student_id": a.student_id, "student_name": name,
             "test_id": a.test_id, "test_title": title, "status": a.status, "score": a.score,
             "started_at": a.started_at} for a, name, title in rows]


@app.get("/api/students/me/history")
def my_history(db: Session = Depends(get_db), user: User = Depends(require_role("student"))):
    attempts = db.scalars(select(Attempt).where(Attempt.student_id == user.id).order_by(Attempt.started_at)).all()
    return [{"attempt_id": a.id, "test_id": a.test_id, "score": a.score, "status": a.status,
             "started_at": a.started_at, "finished_at": a.finished_at} for a in attempts]


@app.get("/api/courses/{course_id}/students/{student_id}/profile")
def student_profile(course_id: int, student_id: int, db: Session = Depends(get_db), user: User = Depends(require_role("teacher"))):
    teacher_course(db, course_id, user)
    if not db.scalar(select(Enrollment.id).where(Enrollment.course_id == course_id, Enrollment.student_id == student_id)):
        raise HTTPException(404, "Обучающийся не записан на курс")
    attempts = db.scalars(select(Attempt).join(Test).where(Test.course_id == course_id, Attempt.student_id == student_id)).all()
    events = db.scalars(select(AttemptEvent).join(Attempt).join(Test).where(Test.course_id == course_id,
                                                                           Attempt.student_id == student_id)).all()
    return {"student_id": student_id, "attempts": [{"id": a.id, "test_id": a.test_id, "score": a.score,
            "status": a.status, "finished_at": a.finished_at} for a in attempts],
            "violations": [{"attempt_id": e.attempt_id, "type": e.event_type, "occurred_at": e.occurred_at,
                            "detail": e.detail} for e in events]}


@app.post("/api/answers/{answer_id}/grade")
async def teacher_grade(answer_id: int, data: GradeIn, db: Session = Depends(get_db), user: User = Depends(require_role("teacher"))):
    answer = db.get(Answer, answer_id)
    if not answer:
        raise HTTPException(404, "Ответ не найден")
    attempt = db.get(Attempt, answer.attempt_id)
    test = db.get(Test, attempt.test_id)
    teacher_course(db, test.course_id, user)
    q = db.get(Question, answer.question_id)
    row = db.scalar(select(AttemptQuestion).where(AttemptQuestion.attempt_id == attempt.id, AttemptQuestion.question_id == q.id))
    if q.type != "long_text" or data.score > row.points:
        raise HTTPException(422, "Оценка должна относиться к развёрнутому ответу и не превышать максимум")
    answer.score, answer.teacher_feedback = data.score, data.feedback
    rows = db.scalars(select(AttemptQuestion).where(AttemptQuestion.attempt_id == attempt.id)).all()
    attempt.score = sum((db.scalar(select(Answer.score).where(Answer.attempt_id == attempt.id,
                                     Answer.question_id == r.question_id)) or 0) for r in rows)
    db.commit()
    return {"answer_id": answer.id, "score": answer.score, "attempt_score": attempt.score}


@app.post("/api/attempts/{attempt_id}/answers/{question_id}/ai-evaluate")
async def ai_evaluate(attempt_id: int, question_id: int, db: Session = Depends(get_db), user: User = Depends(require_role("teacher"))):
    attempt = db.get(Attempt, attempt_id)
    if not attempt:
        raise HTTPException(404, "Попытка не найдена")
    test = db.get(Test, attempt.test_id)
    teacher_course(db, test.course_id, user)
    q = db.get(Question, question_id)
    answer = db.scalar(select(Answer).where(Answer.attempt_id == attempt_id, Answer.question_id == question_id))
    row = db.scalar(select(AttemptQuestion).where(AttemptQuestion.attempt_id == attempt_id, AttemptQuestion.question_id == question_id))
    if not answer or not q or q.type != "long_text" or not row:
        raise HTTPException(422, "Развёрнутый ответ не найден")
    payload = json.loads(answer.answer_json)
    result = await ai_json("Оцени ответ по критериям. Верни JSON {\"score\": число, \"feedback\": строка}. "
                           f"Максимум баллов: {row.points}. Критерии: {q.rubric}. Ответ: {payload}")
    score = max(0.0, min(row.points, float(result.get("score", 0))))
    answer.ai_suggested_score = score
    db.commit()
    return {"ai_suggested_score": score, "feedback": str(result.get("feedback", ""))}
