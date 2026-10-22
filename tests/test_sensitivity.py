from scanner.core.sensitivity import allowed_for_agents, combine


def test_combine_takes_most_restrictive() -> None:
    assert combine() == "public"
    assert combine("public", "licensed") == "licensed"
    assert combine("nda", "public", "customer") == "customer"
    assert combine("personal", "nda") == "personal"


def test_agents_only_see_public() -> None:
    assert allowed_for_agents("public")
    assert not allowed_for_agents("licensed")
