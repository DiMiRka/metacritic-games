import re

import pytest
from sqlalchemy import CheckConstraint

from app.core.models import Base

# Колонки с перечислимыми значениями: у каждой должен быть CHECK
ENUM_COLUMNS = {"status", "state", "kind", "trigger", "source", "role", "phase", "stage"}


def enum_columns():
    for table in Base.metadata.sorted_tables:
        for column in table.columns:
            if column.name in ENUM_COLUMNS:
                yield table, column


@pytest.mark.parametrize(
    ("table", "column"), list(enum_columns()), ids=lambda x: getattr(x, "name", str(x))
)
def test_enum_column_has_check(table, column) -> None:
    checks = [
        str(c.sqltext)
        for c in table.constraints
        if isinstance(c, CheckConstraint) and re.match(rf"{column.name} IN \(", str(c.sqltext))
    ]
    assert checks, f"{table.name}.{column.name} без CHECK"
