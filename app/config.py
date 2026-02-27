from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    secret_key: str = "CHANGE_ME"
    db_path: str = "/data/ribbon.db"
    upload_dir: str = "/uploads"
    backups_dir: str = "/data/backups"
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    debug: bool = False


settings = Settings()
