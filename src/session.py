import asyncio
import uuid

import psycopg
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg_pool import AsyncConnectionPool

from src.config import DB_URI, POSTGRES_POOL_SIZE


MAINTENANCE_DATABASE = "postgres"


def create_unique_session_id(session_id=None):
    return session_id or str(uuid.uuid4())


def _ensure_database_exists():
    """Create the database named in DB_URI when it is still missing.

    Two PostgreSQL rules shape this function:

    * a client cannot connect to a database that does not exist, so the target
      has to be created from a maintenance connection to ``postgres`` instead;
    * ``CREATE DATABASE`` has no ``IF NOT EXISTS`` form and cannot run inside a
      transaction block, hence the explicit existence check and autocommit.

    The synchronous driver is used on purpose: async psycopg connections require
    a SelectorEventLoop on Windows (see the win32 branch in ``src/api.py``),
    while a plain connection works under any event loop. Callers wrap this in
    ``asyncio.to_thread`` so the event loop is never blocked.

    Returns the name of the database that was created, or ``None`` when nothing
    had to be done.
    """
    params = conninfo_to_dict(DB_URI)
    target = params.get("dbname") or params.get("database")
    if not target or target in {MAINTENANCE_DATABASE, "template1"}:
        return None

    admin_params = {key: value for key, value in params.items() if key != "database"}
    admin_params["dbname"] = MAINTENANCE_DATABASE
    try:
        with psycopg.connect(make_conninfo(**admin_params), autocommit=True) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT 1 FROM pg_database WHERE datname = %s", (target,)
                )
                if cursor.fetchone() is not None:
                    return None
                cursor.execute(
                    sql.SQL("CREATE DATABASE {}").format(sql.Identifier(target))
                )
    except psycopg.errors.DuplicateDatabase:
        # Another process won the race between the check and the statement.
        return None
    except psycopg.errors.InsufficientPrivilege as exc:
        raise RuntimeError(
            f"数据库 {target!r} 不存在，且当前用户无权创建。请改用具备 CREATEDB "
            f"权限的账号，或手工执行：CREATE DATABASE {target};"
        ) from exc
    except psycopg.OperationalError as exc:
        raise RuntimeError(
            f"无法连接 PostgreSQL 维护库 {MAINTENANCE_DATABASE!r}，"
            f"请确认数据库服务已启动且 DB_URI 正确：{exc}"
        ) from exc
    except psycopg.Error as exc:
        raise RuntimeError(f"自动创建数据库 {target!r} 失败：{exc}") from exc
    return target


async def create_session_store():
    if not DB_URI:
        raise RuntimeError("缺少 DB_URI，请在 .env 中配置 PostgreSQL 连接。")
    created = await asyncio.to_thread(_ensure_database_exists)
    if created:
        print(f"数据库 {created!r} 不存在，已自动创建。")
    pool = AsyncConnectionPool(
        conninfo=DB_URI,
        min_size=1,
        max_size=POSTGRES_POOL_SIZE,
        kwargs={"autocommit": True, "prepare_threshold": 0},
        open=False,
    )
    await pool.open()
    checkpointer = AsyncPostgresSaver(pool)
    try:
        await checkpointer.setup()
    except Exception:
        await pool.close()
        raise
    return pool, checkpointer


def _print_session_choices(sessions):
    if not sessions:
        print("暂无会话历史")
        return False

    for index, session in enumerate(sessions, 1):
        print(
            f"  [{index}]  {session['id'][:8]}... |\n"
            f"标题: {session['title'] or 'N/A'}\n"
            f"创建: {session['create_time'] or 'N/A'}\n"
            f"上次提问: {session['last_query'] or 'N/A'}\n"
        )
    print("-" * 50)
    return True


def _choose_session(sessions, prompt):
    try:
        choice = input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print("\n已取消")
        return None
    if choice in {"", "0"}:
        print("已取消")
        return None
    try:
        index = int(choice) - 1
    except ValueError:
        print("请输入数字")
        return None

    if 0 <= index < len(sessions):
        return sessions[index]
    print("编号无效")
    return None


def resume_session(local_store, profile_id):
    sessions = local_store.list_conversations(profile_id)
    if not _print_session_choices(sessions):
        return None

    session = _choose_session(sessions, "请输入恢复会话（0取消）: ")
    return session["id"] if session else None


def select_session_for_deletion(local_store, profile_id):
    """Pick a conversation to delete, then require an explicit confirmation.

    Memories are stored separately from conversations, so removing a
    conversation never removes what the assistant was asked to remember.
    """
    sessions = local_store.list_conversations(profile_id)
    if not _print_session_choices(sessions):
        return None

    session = _choose_session(sessions, "请输入要删除的会话（0取消）: ")
    if session is None:
        return None

    title = session["title"] or session["last_query"] or "N/A"
    try:
        answer = input(
            f"确认删除会话 {session['id'][:8]}...（{title}）？"
            f"输入 y 确认，其他任意内容取消: "
        ).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\n已取消")
        return None
    if answer != "y":
        print("已取消")
        return None
    return session["id"]
