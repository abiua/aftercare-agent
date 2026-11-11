from io import StringIO

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory

from aftercare.config import ROOT


def test_chat_upgrade_compiles_typed_foreign_keys_without_existing_metadata():
    config = Config()
    config.set_main_option("script_location", str(ROOT / "backend/migrations"))
    revision = ScriptDirectory.from_config(config).get_revision("20261111_conversations")
    output = StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql", opts={"as_sql": True, "output_buffer": output}
    )
    with Operations.context(context):
        revision.module.upgrade()
    sql = output.getvalue()
    tables = {
        "chat_conversations": [
            ("customer_id", 64, "business_customers"),
            ("active_case_id", 36, "business_cases"),
            ("active_order_id", 64, "business_orders"),
        ],
        "chat_messages": [
            ("conversation_id", 36, "chat_conversations"),
            ("reply_to_id", 36, "chat_messages"),
        ],
        "chat_attachments": [("message_id", 36, "chat_messages")],
        "chat_case_links": [
            ("conversation_id", 36, "chat_conversations"),
            ("case_id", 36, "business_cases"),
            ("source_message_id", 36, "chat_messages"),
        ],
    }
    for table, columns in tables.items():
        ddl = sql.split(f"CREATE TABLE {table} (", 1)[1].split(";", 1)[0]
        for name, length, target in columns:
            assert f"{name} VARCHAR({length})" in ddl
            assert f"FOREIGN KEY({name}) REFERENCES {target} (id)" in ddl
