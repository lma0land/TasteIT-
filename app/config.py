from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "TasteIT API"
    database_url: str = "sqlite:///./tasteit.db"
    jwt_secret: str = "development-only-change-this-secret"
    jwt_expire_minutes: int = 1440
    upload_dir: str = "./storage/materials"
    max_upload_bytes: int = 20 * 1024 * 1024
    ai_base_url: str | None = None
    ai_api_key: str | None = None
    ai_model: str | None = None
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
