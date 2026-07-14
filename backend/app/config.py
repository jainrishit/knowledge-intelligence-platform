from pydantic_settings import BaseSettings
from pydantic import Field, ConfigDict


class Settings(BaseSettings):
    model_config = ConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        populate_by_name=True,
    )

    # LLM — IBM Consulting Advantage endpoint
    claude_api_key: str = Field(alias="CLAUDE_API_KEY")
    claude_base_url: str = Field(
        default="https://api.nextgen-beta.ica.ibm.com/ica",
        alias="CLAUDE_BASE_URL",
    )
    claude_model: str = Field(default="claude-sonnet-4-5", alias="CLAUDE_MODEL")

    # Storage
    database_url: str = Field(
        default="sqlite:///./bob_knowledge_fabric.db",
        alias="DATABASE_URL",
    )
    upload_dir: str = Field(default="./uploads", alias="UPLOAD_DIR")

    # Chunking
    chunk_size: int = Field(default=3000, alias="CHUNK_SIZE")
    chunk_overlap: int = Field(default=200, alias="CHUNK_OVERLAP")

    # Graph traversal
    graph_hop_depth: int = Field(default=2, alias="GRAPH_HOP_DEPTH")
    graph_strength_threshold: float = Field(default=0.5, alias="GRAPH_STRENGTH_THRESHOLD")
    graph_max_nodes: int = Field(default=60, alias="GRAPH_MAX_NODES")

    # Extraction quality
    concept_confidence_min: float = Field(default=0.6, alias="CONCEPT_CONFIDENCE_MIN")

    # QA retrieval
    qa_top_k: int = Field(default=20, alias="QA_TOP_K")

    # Server
    cors_origins: str = Field(
        default="http://localhost:5173,http://localhost:3000",
        alias="CORS_ORIGINS",
    )


settings = Settings()
