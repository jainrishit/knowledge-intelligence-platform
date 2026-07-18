from pydantic_settings import BaseSettings
from pydantic import Field, ConfigDict


class Settings(BaseSettings):
    model_config = ConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        populate_by_name=True,
    )

    # Raw key for local dev. In production, set CLAUDE_SECRET_NAME and use a
    # cloud SecretProvider so the key never needs to be in the environment.
    claude_api_key: str = Field(default="", alias="CLAUDE_API_KEY")
    claude_secret_name: str = Field(default="CLAUDE_API_KEY", alias="CLAUDE_SECRET_NAME")
    claude_base_url: str = Field(
        default="https://api.nextgen-beta.ica.ibm.com/ica",
        alias="CLAUDE_BASE_URL",
    )
    claude_model: str = Field(default="claude-sonnet-4-5", alias="CLAUDE_MODEL")

    # Storage
    database_url: str = Field(
        default="sqlite:///./knowledge_platform.db",
        alias="DATABASE_URL",
    )
    upload_dir: str = Field(default="./uploads", alias="UPLOAD_DIR")
    upload_max_bytes: int = Field(default=26_214_400, alias="MAX_UPLOAD_BYTES")  # 25 MB per file
    spreadsheet_max_rows: int = Field(default=10_000, alias="SPREADSHEET_MAX_ROWS")

    # Chunking
    chunk_size: int = Field(default=3000, alias="CHUNK_SIZE")
    chunk_overlap: int = Field(default=200, alias="CHUNK_OVERLAP")

    # Graph traversal
    graph_hop_depth: int = Field(default=2, alias="GRAPH_HOP_DEPTH")
    # Lower threshold to 0.35 — matches the relationship extraction STRENGTH_FLOOR so that
    # every stored relationship participates in retrieval BFS (previously 0.5 cut out ~30%
    # of extracted relationships, making the graph appear sparser than it really was).
    graph_strength_threshold: float = Field(default=0.35, alias="GRAPH_STRENGTH_THRESHOLD")
    # Raised from 60 to 100 — gives BFS more room to explore without context explosion.
    # The retrieval layer still de-ranks low-relevance nodes before sending to the LLM.
    graph_max_nodes: int = Field(default=100, alias="GRAPH_MAX_NODES")

    # Extraction quality
    concept_confidence_min: float = Field(default=0.6, alias="CONCEPT_CONFIDENCE_MIN")

    # Pattern extraction threshold — minimum workspace knowledge growth (%) to trigger re-extraction
    pattern_extraction_threshold_pct: float = Field(
        default=10.0, alias="PATTERN_EXTRACTION_THRESHOLD_PERCENT"
    )

    # QA retrieval — raised from 20 to 30 seed nodes so BFS starts from a richer
    # set of entry points, improving coverage of relevant concepts in the graph.
    qa_top_k: int = Field(default=30, alias="QA_TOP_K")

    # LLM governance — circuit breaker
    llm_cb_failure_threshold: int = Field(default=5, alias="LLM_CB_FAILURE_THRESHOLD")
    llm_cb_recovery_timeout: float = Field(default=60.0, alias="LLM_CB_RECOVERY_TIMEOUT")

    # LLM governance — rate limiting
    max_llm_requests_per_minute: int = Field(default=60, alias="MAX_LLM_REQUESTS_PER_MINUTE")
    llm_rate_window_seconds: float = Field(default=60.0, alias="LLM_RATE_WINDOW_SECONDS")

    # LLM governance — cost tracking (USD per 1 000 tokens, Claude Sonnet defaults)
    llm_cost_per_1k_input_tokens: float = Field(default=0.003, alias="LLM_COST_PER_1K_INPUT_TOKENS")
    llm_cost_per_1k_output_tokens: float = Field(default=0.015, alias="LLM_COST_PER_1K_OUTPUT_TOKENS")

    # LLM resilience — timeouts (seconds)
    # read_timeout raised to 180s: a blueprint generation at max_tokens=8192 observed
    # 118.7s latency from the IBM Gateway.  120s left only 1.3s of margin.
    # 180s provides a 60s safety buffer while still guaranteeing a timeout eventually.
    llm_connect_timeout: float = Field(default=10.0, alias="LLM_CONNECT_TIMEOUT")
    llm_read_timeout: float = Field(default=180.0, alias="LLM_READ_TIMEOUT")
    llm_write_timeout: float = Field(default=30.0, alias="LLM_WRITE_TIMEOUT")

    # LLM resilience — retry strategy
    # max_retries=1: one retry after the initial attempt (2 total attempts).
    # Rationale: read_timeout=180s means max_retries=2 produces a 3×180=540s worst case.
    # With max_retries=1: worst case = 2×180s + ~5s backoff = ~365s (~6 min).
    # One retry is enough to recover from a transient 502; more retries just extend the hang.
    llm_max_retries: int = Field(default=1, alias="LLM_MAX_RETRIES")
    llm_retry_max_wait: float = Field(default=8.0, alias="LLM_RETRY_MAX_WAIT")

    # Server
    cors_origins: str = Field(
        default="http://localhost:5173,http://localhost:3000",
        alias="CORS_ORIGINS",
    )


settings = Settings()
