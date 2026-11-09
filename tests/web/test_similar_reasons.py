"""Строка «почему похожа» под похожей игрой"""

from app.catalog.models import Game
from app.catalog.similar import (
    Closeness,
    SameDeveloper,
    SharedGenres,
    SharedPlatforms,
    SimilarDescription,
    SimilarGame,
    Source,
)
from app.web.templating import SIMILAR_FALLBACK, SIMILAR_LINE_LIMIT, similar_reasons


def line(*reasons, source=Source.METADATA) -> str:
    return similar_reasons(SimilarGame(Game(slug="x", title="x"), source, tuple(reasons)))


def test_order_and_separator() -> None:
    assert line(SameDeveloper(("FromSoftware",)), SharedGenres(("Action RPG",))) == (
        "тоже от FromSoftware · Action RPG"
    )
    assert line(SharedGenres(("Action RPG",)), SimilarDescription(0.74, Closeness.CLOSE)) == (
        "Action RPG · близкое описание"
    )


def test_line_fits_limit_and_keeps_first_reason() -> None:
    text = line(
        SameDeveloper(("FromSoftware",)),
        SharedGenres(("Action RPG", "Souls-like")),
        SimilarDescription(0.8, Closeness.VERY_CLOSE),
    )
    assert text == "тоже от FromSoftware · Action RPG, Souls-like"
    assert len(text) <= SIMILAR_LINE_LIMIT
    long = line(SameDeveloper(("A very long studio name " * 3,)), SharedGenres(("RPG",)))
    assert long.startswith("тоже от A very long") and "RPG" not in long


def test_more_than_two_genres_collapsed() -> None:
    assert line(SharedGenres(("Action RPG", "Souls-like", "Open World", "Fantasy"))) == (
        "Action RPG, Souls-like и ещё 2"
    )


def test_platforms_only_when_nothing_else() -> None:
    assert line(SharedGenres(("RPG",)), SharedPlatforms(("PC", "PS5"))) == "RPG · есть на PC и PS5"
    assert (
        line(SameDeveloper(("Studio",)), SharedGenres(("RPG",)), SharedPlatforms(("PC",)))
        == "тоже от Studio · RPG"
    )


def test_neutral_line_without_reasons() -> None:
    assert line(source=Source.EMBEDDING) == SIMILAR_FALLBACK == "похожа по описанию и жанрам"
