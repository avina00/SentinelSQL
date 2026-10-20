from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    openai_api_key: str
    postgres_uri: str = ""
    llm_model: str = ""
    max_retries: int = 3

    class Config:
        env_file = ".env"
        extra = "ignore"

settings = Settings()