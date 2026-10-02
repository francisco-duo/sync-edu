from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Dataset inicial determinístico (pequeno por padrão; a escala de 4.000 vem depois).
    seed_students: int = 20
    seed_classes: int = 3
    seed_random: int = 42
