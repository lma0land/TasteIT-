# TasteIT backend

Python REST API для образовательной платформы TasteIT!. Здесь нет клиентского приложения.

## Запуск

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
Copy-Item .env.example .env
uvicorn app.main:app --reload
```

Документация API: `http://127.0.0.1:8000/docs`. Таблицы SQLite создаются при запуске. Для другого хранилища задайте `DATABASE_URL`.

## Основные маршруты

- `POST /api/auth/register`, `/api/auth/login`, `GET /api/auth/me`
- `POST /api/courses`, `POST /api/courses/join`, курсы и участники
- `POST /api/courses/{id}/materials` для PDF/DOCX/TXT (до 20 МБ), генерация черновика
- темы и вопросы курса, ручное редактирование и отметка проверки
- сборка и явная публикация теста
- старт теста, поочерёдная выдача вопроса без правильного ответа, сохранение ответа, событие нарушения и завершение
- результаты обучающегося и аналитика преподавателя

Все защищённые маршруты требуют `Authorization: Bearer <token>`. Пароли хранятся как Argon2-хэши. Ключ ИИ задаётся только в серверном окружении. Для production используйте HTTPS, PostgreSQL и внешнее файловое хранилище.
