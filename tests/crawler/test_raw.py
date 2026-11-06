from app.crawler.processor import parse_bundle
from app.crawler.raw import RawBundle, stable_card
from app.scraper.parsers import parse_game
from tests.conftest import load_fixture


def elden_bundle() -> RawBundle:
    return RawBundle(
        card=stable_card(load_fixture("composer_elden-ring.json")),
        user_stats={"pc": load_fixture("user_stats_elden-ring_pc.json")},
        critic_reviews=load_fixture("critic_reviews_elden-ring.json")["data"]["items"],
        user_reviews=load_fixture("user_reviews_elden-ring.json")["data"]["items"],
    )


def test_stable_card_keeps_only_parsed_components() -> None:
    card = stable_card(load_fixture("composer_elden-ring.json"))
    names = [c["meta"]["componentName"] for c in card["components"]]
    assert names == [
        "product",
        "critic-score-summary",
        "critic-reviews",
        "user-score-summary",
        "user-reviews",
    ]
    assert parse_game(card) is not None


def test_digest_ignores_volatile_blocks_and_key_order() -> None:
    raw = load_fixture("composer_elden-ring.json")
    changed_prices = load_fixture("composer_elden-ring.json")
    changed_prices["components"][3]["data"]["items"] = []
    reordered = dict(reversed(list(raw.items())))
    digests = {RawBundle(stable_card(c)).digest for c in (raw, changed_prices, reordered)}
    assert len(digests) == 1

    raw["components"][0]["data"]["item"]["title"] = "другое"
    assert RawBundle(stable_card(raw)).digest not in digests


def test_compress_roundtrip_is_small() -> None:
    bundle = elden_bundle()
    body = bundle.compress()
    assert RawBundle.decompress(body) == bundle
    assert len(body) < len(bundle.canonical()) / 3


def test_parse_bundle_without_network() -> None:
    data, reviews = parse_bundle(elden_bundle(), "elden-ring")
    scores = {p.platform: p.userscore for p in data.platforms}
    assert scores["playstation-5"] == 8.4
    assert scores["pc"] == 7.6
    assert scores["xbox-one"] is None
    assert (len(reviews["critic"]), len(reviews["user"])) == (10, 30)
