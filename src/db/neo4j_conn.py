import logging
import threading

from neo4j import GraphDatabase

from src.config import NEO4J_PASSWORD, NEO4J_URI, NEO4J_USER


logger = logging.getLogger(__name__)


class Neo4jConnectionError(RuntimeError):
    """Raised when the Neo4j driver cannot be created or used."""


class Neo4jConnection:
    """Process-wide, thread-safe Neo4j driver holder."""

    _instance = None
    _lock = threading.Lock()

    def __new__(cls, url=None, username=None, password=None):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    instance = super().__new__(cls)
                    resolved_url = url or NEO4J_URI
                    resolved_user = username or NEO4J_USER
                    resolved_password = password or NEO4J_PASSWORD
                    if not resolved_password:
                        raise Neo4jConnectionError("缺少 NEO4J_PASSWORD，请在 .env 中配置。")
                    try:
                        instance.driver = GraphDatabase.driver(
                            resolved_url,
                            auth=(resolved_user, resolved_password),
                        )
                        instance.driver.verify_connectivity()
                    except Neo4jConnectionError:
                        raise
                    except Exception as exc:
                        logger.error("Neo4j connection failed: %s", exc)
                        raise Neo4jConnectionError(
                            f"无法连接 Neo4j（{resolved_url}），请检查数据库是否启动。"
                        ) from exc
                    cls._instance = instance
        return cls._instance

    @classmethod
    def close_if_initialized(cls):
        with cls._lock:
            instance = cls._instance
            cls._instance = None
        if instance is not None:
            try:
                instance.driver.close()
            except Exception:
                logger.exception("Failed to close Neo4j driver")

    def get_session(self):
        try:
            return self.driver.session()
        except Exception as exc:
            logger.error("Failed to create Neo4j session: %s", exc)
            raise Neo4jConnectionError("数据库会话不可用，请稍后重试。") from exc

    def ensure_indexes(self, *, await_online: bool = True, timeout_seconds: int = 600):
        """Create the lookup indexes the importer and query tools depend on.

        Without an index, ``MERGE``/``MATCH`` on these properties falls back to a
        full label scan, which makes the import degrade into quadratic work and
        appear to hang. Schema commands cannot run inside an explicit
        transaction alongside data writes, so this uses its own auto-commit
        session and must be called *before* the import transaction is opened.
        """
        statements = (
            "CREATE INDEX index_model_source_id IF NOT EXISTS FOR (m:车型) ON (m._source_id)",
            "CREATE INDEX index_series_name IF NOT EXISTS FOR (s:车系) ON (s.name)",
            "CREATE INDEX index_brand_name IF NOT EXISTS FOR (b:品牌) ON (b.name)",
            "CREATE INDEX index_energy_name IF NOT EXISTS FOR (e:能源类型) ON (e.name)",
            "CREATE INDEX index_price_name IF NOT EXISTS FOR (p:价格区间) ON (p.name)",
        )
        with self.get_session() as session:
            for statement in statements:
                session.run(statement).consume()
            if await_online:
                try:
                    session.run(f"CALL db.awaitIndexes({int(timeout_seconds)})").consume()
                except Exception:
                    # A missing await only costs speed, never correctness: the
                    # indexes still finish populating in the background.
                    logger.warning(
                        "等待索引 ONLINE 超时，导入可能变慢但不会失败。", exc_info=True
                    )

    def close(self):
        self.close_if_initialized()
